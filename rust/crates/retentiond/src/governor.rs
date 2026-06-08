//! Slice 6.1d (part 2) — the **continuous space governor**: the tier state
//! machine, `st_dev` budget grouping, reserve tiers, the inode budget, and the
//! sparse-`disk.img` guard.
//!
//! The governor runs **independently of the copy/verify pipeline** so it keeps
//! reacting (and can preempt) even when copying is wedged ([`docs/specs/storage.md`]
//! §3). This module holds the **pure** classification: given a `statfs` snapshot
//! of the relevant paths, the [`config::GovernorConfig`] thresholds, and whether a
//! safe eviction candidate exists, it computes the [`Tier`] with **hysteresis**
//! (enter ≠ exit, no flapping) and the supporting facts the status API reports.
//!
//! Key invariants encoded here:
//! - Paths that **share a device** (`st_dev`) collapse to one budget under the
//!   **strictest** reserve — archive growth on a shared device can starve the OS.
//! - The **OS/root reserve is sacrosanct**: breaching it forces at least Critical
//!   regardless of how the archive looks.
//! - **Inodes** are a parallel budget (segments/thumbnails exhaust inodes first).
//! - A sub-99%-allocated `disk.img` raises a **sparse-image Critical** alert; the
//!   not-yet-allocated blocks are subtracted from archive headroom.
//! - **Exhausted** is *not latched*: it is recomputed each cycle from live space
//!   and whether any safe candidate exists, so it auto-exits the moment one does.

use crate::config::GovernorConfig;
use crate::io::FsStat;

/// Storage governor tier, ordered by severity (`Healthy` is the least severe).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Tier {
    /// Full operation.
    Healthy,
    /// Mild pressure: trim caches, evict durable low-value, warn UI.
    Low,
    /// Pause Recent mirroring / bulk Sentry; evict durable aggressively.
    Critical,
    /// Stop all optional writers; (with opt-in) evict undurable low-value.
    Emergency,
    /// Below Emergency **and** no safe candidate remains — surface blockers,
    /// stop, auto-exit the instant a candidate reappears.
    Exhausted,
}

impl Tier {
    /// The most severe of two tiers.
    #[must_use]
    pub fn max_severity(self, other: Self) -> Self {
        if self >= other { self } else { other }
    }

    /// Lower-case wire string for the status API.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Healthy => "Healthy",
            Self::Low => "Low",
            Self::Critical => "Critical",
            Self::Emergency => "Emergency",
            Self::Exhausted => "Exhausted",
        }
    }
}

/// `statfs` seam. The live implementation calls `statfs(2)`; tests inject
/// deterministic [`FsStat`] snapshots.
pub trait Statfs {
    /// Read filesystem statistics for `path`.
    ///
    /// # Errors
    /// Propagates the underlying `statfs` failure.
    fn statfs(&self, path: &str) -> std::io::Result<FsStat>;
}

/// The role a probed path plays in the budget model.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FsRole {
    /// `/` (OS root) — its reserve is sacrosanct.
    Root,
    /// The data filesystem holding `archive/`, `SQLite`, staging.
    Data,
}

/// One probed path's `statfs` reading plus its role.
#[derive(Debug, Clone, Copy)]
pub struct FsSample {
    /// The role this path plays.
    pub role: FsRole,
    /// The `statfs` reading.
    pub stat: FsStat,
}

/// `disk.img` accounting for the sparse-image guard. Provisioning `fallocate`s
/// the image fully, so steady-state `allocated == nominal`.
#[derive(Debug, Clone, Copy)]
pub struct DiskImgAccounting {
    /// Full nominal (logical) size of the image.
    pub nominal_bytes: u64,
    /// Currently allocated blocks (`st_blocks × 512`).
    pub allocated_bytes: u64,
}

impl DiskImgAccounting {
    /// Not-yet-allocated bytes that must be reserved from archive headroom so a
    /// momentarily sparse image cannot be mistaken for free space.
    #[must_use]
    pub const fn sparse_guard_bytes(&self) -> u64 {
        self.nominal_bytes.saturating_sub(self.allocated_bytes)
    }

