//! Slice 6.1b — **`RecentClips`**: empirical rotation tracking, "keeping up?" /
//! "unobserved-gap" health, and the bounded rolling Pi-side mirror.
//!
//! `RecentClips` is the rolling buffer the car overwrites continuously. Its
//! retention window is an **unreadable 1–24 h vehicle setting**
//! ([`docs/specs/retentiond.md`] §2), so we never assume "~1 hour" — we
//! [`RotationEstimator`] it empirically by watching how long segments stay
//! visible across `scannerd` passes, and we win by mirroring **continuously and
//! oldest-first**, not by racing a known deadline.
//!
//! Two honesty signals are surfaced distinctly:
//! - **falling behind** — a segment we *observed* was overwritten by the car
//!   before we archived it (a real, reportable loss for an observed segment);
//! - **unobserved gap** — scan lag / offline time exceeded the estimated window,
//!   so segments may have been created *and* overwritten between scans; we cannot
//!   claim coverage and say so rather than implying it.
//!
//! The mirror is **quota-capped** and evicts the **oldest non-pinned** segment
//! only **after a grace window**, so context that becomes event-adjacent shortly
//! after capture is not thrown away. `RecentClips` is **never** deleted from the
//! car here — the only deletion is of *our* Pi-side mirror copy.

use std::collections::HashSet;

use crate::time::MonoMs;

/// One `RecentClips` segment as seen on the car volume.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecentSegment {
    /// Unique key (e.g. `timestamp-camera`).
    pub key: String,
    /// Capture-time ordering key derived from the filename timestamp. Only
    /// relative ordering/spans are used, never an absolute wall-clock meaning.
    pub capture_ms: i64,
    /// Segment size in bytes.
    pub size: u64,
}

/// The two distinct `RecentClips` honesty signals (kept separate on purpose).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RecentHealth {
    /// Count of **observed** segments that disappeared from the car before we
    /// archived them (a concrete falling-behind warning).
    pub lost_observed: u32,
    /// Whether scan lag / offline time exceeded the estimated rotation window —
    /// we cannot claim coverage for what we never saw.
    pub unobserved_gap: bool,
}

impl RecentHealth {
    /// True iff nothing observed was lost and there is no unobserved gap.
    #[must_use]
    pub const fn keeping_up(&self) -> bool {
        self.lost_observed == 0 && !self.unobserved_gap
    }
}

/// One observation pass fed to [`RotationEstimator::observe`].
#[derive(Debug, Clone, Copy)]
pub struct RotationObservation<'a> {
    /// Segments currently visible on the car volume this pass.
    pub visible: &'a [RecentSegment],
    /// Keys already durably mirrored into the archive (so their loss from the
    /// car is *not* a falling-behind event).
    pub archived_keys: &'a HashSet<String>,
    /// Milliseconds since the previous successful pass (`0` on the first pass /
    /// no gap). Compared against the window estimate to detect an unobserved gap.
    pub scan_gap_ms: i64,
}

/// Stateful empirical rotation tracker. One instance lives for the daemon's
/// lifetime; [`Self::observe`] is called once per `scannerd` pass.
#[derive(Debug, Default)]
pub struct RotationEstimator {
    /// Previously-seen, not-yet-archived segment keys (candidates for loss).
    pending: HashSet<String>,
    /// Last computed window estimate (ms), if at least two segments were ever
    /// visible together.
    window_estimate_ms: Option<i64>,
}

impl RotationEstimator {
    /// Create an estimator with no history.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// The current empirical rotation-window estimate (ms): the span between the
    /// oldest and newest segment most recently visible together. `None` until at
    /// least two segments are seen in one pass. **Advisory only** — used for
    /// health, never as a hard deadline.
    #[must_use]
    pub const fn window_estimate_ms(&self) -> Option<i64> {
        self.window_estimate_ms
    }

