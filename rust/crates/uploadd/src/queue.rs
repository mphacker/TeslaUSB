//! The **durable, resumable upload queue** — the state machine
//! `Queued → InProgress → Done | Failed`, idempotent across restart/power loss
//! ([`uploadd.md`] §2.1, §4).
//!
//! Two pieces:
//!
//! * [`QueueItem`] + [`UploadState`] — the per-item record and the pure
//!   transitions over it. A transition mutates *only* in-memory state; the engine
//!   persists each one through the [`QueueStore`] seam so it survives a crash.
//! * [`UploadQueue`] — a pure in-memory index over the items the live daemon
//!   hydrates from the store at boot. It enforces **idempotent enqueue** (an
//!   item id is never duplicated) and computes the **priority-ordered** next item
//!   to work, treating a leftover `InProgress` item (a crash mid-transfer) as
//!   *resumable* rather than restarting it from scratch.
//!
//! Persistence lives in `indexd` (the sole `SQLite` writer), so the queue is
//! modeled behind the [`QueueStore`] trait exactly as `retentiond` models its
//! `IndexClient`: the host core is the state machine + resume logic; the DB I/O
//! is a gated executor.
//!
//! # Why this is idempotent / non-duplicating
//!
//! - **Enqueue** dedupes on [`ArchiveItemId`]: re-enqueuing an item already in
//!   the queue is a no-op, so a scanner that re-offers the same item cannot
//!   create a second upload.
//! - **Resume** continues an `InProgress` item from its persisted
//!   [`QueueItem::bytes_uploaded`] checkpoint; it never appends a duplicate row.
//! - **Completion** is terminal: a `Done` item is never selected again, so a
//!   verified upload is never re-sent. (The durability mark is itself idempotent,
//!   so even a crash *between* the upload and the mark re-marks harmlessly.)
//!
//! [`uploadd.md`]: ../../../../docs/specs/uploadd.md

use crate::error::IndexError;
use crate::priority::{PriorityKey, PriorityPolicy, UploadCategory};
use crate::source::{ArchiveItemId, ContentHash};

/// The lifecycle state of one queued upload ([`uploadd.md`] §2.1).
///
/// [`uploadd.md`]: ../../../../docs/specs/uploadd.md
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UploadState {
    /// Enqueued, not yet started.
    Queued,
    /// A transfer is in flight (or was, before a crash — then it is *resumable*).
    InProgress,
    /// Uploaded and remotely verified; durability has been flagged. Terminal.
    Done,
    /// The last attempt failed. Retryable while `attempts < max_attempts`,
    /// otherwise parked for operator inspection (never deleted).
    Failed,
}

/// One item in the durable upload queue.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QueueItem {
    /// Stable archive-item identity (the dedupe key for idempotent enqueue).
    pub id: ArchiveItemId,
    /// Item-relative source path under the archive root (resolved to a guarded
    /// [`crate::source::ArchivePath`] at transfer time — never an absolute/LUN
    /// path).
    pub source_rel: String,
    /// Destination object key on the remote.
    pub remote_key: String,
    /// User-policy class, for prioritization.
    pub category: UploadCategory,
    /// Enqueue sequence (monotonic), the FIFO tie-break within a priority class.
    pub seq: u64,
    /// Total source size in bytes.
    pub total_bytes: u64,
    /// Expected whole-file content hash, checked against the remote digest for
    /// integrity.
    pub expected_hash: ContentHash,
    /// Current lifecycle state.
    pub state: UploadState,
    /// Durable resume checkpoint: bytes confirmed uploaded so far.
    pub bytes_uploaded: u64,
    /// Number of transfer attempts made so far.
    pub attempts: u32,
    /// Last failure reason, for `/api/cloud` diagnostics.
    pub last_error: Option<String>,
}

impl QueueItem {
    /// Create a freshly-`Queued` item.
    #[must_use]
    pub fn new(
        id: ArchiveItemId,
        source_rel: impl Into<String>,
        remote_key: impl Into<String>,
        category: UploadCategory,
        seq: u64,
        total_bytes: u64,
        expected_hash: ContentHash,
    ) -> Self {
        Self {
            id,
            source_rel: source_rel.into(),
            remote_key: remote_key.into(),
            category,
            seq,
            total_bytes,
            expected_hash,
            state: UploadState::Queued,
            bytes_uploaded: 0,
            attempts: 0,
            last_error: None,
        }
    }

    /// The priority sort key for this item under `policy`.
    #[must_use]
    pub fn priority_key(&self, policy: &PriorityPolicy) -> PriorityKey {
        PriorityKey {
            class_rank: policy.rank(self.category),
            seq: self.seq,
        }
    }

