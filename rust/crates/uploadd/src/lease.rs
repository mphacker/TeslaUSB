//! The **upload lease** `uploadd` holds while transferring, and the
//! [`LeaseClient`] IPC seam it drives against `indexd`.
//!
//! Built to contract **D3** ([`single-writer-lease.md`]). `uploadd` is a lease
//! **holder**: while a transfer is in flight it holds a TTL upload lease on the
//! item so `retentiond`'s space governor cannot evict the file mid-read
//! (invariant **L**). It does **not** write the `leases` table — only `indexd`
//! (the sole `SQLite` writer) does — so every mutation is an
//! acquire/renew/release RPC ([`single-writer-lease.md`] §2).
//!
//! # Shape convergence with `retentiond`
//!
//! The [`Lease`] record here **mirrors** `retentiond::lease::Lease` field-for-
//! field (`LeaseId`/`LeaseGen`/`BootId`/`MonoMs`/[`LeaseKind`]/`holder`) and the
//! [`Lease::is_unexpired`] predicate is byte-identical to retentiond's honoring
//! rule (`boot_id == current && expires_mono_ms > mono_now`). That is the whole
//! point: the lease this lane *writes* (via `indexd`) must be readable by the
//! lane that *honors* it. The shared home is `teslausb-core::contracts::lease`;
//! until convergence both lanes carry the mirror. **Divergence flag for the
//! supervisor:** retentiond brands `MonoMs`/`BootId` via its `crate::time`; this
//! lane uses the structurally-identical [`crate::time`] types. Same wire shape.
//!
//! [`single-writer-lease.md`]: ../../../../docs/specs/contracts/single-writer-lease.md

use crate::source::ArchiveItemId;
use crate::time::{BootId, MonoMs};

/// What kind of operation holds a lease (contract §7). `uploadd` only ever holds
/// [`Self::Upload`]; [`Self::Playback`] is `webd`'s and is included so the
/// mirrored shape matches retentiond's.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LeaseKind {
    /// `uploadd` is transferring the item to durable storage.
    Upload,
    /// `webd` is streaming/exporting the item to a user.
    Playback,
}

/// Opaque lease identity (contract §7 `lease_id: i64`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct LeaseId(pub i64);

/// 128-bit generation token (contract §2.1): returned at `acquire`, required by
/// `renew`/`release` so a delayed message from a crashed-then-restarted holder
/// cannot extend or drop a lease that was already reaped and re-granted.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct LeaseGen(pub u128);

/// A lease record, in the exact shape `retentiond`'s eviction predicate reads.
///
/// `uploadd` assembles one of these from a [`LeaseGrant::Granted`] plus its own
/// boot/holder/kind so the held lease can be reasoned about (and tested) with
/// the *same* `is_unexpired` rule the governor applies.
#[derive(Debug, Clone)]
pub struct Lease {
    /// Lease identity.
    pub lease_id: LeaseId,
    /// Generation token (contract §2.1 `gen`; renamed because Rust 2024 reserves
    /// `gen`). Returned at `acquire`, required by `renew`/`release`.
    pub gen_token: LeaseGen,
    /// Boot under which the deadline was minted; a different boot ⇒ stale.
    pub boot_id: BootId,
    /// Boot-scoped monotonic deadline; `<= mono_now` ⇒ expired.
    pub expires_mono_ms: MonoMs,
    /// Upload vs playback.
    pub kind: LeaseKind,
    /// Service + instance string, for diagnostics/`/api/storage` only.
    pub holder: String,
}

impl Lease {
    /// Whether this lease is **unexpired** *now*, per contract §3:
    /// `boot_id == current && expires_mono_ms > mono_now`. Identical to
    /// `retentiond::lease::Lease::is_unexpired`.
    #[must_use]
    pub fn is_unexpired(&self, current_boot: &BootId, mono_now: MonoMs) -> bool {
        &self.boot_id == current_boot && self.expires_mono_ms > mono_now
    }
}

/// Result of an `acquire` RPC (contract §2.1).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LeaseGrant {
    /// The lease was granted.
    Granted {
        /// Assigned lease identity.
        lease_id: LeaseId,
        /// Generation token for subsequent `renew`/`release`.
        gen_token: LeaseGen,
        /// Boot-scoped monotonic deadline.
        expires_mono_ms: MonoMs,
    },
    /// The item could not be leased (already `DELETE_CLAIMED`+, or absent). The
    /// holder must **not** start a transfer.
    Denied {
        /// Human-readable reason for diagnostics.
        reason: String,
    },
}

/// Result of a `renew` RPC (contract §2.2): renew is strictly conditional.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RenewResult {
    /// The lease was extended to a new deadline.
    Renewed {
        /// The new boot-scoped monotonic deadline.
        expires_mono_ms: MonoMs,
    },
    /// `gen` mismatch, the lease was already past its deadline, or the subject
    /// is no longer `LIVE`. The holder must **stop the transfer** and, if still
    /// wanted, re-`acquire` (which may be `Denied` if a delete has begun).
    Stale {
        /// Human-readable reason for diagnostics.
        reason: String,
    },
}

/// Result of a `release` RPC (contract §2.1).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReleaseResult {
    /// The lease existed and was released.
    Released,
    /// Nothing to release (already expired/reaped) — a benign no-op.
    NoOp,
}

/// The lease IPC seam against `indexd`. The live impl is a UDS RPC client
/// ([`single-writer-lease.md`] OQ-2); tests inject a deterministic fake.
///
/// There is deliberately **no** `delete`/`claim` verb here — claiming and
/// deleting are `retentiond`'s, never a holder's.
///
/// [`single-writer-lease.md`]: ../../../../docs/specs/contracts/single-writer-lease.md
pub trait LeaseClient {
    /// Acquire an upload lease on `item` for `ttl_ms`, identifying as `holder`.
    fn acquire(
        &self,
        item: ArchiveItemId,
        kind: LeaseKind,
        holder: &str,
        ttl_ms: i64,
    ) -> LeaseGrant;

    /// Renew an existing lease for a further `ttl_ms` (strictly conditional —
    /// see [`RenewResult`]).
    fn renew(&self, lease_id: LeaseId, gen_token: LeaseGen, ttl_ms: i64) -> RenewResult;

    /// Release a held lease.
    fn release(&self, lease_id: LeaseId, gen_token: LeaseGen) -> ReleaseResult;
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{Lease, LeaseGen, LeaseId, LeaseKind};
    use crate::time::{BootId, MonoMs};

    fn held(boot: &str, expires: i64) -> Lease {
        Lease {
            lease_id: LeaseId(7),
            gen_token: LeaseGen(0xabcd),
            boot_id: BootId(boot.to_owned()),
            expires_mono_ms: MonoMs(expires),
            kind: LeaseKind::Upload,
            holder: "uploadd".to_owned(),
        }
    }

    #[test]
    fn held_upload_lease_is_unexpired_while_future_and_same_boot() {
        let boot = BootId("boot-A".to_owned());
        assert!(held("boot-A", 2_000).is_unexpired(&boot, MonoMs(1_000)));
    }

    #[test]
    fn deadline_at_or_before_now_is_expired() {
        let boot = BootId("boot-A".to_owned());
        assert!(!held("boot-A", 1_000).is_unexpired(&boot, MonoMs(1_000)));
        assert!(!held("boot-A", 999).is_unexpired(&boot, MonoMs(1_000)));
    }

    #[test]
    fn prior_boot_lease_is_stale_regardless_of_deadline() {
        let boot = BootId("boot-B".to_owned());
        assert!(!held("boot-A", 9_999_999).is_unexpired(&boot, MonoMs(1_000)));
    }
}
