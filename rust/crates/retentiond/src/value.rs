//! Slice 6.1d (part 1) — the **value-scoring eviction model**.
//!
//! The governor frees space by deleting the **least-valuable safe** Pi-side item
//! first ([`docs/specs/storage.md`] §4). Correctness here is dominated by one
//! rule from the design review: **fail-closed at candidate construction**. The
//! candidate list returned by [`list_eviction_candidates`] *never contains* an
//! item that must not be auto-deleted (pinned, leased, in-grace, quarantined,
//! inside `disk.img`, **undurable `SavedClips`**, or undurable Sentry/Track
//! unless the operator opted in and the tier is Emergency). A later sort or tier
//! branch therefore cannot reintroduce an unsafe item — under exhaustion the safe
//! set is simply **empty**, which the governor reports as a blocker rather than
//! broadening eligibility.
//!
//! Value is a comparable integer: a **base by class** (durability is the dominant
//! axis — a durable copy means deletion is *not* loss) plus clamped modifiers.
//! Size is used **only as an efficiency tie-breaker** between equal-value items,
//! never to prefer a high-value large item over a low-value small one
//! ([`docs/specs/storage.md`] §4.3). All weights are CALIBRATION-GATED.

use crate::durability::Durability;
use crate::folder::FolderClass;
use crate::governor::Tier;
use crate::io::ArchiveItemId;

// CALIBRATION-GATED (Task 2.7 / storage.md §7): provisional value weights from
// the storage.md §4.3 advisory table. The *ordering logic* below is correct
// independent of these magnitudes; calibration only retunes them.
mod weights {
    pub(super) const TEMP_TRASH: i64 = -1000;
    pub(super) const THUMB_CACHE: i64 = -900;
    pub(super) const RECENT_MIRROR: i64 = 0;
    pub(super) const DURABLE_FLOOD_SENTRY: i64 = 150;
    pub(super) const DURABLE_NORMAL_SENTRY: i64 = 300;
    pub(super) const DURABLE_TRACKMODE: i64 = 450;
    pub(super) const DURABLE_SAVED: i64 = 650;
    pub(super) const UNDURABLE_FLOOD_SENTRY: i64 = 700;
    pub(super) const UNDURABLE_NORMAL_SENTRY: i64 = 800;
    pub(super) const UNDURABLE_TRACKMODE: i64 = 900;

    pub(super) const USER_SAVE: i64 = 300;
    pub(super) const SENTRY_FLOOD: i64 = -250;
    pub(super) const IMPACT_EVENT: i64 = 200;
    pub(super) const HAS_TELEMETRY: i64 = 100;
    pub(super) const EVENT_ADJACENT: i64 = 150;
    pub(super) const DUPLICATE_CLUSTER: i64 = -150;
    pub(super) const USER_DISPOSABLE: i64 = -500;
    pub(super) const RECENCY: i64 = 150;
}

/// Coarse recency bucket → a clamped score modifier (newer is more valuable).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Recency {
    /// Recently captured — slightly more valuable.
    Newer,
    /// Neither new nor very old.
    Mid,
    /// Very old — slightly less valuable.
    VeryOld,
}

/// The non-protected base class of an eviction unit (the sacrifice order of
/// [`docs/specs/storage.md`] §3.2 / §4.3 falls out of the base weights).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EvictionKind {
    /// Incomplete temp / copy scratch.
    TempTrash,
    /// Thumbnails / regenerable cache.
    ThumbnailCache,
    /// A `RecentClips` Pi-side mirror segment.
    RecentMirror,
    /// An archived event folder (`SavedClips`/`SentryClips`/`TeslaTrackMode`).
    Event {
        /// Which event folder this archived item came from.
        folder: FolderClass,
    },
}

/// Class-A (no permanent loss on eviction) vs Class-B (permanent loss).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LossClass {
    /// Eviction loses nothing irreplaceable (durable, regenerable, or best-effort
    /// mirror).
    ClassA,
    /// Eviction is permanent loss (undurable footage) — only under Emergency +
    /// explicit opt-in, and never for `SavedClips`.
    ClassB,
}

