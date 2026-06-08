//! Operator-tunable configuration — **all calibration-gated**.
//!
//! Per the hardware-first constraint (Task 2.7 / [`docs/specs/storage.md`] §7),
//! `retentiond` must **not** ship guessed governor defaults as fact. Every
//! threshold, cadence, quota, and value weight lives here as an explicit field
//! with a **provisional placeholder** taken from the `storage.md` §2.1 advisory
//! tables. The decision logic in [`crate::governor`], [`crate::value`],
//! [`crate::recent`], and [`crate::lease`] is correct **independent of these
//! numbers**; the calibration spike only sets the values.
//!
//! Each [`Default`] below is annotated `CALIBRATION-GATED`. They are persisted
//! settings surfaced in the storage UI, not constants baked into logic.

use serde::{Deserialize, Serialize};

/// A `max(percentage, absolute floor)` threshold. Percentages fail on small
/// cards; absolute floors fail on large ones — [`docs/specs/storage.md`] §2.1
/// uses **both**.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct ThresholdPair {
    /// Fraction of total filesystem size (0.0..=1.0).
    pub pct: f64,
    /// Absolute floor in bytes.
    pub floor_bytes: u64,
}

impl ThresholdPair {
    /// The effective limit in bytes for a filesystem of `total_bytes`:
    /// `max(pct * total, floor_bytes)`.
    #[must_use]
    pub fn limit_bytes(&self, total_bytes: u64) -> u64 {
        // Saturating, lossy-but-monotonic conversion: this is a coarse threshold,
        // never an exact accounting.
        #[allow(
            clippy::cast_precision_loss,
            clippy::cast_possible_truncation,
            clippy::cast_sign_loss
        )]
        let pct_bytes = (self.pct.max(0.0) * total_bytes as f64) as u64;
        pct_bytes.max(self.floor_bytes)
    }
}

/// Hysteresis band for one tier: enter (high-water) and exit (low-water) marks,
/// kept distinct so tiers do not flap under steady pressure.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct TierBand {
    /// Enter this tier when free space drops **below** this.
    pub enter_below: ThresholdPair,
    /// Leave this tier (toward healthier) only when free space rises **above**
    /// this — always `>= enter_below` so there is a dead band.
    pub exit_above: ThresholdPair,
}

/// Free-inode thresholds (a parallel budget; thumbnails/segments exhaust inodes
/// before bytes).
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct InodeThresholds {
    /// Enter Low below this free-inode fraction.
    pub low_frac: f64,
    /// Enter Critical below this.
    pub critical_frac: f64,
    /// Enter Emergency below this.
    pub emergency_frac: f64,
}

/// Governor space/inode thresholds, reserves, and cadence.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GovernorConfig {
    /// Free-space band for the Low tier.
    pub low: TierBand,
    /// Free-space band for the Critical tier.
    pub critical: TierBand,
    /// Free-space band for the Emergency tier.
    pub emergency: TierBand,
    /// Enter Exhausted when free space drops below this **or** no safe candidate
    /// remains ([`docs/specs/storage.md`] §3.1).
    pub exhausted_enter_below: ThresholdPair,
    /// Free-inode thresholds.
    pub inodes: InodeThresholds,
    /// OS/root reserve floor — **sacrosanct**. Breaching it forces at least
    /// Critical regardless of archive usage.
    pub root_reserve: ThresholdPair,
    /// Statfs cadence per tier in milliseconds (faster under pressure).
    pub cadence_ms: TierCadence,
}

/// Per-tier statfs cadence (ms). Faster under pressure; event wakeups augment it.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct TierCadence {
    /// Cadence while Healthy.
    pub healthy_ms: u64,
    /// Cadence while Low.
    pub low_ms: u64,
    /// Cadence while Critical.
    pub critical_ms: u64,
    /// Cadence while Emergency or Exhausted.
    pub emergency_ms: u64,
}