    /// Observe one scan pass and return the [`RecentHealth`] signals.
    pub fn observe(&mut self, obs: RotationObservation<'_>) -> RecentHealth {
        // The scan gap that just elapsed must be judged against the window we
        // knew BEFORE this pass — the estimate this pass produces describes the
        // span we can see *now*, not the coverage during the gap. Capture it
        // first, then update.
        let prev_window = self.window_estimate_ms;

        // Update the window estimate from the currently-visible span.
        if let (Some(min), Some(max)) = (
            obs.visible.iter().map(|s| s.capture_ms).min(),
            obs.visible.iter().map(|s| s.capture_ms).max(),
        ) {
            if obs.visible.len() >= 2 {
                self.window_estimate_ms = Some(max.saturating_sub(min).max(0));
            }
        }

        let visible_keys: HashSet<&str> = obs.visible.iter().map(|s| s.key.as_str()).collect();

        // Falling behind: a previously-seen, unarchived segment that is now
        // neither visible nor archived was overwritten before we copied it.
        let mut lost_observed = 0u32;
        for key in &self.pending {
            if !visible_keys.contains(key.as_str()) && !obs.archived_keys.contains(key) {
                lost_observed = lost_observed.saturating_add(1);
            }
        }

        // Rebuild the pending set: still-visible segments that are not yet
        // archived remain candidates; archived/disappeared ones drop out.
        self.pending = obs
            .visible
            .iter()
            .filter(|s| !obs.archived_keys.contains(&s.key))
            .map(|s| s.key.clone())
            .collect();

        // Unobserved gap: if more than a full *previously-estimated* window
        // elapsed between scans (or we were offline), segments could have been
        // created AND overwritten unseen — we cannot claim coverage.
        let unobserved_gap = prev_window.is_some_and(|w| w > 0 && obs.scan_gap_ms > w);

        RecentHealth {
            lost_observed,
            unobserved_gap,
        }
    }
}

/// One archived `RecentClips` mirror segment (Pi-side), for eviction planning.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MirrorSegment {
    /// Segment key.
    pub key: String,
    /// Capture-time ordering key (oldest evicted first).
    pub capture_ms: i64,
    /// Bytes the mirror copy occupies.
    pub size: u64,
    /// When this copy was first archived (monotonic), for the grace window.
    pub first_archived: MonoMs,
    /// Whether the segment is **pinned** (event-adjacent or policy/telemetry
    /// pinned) and therefore not evictable by the rolling cap.
    pub pinned: bool,
}

impl MirrorSegment {
    /// Whether this segment is past its grace window at `now` and may be evicted.
    #[must_use]
    fn past_grace(&self, now: MonoMs, grace_ms: i64) -> bool {
        now.saturating_elapsed_since(self.first_archived) >= grace_ms
    }
}

/// The result of planning the rolling-mirror cap: an ordered list of segment
/// keys to evict (oldest first), and whether the quota is still exceeded after
/// evicting everything that was safe to evict.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct MirrorEvictionPlan {
    /// Keys to evict, oldest first.
    pub evict: Vec<String>,
    /// True if, after evicting all eligible segments, the mirror is **still**
    /// over quota (only pinned / in-grace segments remain). Best-effort by
    /// design — we never evict a pinned or in-grace segment to get under quota.
    pub still_over_quota: bool,
}

/// Plan rolling-mirror eviction to fit `incoming_bytes` of new segments within
/// `quota_bytes`, evicting the **oldest non-pinned** segment that is **past its
/// grace window** first ([`docs/specs/retentiond.md`] §3.3).
///
/// Returns the keys to evict in order. A pinned or in-grace segment is never
/// chosen, even if that leaves the mirror over quota (best-effort, no-loss-of-
/// context guarantee).
#[must_use]
pub fn plan_mirror_eviction(
    archived: &[MirrorSegment],
    incoming_bytes: u64,
    quota_bytes: u64,
    grace_ms: i64,
    now: MonoMs,
) -> MirrorEvictionPlan {
    let mut total: u64 = archived
        .iter()
        .map(|s| s.size)
        .fold(incoming_bytes, u64::saturating_add);

    // Candidates evictable now: non-pinned and past grace, sorted oldest first.
    let mut candidates: Vec<&MirrorSegment> = archived
        .iter()
        .filter(|s| !s.pinned && s.past_grace(now, grace_ms))
        .collect();
    candidates.sort_by(|a, b| a.capture_ms.cmp(&b.capture_ms).then(a.key.cmp(&b.key)));

    let mut evict = Vec::new();
    for seg in candidates {
        if total <= quota_bytes {
            break;
        }
        evict.push(seg.key.clone());
        total = total.saturating_sub(seg.size);
    }

    MirrorEvictionPlan {
        evict,
        still_over_quota: total > quota_bytes,
    }
}

/// A `RecentClips` segment eligible to be mirrored this cycle.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecentCandidate {
    /// Segment key.
    pub key: String,
    /// Capture-time ordering key.
    pub capture_ms: i64,
    /// Whether the segment is adjacent to a Saved/Sentry event (archived first).
    pub event_adjacent: bool,
}

