//! Phase-1 `RecentClips` archive driver (mount-free).
//!
//! Inventory comes from read-only `indexd` `SQLite` candidates, and source bytes
//! are copied by the injected `ArchiveStore` (live store uses `ReadFile`).

use std::collections::VecDeque;
use std::io;

use crate::archive::ArchiveStore;
use crate::candidates::{Candidate, CandidateSource};
use crate::register_client::{
    ArchiveAngleRef, ArchiveItemRef, ArchiveRegistration, RegisterClient,
};

/// Maximum number of register attempts for one canonical key before dropping it.
pub const MAX_REGISTER_ATTEMPTS: u32 = 5;
/// Maximum number of pending register payloads held in memory.
pub const MAX_PENDING: usize = 256;

/// One queued register payload awaiting retry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingRegistration {
    reg: ArchiveRegistration,
    attempts: u32,
}

/// Stateful cross-cycle data for the archive driver.
#[derive(Debug, Default)]
pub struct DriverState {
    pending: VecDeque<PendingRegistration>,
}

impl DriverState {
    /// Construct empty driver state.
    #[must_use]
    pub fn new() -> Self {
        Self {
            pending: VecDeque::new(),
        }
    }
}

/// One cycle report for the archive-recent-only loop.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CycleReport {
    /// Count of candidates observed this cycle.
    pub observed: usize,
    /// Count of fresh candidates registered immediately this cycle.
    pub registered: usize,
    /// Count of pending registrations successfully drained this cycle.
    pub registered_from_pending: usize,
    /// Count of candidates whose copy failed (registration skipped).
    pub copy_failed: usize,
    /// Count of registrations deferred to pending due to register failure.
    pub register_deferred: usize,
    /// Count of observed candidates skipped because their key is already pending.
    pub skipped_already_pending: usize,
    /// Count of pending items dropped (poison or queue-bound eviction).
    pub dropped_poison: usize,
    /// Pending queue length at cycle end.
    pub pending_len: usize,
}

/// Run one non-destructive archive cycle.
///
/// Order is fixed:
/// 1. drain pending registrations first,
/// 2. list current archive candidates,
/// 3. copy each candidate's angles,
/// 4. register copied candidates (or defer register-only retry).
///
/// # Errors
///
/// Returns candidate-inventory failures from [`CandidateSource::list_candidates`].
pub fn archive_recent_once(
    candidates: &dyn CandidateSource,
    store: &dyn ArchiveStore,
    register: &dyn RegisterClient,
    state: &mut DriverState,
    now_epoch_s: i64,
) -> io::Result<CycleReport> {
    let mut report = CycleReport::default();
    drain_pending(register, state, &mut report);

    let clips = candidates.list_candidates()?;
    report.observed = clips.len();

    for candidate in clips {
        if state
            .pending
            .iter()
            .any(|pending| pending.reg.canonical_key == candidate.canonical_key)
        {
            report.skipped_already_pending = report.skipped_already_pending.saturating_add(1);
            continue;
        }

        let Some(archive_item_path) = archive_item_path_for_candidate(&candidate) else {
            report.copy_failed = report.copy_failed.saturating_add(1);
            continue;
        };

        let mut copied_angles = Vec::with_capacity(candidate.angles.len());
        let mut segment_size_bytes = 0_i64;
        let mut copy_failed = false;

        for angle in &candidate.angles {
            let file_name = basename(&angle.file_ref);
            let dest_rel = format!("{archive_item_path}/{file_name}");
            if store.copy_and_hash_dest(&angle.file_ref, &dest_rel).is_err() {
                report.copy_failed = report.copy_failed.saturating_add(1);
                copy_failed = true;
                break;
            }

            let size_bytes = u64_to_i64_saturating(angle.size_bytes);
            segment_size_bytes = segment_size_bytes.saturating_add(size_bytes);
            copied_angles.push(ArchiveAngleRef {
                camera: angle.camera.clone(),
                file_ref: dest_rel,
                offset_ms: angle.offset_ms,
                duration_s: angle.duration_s,
                size_bytes,
            });
        }

        if copy_failed {
            for copied in &copied_angles {
                let _ = store.remove_dest(&copied.file_ref);
            }
            continue;
        }

        let reg = ArchiveRegistration {
            canonical_key: candidate.canonical_key.clone(),
            folder_class: "RecentClips".to_owned(),
            partition: candidate.partition.clone(),
            started_at: candidate.started_at,
            ended_at: candidate.ended_at,
            duration_s: candidate.duration_s,
            archive: ArchiveItemRef {
                path: archive_item_path,
                size_bytes: segment_size_bytes,
                file_count: usize_to_i64_saturating(copied_angles.len()),
                archived_at: now_epoch_s,
            },
            angles: copied_angles,
        };

        if register.register(&reg).is_ok() {
            report.registered = report.registered.saturating_add(1);
        } else {
            report.register_deferred = report.register_deferred.saturating_add(1);
            enqueue_pending(reg, state, &mut report);
        }
    }

    report.pending_len = state.pending.len();
    Ok(report)
}