impl Default for GovernorConfig {
    fn default() -> Self {
        // CALIBRATION-GATED (Task 2.7 / storage.md §7): provisional; real values
        // from the governor-calibration spike. Numbers below mirror the 256 GB
        // advisory table in storage.md §2.1 purely as a starting point.
        Self {
            low: TierBand {
                enter_below: ThresholdPair {
                    pct: 0.06,
                    floor_bytes: 16 << 30,
                },
                exit_above: ThresholdPair {
                    pct: 0.08,
                    floor_bytes: 20 << 30,
                },
            },
            critical: TierBand {
                enter_below: ThresholdPair {
                    pct: 0.03,
                    floor_bytes: 8 << 30,
                },
                exit_above: ThresholdPair {
                    pct: 0.06,
                    floor_bytes: 16 << 30,
                },
            },
            emergency: TierBand {
                enter_below: ThresholdPair {
                    pct: 0.015,
                    floor_bytes: 4 << 30,
                },
                exit_above: ThresholdPair {
                    pct: 0.03,
                    floor_bytes: 8 << 30,
                },
            },
            exhausted_enter_below: ThresholdPair {
                pct: 0.0075,
                floor_bytes: 2 << 30,
            },
            inodes: InodeThresholds {
                low_frac: 0.03,
                critical_frac: 0.015,
                emergency_frac: 0.0075,
            },
            root_reserve: ThresholdPair {
                pct: 0.05,
                floor_bytes: 2 << 30,
            },
            cadence_ms: TierCadence {
                healthy_ms: 60_000,
                low_ms: 30_000,
                critical_ms: 15_000,
                emergency_ms: 5_000,
            },
        }
    }
}

/// `RecentClips` rolling-mirror policy.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct RecentMirrorConfig {
    /// Byte quota for the Pi-side `RecentClips` mirror; when full, evict the
    /// oldest non-pinned segment past its grace window.
    pub quota_bytes: u64,
    /// Grace window (ms) since first-archived before a segment becomes evictable,
    /// long enough to cover the worst-case delay before a segment becomes
    /// event-adjacent ([`docs/specs/retentiond.md`] §3.3).
    pub grace_ms: i64,
}

impl Default for RecentMirrorConfig {
    fn default() -> Self {
        // CALIBRATION-GATED (Task 2.7 / storage.md §7): provisional placeholders.
        Self {
            quota_bytes: 16 << 30,
            grace_ms: 30 * 60 * 1000,
        }
    }
}

/// Lease TTL / heartbeat defaults (`single-writer-lease.md` OQ-5, flagged
/// TUNABLE — validate on hardware).
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct LeaseConfig {
    /// Default lease TTL in seconds.
    pub ttl_s: u32,
    /// Renew interval in seconds (`ttl_s / 3`).
    pub renew_interval_s: u32,
}

impl Default for LeaseConfig {
    fn default() -> Self {
        // CALIBRATION-GATED (Task 2.7 / storage.md §7): provisional ttl=60s.
        Self {
            ttl_s: 60,
            renew_interval_s: 20,
        }
    }
}

/// Top-level retention configuration.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct RetentionConfig {
    /// Space governor thresholds/reserves/cadence.
    pub governor: GovernorConfig,
    /// `RecentClips` mirror policy.
    pub recent: RecentMirrorConfig,
    /// Lease TTL/heartbeat.
    pub lease: LeaseConfig,
    /// Whether the operator opted in to Emergency eviction of **undurable**
    /// `SentryClips` (Class-B, permanent loss). **Off by default**; undurable
    /// `SavedClips` is never included regardless.
    pub allow_emergency_undurable_sentry: bool,
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{RetentionConfig, ThresholdPair};

    #[test]
    fn threshold_pair_takes_the_max_of_pct_and_floor() {
        let t = ThresholdPair {
            pct: 0.10,
            floor_bytes: 5,
        };
        // pct dominates on a large fs.
        assert_eq!(t.limit_bytes(1000), 100);
        // floor dominates on a small fs.
        assert_eq!(t.limit_bytes(10), 5);
    }

    #[test]
    fn default_bands_have_a_dead_zone_exit_above_enter() {
        let c = RetentionConfig::default().governor;
        // For each tier, the exit (low-water) limit must be >= the enter
        // (high-water) limit on a representative card, or hysteresis is broken.
        let total = 256u64 << 30;
        for band in [c.low, c.critical, c.emergency] {
            assert!(
                band.exit_above.limit_bytes(total) >= band.enter_below.limit_bytes(total),
                "exit must be above enter for hysteresis"
            );
        }
    }

    #[test]
    fn emergency_undurable_sentry_is_off_by_default() {
        assert!(!RetentionConfig::default().allow_emergency_undurable_sentry);
    }
}