    /// Whether the image is under 99% allocated (raises a sparse-image alert).
    #[must_use]
    pub fn is_sparse(&self) -> bool {
        if self.nominal_bytes == 0 {
            return false;
        }
        // allocated/nominal < 0.99  ⇔  allocated*100 < nominal*99
        self.allocated_bytes.saturating_mul(100) < self.nominal_bytes.saturating_mul(99)
    }
}

/// The governor's per-cycle assessment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GovernorAssessment {
    /// The final tier (most severe of all signals).
    pub tier: Tier,
    /// Whether root and data share a device (one combined budget).
    pub shared_device: bool,
    /// Whether the sacrosanct OS/root reserve is breached.
    pub root_reserve_breached: bool,
    /// Whether the `disk.img` is under-allocated (sparse-image warning).
    pub sparse_image_warning: bool,
    /// Tier implied by free **space** alone (after hysteresis).
    pub space_tier: Tier,
    /// Tier implied by free **inodes** alone.
    pub inode_tier: Tier,
    /// Free bytes on the data filesystem.
    pub data_free_bytes: u64,
    /// Free inodes on the data filesystem.
    pub data_free_inodes: u64,
    /// Estimated bytes usable for archive growth after reserves + sparse guard.
    pub usable_for_archive_bytes: u64,
}

/// Evaluate the governor for one cycle.
///
/// `prev_space` is **last cycle's pure space tier** ([`GovernorAssessment::space_tier`],
/// not the combined `tier`) — feeding the combined tier back would let inode/root/
/// sparse pressure or the no-candidate overlay contaminate the space hysteresis.
/// `samples` must include at least one [`FsRole::Data`] path; if it does not (a
/// `statfs` failure left us blind), the assessment fails **toward** safety by
/// reporting [`Tier::Critical`]. `has_safe_candidate` is whether
/// [`crate::value::list_eviction_candidates`] would currently return anything — it
/// gates entry to and exit from [`Tier::Exhausted`] (applied as a live overlay on
/// the *combined* tier, never latched).
#[must_use]
pub fn evaluate(
    prev_space: Tier,
    samples: &[FsSample],
    disk_img: DiskImgAccounting,
    has_safe_candidate: bool,
    cfg: &GovernorConfig,
) -> GovernorAssessment {
    let Some(data) = samples
        .iter()
        .find(|s| s.role == FsRole::Data)
        .map(|s| s.stat)
    else {
        // Blind to the data filesystem — be conservative.
        return GovernorAssessment {
            tier: Tier::Critical,
            shared_device: false,
            root_reserve_breached: false,
            sparse_image_warning: disk_img.is_sparse(),
            space_tier: Tier::Critical,
            inode_tier: Tier::Healthy,
            data_free_bytes: 0,
            data_free_inodes: 0,
            usable_for_archive_bytes: 0,
        };
    };

    let root = samples
        .iter()
        .find(|s| s.role == FsRole::Root)
        .map(|s| s.stat);
    let shared_device = root.is_some_and(|r| r.dev_id == data.dev_id);

    // Root reserve (sacrosanct). On a shared device it applies to the single
    // combined budget (== data). When no root sample is available (statfs failed,
    // or a single-card appliance where `/` and data are the same filesystem) we
    // fall back to the data stat: the OS reserve must be enforced even when we
    // cannot see a distinct root, so this fails **toward** protecting it rather
    // than silently disabling the check.
    let root_stat = if shared_device {
        data
    } else {
        root.unwrap_or(data)
    };
    let root_reserve_breached =
        root_stat.free_bytes < cfg.root_reserve.limit_bytes(root_stat.total_bytes);

    // Pure free-space tier with hysteresis (no overlay folded in, so it can be
    // safely fed back as `prev_space` next cycle).
    let space_tier = classify_space(prev_space, data.free_bytes, data.total_bytes, cfg);

    // Inode tier (parallel budget; no hysteresis band — inodes rarely flap).
    let inode_tier = classify_inodes(data, cfg);

    let sparse_image_warning = disk_img.is_sparse();

    // Combine: the most severe of every independent signal.
    let mut tier = space_tier.max_severity(inode_tier);
    if root_reserve_breached {
        tier = tier.max_severity(Tier::Critical);
    }
    if sparse_image_warning {
        tier = tier.max_severity(Tier::Critical);
    }

    // No-safe-candidate exhaustion overlay, applied to the COMBINED tier and
    // recomputed live (never latched): if we are at Emergency-or-worse for any
    // reason and nothing is safe to evict, we are Exhausted; the instant a
    // candidate reappears (or pressure eases) this drops back automatically. It
    // is deliberately NOT folded into `space_tier`, so it cannot contaminate the
    // hysteresis state fed back next cycle.
    if tier >= Tier::Emergency && !has_safe_candidate {
        tier = Tier::Exhausted;
    }

    let usable_for_archive_bytes = usable_for_archive(data, shared_device, disk_img, cfg);

    GovernorAssessment {
        tier,
        shared_device,
        root_reserve_breached,
        sparse_image_warning,
        space_tier,
        inode_tier,
        data_free_bytes: data.free_bytes,
        data_free_inodes: data.free_inodes,
        usable_for_archive_bytes,
    }
}