/// Why an item was hard-excluded from the eviction candidate set.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExclusionReason {
    /// Pinned / favorited / user-marked-keep.
    Pinned,
    /// Holds an unexpired playback/upload lease.
    Leased,
    /// Inside the stability / grace window.
    InGrace,
    /// Index row is in a `QUARANTINED`/inconsistent state.
    Quarantined,
    /// Lives inside the car-visible `disk.img` (never a host-space target).
    InsideDiskImg,
    /// Undurable `SavedClips` — **never** auto-deleted.
    UndurableSaved,
    /// Undurable Sentry/Track and the Emergency + opt-in gate is not satisfied.
    UndurableProtected,
}

/// A candidate item with all the signals needed to score it. Hard-exclusion
/// flags are evaluated first by [`list_eviction_candidates`].
///
/// This is intentionally a **flat record of independent boolean signals** mirrored
/// from `indexd`/`scannerd` columns (pinned, leased, telemetry, …); collapsing
/// them into sub-enums would obscure the 1:1 mapping to the index schema, so
/// `struct_excessive_bools` is allowed.
#[derive(Debug, Clone)]
#[allow(clippy::struct_excessive_bools)]
pub struct EvictionItem {
    /// Stable archive-item identity.
    pub id: ArchiveItemId,
    /// The item's base class.
    pub kind: EvictionKind,
    /// Durability (off-device copy?) — irrelevant for non-event kinds.
    pub durability: Durability,
    /// Whether the (Sentry) item is part of a detected flood.
    pub sentry_flood: bool,
    /// Bytes the item occupies (efficiency tie-breaker only).
    pub size: u64,
    /// Recency bucket.
    pub recency: Recency,
    /// User pressed Save/honk.
    pub user_save: bool,
    /// Impact / alarm / severe event (from `indexd`).
    pub impact_event: bool,
    /// Carries telemetry / `event.json` / geo waypoints.
    pub has_telemetry: bool,
    /// Adjacent to a Saved event (context).
    pub event_adjacent: bool,
    /// Duplicate / same-time-place cluster.
    pub duplicate_cluster: bool,
    /// User explicitly marked disposable.
    pub user_marked_disposable: bool,
    /// Hard exclusion: pinned / favorited.
    pub pinned: bool,
    /// Hard exclusion: has an unexpired lease.
    pub leased: bool,
    /// Hard exclusion: inside the grace window.
    pub in_grace: bool,
    /// Hard exclusion: quarantined / inconsistent.
    pub quarantined: bool,
    /// Hard exclusion: lives inside `disk.img`.
    pub inside_disk_img: bool,
}

/// A safe, scored eviction candidate (already past all hard exclusions).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScoredCandidate {
    /// The item.
    pub id: ArchiveItemId,
    /// Computed value (lower = delete first).
    pub value: i64,
    /// Loss class (A safe / B permanent-loss).
    pub loss_class: LossClass,
    /// Bytes reclaimable by evicting this item.
    pub size: u64,
}

/// The current eviction policy context (tier + the Class-B opt-in).
#[derive(Debug, Clone, Copy)]
pub struct EvictionPolicy {
    /// Current governor tier.
    pub tier: Tier,
    /// Operator opt-in for Emergency eviction of **undurable** Sentry/Track.
    pub allow_emergency_undurable_sentry: bool,
}