fn archive_item_path_for_candidate(candidate: &Candidate) -> Option<String> {
    let timestamp = candidate.canonical_key.rsplit('/').next()?;
    if timestamp.len() < 10 {
        return None;
    }
    let date = timestamp.get(..10)?;
    Some(format!("RecentClips/{date}/{timestamp}"))
}

fn drain_pending(register: &dyn RegisterClient, state: &mut DriverState, report: &mut CycleReport) {
    let mut retained = VecDeque::with_capacity(state.pending.len());
    for mut pending in std::mem::take(&mut state.pending) {
        if register.register(&pending.reg).is_ok() {
            report.registered_from_pending = report.registered_from_pending.saturating_add(1);
            continue;
        }

        pending.attempts = pending.attempts.saturating_add(1);
        if pending.attempts >= MAX_REGISTER_ATTEMPTS {
            report.dropped_poison = report.dropped_poison.saturating_add(1);
            continue;
        }
        retained.push_back(pending);
    }
    state.pending = retained;
}

fn enqueue_pending(reg: ArchiveRegistration, state: &mut DriverState, report: &mut CycleReport) {
    if state
        .pending
        .iter()
        .any(|pending| pending.reg.canonical_key == reg.canonical_key)
    {
        return;
    }

    if state.pending.len() >= MAX_PENDING {
        let _ = state.pending.pop_front();
        report.dropped_poison = report.dropped_poison.saturating_add(1);
    }

    state
        .pending
        .push_back(PendingRegistration { reg, attempts: 1 });
}

fn u64_to_i64_saturating(value: u64) -> i64 {
    i64::try_from(value).unwrap_or(i64::MAX)
}

fn usize_to_i64_saturating(value: usize) -> i64 {
    i64::try_from(value).unwrap_or(i64::MAX)
}

