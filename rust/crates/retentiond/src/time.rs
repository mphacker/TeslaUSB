//! Boot-scoped monotonic time and the injectable [`Clock`] seam.
//!
//! The Pi has **no RTC**, so wall-clock time can jump arbitrarily at boot and
//! after NTP sync. Every retention deadline (lease TTLs, the `RecentClips` grace
//! window, governor cadence) is therefore expressed in **boot-scoped monotonic
//! milliseconds** ([`MonoMs`]) tagged with the [`BootId`] they were minted under.
//! A deadline from a prior boot is meaningless by construction — it can neither
//! pin a dead lease forever nor reap a live one (`single-writer-lease.md` §4.2).

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
    /// negative span (clock seen going backwards within a boot — should not
    /// happen for a monotonic source) collapses to `0`.
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
/// fake so every TTL / grace / cadence branch is exercised over a synthetic
/// timeline with no real sleeping.
pub trait Clock {
    /// The current boot-scoped monotonic timestamp.
    fn mono_now(&self) -> MonoMs;

    /// The identity of the current boot. Stable for the life of the process.
    fn boot_id(&self) -> BootId;
}
