//! Upload **prioritization** by user policy ([`uploadd.md`] §2.4): events/Sentry
//! first, then trips, then bulk.
//!
//! The *order itself* is policy, and changing its **semantics** is an ASK-FIRST
//! item ([`uploadd.md`] §6). So this module does **not** invent a new ordering;
//! it encodes today's prioritized behavior as the [`PriorityPolicy`] **default**
//! (a reorderable list of [`UploadCategory`] classes), and lets an operator
//! reorder the classes via config without any logic change. Within a class,
//! items break ties **FIFO** by enqueue sequence, so the order is total and
//! deterministic.
//!
//! [`uploadd.md`]: ../../../../docs/specs/uploadd.md

/// The user-policy class an archive item belongs to for upload ordering.
///
/// The mapping from a concrete `TeslaCam` folder to a class is the caller's job
/// (it lives with the archive/queue wiring); this enum is only the orderable
/// classification the policy ranks.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum UploadCategory {
    /// Event-triggered saves and Sentry incidents — the footage a user most
    /// wants off-device first.
    EventSentry,
    /// Trip / drive footage.
    Trip,
    /// Everything else (background bulk archive).
    Bulk,
}

/// Number of distinct [`UploadCategory`] classes (the width of a policy order).
const CATEGORY_COUNT: usize = 3;

/// The ordering policy: a ranking of [`UploadCategory`] classes, highest
/// priority first.
///
/// The default is the spec's order (`EventSentry` → `Trip` → `Bulk`). An
/// operator may reorder the classes; the **set** of classes is fixed (adding a
/// new class is an ASK-FIRST spec change), so the policy is represented as a
/// permutation of all classes rather than an open list.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PriorityPolicy {
    order: [UploadCategory; CATEGORY_COUNT],
}

impl Default for PriorityPolicy {
    fn default() -> Self {
        // uploadd.md §2.4: events/Sentry first, then trips, then bulk.
        Self {
            order: [
                UploadCategory::EventSentry,
                UploadCategory::Trip,
                UploadCategory::Bulk,
            ],
        }
    }
}

impl PriorityPolicy {
    /// Build a policy from an explicit class order, highest priority first.
    ///
    /// # Errors
    /// Returns an error string if `order` is not a permutation of all
    /// [`UploadCategory`] classes (a duplicate or a missing class would make the
    /// ranking ambiguous or partial).
    pub fn from_order(order: [UploadCategory; CATEGORY_COUNT]) -> Result<Self, &'static str> {
        // A duplicate-free array of fixed width CATEGORY_COUNT over an enum with
        // exactly CATEGORY_COUNT variants is necessarily a full permutation, so a
        // pairwise duplicate check is sufficient (and avoids index bookkeeping).
        for (i, cat) in order.iter().enumerate() {
            if order.iter().skip(i + 1).any(|other| other == cat) {
                return Err("priority order has a duplicate category");
            }
        }
        Ok(Self { order })
    }

    /// The rank of `category`: `0` is highest priority. A lower rank uploads
    /// before a higher one.
    #[must_use]
    pub fn rank(&self, category: UploadCategory) -> u8 {
        // Find the class in the order; it is always present (the policy is a
        // total permutation by construction). Fallback to the lowest priority
        // is unreachable but keeps the function total without a panic.
        self.order
            .iter()
            .position(|&c| c == category)
            .and_then(|p| u8::try_from(p).ok())
            .unwrap_or(u8::MAX)
    }
}

/// A total, deterministic sort key for one queued item: its policy class rank
/// (lower first) then its enqueue sequence (lower / older first), so ties within
/// a class are uploaded FIFO.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct PriorityKey {
    /// Class rank from [`PriorityPolicy::rank`]; `0` is highest priority.
    pub class_rank: u8,
    /// Enqueue sequence; older items (smaller value) go first within a class.
    pub seq: u64,
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{PriorityKey, PriorityPolicy, UploadCategory};

    #[test]
    fn default_policy_ranks_events_then_trips_then_bulk() {
        let p = PriorityPolicy::default();
        assert!(p.rank(UploadCategory::EventSentry) < p.rank(UploadCategory::Trip));
        assert!(p.rank(UploadCategory::Trip) < p.rank(UploadCategory::Bulk));
    }

    #[test]
    fn priority_key_orders_by_class_then_fifo() {
        let p = PriorityPolicy::default();
        let event_new = PriorityKey {
            class_rank: p.rank(UploadCategory::EventSentry),
            seq: 100,
        };
        let trip_old = PriorityKey {
            class_rank: p.rank(UploadCategory::Trip),
            seq: 1,
        };
        // A newer event still beats an older trip: class dominates.
        assert!(event_new < trip_old);

        let event_old = PriorityKey {
            class_rank: p.rank(UploadCategory::EventSentry),
            seq: 1,
        };
        // Within the same class, the older (smaller seq) wins (FIFO).
        assert!(event_old < event_new);
    }

    #[test]
    fn from_order_rejects_duplicates() {
        let bad = PriorityPolicy::from_order([
            UploadCategory::EventSentry,
            UploadCategory::EventSentry,
            UploadCategory::Bulk,
        ]);
        assert!(bad.is_err());
    }

    #[test]
    fn custom_order_changes_ranking_without_new_semantics() {
        let p = PriorityPolicy::from_order([
            UploadCategory::Trip,
            UploadCategory::EventSentry,
            UploadCategory::Bulk,
        ])
        .expect("valid permutation");
        assert!(p.rank(UploadCategory::Trip) < p.rank(UploadCategory::EventSentry));
    }
}
