//! Slice 6.1e (part 1) — the **lease shape** and the governor's lease-honoring
//! predicate, built to contract **D3** (`single-writer-lease.md`) as the source
//! of truth.
//!
//! `retentiond` is *not* a lease store and *not* a lease holder — `indexd` owns
//! the `leases` table (sole `SQLite` writer) and `webd`/`uploadd` hold leases. What
//! `retentiond` needs from this module is the **honoring rule** (contract §3):
//! before evicting a candidate, an item with any **unexpired** lease is a *hard
//! exclusion*. "Unexpired" is defined precisely as
//! `boot_id == current && expires_mono_ms > mono_now` (contract §3, §4.2). A lease
//! from a prior boot, or past its monotonic deadline, is ignored — the Pi has no
//! RTC, so deadlines are **boot-scoped monotonic**, never wall-clock.
//!
//! The types here mirror the illustrative shapes in contract §7 so they stay
//! consistent with `webd` (5.1b) and `uploadd` (6.3). **Divergence flag for the
//! supervisor:** the contract sketches `expires_mono_ms: i64` + a sibling
//! `boot_id`; this lane wraps them in the branded [`MonoMs`] / [`BootId`] from
//! [`crate::time`] so a cross-boot or wall-clock comparison is a *type-level*
//! mistake rather than a silent bug. The wire/storage representation is unchanged
//! (an `i64` plus a boot string); only the in-memory honoring type is branded.

use crate::time::{BootId, MonoMs};

/// What kind of operation holds a lease (contract §7). Governs preemption policy
/// in §5: an **upload** lease may be cooperatively preempted under Emergency; a
/// **playback** lease (a user actively watching) is never preempted.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LeaseKind {
    /// `uploadd` is transferring the item to durable storage.
    Upload,
    /// `webd` is streaming/exporting the item to a user.
    Playback,
}

/// The delete-state of an archive item (contract §4 / §7; column on
/// `archive_items` in D1). The state machine is
/// `Live → DeleteClaimed → Deleting → Deleted`, with `DeleteFailed` and
/// `Quarantined` for anomalies surfaced by the startup recovery matrix.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeleteState {
    /// Normal, present, leasable, evictable.
    Live,
    /// The governor has claimed it for delete; no new lease may be granted.
    DeleteClaimed,
    /// Renamed into `.retention-trash`; unlink in progress.
    Deleting,
    /// Fully unlinked and accounted.
    Deleted,
    /// A delete attempt failed; needs recovery/inspection.
    DeleteFailed,
    /// Inconsistent FS↔DB state detected; excluded from all automation.
    Quarantined,
}

impl DeleteState {
    /// Whether a *new* playback/upload lease may be granted on an item in this
    /// state. Only `Live` items are leasable (contract §3): once `DeleteClaimed`
    /// or beyond, `acquire` must be `Denied`, so a stream cannot start on a file
    /// the governor just claimed.
    #[must_use]
    pub const fn is_leasable(self) -> bool {
        matches!(self, Self::Live)
    }

    /// Whether the governor may consider an item in this state for eviction at
    /// all. Anything already claimed/deleting/deleted/quarantined is off-limits.
    #[must_use]
    pub const fn is_evictable(self) -> bool {
        matches!(self, Self::Live)
    }
}

/// Opaque lease identity (contract §7 `lease_id: i64`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct LeaseId(pub i64);

/// 128-bit generation token (contract §2.1): returned at `acquire`, required by
/// `renew`/`release` so a delayed message from a crashed-then-restarted holder
/// cannot extend or drop a lease that was already reaped and re-granted.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct LeaseGen(pub u128);

/// A lease as `retentiond` observes it via `indexd` (read-only here — only
/// `indexd` mutates the row). Carries everything the honoring predicate needs.
#[derive(Debug, Clone)]
pub struct Lease {
    /// Lease identity.
    pub lease_id: LeaseId,
    /// Generation token (contract §2.1 `gen`; renamed from the contract's bare
    /// `gen` because Rust 2024 reserves that keyword). Returned at `acquire`,
    /// required by `renew`/`release`.
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
    /// `boot_id == current && expires_mono_ms > mono_now`. An unexpired lease is
    /// a hard exclusion from eviction; an expired or prior-boot lease is ignored
    /// (and reaped by `indexd`).
    #[must_use]
    pub fn is_unexpired(&self, current_boot: &BootId, mono_now: MonoMs) -> bool {
        &self.boot_id == current_boot && self.expires_mono_ms > mono_now
    }