/// Classify the **pure** free-space tier with hysteresis (no overlay).
///
/// Worsening (free dropping) follows the *enter* (high-water) marks directly.
/// Improving (free rising) is gated tier-by-tier: the result may only climb to a
/// healthier tier once free has cleared the *exit* (low-water) band of each tier
/// it passes through, and never past the enter-based floor `raw`. This both kills
/// flapping under steady pressure **and** prevents skipping an intermediate
/// tier's dead band (e.g. recovering Emergency→Low without clearing Critical's
/// exit).
fn classify_space(prev_space: Tier, free: u64, total: u64, cfg: &GovernorConfig) -> Tier {
    // Enter-based tier (the most severe we could be right now), severe first.
    let raw = if free < cfg.exhausted_enter_below.limit_bytes(total) {
        Tier::Exhausted
    } else if free < cfg.emergency.enter_below.limit_bytes(total) {
        Tier::Emergency
    } else if free < cfg.critical.enter_below.limit_bytes(total) {
        Tier::Critical
    } else if free < cfg.low.enter_below.limit_bytes(total) {
        Tier::Low
    } else {
        Tier::Healthy
    };

    // Worsening or steady: enter marks govern directly.
    if raw >= prev_space {
        return raw;
    }

    // Improving: climb from prev_space toward `raw`, one tier at a time, only
    // across cleared exit bands. Stop at `raw` (the enter floor) or at the first
    // tier whose exit band is not yet cleared.
    let mut result = prev_space;
    while result > raw && cleared_exit(result, free, total, cfg) {
        result = one_tier_healthier(result);
    }
    result
}

/// The next-healthier tier (saturating at `Healthy`).
const fn one_tier_healthier(tier: Tier) -> Tier {
    match tier {
        Tier::Exhausted => Tier::Emergency,
        Tier::Emergency => Tier::Critical,
        Tier::Critical => Tier::Low,
        Tier::Low | Tier::Healthy => Tier::Healthy,
    }
}

/// Whether free space has risen above the exit (low-water) mark required to leave
/// `tier` toward a healthier one.
fn cleared_exit(tier: Tier, free: u64, total: u64, cfg: &GovernorConfig) -> bool {
    match tier {
        // No band to clear when already Healthy.
        Tier::Healthy => true,
        Tier::Low => free >= cfg.low.exit_above.limit_bytes(total),
        Tier::Critical => free >= cfg.critical.exit_above.limit_bytes(total),
        Tier::Emergency => free >= cfg.emergency.exit_above.limit_bytes(total),
        // Exhausted-by-floor: require a small dead band above the floor (the
        // Emergency *enter* mark) before climbing, so we don't flap at the floor.
        Tier::Exhausted => free >= cfg.emergency.enter_below.limit_bytes(total),
    }
}