/// Determine whether `item` is hard-excluded, and why. Order matters only for
/// the *reported* reason; any `Some` means "never auto-delete this now".
#[must_use]
pub fn hard_exclusion(item: &EvictionItem, policy: EvictionPolicy) -> Option<ExclusionReason> {
    if item.pinned {
        return Some(ExclusionReason::Pinned);
    }
    if item.leased {
        return Some(ExclusionReason::Leased);
    }
    if item.in_grace {
        return Some(ExclusionReason::InGrace);
    }
    if item.quarantined {
        return Some(ExclusionReason::Quarantined);
    }
    if item.inside_disk_img {
        return Some(ExclusionReason::InsideDiskImg);
    }

    // Durability floor (the fail-closed core). Only event folders carry a
    // durability obligation; temp/thumb/recent-mirror are always Class-A safe.
    if let EvictionKind::Event { folder } = item.kind {
        if !item.durability.is_durable() {
            match folder {
                // Undurable Saved is NEVER auto-deleted, at any tier.
                FolderClass::SavedClips => return Some(ExclusionReason::UndurableSaved),
                // Undurable Sentry/Track: Class-B, only Emergency + opt-in.
                FolderClass::SentryClips | FolderClass::TeslaTrackMode => {
                    let emergency = matches!(policy.tier, Tier::Emergency | Tier::Exhausted);
                    if !(emergency && policy.allow_emergency_undurable_sentry) {
                        return Some(ExclusionReason::UndurableProtected);
                    }
                }
                // An undurable RecentClips-classified event is nonsensical;
                // treat as protected to fail closed.
                FolderClass::RecentClips => return Some(ExclusionReason::UndurableProtected),
            }
        }
    }
    None
}

/// The base value for an item's class (durability splits event bases).
fn base_value(item: &EvictionItem) -> i64 {
    match item.kind {
        EvictionKind::TempTrash => weights::TEMP_TRASH,
        EvictionKind::ThumbnailCache => weights::THUMB_CACHE,
        EvictionKind::RecentMirror => weights::RECENT_MIRROR,
        EvictionKind::Event { folder } => event_base(folder, item.durability, item.sentry_flood),
    }
}

fn event_base(folder: FolderClass, durability: Durability, flood: bool) -> i64 {
    match (folder, durability.is_durable()) {
        (FolderClass::SentryClips, true) => {
            if flood {
                weights::DURABLE_FLOOD_SENTRY
            } else {
                weights::DURABLE_NORMAL_SENTRY
            }
        }
        (FolderClass::TeslaTrackMode, true) => weights::DURABLE_TRACKMODE,
        (FolderClass::SavedClips, true) => weights::DURABLE_SAVED,
        (FolderClass::SentryClips, false) => {
            if flood {
                weights::UNDURABLE_FLOOD_SENTRY
            } else {
                weights::UNDURABLE_NORMAL_SENTRY
            }
        }
        (FolderClass::TeslaTrackMode, false) => weights::UNDURABLE_TRACKMODE,
        // Saved (undurable) and Recent-classified events are excluded earlier;
        // a defensive high value keeps them last if they somehow appear.
        (FolderClass::SavedClips | FolderClass::RecentClips, _) => i64::MAX / 2,
    }
}

/// Compute the comparable value of an item (lower = evict first).
#[must_use]
pub fn value_score(item: &EvictionItem) -> i64 {
    let mut v = base_value(item);
    let recency = match item.recency {
        Recency::Newer => weights::RECENCY,
        Recency::Mid => 0,
        Recency::VeryOld => -weights::RECENCY,
    };
    v = v.saturating_add(recency);
    if item.user_save {
        v = v.saturating_add(weights::USER_SAVE);
    }
    if item.sentry_flood {
        v = v.saturating_add(weights::SENTRY_FLOOD);
    }
    if item.impact_event {
        v = v.saturating_add(weights::IMPACT_EVENT);
    }
    if item.has_telemetry {
        v = v.saturating_add(weights::HAS_TELEMETRY);
    }
    if item.event_adjacent {
        v = v.saturating_add(weights::EVENT_ADJACENT);
    }
    if item.duplicate_cluster {
        v = v.saturating_add(weights::DUPLICATE_CLUSTER);
    }
    if item.user_marked_disposable {
        v = v.saturating_add(weights::USER_DISPOSABLE);
    }
    v
}

