//! Slice 6.1f — the **storage status/health** payload `retentiond` publishes to
//! `webd`, mirroring the `StorageHealth` shape in contract **D2**
//! (`webd-api.md` §4) and `storage.md` §6.
//!
//! This is a *projection* of the governor + index state into a serde-serializable
//! struct; `webd` serves it at `GET /api/storage/health`. Two design points from
//! the contract are load-bearing:
//!
//! - **Two distinct signals.** [`StorageHealth::car_writeable`] (is the `TeslaCam`
//!   LUN writable — the invariant the whole appliance exists to protect) is
//!   reported **separately** from [`StorageHealth::archive_tier`] (how full the
//!   Pi-side archive is). The card can be Critical for archiving while the car
//!   still writes perfectly; conflating them would hide a real problem or raise a
//!   false alarm.
//! - **`disk.img` logical-vs-allocated** is surfaced so a sub-allocated (sparse)
//!   image raises a visible warning rather than masquerading as free space.
//!
//! The field names match the contract's Rust shape verbatim so `webd` can adopt
//! the type without a translation layer; `webd` owns the final HTTP/JSON
//! representation.

use serde::{Deserialize, Serialize};

use crate::governor::{DiskImgAccounting, GovernorAssessment};

/// Free-space/inode reading for one filesystem (contract `FsFree`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FsFree {
    /// Mount point (e.g. `/` or the data mount).
    pub mount: String,
    /// Free bytes available to an unprivileged writer.
    pub free_bytes: u64,
    /// Total bytes of the filesystem.
    pub total_bytes: u64,
    /// Free inodes.
    pub free_inodes: u64,
    /// Total inodes.
    pub total_inodes: u64,
    /// Whether this filesystem's sacrosanct reserve is breached.
    pub reserve_breached: bool,
}

/// Bytes + file count attributed to one storage class (contract `ClassUsage`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClassUsage {
    /// Class label (`SentryClips`, `SavedClips`, `RecentClips`, `TeslaTrackMode`,
    /// `thumb`, `cache`, `staging`, …).
    pub class: String,
    /// Bytes used by the class.
    pub bytes: u64,
    /// Number of files/items in the class.
    pub file_count: u64,
}

/// A one-line summary of the most recent eviction (contract `EvictionSummary`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvictionSummary {
    /// When it happened, UTC epoch seconds (display only — internal scheduling
    /// uses monotonic time).
    pub at: i64,
    /// What was evicted (human-readable).
    pub what: String,
    /// Why it was chosen (e.g. "least-valuable durable Sentry under Critical").
    pub why: String,
    /// Bytes reclaimed.
    pub bytes_freed: u64,
}

/// The full storage-health projection (contract D2 §4 / `storage.md` §6).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StorageHealth {
    /// **Invariant signal:** is the car-visible `TeslaCam` LUN writable?
    pub car_writeable: bool,
    /// **Archive signal (distinct):** governor tier
    /// (`Healthy|Low|Critical|Emergency|Exhausted`).
    pub archive_tier: String,
    /// Per-filesystem free space/inodes (root + data; one entry if they share an
    /// `st_dev`).
    pub per_fs: Vec<FsFree>,
    /// Nominal (logical) size of `disk.img`.
    pub disk_img_logical_bytes: u64,
    /// Currently allocated bytes of `disk.img`; `< logical` ⇒ sparse-image warn.
    pub disk_img_allocated_bytes: u64,
    /// Archive usage broken down by class.
    pub archive_by_class: Vec<ClassUsage>,
    /// `SQLite` WAL size.
    pub wal_bytes: u64,
    /// Log usage.
    pub log_bytes: u64,
    /// Bytes held by pinned/favorited items (never auto-evicted).
    pub pinned_bytes: u64,
    /// Bytes held by items with an unexpired lease (temporarily un-evictable).
    pub leased_bytes: u64,
    /// Bytes the governor could reclaim right now (the safe candidate set).
    pub reclaimable_bytes: u64,
    /// Classes eviction would target next, least-valuable first.
    pub next_candidate_classes: Vec<String>,
    /// Whether undurable footage is currently being sacrificed (Emergency +
    /// opt-in) — a prominent UI warning.
    pub sacrificing_undurable: bool,
    /// Which optional writers are currently paused (e.g. Recent mirroring).
    pub paused_writers: Vec<String>,
    /// The most recent eviction, if any.
    pub last_eviction: Option<EvictionSummary>,
}

impl StorageHealth {
    /// Whether the `disk.img` is under-allocated (sparse-image warning). Derived
    /// from the logical-vs-allocated pair so callers don't recompute the rule.
    #[must_use]
    pub fn sparse_image_warning(&self) -> bool {
        DiskImgAccounting {
            nominal_bytes: self.disk_img_logical_bytes,
            allocated_bytes: self.disk_img_allocated_bytes,
        }
        .is_sparse()
    }
}

