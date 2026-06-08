//! Slice 6.1e (part 2) — the **single-deleter, crash-safe delete protocol** and
//! the **startup recovery matrix**, built to contract **D3**
//! (`single-writer-lease.md` §4) and `storage.md` §5.1.
//!
//! `retentiond` is the **sole** unlinker of Pi-side archive files. It never
//! writes the index itself: every `delete_state` transition
//! (`LIVE → DELETE_CLAIMED → DELETING → DELETED`) is an idempotent RPC to
//! `indexd` (the sole `SQLite` writer). The two side-effect seams are
//! [`IndexClient`] (those RPCs, plus the atomic claim that doubles as the
//! lease-honoring gate of contract §3) and [`ArchiveDeleteOps`] (rename / fsync /
//! recursive unlink on the archive filesystem).
//!
//! # Why this ordering is safe
//!
//! The protocol ([`run_delete`]) is **rename-then-unlink** so it is idempotent
//! across power loss:
//!
//! 1. `claim` (atomic in `indexd`: abort if any **unexpired lease**, else
//!    `DELETE_CLAIMED`). This is the delete-vs-lease race gate — a denied claim
//!    performs **no filesystem mutation at all**.
//! 2. `rename` the source into `.retention-trash/<id>.<gen>.deleting`, where
//!    `<gen>` is a **random 128-bit token, never wall-clock** (the Pi has no RTC;
//!    a clock reset must not collide trash names).
//! 3. `fsync` the **source** parent dir — makes the rename durable.
//! 4. only **now** mark `DELETING` (hazard: never advance the DB past a rename
//!    that is not yet on disk).
//! 5. recursively unlink the trash entry.
//! 6. `fsync` the **trash** parent dir.
//! 7. only **now** mark `DELETED` (hazard: never claim bytes freed before the
//!    unlink is durable).
//!
//! Any interruption leaves a state the [`recovery_action`] matrix can reconcile
//! at next startup, scoped to `.retention-trash` and non-`LIVE`/anomalous rows.

use std::io;

use crate::io::ArchiveItemId;

/// Filesystem seam for the delete protocol. All paths are on the **single**
/// archive filesystem so the rename in step 2 is atomic.
pub trait ArchiveDeleteOps {
    /// Whether `path` currently exists (used by the recovery sweep).
    fn exists(&self, path: &str) -> bool;

    /// Atomically rename `src` to `dst` (same filesystem).
    ///
    /// # Errors
    /// Propagates the underlying `rename` failure.
    fn rename_into_trash(&self, src: &str, dst: &str) -> io::Result<()>;

    /// `fsync` the **parent directory** of `path`, making a prior create/rename
    /// in that directory durable.
    ///
    /// # Errors
    /// Propagates the underlying `fsync`/`open` failure.
    fn fsync_parent(&self, path: &str) -> io::Result<()>;

    /// Recursively unlink `path` (a trash entry).
    ///
    /// # Errors
    /// Propagates the underlying unlink failure.
    fn recursive_delete(&self, path: &str) -> io::Result<()>;
}

/// `indexd` RPC seam. Every method is **idempotent** so it is safe to re-apply
/// if power is lost again mid-recovery (contract §4.1). None carry a token: the
/// single-deleter invariant (exactly one `retentiond`) plus the `delete_state`
/// column itself are the gate, so no per-claim token is needed here. (The
/// 128-bit lease `gen` is a *separate* concept and lives in [`crate::lease`].)
pub trait IndexClient {
    /// Atomically claim an item for deletion (contract §3 gate): abort if the
    /// item has any unexpired lease or is not `LIVE`, else advance it to
    /// `DELETE_CLAIMED` in one transaction.
    fn claim_archive_delete(&self, id: ArchiveItemId) -> ClaimResult;

    /// Advance `DELETE_CLAIMED → DELETING` (after a durable rename).
    ///
    /// # Errors
    /// Propagates an IPC/transaction failure.
    fn mark_deleting(&self, id: ArchiveItemId) -> io::Result<()>;

    /// Advance `DELETING → DELETED` and record `bytes_freed` (after a durable
    /// unlink).
    ///
    /// # Errors
    /// Propagates an IPC/transaction failure.
    fn mark_deleted(&self, id: ArchiveItemId, bytes_freed: u64) -> io::Result<()>;

