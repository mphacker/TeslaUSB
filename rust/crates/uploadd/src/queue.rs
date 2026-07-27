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
//!   hydrates from the store at boot. It enforces **idempotent enqueue** (a
//!   child-object queue key is never duplicated) and computes the
//!   **priority-ordered** next item
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
//! - **Enqueue** dedupes on [`QueueKey`]: re-enqueuing an item already in the
//!   queue is a no-op, so a scanner that re-offers the same child object cannot
//!   create a second upload.
//! - **Resume** continues an `InProgress` item from its persisted
//!   [`QueueItem::bytes_uploaded`] checkpoint; it never appends a duplicate row.
//! - **Completion** is terminal: a `Done` item is never selected again, so a
//!   verified upload is never re-sent. (The durability mark is itself idempotent,
//!   so even a crash *between* the upload and the mark re-marks harmlessly.)
//!
//! [`uploadd.md`]: ../../../../docs/specs/uploadd.md

use std::fmt::Write as _;

use sha2::{Digest, Sha256};

use crate::error::IndexError;
use crate::priority::{PriorityKey, PriorityPolicy, UploadCategory};
use crate::source::ArchiveItemId;
use crate::transfer::VerifySpec;

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

/// Stable child-object queue identity (`cloud_upload_queue` PK).
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct QueueKey {
    /// Which configured destination this row belongs to.
    pub destination_id: String,
    /// Canonical object key on that destination.
    pub remote_key: String,
}

impl QueueKey {
    /// Construct a key from destination + remote object key.
    #[must_use]
    pub fn new(destination_id: impl Into<String>, remote_key: impl Into<String>) -> Self {
        Self {
            destination_id: destination_id.into(),
            remote_key: remote_key.into(),
        }
    }
}

/// One item in the durable upload queue.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QueueItem {
    /// Child-object queue identity (`destination_id`, `remote_key`).
    pub key: QueueKey,
    /// Parent archive-item id (lease subject).
    pub archive_item_id: ArchiveItemId,
    /// Child discriminator under the parent event.
    pub child_key: String,
    /// Item-relative source path under the archive root (resolved to a guarded
    /// [`crate::source::ArchivePath`] at transfer time — never an absolute/LUN
    /// path).
    pub source_rel: String,
    /// User-policy class, for prioritization.
    pub category: UploadCategory,
    /// Enqueue sequence (monotonic), the FIFO tie-break within a priority class.
    pub seq: u64,
    /// Total source size in bytes.
    pub total_bytes: u64,
    /// Per-item verification contract.
    pub verify: VerifySpec,
    /// Current lifecycle state.
    pub state: UploadState,
    /// Durable resume checkpoint: bytes confirmed uploaded so far.
    pub bytes_uploaded: u64,
    /// Number of transfer attempts made so far.
    pub attempts: u32,
    /// Backoff gate (`unix secs`), `None` = immediately eligible.
    pub not_before: Option<i64>,
    /// Last failure reason, for `/api/cloud` diagnostics.
    pub last_error: Option<String>,
}

/// Evidence captured from one backend-verified upload attempt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommitEvidence {
    /// Deterministic idempotency key derived from queue identity + attempt index.
    pub attempt_id: String,
    /// Backend-observed hash value for the uploaded object.
    pub hash: String,
    /// Backend hash algorithm (`sha256`, `md5`, ...).
    pub hash_alg: String,
    /// Bytes committed for this upload.
    pub size: u64,
    /// Optional sealed upload-set fence.
    pub upload_set_id: Option<String>,
}