/// Inputs that the governor assessment does not itself carry, supplied by the
/// index/breakdown layer when assembling [`StorageHealth`].
#[derive(Debug, Clone)]
pub struct HealthInputs {
    /// The invariant signal (from `gadgetd`/heartbeat), independent of the tier.
    pub car_writeable: bool,
    /// Per-filesystem readings to report.
    pub per_fs: Vec<FsFree>,
    /// `disk.img` accounting.
    pub disk_img: DiskImgAccounting,
    /// Archive usage by class.
    pub archive_by_class: Vec<ClassUsage>,
    /// `SQLite` WAL size.
    pub wal_bytes: u64,
    /// Log usage.
    pub log_bytes: u64,
    /// Bytes pinned.
    pub pinned_bytes: u64,
    /// Bytes leased.
    pub leased_bytes: u64,
    /// Bytes reclaimable now.
    pub reclaimable_bytes: u64,
    /// Classes eviction targets next.
    pub next_candidate_classes: Vec<String>,
    /// Whether undurable footage is being sacrificed.
    pub sacrificing_undurable: bool,
    /// Paused optional writers.
    pub paused_writers: Vec<String>,
    /// Last eviction summary.
    pub last_eviction: Option<EvictionSummary>,
}

/// Assemble a [`StorageHealth`] from the governor's [`GovernorAssessment`] and
/// the breakdown [`HealthInputs`]. The tier string and the two distinct signals
/// come straight through unmodified — this function never *derives* the car
/// signal from the tier (they are independent by design).
#[must_use]
pub fn assemble(assessment: &GovernorAssessment, inputs: HealthInputs) -> StorageHealth {
    StorageHealth {
        car_writeable: inputs.car_writeable,
        archive_tier: assessment.tier.as_str().to_string(),
        per_fs: inputs.per_fs,
        disk_img_logical_bytes: inputs.disk_img.nominal_bytes,
        disk_img_allocated_bytes: inputs.disk_img.allocated_bytes,
        archive_by_class: inputs.archive_by_class,
        wal_bytes: inputs.wal_bytes,
        log_bytes: inputs.log_bytes,
        pinned_bytes: inputs.pinned_bytes,
        leased_bytes: inputs.leased_bytes,
        reclaimable_bytes: inputs.reclaimable_bytes,
        next_candidate_classes: inputs.next_candidate_classes,
        sacrificing_undurable: inputs.sacrificing_undurable,
        paused_writers: inputs.paused_writers,
        last_eviction: inputs.last_eviction,
    }
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]
mod tests {
    use super::{ClassUsage, FsFree, HealthInputs, assemble};
    use crate::config::GovernorConfig;
    use crate::governor::{DiskImgAccounting, FsRole, FsSample, Tier, evaluate};
    use crate::io::FsStat;

    const GB: u64 = 1 << 30;

    fn assessment(free: u64, tier_hint_prev: Tier) -> crate::governor::GovernorAssessment {
        let cfg = GovernorConfig::default();
        let stat = FsStat {
            dev_id: 1,
            free_bytes: free,
            total_bytes: 256 * GB,
            free_inodes: 1_000_000,
            total_inodes: 1_000_000,
        };
        evaluate(
            tier_hint_prev,
            &[FsSample {
                role: FsRole::Data,
                stat,
            }],
            DiskImgAccounting {
                nominal_bytes: 4 * GB,
                allocated_bytes: 4 * GB,
            },
            true,
            &cfg,
        )
    }

    fn inputs(car_writeable: bool) -> HealthInputs {
        HealthInputs {
            car_writeable,
            per_fs: vec![FsFree {
                mount: "/data".to_string(),
                free_bytes: 5 * GB,
                total_bytes: 256 * GB,
                free_inodes: 1_000,
                total_inodes: 1_000_000,
                reserve_breached: false,
            }],
            disk_img: DiskImgAccounting {
                nominal_bytes: 4 * GB,
                allocated_bytes: 4 * GB,
            },
            archive_by_class: vec![ClassUsage {
                class: "SentryClips".to_string(),
                bytes: 10 * GB,
                file_count: 42,
            }],
            wal_bytes: 0,
            log_bytes: 0,
            pinned_bytes: 0,
            leased_bytes: 0,
            reclaimable_bytes: 2 * GB,
            next_candidate_classes: vec!["SentryClips".to_string()],
            sacrificing_undurable: false,
            paused_writers: vec![],
            last_eviction: None,
        }
    }

    #[test]
    fn tier_string_matches_assessment() {
        let a = assessment(5 * GB, Tier::Low); // Critical-ish
        let h = assemble(&a, inputs(true));
        assert_eq!(h.archive_tier, a.tier.as_str());
    }

    #[test]
    fn car_signal_is_independent_of_archive_tier() {
        // Archive is Critical, but the car LUN is still writable — both reported.
        let a = assessment(5 * GB, Tier::Low);
        assert!(a.tier >= Tier::Critical);
        let h = assemble(&a, inputs(true));
        assert!(
            h.car_writeable,
            "car_writeable must not be derived from the tier"
        );
        assert_eq!(h.archive_tier, "Critical");
    }

    #[test]
    fn sparse_warning_derived_from_logical_vs_allocated() {
        let a = assessment(100 * GB, Tier::Healthy);
        let mut inp = inputs(true);
        inp.disk_img = DiskImgAccounting {
            nominal_bytes: 4 * GB,
            allocated_bytes: 2 * GB,
        };
        let h = assemble(&a, inp);
        assert!(h.sparse_image_warning());
    }

    #[test]
    fn round_trips_through_serde_json() {
        let a = assessment(100 * GB, Tier::Healthy);
        let h = assemble(&a, inputs(true));
        let json = serde_json::to_string(&h).unwrap();
        let back: super::StorageHealth = serde_json::from_str(&json).unwrap();
        assert_eq!(h, back);
    }
}