    /// Release a claim back to `LIVE` (recovery: original still present, or a
    /// rename failed before anything moved).
    ///
    /// # Errors
    /// Propagates an IPC/transaction failure.
    fn release_delete_claim(&self, id: ArchiveItemId) -> io::Result<()>;

    /// Mark an item `QUARANTINED` (an inconsistent FS↔DB state).
    ///
    /// # Errors
    /// Propagates an IPC/transaction failure.
    fn quarantine(&self, id: ArchiveItemId, reason: &str) -> io::Result<()>;
}

/// Result of an atomic delete claim (the contract §3 lease-honoring gate).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClaimResult {
    /// The item was `LIVE` and unleased; it is now `DELETE_CLAIMED`.
    Claimed,
    /// The item could not be claimed (an unexpired lease, or already
    /// `DELETE_CLAIMED`+). **No filesystem mutation must follow.**
    Denied {
        /// Human-readable reason for diagnostics.
        reason: String,
    },
    /// No such item row.
    NotFound,
}

/// Source of the random 128-bit trash generation token. The live impl reads the
/// OS CSPRNG; tests inject a deterministic sequence.
pub trait RandGen {
    /// A fresh random 128-bit value. Must **never** be derived from wall-clock.
    fn next_u128(&self) -> u128;
}

/// A request to delete one archived item.
#[derive(Debug, Clone)]
pub struct DeleteRequest {
    /// The item to delete (already chosen as a safe candidate by
    /// [`crate::value::list_eviction_candidates`]).
    pub id: ArchiveItemId,
    /// Absolute path of the item's source within the archive.
    pub source_path: String,
    /// Size in bytes (reported as `bytes_freed` on success).
    pub size_bytes: u64,
}

/// Outcome of [`run_delete`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeleteOutcome {
    /// The item was fully deleted and accounted.
    Deleted {
        /// Bytes reclaimed.
        bytes_freed: u64,
    },
    /// The claim was denied (e.g. a lease appeared) or the item vanished; **no**
    /// filesystem change was made.
    Skipped {
        /// Why the delete was skipped.
        reason: String,
    },
    /// An I/O/IPC error interrupted the protocol. The on-disk + DB state is left
    /// in a row the [`recovery_action`] matrix reconciles at next startup; this
    /// is **not** data loss of an unclaimed item.
    Failed {
        /// Where in the protocol it failed.
        reason: String,
    },
}

/// Build the trash path for an item: `<trash_dir>/<id>.<gen>.deleting`. The
/// `gen_token` is rendered as zero-padded lowercase hex of the random 128-bit
/// token (the contract calls this `<gen>`; Rust 2024 reserves `gen`).
#[must_use]
pub fn trash_path(trash_dir: &str, id: ArchiveItemId, gen_token: u128) -> String {
    format!("{trash_dir}/{}.{gen_token:032x}.deleting", id.0)
}