/// Classify the inode tier from the free-inode fraction.
fn classify_inodes(data: FsStat, cfg: &GovernorConfig) -> Tier {
    // A filesystem that does not report inodes (total == 0) yields frac 0.0;
    // treat that as "no inode pressure signal" rather than Emergency.
    if data.total_inodes == 0 {
        return Tier::Healthy;
    }
    let frac = data.free_inodes_frac();
    if frac < cfg.inodes.emergency_frac {
        Tier::Emergency
    } else if frac < cfg.inodes.critical_frac {
        Tier::Critical
    } else if frac < cfg.inodes.low_frac {
        Tier::Low
    } else {
        Tier::Healthy
    }
}

/// Estimate bytes usable for archive growth: free minus the operating reserve
/// (the Low enter mark stands in for it), minus the root reserve on a shared
/// device, minus the sparse-image guard. Saturating at zero.
fn usable_for_archive(
    data: FsStat,
    shared_device: bool,
    disk_img: DiskImgAccounting,
    cfg: &GovernorConfig,
) -> u64 {
    let mut usable = data.free_bytes;
    usable = usable.saturating_sub(cfg.low.enter_below.limit_bytes(data.total_bytes));
    if shared_device {
        usable = usable.saturating_sub(cfg.root_reserve.limit_bytes(data.total_bytes));
    }
    usable.saturating_sub(disk_img.sparse_guard_bytes())
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]
mod tests {
    use super::{DiskImgAccounting, FsRole, FsSample, Tier, classify_inodes, evaluate};
    use crate::config::GovernorConfig;
    use crate::io::FsStat;

    const GB: u64 = 1 << 30;

    fn full_img(nominal: u64) -> DiskImgAccounting {
        DiskImgAccounting {
            nominal_bytes: nominal,
            allocated_bytes: nominal,
        }
    }

    fn data_stat(free: u64, total: u64) -> FsStat {
        FsStat {
            dev_id: 1,
            free_bytes: free,
            total_bytes: total,
            free_inodes: 1_000_000,
            total_inodes: 1_000_000,
        }
    }

    fn data_only(free: u64, total: u64) -> Vec<FsSample> {
        vec![FsSample {
            role: FsRole::Data,
            stat: data_stat(free, total),
        }]
    }

    #[test]
    fn healthy_when_plenty_free() {
        let cfg = GovernorConfig::default();
        let a = evaluate(
            Tier::Healthy,
            &data_only(100 * GB, 256 * GB),
            full_img(4 * GB),
            true,
            &cfg,
        );
        assert_eq!(a.tier, Tier::Healthy);
    }

    #[test]
    fn enters_low_then_critical_as_space_drops() {
        let cfg = GovernorConfig::default();
        let total = 256 * GB;
        // Below 6% (16 GiB) but above 3% → Low.
        let a = evaluate(
            Tier::Healthy,
            &data_only(12 * GB, total),
            full_img(4 * GB),
            true,
            &cfg,
        );
        assert_eq!(a.space_tier, Tier::Low);
        // Below 3% (8 GiB) → Critical.
        let b = evaluate(
            Tier::Low,
            &data_only(5 * GB, total),
            full_img(4 * GB),
            true,
            &cfg,
        );
        assert_eq!(b.space_tier, Tier::Critical);
    }