/// Order candidate segments for archiving: **event-adjacent first, then oldest
/// first** (oldest are closest to being overwritten by the car).
#[must_use]
pub fn archive_order(candidates: &[RecentCandidate]) -> Vec<String> {
    let mut ordered: Vec<&RecentCandidate> = candidates.iter().collect();
    ordered.sort_by(|a, b| {
        b.event_adjacent
            .cmp(&a.event_adjacent)
            .then(a.capture_ms.cmp(&b.capture_ms))
            .then(a.key.cmp(&b.key))
    });
    ordered.into_iter().map(|c| c.key.clone()).collect()
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]
mod tests {
    use std::collections::HashSet;

    use super::{
        MirrorSegment, RecentCandidate, RecentSegment, RotationEstimator, RotationObservation,
        archive_order, plan_mirror_eviction,
    };
    use crate::time::MonoMs;

    fn seg(key: &str, capture: i64, size: u64) -> RecentSegment {
        RecentSegment {
            key: key.to_owned(),
            capture_ms: capture,
            size,
        }
    }

    fn archived_none() -> HashSet<String> {
        HashSet::new()
    }

    #[test]
    fn window_estimate_is_span_of_visible_segments() {
        let mut est = RotationEstimator::new();
        let visible = vec![seg("a", 1000, 1), seg("b", 5000, 1), seg("c", 9000, 1)];
        let arch = archived_none();
        est.observe(RotationObservation {
            visible: &visible,
            archived_keys: &arch,
            scan_gap_ms: 0,
        });
        assert_eq!(est.window_estimate_ms(), Some(8000));
    }

    #[test]
    fn falling_behind_when_observed_unarchived_segment_disappears() {
        let mut est = RotationEstimator::new();
        let arch = archived_none();
        // Pass 1: see a and b, neither archived.
        let p1 = vec![seg("a", 1000, 1), seg("b", 2000, 1)];
        let h1 = est.observe(RotationObservation {
            visible: &p1,
            archived_keys: &arch,
            scan_gap_ms: 0,
        });
        assert_eq!(h1.lost_observed, 0);
        // Pass 2: a is gone (overwritten) and was never archived → lost.
        let p2 = vec![seg("b", 2000, 1)];
        let h2 = est.observe(RotationObservation {
            visible: &p2,
            archived_keys: &arch,
            scan_gap_ms: 10,
        });
        assert_eq!(h2.lost_observed, 1);
        assert!(!h2.keeping_up());
    }

    #[test]
    fn archived_segment_disappearing_is_not_a_loss() {
        let mut est = RotationEstimator::new();
        let p1 = vec![seg("a", 1000, 1), seg("b", 2000, 1)];
        est.observe(RotationObservation {
            visible: &p1,
            archived_keys: &archived_none(),
            scan_gap_ms: 0,
        });
        // a is archived before pass 2; its disappearance from the car is fine.
        let mut arch = HashSet::new();
        arch.insert("a".to_owned());
        let p2 = vec![seg("b", 2000, 1)];
        let h2 = est.observe(RotationObservation {
            visible: &p2,
            archived_keys: &arch,
            scan_gap_ms: 10,
        });
        assert_eq!(h2.lost_observed, 0);
        assert!(h2.keeping_up());
    }

    #[test]
    fn unobserved_gap_when_scan_lag_exceeds_window() {
        let mut est = RotationEstimator::new();
        let visible = vec![seg("a", 0, 1), seg("b", 1000, 1)]; // window = 1000
        let arch = archived_none();
        est.observe(RotationObservation {
            visible: &visible,
            archived_keys: &arch,
            scan_gap_ms: 0,
        });
        // Next pass arrives after 5000ms > 1000ms window → unobserved gap.
        let h = est.observe(RotationObservation {
            visible: &visible,
            archived_keys: &arch,
            scan_gap_ms: 5000,
        });
        assert!(h.unobserved_gap);
        assert!(!h.keeping_up());
    }