/// The loss class of an item (Class-B = undurable footage).
fn loss_class(item: &EvictionItem) -> LossClass {
    match item.kind {
        EvictionKind::Event { folder }
            if folder.is_event_folder() && !item.durability.is_durable() =>
        {
            LossClass::ClassB
        }
        _ => LossClass::ClassA,
    }
}

/// Build the **safe** eviction candidate set, sorted least-valuable first.
///
/// Hard exclusions are applied first, so unsafe items are *absent* from the
/// result (fail-closed). Remaining items are sorted ascending by value, then by
/// loss class (Class-A before Class-B, so no-loss evictions are always preferred
/// at equal value), then by **size descending** (free more bytes first — the only
/// role size plays), then by id for determinism.
#[must_use]
pub fn list_eviction_candidates(
    items: &[EvictionItem],
    policy: EvictionPolicy,
) -> Vec<ScoredCandidate> {
    let mut out: Vec<ScoredCandidate> = items
        .iter()
        .filter(|it| hard_exclusion(it, policy).is_none())
        .map(|it| ScoredCandidate {
            id: it.id,
            value: value_score(it),
            loss_class: loss_class(it),
            size: it.size,
        })
        .collect();

    out.sort_by(|a, b| {
        a.value
            .cmp(&b.value)
            .then(class_rank(a.loss_class).cmp(&class_rank(b.loss_class)))
            .then(b.size.cmp(&a.size))
            .then(a.id.cmp(&b.id))
    });
    out
}