    /// Whether this item is eligible to be worked now, given `max_attempts`.
    /// `Queued` and `InProgress` (resume) are always ready; `Failed` is ready
    /// only while it has retries left; `Done` is never ready.
    #[must_use]
    pub fn is_ready(&self, max_attempts: u32) -> bool {
        match self.state {
            UploadState::Queued | UploadState::InProgress => true,
            UploadState::Failed => self.attempts < max_attempts,
            UploadState::Done => false,
        }
    }

    /// Transition into `InProgress` to begin (or resume) a transfer. Preserves
    /// [`Self::bytes_uploaded`] so a resume continues from the checkpoint.
    pub fn begin(&mut self) {
        self.state = UploadState::InProgress;
    }

    /// Record durable progress (the resume checkpoint), clamped to
    /// [`Self::total_bytes`].
    pub fn checkpoint(&mut self, bytes_uploaded: u64) {
        self.bytes_uploaded = bytes_uploaded.min(self.total_bytes);
    }

    /// Mark the upload `Done` (verified). Terminal.
    pub fn complete(&mut self) {
        self.bytes_uploaded = self.total_bytes;
        self.state = UploadState::Done;
        self.last_error = None;
    }

    /// Mark the current attempt failed. Increments [`Self::attempts`] and records
    /// `reason`. If `reset_offset` is set (an *integrity* failure — the remote
    /// bytes are corrupt), the resume checkpoint is reset to `0` so the next
    /// attempt re-uploads the whole file; a plain mid-transfer I/O failure keeps
    /// the checkpoint so the next attempt resumes.
    pub fn fail(&mut self, reason: impl Into<String>, reset_offset: bool) {
        self.attempts = self.attempts.saturating_add(1);
        self.last_error = Some(reason.into());
        if reset_offset {
            self.bytes_uploaded = 0;
        }
        self.state = UploadState::Failed;
    }
}

/// Pure in-memory index over the queue, hydrated from a [`QueueStore`] at boot.
#[derive(Debug, Default, Clone)]
pub struct UploadQueue {
    items: Vec<QueueItem>,
}

impl UploadQueue {
    /// Build a queue from a hydrated set of items (deduping on id, keeping the
    /// first occurrence — the store should never contain duplicates, but this
    /// keeps the invariant total).
    #[must_use]
    pub fn from_items(items: Vec<QueueItem>) -> Self {
        let mut q = Self::default();
        for item in items {
            q.enqueue(item);
        }
        q
    }

    /// Idempotently enqueue `item`. Returns `true` if it was added, `false` if an
    /// item with the same id was already present (a no-op — no duplicate upload).
    pub fn enqueue(&mut self, item: QueueItem) -> bool {
        if self.items.iter().any(|existing| existing.id == item.id) {
            return false;
        }
        self.items.push(item);
        true
    }

    /// The id of the highest-priority ready item, or `None` if nothing is ready.
    /// Order is total: priority class first, then FIFO by enqueue sequence.
    #[must_use]
    pub fn select_next(&self, policy: &PriorityPolicy, max_attempts: u32) -> Option<ArchiveItemId> {
        self.items
            .iter()
            .filter(|item| item.is_ready(max_attempts))
            .min_by_key(|item| item.priority_key(policy))
            .map(|item| item.id)
    }

    /// Borrow an item by id.
    #[must_use]
    pub fn get(&self, id: ArchiveItemId) -> Option<&QueueItem> {
        self.items.iter().find(|item| item.id == id)
    }

    /// Mutably borrow an item by id.
    pub fn get_mut(&mut self, id: ArchiveItemId) -> Option<&mut QueueItem> {
        self.items.iter_mut().find(|item| item.id == id)
    }

    /// All items, in insertion order (for status snapshots).
    #[must_use]
    pub fn items(&self) -> &[QueueItem] {
        &self.items
    }
}

/// The durable persistence seam for the queue. The live impl funnels every
/// mutation through `indexd` (the sole `SQLite` writer); tests inject an
/// in-memory fake. Every method is **idempotent** so a transition can be safely
/// re-applied after a crash.
pub trait QueueStore {
    /// Hydrate the full queue at boot (including `InProgress` items to resume and
    /// `Failed` items to retry).
    ///
    /// # Errors
    /// Propagates an [`IndexError`] if the load RPC/transaction fails.
    fn load(&self) -> Result<Vec<QueueItem>, IndexError>;