    #[test]
    fn hysteresis_prevents_flap_between_low_and_healthy() {
        let cfg = GovernorConfig::default();
        let total = 256 * GB;
        // Currently Low. Free recovers to 17 GiB: above Low enter (16) but below
        // Low exit (20) → must STAY Low, not flap to Healthy.
        let a = evaluate(
            Tier::Low,
            &data_only(17 * GB, total),
            full_img(4 * GB),
            true,
            &cfg,
        );
        assert_eq!(a.space_tier, Tier::Low);
        // Free recovers past the exit (21 GiB > 20) → now Healthy.
        let b = evaluate(
            Tier::Low,
            &data_only(21 * GB, total),
            full_img(4 * GB),
            true,
            &cfg,
        );
        assert_eq!(b.space_tier, Tier::Healthy);
    }

    #[test]
    fn root_reserve_breach_forces_at_least_critical() {
        let cfg = GovernorConfig::default();
        let total = 256 * GB;
        // Data fs looks healthy, but the SHARED root reserve is breached.
        let root = FsStat {
            dev_id: 9,
            free_bytes: 100 * GB,
            total_bytes: total,
            free_inodes: 1_000_000,
            total_inodes: 1_000_000,
        };
        // Make root its own device with tiny free to breach the 2 GiB floor.
        let breached_root = FsStat {
            free_bytes: 100 << 20,
            ..root
        };
        let samples = vec![
            FsSample {
                role: FsRole::Data,
                stat: data_stat(100 * GB, total),
            },
            FsSample {
                role: FsRole::Root,
                stat: breached_root,
            },
        ];
        let a = evaluate(Tier::Healthy, &samples, full_img(4 * GB), true, &cfg);
        assert!(a.root_reserve_breached);
        assert!(a.tier >= Tier::Critical);
    }

    #[test]
    fn shared_device_is_detected() {
        let cfg = GovernorConfig::default();
        let total = 256 * GB;
        let same = data_stat(100 * GB, total); // dev_id 1
        let samples = vec![
            FsSample {
                role: FsRole::Data,
                stat: same,
            },
            FsSample {
                role: FsRole::Root,
                stat: same,
            },
        ];
        let a = evaluate(Tier::Healthy, &samples, full_img(4 * GB), true, &cfg);
        assert!(a.shared_device);
    }

    #[test]
    fn sparse_image_forces_critical_and_warns() {
        let cfg = GovernorConfig::default();
        // 50% allocated → sparse.
        let sparse = DiskImgAccounting {
            nominal_bytes: 4 * GB,
            allocated_bytes: 2 * GB,
        };
        let a = evaluate(
            Tier::Healthy,
            &data_only(100 * GB, 256 * GB),
            sparse,
            true,
            &cfg,
        );
        assert!(a.sparse_image_warning);
        assert!(a.tier >= Tier::Critical);
    }

    #[test]
    fn exhausted_when_low_space_and_no_safe_candidate_then_auto_exits() {
        let cfg = GovernorConfig::default();
        let total = 256 * GB;
        // Emergency-level free (3 GiB < 4 GiB emergency enter) with NO candidate
        // → Exhausted.
        let exhausted = evaluate(
            Tier::Critical,
            &data_only(3 * GB, total),
            full_img(4 * GB),
            false,
            &cfg,
        );
        assert_eq!(exhausted.tier, Tier::Exhausted);
        // The PURE space tier is Emergency (the overlay is not folded in), so it
        // is what the live loop feeds back as `prev_space`.
        assert_eq!(exhausted.space_tier, Tier::Emergency);
        // Same space but a candidate appears → drops to the space tier (Emergency),
        // not latched at Exhausted.
        let recovered = evaluate(
            exhausted.space_tier,
            &data_only(3 * GB, total),
            full_img(4 * GB),
            true,
            &cfg,
        );
        assert_eq!(recovered.tier, Tier::Emergency);
    }

    #[test]
    fn inode_pressure_raises_tier_independently_of_bytes() {
        let cfg = GovernorConfig::default();
        let total = 256 * GB;
        // Lots of free bytes but inodes nearly exhausted (< 0.75% free) → Emergency.
        let stat = FsStat {
            dev_id: 1,
            free_bytes: 100 * GB,
            total_bytes: total,
            free_inodes: 5,
            total_inodes: 1_000_000,
        };
        let a = evaluate(
            Tier::Healthy,
            &[FsSample {
                role: FsRole::Data,
                stat,
            }],
            full_img(4 * GB),
            true,
            &cfg,
        );
        assert_eq!(a.inode_tier, Tier::Emergency);
        assert!(a.tier >= Tier::Emergency);
    }