/// Execute the crash-safe single-deleter protocol for one item.
///
/// Returns [`DeleteOutcome::Skipped`] *without touching the filesystem* if the
/// claim is denied (the delete-vs-lease race gate). On any I/O error after the
/// claim, returns [`DeleteOutcome::Failed`] having left a recoverable state
/// (never advancing the DB past a not-yet-durable filesystem change).
pub fn run_delete(
    req: &DeleteRequest,
    trash_dir: &str,
    fs: &dyn ArchiveDeleteOps,
    index: &dyn IndexClient,
    rand: &dyn RandGen,
) -> DeleteOutcome {
    // Step 1: atomic claim (also the lease-honoring gate). On denial, STOP —
    // no rename, no unlink. This is what makes a delete-vs-lease race safe.
    match index.claim_archive_delete(req.id) {
        ClaimResult::Claimed => {}
        ClaimResult::Denied { reason } => return DeleteOutcome::Skipped { reason },
        ClaimResult::NotFound => {
            return DeleteOutcome::Skipped {
                reason: "item not found".to_string(),
            };
        }
    }

    // Step 2: rename into trash under a random gen token.
    let gen_token = rand.next_u128();
    let trash = trash_path(trash_dir, req.id, gen_token);
    if let Err(e) = fs.rename_into_trash(&req.source_path, &trash) {
        // Nothing moved. Bring the row back to LIVE so the governor can retry.
        let _ = index.release_delete_claim(req.id);
        return DeleteOutcome::Failed {
            reason: format!("rename: {e}"),
        };
    }

    // Step 3: fsync the SOURCE parent — make the rename durable BEFORE the DB
    // advances. If this fails, FS=trash present + DB=DELETE_CLAIMED ⇒ recovery
    // "continue delete"; leaving it untouched is correct.
    if let Err(e) = fs.fsync_parent(&req.source_path) {
        return DeleteOutcome::Failed {
            reason: format!("fsync source parent: {e}"),
        };
    }

    // Step 4: only now mark DELETING (rename is durable).
    if let Err(e) = index.mark_deleting(req.id) {
        return DeleteOutcome::Failed {
            reason: format!("mark_deleting: {e}"),
        };
    }

    // Step 5: recursively unlink the trash entry.
    if let Err(e) = fs.recursive_delete(&trash) {
        return DeleteOutcome::Failed {
            reason: format!("unlink: {e}"),
        };
    }

    // Step 6: fsync the TRASH parent — make the unlink durable BEFORE claiming
    // the bytes are freed.
    if let Err(e) = fs.fsync_parent(&trash) {
        return DeleteOutcome::Failed {
            reason: format!("fsync trash parent: {e}"),
        };
    }

    // Step 7: only now mark DELETED.
    if let Err(e) = index.mark_deleted(req.id, req.size_bytes) {
        return DeleteOutcome::Failed {
            reason: format!("mark_deleted: {e}"),
        };
    }

    DeleteOutcome::Deleted {
        bytes_freed: req.size_bytes,
    }
}

// ---------------------------------------------------------------------------
// Startup recovery matrix (contract §4.1 / storage.md §5.1)
// ---------------------------------------------------------------------------

use crate::lease::DeleteState;

/// What the recovery sweep finds on disk for a given row.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FsPresence {
    /// The original (pre-rename) path still exists.
    OriginalPresent,
    /// A `.retention-trash` entry exists.
    TrashPresent,
    /// Neither original nor trash exists.
    Neither,
}

/// The idempotent action the recovery sweep must drive through `indexd`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecoveryAction {
    /// `release_delete_claim` → `LIVE` (the governor will re-pick later).
    ReleaseToLive,
    /// Continue the delete: re-run unlink of the trash entry, then mark deleted.
    ContinueDelete,
    /// Finish the delete: the trash entry exists but `DELETING` was set —
    /// re-unlink and mark deleted.
    FinishDelete,
    /// Mark `DELETED` (the file is already gone).
    MarkDeleted,
    /// Mark `QUARANTINED` — an inconsistent FS↔DB combination to investigate.
    Quarantine,
    /// Nothing to do (a consistent, healthy state).
    NoOp,
}