    /// Whether this lease may be **cooperatively preempted** under Emergency
    /// (contract §5): only upload leases, never an active playback stream.
    #[must_use]
    pub const fn is_preemptible(&self) -> bool {
        matches!(self.kind, LeaseKind::Upload)
    }
}

/// Whether **any** lease in `leases` is unexpired for the current boot/time.
/// This is the single fact the value model consumes as
/// [`crate::value::EvictionItem::leased`]: if `true`, the item is hard-excluded.
#[must_use]
pub fn has_unexpired_lease(leases: &[Lease], current_boot: &BootId, mono_now: MonoMs) -> bool {
    leases
        .iter()
        .any(|l| l.is_unexpired(current_boot, mono_now))
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]
mod tests {
    use super::{DeleteState, Lease, LeaseGen, LeaseId, LeaseKind, has_unexpired_lease};
    use crate::time::{BootId, MonoMs};

    fn lease(boot: &str, expires: i64, kind: LeaseKind) -> Lease {
        Lease {
            lease_id: LeaseId(1),
            gen_token: LeaseGen(0x1234),
            boot_id: BootId(boot.to_string()),
            expires_mono_ms: MonoMs(expires),
            kind,
            holder: "webd:conn-1".to_string(),
        }
    }

    #[test]
    fn unexpired_requires_same_boot_and_future_deadline() {
        let now = MonoMs(1_000);
        let boot = BootId("boot-A".to_string());
        // Same boot, deadline in the future → unexpired.
        assert!(lease("boot-A", 2_000, LeaseKind::Playback).is_unexpired(&boot, now));
        // Same boot, deadline exactly now → expired (strict >).
        assert!(!lease("boot-A", 1_000, LeaseKind::Playback).is_unexpired(&boot, now));
        // Same boot, deadline in the past → expired.
        assert!(!lease("boot-A", 500, LeaseKind::Playback).is_unexpired(&boot, now));
    }

    #[test]
    fn prior_boot_lease_is_always_stale() {
        let now = MonoMs(1_000);
        let boot = BootId("boot-B".to_string());
        // Deadline far in the "future" numerically, but a different boot → stale.
        assert!(!lease("boot-A", 9_999_999, LeaseKind::Upload).is_unexpired(&boot, now));
    }

    #[test]
    fn has_unexpired_lease_is_any_over_the_set() {
        let now = MonoMs(1_000);
        let boot = BootId("boot-A".to_string());
        let leases = vec![
            lease("boot-A", 500, LeaseKind::Upload),     // expired
            lease("boot-A", 2_000, LeaseKind::Playback), // live
        ];
        assert!(has_unexpired_lease(&leases, &boot, now));
        // All expired → no exclusion.
        let expired = vec![lease("boot-A", 100, LeaseKind::Upload)];
        assert!(!has_unexpired_lease(&expired, &boot, now));
        // Empty set → no exclusion.
        assert!(!has_unexpired_lease(&[], &boot, now));
    }

    #[test]
    fn only_live_is_leasable_and_evictable() {
        assert!(DeleteState::Live.is_leasable());
        assert!(DeleteState::Live.is_evictable());
        for s in [
            DeleteState::DeleteClaimed,
            DeleteState::Deleting,
            DeleteState::Deleted,
            DeleteState::DeleteFailed,
            DeleteState::Quarantined,
        ] {
            assert!(!s.is_leasable(), "{s:?} must not be leasable");
            assert!(!s.is_evictable(), "{s:?} must not be evictable");
        }
    }

    #[test]
    fn only_upload_leases_are_preemptible() {
        assert!(lease("b", 1, LeaseKind::Upload).is_preemptible());
        assert!(!lease("b", 1, LeaseKind::Playback).is_preemptible());
    }
}