    #[test]
    fn missing_inodes_report_no_pressure() {
        let cfg = GovernorConfig::default();
        let stat = FsStat {
            dev_id: 1,
            free_bytes: 100 * GB,
            total_bytes: 256 * GB,
            free_inodes: 0,
            total_inodes: 0,
        };
        assert_eq!(classify_inodes(stat, &cfg), Tier::Healthy);
    }

    #[test]
    fn blind_to_data_fs_fails_to_critical() {
        let cfg = GovernorConfig::default();
        let root_only = vec![FsSample {
            role: FsRole::Root,
            stat: data_stat(100 * GB, 256 * GB),
        }];
        let a = evaluate(Tier::Healthy, &root_only, full_img(4 * GB), true, &cfg);
        assert_eq!(a.tier, Tier::Critical);
    }

    #[test]
    fn recovery_does_not_skip_intermediate_exit_bands() {
        // Bug #2: improving from a severe tier must climb one band at a time,
        // honouring each intermediate exit mark, not jump straight to the raw
        // enter floor. Start Emergency; free recovers to 9 GiB. Enter floor at
        // 9 GiB is Low (>= 8 critical enter), but Critical's exit (16 GiB) is NOT
        // cleared, so we must stop at Critical, not fall through to Low.
        let cfg = GovernorConfig::default();
        let total = 256 * GB;
        let a = evaluate(
            Tier::Emergency,
            &data_only(9 * GB, total),
            full_img(4 * GB),
            true,
            &cfg,
        );
        assert_eq!(a.space_tier, Tier::Critical);

        // Free now clears Critical's exit (17 GiB > 16) but not Low's exit
        // (20 GiB) → climbs exactly one more band to Low.
        let b = evaluate(
            a.space_tier,
            &data_only(17 * GB, total),
            full_img(4 * GB),
            true,
            &cfg,
        );
        assert_eq!(b.space_tier, Tier::Low);

        // Finally clears Low's exit (21 GiB > 20) → Healthy.
        let c = evaluate(
            b.space_tier,
            &data_only(21 * GB, total),
            full_img(4 * GB),
            true,
            &cfg,
        );
        assert_eq!(c.space_tier, Tier::Healthy);
    }

    #[test]
    fn prev_combined_tier_does_not_contaminate_space_hysteresis() {
        // Bug #3: the caller feeds back the previous *space* tier, never the
        // combined tier. Even if last cycle's combined tier was Exhausted (e.g.
        // via the no-candidate overlay) while space was only Emergency, a fully
        // recovered data fs must report Healthy — not be pinned by a band space
        // never actually held.
        let cfg = GovernorConfig::default();
        let total = 256 * GB;
        let a = evaluate(
            Tier::Emergency, // previous *space* tier
            &data_only(100 * GB, total),
            full_img(4 * GB),
            true,
            &cfg,
        );
        assert_eq!(a.space_tier, Tier::Healthy);
        assert_eq!(a.tier, Tier::Healthy);
    }

    #[test]
    fn root_reserve_enforced_even_without_a_root_sample() {
        // Bug #4: on a single-card appliance there is no distinct Root sample, so
        // the root reserve must fall back to the data fs rather than fail open.
        // Data free (1 GiB) is below the 2 GiB root floor → breached.
        let cfg = GovernorConfig::default();
        let total = 256 * GB;
        let a = evaluate(
            Tier::Healthy,
            &data_only(GB, total),
            full_img(4 * GB),
            true,
            &cfg,
        );
        assert!(a.root_reserve_breached);
        assert!(a.tier >= Tier::Critical);
    }
}