    /// Durably upsert the current state of `item` (state, checkpoint, attempts).
    /// Keyed on [`QueueItem::id`], so re-applying is idempotent.
    ///
    /// # Errors
    /// Propagates an [`IndexError`] if the persist RPC/transaction fails.
    fn persist(&self, item: &QueueItem) -> Result<(), IndexError>;
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{QueueItem, UploadQueue, UploadState};
    use crate::priority::{PriorityPolicy, UploadCategory};
    use crate::source::{ArchiveItemId, ContentHash};

    fn item(id: i64, cat: UploadCategory, seq: u64) -> QueueItem {
        QueueItem::new(
            ArchiveItemId(id),
            format!("clips/{id}.mp4"),
            format!("remote/{id}.mp4"),
            cat,
            seq,
            1000,
            ContentHash::new([0u8; 32]),
        )
    }

    #[test]
    fn enqueue_is_idempotent_on_id() {
        let mut q = UploadQueue::default();
        assert!(q.enqueue(item(1, UploadCategory::Bulk, 0)));
        // Same id, even with different details, must not duplicate.
        assert!(!q.enqueue(item(1, UploadCategory::EventSentry, 5)));
        assert_eq!(q.items().len(), 1);
    }

    #[test]
    fn select_next_follows_priority_then_fifo() {
        let policy = PriorityPolicy::default();
        let mut q = UploadQueue::default();
        q.enqueue(item(1, UploadCategory::Bulk, 0));
        q.enqueue(item(2, UploadCategory::Trip, 1));
        q.enqueue(item(3, UploadCategory::EventSentry, 2)); // newest, but events first
        q.enqueue(item(4, UploadCategory::EventSentry, 3));
        // Event id=3 (older event) before id=4, both before trip, before bulk.
        assert_eq!(q.select_next(&policy, 5), Some(ArchiveItemId(3)));
    }

    #[test]
    fn done_items_are_never_reselected() {
        let policy = PriorityPolicy::default();
        let mut q = UploadQueue::default();
        q.enqueue(item(1, UploadCategory::EventSentry, 0));
        q.get_mut(ArchiveItemId(1)).unwrap().complete();
        assert_eq!(q.select_next(&policy, 5), None);
    }

    #[test]
    fn failed_item_is_retryable_until_attempts_exhausted() {
        let policy = PriorityPolicy::default();
        let mut q = UploadQueue::default();
        q.enqueue(item(1, UploadCategory::Trip, 0));
        let it = q.get_mut(ArchiveItemId(1)).unwrap();
        it.fail("net blip", false);
        assert_eq!(it.attempts, 1);
        assert_eq!(q.select_next(&policy, 5), Some(ArchiveItemId(1)));
        // Exhaust retries.
        for _ in 0..4 {
            q.get_mut(ArchiveItemId(1)).unwrap().fail("again", false);
        }
        assert_eq!(q.get(ArchiveItemId(1)).unwrap().attempts, 5);
        assert_eq!(
            q.select_next(&policy, 5),
            None,
            "terminal Failed not selected"
        );
    }

    #[test]
    fn resume_after_restart_continues_from_checkpoint_without_duplicating() {
        // Simulate a crash mid-transfer: an InProgress item with partial bytes,
        // persisted. A restart re-hydrates from the same snapshot.
        let mut original = item(1, UploadCategory::EventSentry, 0);
        original.begin();
        original.checkpoint(400);
        let persisted = vec![original.clone()];

        // "Restart": rebuild the queue from the persisted snapshot.
        let q = UploadQueue::from_items(persisted);
        assert_eq!(q.items().len(), 1, "no duplicate row on resume");
        let resumed = q.get(ArchiveItemId(1)).unwrap();
        assert_eq!(resumed.state, UploadState::InProgress);
        assert_eq!(resumed.bytes_uploaded, 400, "resumes from checkpoint");
        // And it is still selectable to be resumed.
        assert_eq!(
            q.select_next(&PriorityPolicy::default(), 5),
            Some(ArchiveItemId(1))
        );
    }

    #[test]
    fn integrity_failure_resets_offset_plain_failure_keeps_it() {
        let mut it = item(1, UploadCategory::Bulk, 0);
        it.begin();
        it.checkpoint(600);
        it.fail("mid-transfer io", false);
        assert_eq!(it.bytes_uploaded, 600, "plain failure keeps checkpoint");
        it.begin();
        it.checkpoint(900);
        it.fail("integrity mismatch", true);
        assert_eq!(it.bytes_uploaded, 0, "integrity failure resets checkpoint");
    }
}