    #[test]
    fn unobserved_gap_uses_prior_window_not_this_passes_estimate() {
        // Bug #5: the gap must be judged against the window known BEFORE this
        // pass. Pass 1 establishes a small (1000ms) window. Pass 2 arrives after
        // 2000ms — longer than that prior window, so it IS an unobserved gap —
        // but pass 2's own visible span is huge (10000ms). Judging the gap
        // against this pass's fresh estimate (2000 > 10000 == false) would hide
        // the gap; judging against the prior window (2000 > 1000 == true) catches
        // it.
        let mut est = RotationEstimator::new();
        let arch = archived_none();
        let p1 = vec![seg("a", 0, 1), seg("b", 1000, 1)]; // prior window = 1000
        est.observe(RotationObservation {
            visible: &p1,
            archived_keys: &arch,
            scan_gap_ms: 0,
        });
        let p2 = vec![seg("c", 0, 1), seg("d", 10_000, 1)]; // this window = 10000
        let h = est.observe(RotationObservation {
            visible: &p2,
            archived_keys: &arch,
            scan_gap_ms: 2000,
        });
        assert!(h.unobserved_gap);
    }

    #[test]
    fn first_pass_never_reports_unobserved_gap() {
        // With no prior window, a long first scan_gap cannot be judged → no gap.
        let mut est = RotationEstimator::new();
        let visible = vec![seg("a", 0, 1), seg("b", 1000, 1)];
        let h = est.observe(RotationObservation {
            visible: &visible,
            archived_keys: &archived_none(),
            scan_gap_ms: 999_999,
        });
        assert!(!h.unobserved_gap);
    }

    #[test]
    fn mirror_evicts_oldest_nonpinned_past_grace_first() {
        let now = MonoMs(100_000);
        let archived = vec![
            MirrorSegment {
                key: "old".to_owned(),
                capture_ms: 1,
                size: 10,
                first_archived: MonoMs(0),
                pinned: false,
            },
            MirrorSegment {
                key: "mid".to_owned(),
                capture_ms: 2,
                size: 10,
                first_archived: MonoMs(0),
                pinned: false,
            },
            MirrorSegment {
                key: "new".to_owned(),
                capture_ms: 3,
                size: 10,
                first_archived: MonoMs(0),
                pinned: false,
            },
        ];
        // quota 20, incoming 10 → total 40, must evict 2 oldest.
        let plan = plan_mirror_eviction(&archived, 10, 20, 1000, now);
        assert_eq!(plan.evict, vec!["old".to_owned(), "mid".to_owned()]);
        assert!(!plan.still_over_quota);
    }

    #[test]
    fn mirror_never_evicts_pinned_or_in_grace_even_if_over_quota() {
        let now = MonoMs(500); // grace 1000 not yet elapsed for first_archived=0
        let archived = vec![
            MirrorSegment {
                key: "pinned".to_owned(),
                capture_ms: 1,
                size: 100,
                first_archived: MonoMs(0),
                pinned: true,
            },
            MirrorSegment {
                key: "ingrace".to_owned(),
                capture_ms: 2,
                size: 100,
                first_archived: MonoMs(0),
                pinned: false,
            },
        ];
        let plan = plan_mirror_eviction(&archived, 0, 10, 1000, now);
        assert!(plan.evict.is_empty());
        assert!(plan.still_over_quota); // best-effort: stays over quota rather than evicting protected segments
    }

    #[test]
    fn grace_window_protects_recently_archived() {
        let archived = vec![MirrorSegment {
            key: "fresh".to_owned(),
            capture_ms: 1,
            size: 100,
            first_archived: MonoMs(0),
            pinned: false,
        }];
        // now=999 < grace 1000 → not evictable; now=1000 → evictable.
        assert!(
            plan_mirror_eviction(&archived, 0, 10, 1000, MonoMs(999))
                .evict
                .is_empty()
        );
        assert_eq!(
            plan_mirror_eviction(&archived, 0, 10, 1000, MonoMs(1000)).evict,
            vec!["fresh".to_owned()]
        );
    }

    #[test]
    fn archive_order_event_adjacent_then_oldest() {
        let cands = vec![
            RecentCandidate {
                key: "old_generic".to_owned(),
                capture_ms: 1,
                event_adjacent: false,
            },
            RecentCandidate {
                key: "new_event".to_owned(),
                capture_ms: 9,
                event_adjacent: true,
            },
            RecentCandidate {
                key: "old_event".to_owned(),
                capture_ms: 2,
                event_adjacent: true,
            },
            RecentCandidate {
                key: "new_generic".to_owned(),
                capture_ms: 8,
                event_adjacent: false,
            },
        ];
        // event-adjacent first (oldest among them first), then generics oldest first.
        assert_eq!(
            archive_order(&cands),
            vec![
                "old_event".to_owned(),
                "new_event".to_owned(),
                "old_generic".to_owned(),
                "new_generic".to_owned()
            ]
        );
    }
}