/// Deterministic per-attempt idempotency key from queue identity + attempt index.
///
/// `indexd` stores `attempt_id` as the global primary key of
/// `cloud_upload_attempts` and rejects reuse against a different queue row, so
/// this must be unique across `(destination_id, remote_key, attempts)`. It also
/// caps the column at 128 bytes while `remote_key` alone may be 1024, so the
/// identity is folded into a fixed-width digest rather than interpolated: a
/// longer key would otherwise produce an over-long id that `indexd` rejects
/// identically on every retry, wedging the item in a permanent retry loop.
///
/// The digest is length-prefixed so `(dest, key)` splits cannot alias, and is
/// derived only from stable identity, so it survives a restart unchanged and
/// keeps commit replay idempotent.
#[must_use]
pub fn attempt_id_for(key: &QueueKey, attempts: u32) -> String {
    let mut hasher = Sha256::new();
    hasher.update((key.destination_id.len() as u64).to_le_bytes());
    hasher.update(key.destination_id.as_bytes());
    hasher.update(key.remote_key.as_bytes());
    let digest: [u8; 32] = hasher.finalize().into();
    let mut out = format!("{attempts}:");
    for byte in digest {
        write!(out, "{byte:02x}").expect("writing to a String cannot fail");
    }
    out
}

impl QueueItem {
    /// Create a freshly-`Queued` item.
    #[must_use]
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        key: QueueKey,
        archive_item_id: ArchiveItemId,
        child_key: impl Into<String>,
        source_rel: impl Into<String>,
        category: UploadCategory,
        seq: u64,
        total_bytes: u64,
        verify: VerifySpec,
    ) -> Self {
        Self {
            key,
            archive_item_id,
            child_key: child_key.into(),
            source_rel: source_rel.into(),
            category,
            seq,
            total_bytes,
            verify,
            state: UploadState::Queued,
            bytes_uploaded: 0,
            attempts: 0,
            not_before: None,
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

    /// Whether this item is retryable by attempt policy alone.
    #[must_use]
    pub fn is_retryable(&self, max_attempts: u32) -> bool {
        match self.state {
            UploadState::Queued | UploadState::InProgress => true,
            UploadState::Failed => self.attempts < max_attempts,
            UploadState::Done => false,
        }
    }

    /// Whether this item is eligible to be worked now, given `max_attempts` and
    /// `now_unix_s`.
    /// `Queued` and `InProgress` (resume) are always ready; `Failed` is ready
    /// only while it has retries left; `Done` is never ready.
    #[must_use]
    pub fn is_ready(&self, max_attempts: u32, now_unix_s: i64) -> bool {
        self.not_before.is_none_or(|at| at <= now_unix_s) && self.is_retryable(max_attempts)
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
    /// Build a queue from a hydrated set of items (deduping on queue key, keeping the
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
    /// item with the same queue key was already present (a no-op — no duplicate
    /// upload).
    pub fn enqueue(&mut self, item: QueueItem) -> bool {
        if self.items.iter().any(|existing| existing.key == item.key) {
            return false;
        }
        self.items.push(item);
        true
    }

    /// The key of the highest-priority ready item, or `None` if nothing is ready.
    /// Order is total: priority class first, then FIFO by enqueue sequence.
    #[must_use]
    pub fn select_next(
        &self,
        policy: &PriorityPolicy,
        max_attempts: u32,
        now_unix_s: i64,
    ) -> Option<QueueKey> {
        self.items
            .iter()
            .filter(|item| item.is_ready(max_attempts, now_unix_s))
            .min_by_key(|item| item.priority_key(policy))
            .map(|item| item.key.clone())
    }

    /// Borrow an item by queue key.
    #[must_use]
    pub fn get(&self, key: &QueueKey) -> Option<&QueueItem> {
        self.items.iter().find(|item| &item.key == key)
    }

    /// Mutably borrow an item by queue key.
    pub fn get_mut(&mut self, key: &QueueKey) -> Option<&mut QueueItem> {
        self.items.iter_mut().find(|item| &item.key == key)
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
    /// Keyed on [`QueueItem::key`], so re-applying is idempotent.
    ///
    /// # Errors
    /// Propagates an [`IndexError`] if the persist RPC/transaction fails.
    fn persist(&self, item: &QueueItem) -> Result<(), IndexError>;

    /// Durably commit one backend-verified upload attempt.
    ///
    /// Idempotency is keyed by [`CommitEvidence::attempt_id`]: replaying the same
    /// logical attempt with the same evidence must be safe. This is the **only**
    /// path that durably records a row as done. Parent durability remains
    /// independent and is conferred only by `cloud_finalize_parent_upload`.
    ///
    /// # Errors
    /// Propagates an [`IndexError`] if the commit RPC/transaction fails.
    fn commit(&self, item: &QueueItem, evidence: &CommitEvidence) -> Result<(), IndexError> {
        let mut done = item.clone();
        done.complete();
        let _ = evidence;
        self.persist(&done)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{CommitEvidence, QueueItem, QueueKey, QueueStore, UploadQueue, UploadState};
    use crate::error::IndexError;
    use crate::priority::{PriorityPolicy, UploadCategory};
    use crate::source::ArchiveItemId;
    use crate::transfer::{VerifyAlg, VerifySpec};

    fn item(id: i64, cat: UploadCategory, seq: u64) -> QueueItem {
        QueueItem::new(
            QueueKey::new("dest-a", format!("remote/{id}.mp4")),
            ArchiveItemId(id),
            format!("clip/{id}.mp4"),
            format!("clips/{id}.mp4"),
            cat,
            seq,
            1000,
            VerifySpec::Native {
                alg: VerifyAlg::Sha256,
                expected: format!("{id:064x}"),
            },
        )
    }

    #[test]
    fn enqueue_is_idempotent_on_queue_key() {
        let mut q = UploadQueue::default();
        assert!(q.enqueue(item(1, UploadCategory::Bulk, 0)));
        let mut dup = item(2, UploadCategory::EventSentry, 5);
        dup.key = QueueKey::new("dest-a", "remote/1.mp4");
        // Same key, even with different parent id/details, must not duplicate.
        assert!(!q.enqueue(dup));
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
        assert_eq!(
            q.select_next(&policy, 5, 100),
            Some(QueueKey::new("dest-a", "remote/3.mp4"))
        );
    }

    #[test]
    fn done_items_are_never_reselected() {
        let policy = PriorityPolicy::default();
        let mut q = UploadQueue::default();
        q.enqueue(item(1, UploadCategory::EventSentry, 0));
        let key = QueueKey::new("dest-a", "remote/1.mp4");
        q.get_mut(&key).unwrap().complete();
        assert_eq!(q.select_next(&policy, 5, 100), None);
    }

    #[test]
    fn failed_item_is_retryable_until_attempts_exhausted() {
        let policy = PriorityPolicy::default();
        let mut q = UploadQueue::default();
        q.enqueue(item(1, UploadCategory::Trip, 0));
        let key = QueueKey::new("dest-a", "remote/1.mp4");
        let it = q.get_mut(&key).unwrap();
        it.fail("net blip", false);
        assert_eq!(it.attempts, 1);
        assert_eq!(q.select_next(&policy, 5, 100), Some(key.clone()));
        // Exhaust retries.
        for _ in 0..4 {
            q.get_mut(&key).unwrap().fail("again", false);
        }
        assert_eq!(q.get(&key).unwrap().attempts, 5);
        assert_eq!(
            q.select_next(&policy, 5, 100),
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
        let key = QueueKey::new("dest-a", "remote/1.mp4");
        let resumed = q.get(&key).unwrap();
        assert_eq!(resumed.state, UploadState::InProgress);
        assert_eq!(resumed.bytes_uploaded, 400, "resumes from checkpoint");
        // And it is still selectable to be resumed.
        assert_eq!(q.select_next(&PriorityPolicy::default(), 5, 100), Some(key));
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

    #[test]
    fn select_next_skips_not_before_until_due() {
        let policy = PriorityPolicy::default();
        let mut q = UploadQueue::default();
        let mut blocked = item(1, UploadCategory::EventSentry, 0);
        blocked.not_before = Some(200);
        q.enqueue(blocked);
        q.enqueue(item(2, UploadCategory::Trip, 1));

        assert_eq!(
            q.select_next(&policy, 5, 100),
            Some(QueueKey::new("dest-a", "remote/2.mp4"))
        );
        assert_eq!(
            q.select_next(&policy, 5, 200),
            Some(QueueKey::new("dest-a", "remote/1.mp4"))
        );
    }

    #[derive(Default)]
    struct FakeStore {
        items: Vec<QueueItem>,
    }

    impl QueueStore for std::cell::RefCell<FakeStore> {
        fn load(&self) -> Result<Vec<QueueItem>, IndexError> {
            Ok(self.borrow().items.clone())
        }

        fn persist(&self, item: &QueueItem) -> Result<(), IndexError> {
            let mut inner = self.borrow_mut();
            if let Some(slot) = inner.items.iter_mut().find(|existing| existing.key == item.key) {
                *slot = item.clone();
            } else {
                inner.items.push(item.clone());
            }
            Ok(())
        }

        fn commit(&self, item: &QueueItem, evidence: &CommitEvidence) -> Result<(), IndexError> {
            let _ = evidence;
            let mut committed = item.clone();
            committed.complete();
            self.persist(&committed)
        }
    }

    #[test]
    fn store_round_trips_and_upserts_by_queue_key() {
        let store = std::cell::RefCell::new(FakeStore::default());
        let mut first = item(1, UploadCategory::Trip, 0);
        first.not_before = Some(1234);
        store.persist(&first).unwrap();

        let mut update = first.clone();
        update.archive_item_id = ArchiveItemId(9);
        update.bytes_uploaded = 777;
        update.last_error = Some("retry".to_owned());
        store.persist(&update).unwrap();

        let loaded = store.load().unwrap();
        assert_eq!(loaded, vec![update]);
    }

    #[test]
    fn attempt_id_is_stable_for_same_key_and_attempt_index() {
        let key = QueueKey::new("dest-a", "remote/clip.mp4");
        let one = super::attempt_id_for(&key, 3);
        let two = super::attempt_id_for(&key, 3);
        let changed = super::attempt_id_for(&key, 4);
        assert_eq!(one, two);
        assert_ne!(one, changed);
    }

    /// `indexd` caps `attempt_id` at 128 bytes but accepts a `remote_key` up to
    /// 1024 and a `destination_id` up to 128. An id derived by interpolating
    /// those would overflow the cap and be rejected identically on every retry,
    /// wedging the item forever, so the width must not track input length.
    #[test]
    fn attempt_id_stays_within_the_indexd_length_cap_at_maximum_input_sizes() {
        const INDEXD_ATTEMPT_ID_MAX: usize = 128;
        let key = QueueKey::new("d".repeat(128), "k".repeat(1024));
        let id = super::attempt_id_for(&key, u32::MAX);
        assert!(
            id.len() <= INDEXD_ATTEMPT_ID_MAX,
            "attempt_id {} bytes exceeds the indexd cap of {INDEXD_ATTEMPT_ID_MAX}",
            id.len()
        );
        assert!(!id.is_empty(), "attempt_id must be non-empty");
    }

    /// The id is the global primary key of `cloud_upload_attempts`, so distinct
    /// queue rows must not collide — including splits that would alias under a
    /// naive concatenation of the two identity fields.
    #[test]
    fn attempt_id_distinguishes_queue_rows_including_ambiguous_field_splits() {
        let same_attempt = 1;
        let a = super::attempt_id_for(&QueueKey::new("dest-a", "remote/clip.mp4"), same_attempt);
        let b = super::attempt_id_for(&QueueKey::new("dest-b", "remote/clip.mp4"), same_attempt);
        let c = super::attempt_id_for(&QueueKey::new("dest-a", "remote/other.mp4"), same_attempt);
        assert_ne!(a, b, "differing destination_id must not collide");
        assert_ne!(a, c, "differing remote_key must not collide");

        let split_left = super::attempt_id_for(&QueueKey::new("ab", "c"), same_attempt);
        let split_right = super::attempt_id_for(&QueueKey::new("a", "bc"), same_attempt);
        assert_ne!(
            split_left, split_right,
            "field boundary must not be ambiguous"
        );
    }
}