/// Pure reconciliation of a row's `delete_state` against what is on disk
/// (contract §4.1 matrix). `LIVE`/`Deleted` with the expected on-disk state are
/// healthy no-ops; everything inconsistent fails **toward** quarantine for an
/// operator to investigate rather than guessing a destructive action.
///
/// The matrix is written out **one explicit row per (state, presence) pair** to
/// match the contract table verbatim; several rows legitimately share an action,
/// so `match_same_arms` is allowed here for readability/auditability.
#[must_use]
#[allow(clippy::match_same_arms)]
pub fn recovery_action(db: DeleteState, fs: FsPresence) -> RecoveryAction {
    use DeleteState as D;
    use FsPresence as F;
    match (db, fs) {
        // Claimed but never renamed → release back to LIVE.
        (D::DeleteClaimed, F::OriginalPresent) => RecoveryAction::ReleaseToLive,
        // Claimed and renamed → continue the delete.
        (D::DeleteClaimed, F::TrashPresent) => RecoveryAction::ContinueDelete,
        // Claimed, nothing on disk → row with no file → mark deleted.
        (D::DeleteClaimed, F::Neither) => RecoveryAction::MarkDeleted,

        // Deleting with trash present → finish the delete.
        (D::Deleting, F::TrashPresent) => RecoveryAction::FinishDelete,
        // Deleting with nothing present → mark deleted.
        (D::Deleting, F::Neither) => RecoveryAction::MarkDeleted,
        // Deleting but the ORIGINAL is back → impossible if invariants held →
        // quarantine.
        (D::Deleting, F::OriginalPresent) => RecoveryAction::Quarantine,

        // LIVE with trash present → a trash entry for a live row → quarantine.
        (D::Live, F::TrashPresent) => RecoveryAction::Quarantine,
        // LIVE present and well → nothing to do.
        (D::Live, F::OriginalPresent) => RecoveryAction::NoOp,
        // LIVE with no file → row with no file → mark deleted (matrix catch-all).
        (D::Live, F::Neither) => RecoveryAction::MarkDeleted,

        // DELETED but the original reappeared → quarantine.
        (D::Deleted, F::OriginalPresent) => RecoveryAction::Quarantine,
        // DELETED with trash still present → unlink never finished but DB said
        // done → quarantine (inconsistent).
        (D::Deleted, F::TrashPresent) => RecoveryAction::Quarantine,
        // DELETED and gone → consistent.
        (D::Deleted, F::Neither) => RecoveryAction::NoOp,

        // A prior failed attempt: reconcile by what is on disk.
        (D::DeleteFailed, F::OriginalPresent) => RecoveryAction::ReleaseToLive,
        (D::DeleteFailed, F::TrashPresent) => RecoveryAction::ContinueDelete,
        (D::DeleteFailed, F::Neither) => RecoveryAction::MarkDeleted,

        // Already quarantined → leave for the operator.
        (D::Quarantined, _) => RecoveryAction::NoOp,
    }
}