/// Return the final path component in a slash-separated relative path.
#[must_use]
pub fn basename(src_rel: &str) -> &str {
    match src_rel.rsplit_once('/') {
        Some((_, base)) => base,
        None => src_rel,
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
    use std::{
        cell::{Cell, RefCell},
        collections::{HashSet, VecDeque},
        io,
    };

    use crate::{
        archive::ArchiveStore,
        candidates::{Candidate, CandidateAngle, CandidateSource},
        io::{ContentHash, FileIdentity},
        register_client::{ArchiveRegistration, RegisterClient, RegisterError, RegistrationOk},
    };

    use super::{DriverState, MAX_REGISTER_ATTEMPTS, archive_recent_once, basename};

    const KEY: &str = "0:TeslaCam/RecentClips/2026-06-19_10-00-00";
    const PATH: &str = "RecentClips/2026-06-19/2026-06-19_10-00-00";

    fn sample_candidate() -> Candidate {
        Candidate {
            clip_id: 1,
            canonical_key: KEY.to_owned(),
            partition: "slot0".to_owned(),
            started_at: 1_700_000_000,
            ended_at: 1_700_000_060,
            duration_s: Some(60),
            angles: vec![
                CandidateAngle {
                    camera: "front".to_owned(),
                    file_ref: "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4".to_owned(),
                    offset_ms: 0,
                    duration_s: Some(60),
                    size_bytes: 10,
                },
                CandidateAngle {
                    camera: "back".to_owned(),
                    file_ref: "TeslaCam/RecentClips/2026-06-19_10-00-00-back.mp4".to_owned(),
                    offset_ms: 500,
                    duration_s: Some(59),
                    size_bytes: 20,
                },
            ],
        }
    }

    #[derive(Default)]
    struct FakeCandidates {
        clips: RefCell<Vec<Candidate>>,
    }

    impl FakeCandidates {
        fn set(&self, clips: Vec<Candidate>) {
            *self.clips.borrow_mut() = clips;
        }
    }

    impl CandidateSource for FakeCandidates {
        fn list_candidates(&self) -> io::Result<Vec<Candidate>> {
            Ok(self.clips.borrow().clone())
        }
    }

    #[derive(Default)]
    struct FakeStore {
        copies: RefCell<Vec<(String, String)>>,
        fail_once_src: RefCell<Option<String>>,
        removed: RefCell<Vec<String>>,
        landed: RefCell<HashSet<String>>,
    }

    impl FakeStore {
        fn fail_once_for(&self, src_rel: &str) {
            *self.fail_once_src.borrow_mut() = Some(src_rel.to_owned());
        }
    }

    impl ArchiveStore for FakeStore {
        fn copy_and_hash_dest(&self, src_rel: &str, dest_rel: &str) -> io::Result<ContentHash> {
            if self.fail_once_src.borrow().as_deref() == Some(src_rel) {
                *self.fail_once_src.borrow_mut() = None;
                return Err(io::Error::other("copy failed"));
            }
            self.copies
                .borrow_mut()
                .push((src_rel.to_owned(), dest_rel.to_owned()));
            self.landed.borrow_mut().insert(dest_rel.to_owned());
            Ok(ContentHash::new([0_u8; 32]))
        }

        fn source_identity(&self, _src_rel: &str) -> io::Result<FileIdentity> {
            Err(io::Error::other("unused by archive driver tests"))
        }

        fn list_source_rel_names(&self, _src_dir: &str) -> io::Result<Vec<String>> {
            Err(io::Error::other("unused by archive driver tests"))
        }

        fn remove_dest(&self, dest_rel: &str) -> io::Result<()> {
            self.removed.borrow_mut().push(dest_rel.to_owned());
            self.landed.borrow_mut().remove(dest_rel);
            Ok(())
        }
    }

    #[derive(Default)]
    struct FakeRegister {
        calls: RefCell<Vec<ArchiveRegistration>>,
        failures: RefCell<VecDeque<bool>>,
        always_fail: Cell<bool>,
    }

    impl FakeRegister {
        fn with_failures(failures: Vec<bool>) -> Self {
            Self {
                calls: RefCell::new(Vec::new()),
                failures: RefCell::new(failures.into()),
                always_fail: Cell::new(false),
            }
        }

        fn set_always_fail(&self, always_fail: bool) {
            self.always_fail.set(always_fail);
        }
    }

    impl RegisterClient for FakeRegister {
        fn register(&self, reg: &ArchiveRegistration) -> Result<RegistrationOk, RegisterError> {
            self.calls.borrow_mut().push(reg.clone());
            let should_fail = {
                let mut failures = self.failures.borrow_mut();
                if let Some(next) = failures.pop_front() {
                    next
                } else {
                    self.always_fail.get()
                }
            };
            if should_fail {
                Err(RegisterError::Io(io::Error::other("register failed")))
            } else {
                Ok(RegistrationOk {
                    clip_id: 1,
                    archive_item_id: 1,
                })
            }
        }
    }

    #[test]
    fn happy_path_copies_angles_and_registers_once() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::new();

        let report =
            archive_recent_once(&candidates, &store, &register, &mut state, 2_000_000_000).unwrap();
        assert_eq!(report.observed, 1);
        assert_eq!(report.registered, 1);
        assert_eq!(report.pending_len, 0);

        let copies = store.copies.borrow();
        assert_eq!(copies.len(), 2);
        assert_eq!(
            copies[0].1,
            "RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-front.mp4"
        );
        assert_eq!(
            copies[1].1,
            "RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-back.mp4"
        );

        let calls = register.calls.borrow();
        assert_eq!(calls.len(), 1);
        let reg = &calls[0];
        assert_eq!(reg.canonical_key, KEY);
        assert_eq!(reg.archive.path, PATH);
        assert_eq!(reg.archive.size_bytes, 30);
        assert_eq!(reg.archive.file_count, 2);
        assert_eq!(reg.partition, "slot0");
        assert_eq!(reg.started_at, 1_700_000_000);
        assert_eq!(reg.ended_at, 1_700_000_060);
        assert_eq!(reg.duration_s, Some(60));
        assert_eq!(reg.angles.len(), 2);
        assert_eq!(reg.angles[1].offset_ms, 500);
    }

    #[test]
    fn copy_failure_skips_registration_and_retries_next_cycle() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        store.fail_once_for("TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4");
        let register = FakeRegister::default();
        let mut state = DriverState::new();

        let first = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(first.copy_failed, 1);
        assert_eq!(register.calls.borrow().len(), 0);

        let second = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(second.registered, 1);
        assert_eq!(register.calls.borrow().len(), 1);
    }

    #[test]
    fn copy_failure_after_first_angle_removes_landed_dest_and_retries_cleanly() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        store.fail_once_for("TeslaCam/RecentClips/2026-06-19_10-00-00-back.mp4");
        let register = FakeRegister::default();
        let mut state = DriverState::new();

        let first = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(first.copy_failed, 1);
        assert_eq!(register.calls.borrow().len(), 0);
        assert_eq!(
            store.removed.borrow().as_slice(),
            &["RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-front.mp4"
                .to_owned()]
        );
        assert!(store.landed.borrow().is_empty(), "no orphan angle files left");

        let second = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(second.registered, 1);
        assert_eq!(register.calls.borrow().len(), 1);
    }

    #[test]
    fn register_failure_defers_then_drains_without_recopied_bytes() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        let register = FakeRegister::with_failures(vec![true, false]);
        let mut state = DriverState::new();

        let deferred = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(deferred.registered, 0);
        assert_eq!(deferred.register_deferred, 1);
        assert_eq!(deferred.pending_len, 1);
        assert_eq!(store.copies.borrow().len(), 2);

        candidates.set(Vec::new());
        let drained = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(drained.registered_from_pending, 1);
        assert_eq!(drained.pending_len, 0);
        assert_eq!(store.copies.borrow().len(), 2);
    }

    #[test]
    fn pending_key_reemit_is_skipped_not_recopied() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        register.set_always_fail(true);
        let mut state = DriverState::new();

        let first = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(first.register_deferred, 1);
        assert_eq!(state.pending.len(), 1);
        let copies_after_initial_failure = store.copies.borrow().len();

        let second = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(second.skipped_already_pending, 1);
        assert_eq!(second.register_deferred, 0);
        assert_eq!(store.copies.borrow().len(), copies_after_initial_failure);
        assert_eq!(state.pending.len(), 1);
    }

    #[test]
    fn poison_pending_is_dropped_after_max_attempts() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        register.set_always_fail(true);
        let mut state = DriverState::new();

        archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(state.pending.len(), 1);

        candidates.set(Vec::new());
        let mut saw_drop = false;
        for tick in 0..=MAX_REGISTER_ATTEMPTS {
            let report = archive_recent_once(&candidates, &store, &register, &mut state, 2 + i64::from(tick))
                .unwrap();
            if report.dropped_poison > 0 {
                saw_drop = true;
                assert_eq!(report.pending_len, 0);
                break;
            }
        }
        assert!(saw_drop, "pending registration should eventually poison-drop");
    }

    #[test]
    fn basename_handles_common_edges() {
        assert_eq!(basename("a.mp4"), "a.mp4");
        assert_eq!(basename("TeslaCam/RecentClips/a.mp4"), "a.mp4");
        assert_eq!(basename("nested/more/deep/file.mp4"), "file.mp4");
        assert_eq!(basename("/leading/slash.mp4"), "slash.mp4");
        assert_eq!(basename("trailing/"), "");
    }
}