const fn class_rank(c: LossClass) -> u8 {
    match c {
        LossClass::ClassA => 0,
        LossClass::ClassB => 1,
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
    use super::{
        EvictionItem, EvictionKind, EvictionPolicy, ExclusionReason, LossClass, Recency,
        hard_exclusion, list_eviction_candidates,
    };
    use crate::durability::Durability;
    use crate::folder::FolderClass;
    use crate::governor::Tier;
    use crate::io::ArchiveItemId;

    fn item(id: i64, kind: EvictionKind, durability: Durability) -> EvictionItem {
        EvictionItem {
            id: ArchiveItemId(id),
            kind,
            durability,
            sentry_flood: false,
            size: 100,
            recency: Recency::Mid,
            user_save: false,
            impact_event: false,
            has_telemetry: false,
            event_adjacent: false,
            duplicate_cluster: false,
            user_marked_disposable: false,
            pinned: false,
            leased: false,
            in_grace: false,
            quarantined: false,
            inside_disk_img: false,
        }
    }

    fn event(id: i64, folder: FolderClass, dur: Durability) -> EvictionItem {
        item(id, EvictionKind::Event { folder }, dur)
    }

    fn policy(tier: Tier, opt_in: bool) -> EvictionPolicy {
        EvictionPolicy {
            tier,
            allow_emergency_undurable_sentry: opt_in,
        }
    }

    #[test]
    fn undurable_saved_is_never_a_candidate_even_in_exhausted() {
        let it = event(1, FolderClass::SavedClips, Durability::Undurable);
        for tier in [
            Tier::Healthy,
            Tier::Low,
            Tier::Critical,
            Tier::Emergency,
            Tier::Exhausted,
        ] {
            assert_eq!(
                hard_exclusion(&it, policy(tier, true)),
                Some(ExclusionReason::UndurableSaved),
                "tier {tier:?}"
            );
        }
        // And it never appears in the candidate list.
        let cands = list_eviction_candidates(&[it], policy(Tier::Exhausted, true));
        assert!(cands.is_empty());
    }

    #[test]
    fn undurable_sentry_only_evictable_under_emergency_plus_optin() {
        let it = event(1, FolderClass::SentryClips, Durability::Undurable);
        // Not emergency → protected.
        assert_eq!(
            hard_exclusion(&it, policy(Tier::Critical, true)),
            Some(ExclusionReason::UndurableProtected)
        );
        // Emergency but no opt-in → protected.
        assert_eq!(
            hard_exclusion(&it, policy(Tier::Emergency, false)),
            Some(ExclusionReason::UndurableProtected)
        );
        // Emergency + opt-in → allowed (Class-B).
        assert!(hard_exclusion(&it, policy(Tier::Emergency, true)).is_none());
        let cands = list_eviction_candidates(&[it], policy(Tier::Emergency, true));
        assert_eq!(cands.len(), 1);
        assert_eq!(cands[0].loss_class, LossClass::ClassB);
    }

    #[test]
    fn pinned_leased_grace_quarantine_diskimg_always_excluded() {
        let mut it = event(1, FolderClass::SentryClips, Durability::Durable);
        it.pinned = true;
        assert_eq!(
            hard_exclusion(&it, policy(Tier::Healthy, false)),
            Some(ExclusionReason::Pinned)
        );
        let mut it = event(1, FolderClass::SentryClips, Durability::Durable);
        it.leased = true;
        assert_eq!(
            hard_exclusion(&it, policy(Tier::Healthy, false)),
            Some(ExclusionReason::Leased)
        );
        let mut it = event(1, FolderClass::SentryClips, Durability::Durable);
        it.in_grace = true;
        assert_eq!(
            hard_exclusion(&it, policy(Tier::Healthy, false)),
            Some(ExclusionReason::InGrace)
        );
        let mut it = event(1, FolderClass::SentryClips, Durability::Durable);
        it.quarantined = true;
        assert_eq!(
            hard_exclusion(&it, policy(Tier::Healthy, false)),
            Some(ExclusionReason::Quarantined)
        );
        let mut it = event(1, FolderClass::SentryClips, Durability::Durable);
        it.inside_disk_img = true;
        assert_eq!(
            hard_exclusion(&it, policy(Tier::Healthy, false)),
            Some(ExclusionReason::InsideDiskImg)
        );
    }

    #[test]
    fn ordering_is_least_valuable_safe_first() {
        // temp/thumb < recent < durable-flood-sentry < durable-saved.
        let items = vec![
            event(4, FolderClass::SavedClips, Durability::Durable),
            item(1, EvictionKind::TempTrash, Durability::Durable),
            item(3, EvictionKind::RecentMirror, Durability::Durable),
            item(2, EvictionKind::ThumbnailCache, Durability::Durable),
        ];
        let cands = list_eviction_candidates(&items, policy(Tier::Critical, false));
        let order: Vec<i64> = cands.iter().map(|c| c.id.0).collect();
        assert_eq!(order, vec![1, 2, 3, 4]);
    }

    #[test]
    fn durable_saved_evicted_after_durable_sentry() {
        let saved = event(2, FolderClass::SavedClips, Durability::Durable);
        let sentry = event(1, FolderClass::SentryClips, Durability::Durable);
        let cands = list_eviction_candidates(&[saved, sentry], policy(Tier::Critical, false));
        // Sentry (lower value) evicted before Saved (higher value).
        assert_eq!(cands[0].id.0, 1);
        assert_eq!(cands[1].id.0, 2);
    }

    #[test]
    fn size_is_only_a_tiebreaker_never_outranks_value() {
        // A small low-value item beats a large high-value item.
        let mut small_low = item(1, EvictionKind::RecentMirror, Durability::Durable);
        small_low.size = 1;
        let mut large_high = event(2, FolderClass::SavedClips, Durability::Durable);
        large_high.size = 1_000_000;
        let cands =
            list_eviction_candidates(&[large_high, small_low], policy(Tier::Critical, false));
        assert_eq!(
            cands[0].id.0, 1,
            "low-value small item must still be evicted first"
        );
    }

    #[test]
    fn equal_value_prefers_larger_for_efficiency() {
        let mut a = item(1, EvictionKind::RecentMirror, Durability::Durable);
        a.size = 10;
        let mut b = item(2, EvictionKind::RecentMirror, Durability::Durable);
        b.size = 100;
        let cands = list_eviction_candidates(&[a, b], policy(Tier::Critical, false));
        // Same value → larger first (frees more bytes per delete).
        assert_eq!(cands[0].id.0, 2);
    }
}