/// Drive one recovery action to completion through `indexd` (and the filesystem
/// for the continue/finish cases). Idempotent: safe to re-run after another
/// power loss.
///
/// # Errors
/// Propagates the first IPC/filesystem failure so the sweep can retry next boot.
pub fn run_recovery(
    id: ArchiveItemId,
    source_path: &str,
    trash_path_str: &str,
    action: RecoveryAction,
    size_bytes: u64,
    fs: &dyn ArchiveDeleteOps,
    index: &dyn IndexClient,
) -> io::Result<()> {
    match action {
        RecoveryAction::ReleaseToLive => index.release_delete_claim(id),
        RecoveryAction::ContinueDelete | RecoveryAction::FinishDelete => {
            // Idempotent: advance to DELETING (no-op if already), unlink the
            // trash entry if still there, fsync, then mark deleted.
            index.mark_deleting(id)?;
            if fs.exists(trash_path_str) {
                fs.recursive_delete(trash_path_str)?;
                fs.fsync_parent(trash_path_str)?;
            }
            index.mark_deleted(id, size_bytes)
        }
        RecoveryAction::MarkDeleted => index.mark_deleted(id, size_bytes),
        RecoveryAction::Quarantine => index.quarantine(id, "recovery: inconsistent FS/DB state"),
        RecoveryAction::NoOp => {
            let _ = (source_path, fs);
            Ok(())
        }
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
    use std::cell::RefCell;
    use std::io;

    use super::{
        ArchiveDeleteOps, ClaimResult, DeleteOutcome, DeleteRequest, FsPresence, IndexClient,
        RandGen, RecoveryAction, recovery_action, run_delete, trash_path,
    };
    use crate::io::ArchiveItemId;
    use crate::lease::DeleteState;

    #[derive(Default)]
    struct FakeRand {
        next: RefCell<u128>,
    }
    impl RandGen for FakeRand {
        fn next_u128(&self) -> u128 {
            let v = *self.next.borrow();
            *self.next.borrow_mut() = v.wrapping_add(1);
            v
        }
    }

    /// Records the ordered sequence of side effects, and can be told to fail at
    /// a named step to simulate a crash/IO error.
    #[derive(Default)]
    struct Recorder {
        ops: RefCell<Vec<String>>,
        fail_at: Option<&'static str>,
        existing: RefCell<Vec<String>>,
    }
    impl Recorder {
        fn with_fail(step: &'static str) -> Self {
            Self {
                fail_at: Some(step),
                ..Self::default()
            }
        }
        fn log(&self, s: &str) {
            self.ops.borrow_mut().push(s.to_string());
        }
        fn boom(&self, step: &'static str) -> io::Result<()> {
            if self.fail_at == Some(step) {
                Err(io::Error::other(format!("injected failure at {step}")))
            } else {
                Ok(())
            }
        }
    }
    impl ArchiveDeleteOps for Recorder {
        fn exists(&self, path: &str) -> bool {
            self.existing.borrow().iter().any(|p| p == path)
        }
        fn rename_into_trash(&self, src: &str, dst: &str) -> io::Result<()> {
            self.log(&format!("rename {src} -> {dst}"));
            self.boom("rename")
        }
        fn fsync_parent(&self, path: &str) -> io::Result<()> {
            self.log(&format!("fsync_parent {path}"));
            // Distinguish source vs trash fsync by the ".deleting" suffix.
            if path.ends_with(".deleting") {
                self.boom("fsync_trash")
            } else {
                self.boom("fsync_source")
            }
        }
        fn recursive_delete(&self, path: &str) -> io::Result<()> {
            self.log(&format!("unlink {path}"));
            self.boom("unlink")
        }
    }

    #[derive(Default)]
    struct FakeIndex {
        claim: Option<ClaimResult>,
        transitions: RefCell<Vec<String>>,
        fail_at: Option<&'static str>,
    }
    impl FakeIndex {
        fn claimed() -> Self {
            Self {
                claim: Some(ClaimResult::Claimed),
                ..Self::default()
            }
        }
        fn boom(&self, step: &'static str) -> io::Result<()> {
            if self.fail_at == Some(step) {
                Err(io::Error::other("injected index failure"))
            } else {
                Ok(())
            }
        }
    }
    impl IndexClient for FakeIndex {
        fn claim_archive_delete(&self, _id: ArchiveItemId) -> ClaimResult {
            self.claim.clone().unwrap_or(ClaimResult::NotFound)
        }
        fn mark_deleting(&self, _id: ArchiveItemId) -> io::Result<()> {
            self.transitions.borrow_mut().push("DELETING".to_string());
            self.boom("mark_deleting")
        }
        fn mark_deleted(&self, _id: ArchiveItemId, bytes: u64) -> io::Result<()> {
            self.transitions
                .borrow_mut()
                .push(format!("DELETED({bytes})"));
            self.boom("mark_deleted")
        }
        fn release_delete_claim(&self, _id: ArchiveItemId) -> io::Result<()> {
            self.transitions.borrow_mut().push("LIVE".to_string());
            Ok(())
        }
        fn quarantine(&self, _id: ArchiveItemId, _reason: &str) -> io::Result<()> {
            self.transitions
                .borrow_mut()
                .push("QUARANTINED".to_string());
            Ok(())
        }
    }

    fn req() -> DeleteRequest {
        DeleteRequest {
            id: ArchiveItemId(42),
            source_path: "/archive/SentryClips/event-1".to_string(),
            size_bytes: 1234,
        }
    }

    #[test]
    fn happy_path_executes_steps_in_safe_order() {
        let fs = Recorder::default();
        let index = FakeIndex::claimed();
        let rand = FakeRand::default();
        let out = run_delete(&req(), "/archive/.retention-trash", &fs, &index, &rand);
        assert_eq!(out, DeleteOutcome::Deleted { bytes_freed: 1234 });

        let ops = fs.ops.borrow().clone();
        // rename, fsync source, unlink, fsync trash — in this exact order.
        assert!(ops[0].starts_with("rename"));
        assert!(ops[1].starts_with("fsync_parent /archive/SentryClips"));
        assert!(ops[2].starts_with("unlink"));
        assert!(ops[3].ends_with(".deleting"));

        // DB advanced DELETING only AFTER the durable rename, DELETED last.
        let tr = index.transitions.borrow().clone();
        assert_eq!(
            tr,
            vec!["DELETING".to_string(), "DELETED(1234)".to_string()]
        );
    }

    #[test]
    fn denied_claim_skips_without_touching_filesystem() {
        let fs = Recorder::default();
        let index = FakeIndex {
            claim: Some(ClaimResult::Denied {
                reason: "leased".to_string(),
            }),
            ..FakeIndex::default()
        };
        let rand = FakeRand::default();
        let out = run_delete(&req(), "/t", &fs, &index, &rand);
        assert!(matches!(out, DeleteOutcome::Skipped { .. }));
        // CRITICAL: a lease race must cause ZERO filesystem mutation.
        assert!(fs.ops.borrow().is_empty());
        assert!(index.transitions.borrow().is_empty());
    }

    #[test]
    fn rename_failure_releases_claim_back_to_live() {
        let fs = Recorder::with_fail("rename");
        let index = FakeIndex::claimed();
        let rand = FakeRand::default();
        let out = run_delete(&req(), "/t", &fs, &index, &rand);
        assert!(matches!(out, DeleteOutcome::Failed { .. }));
        // Nothing moved durably; row returned to LIVE for a later retry.
        assert_eq!(index.transitions.borrow().clone(), vec!["LIVE".to_string()]);
    }

    #[test]
    fn crash_after_rename_before_mark_deleting_leaves_continue_delete_state() {
        // fsync source parent fails → trash present, DB still DELETE_CLAIMED.
        let fs = Recorder::with_fail("fsync_source");
        let index = FakeIndex::claimed();
        let rand = FakeRand::default();
        let out = run_delete(&req(), "/t", &fs, &index, &rand);
        assert!(matches!(out, DeleteOutcome::Failed { .. }));
        // DB did NOT advance to DELETING (hazard avoided) and was NOT released.
        assert!(index.transitions.borrow().is_empty());
        // Recovery of (DELETE_CLAIMED, TrashPresent) continues the delete.
        assert_eq!(
            recovery_action(DeleteState::DeleteClaimed, FsPresence::TrashPresent),
            RecoveryAction::ContinueDelete
        );
    }

    #[test]
    fn crash_after_unlink_before_mark_deleted_recovers_to_mark_deleted() {
        // fsync trash parent fails → file gone, DB still DELETING.
        let fs = Recorder::with_fail("fsync_trash");
        let index = FakeIndex::claimed();
        let rand = FakeRand::default();
        let out = run_delete(&req(), "/t", &fs, &index, &rand);
        assert!(matches!(out, DeleteOutcome::Failed { .. }));
        // DELETING was set, DELETED was not.
        assert_eq!(
            index.transitions.borrow().clone(),
            vec!["DELETING".to_string()]
        );
        // Recovery of (DELETING, Neither) marks deleted.
        assert_eq!(
            recovery_action(DeleteState::Deleting, FsPresence::Neither),
            RecoveryAction::MarkDeleted
        );
    }

    #[test]
    fn trash_path_is_hex_and_never_wall_clock() {
        let p = trash_path("/a/.retention-trash", ArchiveItemId(7), 0xdead_beef);
        assert_eq!(
            p,
            "/a/.retention-trash/7.000000000000000000000000deadbeef.deleting"
        );
    }

    #[test]
    fn recovery_matrix_covers_every_contract_row() {
        use DeleteState as D;
        use FsPresence as F;
        use RecoveryAction as R;
        // Verbatim against contract §4.1.
        assert_eq!(
            recovery_action(D::DeleteClaimed, F::OriginalPresent),
            R::ReleaseToLive
        );
        assert_eq!(
            recovery_action(D::DeleteClaimed, F::TrashPresent),
            R::ContinueDelete
        );
        assert_eq!(
            recovery_action(D::Deleting, F::TrashPresent),
            R::FinishDelete
        );
        assert_eq!(recovery_action(D::Deleting, F::Neither), R::MarkDeleted);
        assert_eq!(recovery_action(D::Live, F::TrashPresent), R::Quarantine);
        assert_eq!(
            recovery_action(D::Deleted, F::OriginalPresent),
            R::Quarantine
        );
        // row | no file → mark deleted.
        assert_eq!(
            recovery_action(D::DeleteClaimed, F::Neither),
            R::MarkDeleted
        );
        assert_eq!(recovery_action(D::Live, F::Neither), R::MarkDeleted);
        // Healthy no-ops.
        assert_eq!(recovery_action(D::Live, F::OriginalPresent), R::NoOp);
        assert_eq!(recovery_action(D::Deleted, F::Neither), R::NoOp);
        // Quarantined stays quarantined.
        assert_eq!(recovery_action(D::Quarantined, F::TrashPresent), R::NoOp);
    }
}
