//! Boot-scoped monotonic time, the injectable [`Clock`] seam, and the [`Waiter`]
//! self-pacing seam.
//!
//! The Pi has **no RTC**, so wall-clock time can jump arbitrarily at boot and
//! after NTP sync. Every upload deadline (lease TTLs, renew cadence, throttle
//! pacing) is therefore expressed in **boot-scoped monotonic milliseconds**
//! ([`MonoMs`]) tagged with the [`BootId`] they were minted under. A lease
//! deadline from a prior boot is meaningless by construction
//! ([`single-writer-lease.md`] §4.2).
//!
//! These types deliberately mirror `retentiond::time` (the lane that owns the
//! lease-honoring predicate) so the two converge onto a shared
//! `teslausb-core::contracts` home without a wire/representation change.
//!
//! [`single-writer-lease.md`]: ../../../../docs/specs/contracts/single-writer-lease.md

use serde::{Deserialize, Serialize};

/// Monotonic milliseconds since an arbitrary, boot-local epoch.
///
/// Only **differences** within one [`BootId`] are meaningful; the absolute value
/// carries no wall-clock meaning. Comparisons across boots are a bug, which is
/// why a [`BootId`] always travels alongside a stored deadline.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct MonoMs(pub i64);

impl MonoMs {
    /// Add `delta_ms` milliseconds, saturating at [`i64::MAX`] rather than
    /// wrapping (a wrapped deadline could appear to be in the past).
    #[must_use]
    pub const fn saturating_add_ms(self, delta_ms: i64) -> Self {
        Self(self.0.saturating_add(delta_ms))
    }

    /// Milliseconds elapsed from `earlier` to `self`, saturating at zero. A
    /// negative span (a monotonic clock seen going backwards — should not happen)
    /// collapses to `0`.
    #[must_use]
    pub const fn saturating_elapsed_since(self, earlier: Self) -> i64 {
        let delta = self.0.saturating_sub(earlier.0);
        if delta < 0 { 0 } else { delta }
    }
}

/// Opaque per-boot identity, minted fresh on each daemon/`indexd` start.
///
/// Carried with every persisted monotonic deadline so a value from an earlier
/// boot is recognised as stale rather than silently compared against the current
/// monotonic clock.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct BootId(pub String);

/// The injectable monotonic-clock seam.
///
/// The live implementation reads `CLOCK_MONOTONIC`; tests drive a deterministic
/// fake so every TTL / renew / pacing branch is exercised over a synthetic
/// timeline with no real sleeping.
pub trait Clock {
    /// The current boot-scoped monotonic timestamp.
    fn mono_now(&self) -> MonoMs;

    /// The identity of the current boot. Stable for the life of the process.
    fn boot_id(&self) -> BootId;
}

/// The injectable self-pacing seam.
///
/// When the upload [`crate::throttle::Pacer`] reports the token bucket is empty,
/// the transfer loop must wait before sending the next chunk. The live impl
/// sleeps the thread; tests inject a fake that simply advances the shared fake
/// clock, so throttle behaviour is verified deterministically with no real time
/// passing.
pub trait Waiter {
    /// Block (or, in tests, advance the synthetic timeline) for `ms`
    /// milliseconds. A `0` wait is a no-op.
    fn wait_ms(&self, ms: u64);
}
