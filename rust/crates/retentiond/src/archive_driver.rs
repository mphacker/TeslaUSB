//! Phase-1 `RecentClips` archive driver (mount-free).
//!
//! Inventory comes from read-only `indexd` `SQLite` candidates, and source bytes
//! are copied by the injected `ArchiveStore` (live store uses `ReadFile`).

use std::collections::VecDeque;
use std::io::{self, Write};

use crate::archive::ArchiveStore;
use crate::candidates::{Candidate, CandidateSource};
use crate::probe::{ArchivePlayability, UnplayableReason};
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
    disposition: RegistrationDisposition,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RegistrationDisposition {
    Live,
    Quarantine,
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
    /// Count of copied candidates quarantined as undecodable.
    pub quarantined_undecodable: usize,
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
            if store
                .copy_and_hash_dest(&angle.file_ref, &dest_rel)
                .is_err()
            {
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

        let probe_failures = collect_probe_failures(store, &reg.angles);
        if probe_failures.is_empty() {
            if register.register(&reg).is_ok() {
                report.registered = report.registered.saturating_add(1);
            } else {
                report.register_deferred = report.register_deferred.saturating_add(1);
                enqueue_pending(reg, RegistrationDisposition::Live, state, &mut report);
            }
        } else {
            let failure_detail = probe_failures
                .iter()
                .map(ProbeFailure::to_log_fragment)
                .collect::<Vec<_>>()
                .join(",");
            log_quarantine_warning(&reg.canonical_key, &failure_detail);
            if register.register_quarantined(&reg).is_ok() {
                report.quarantined_undecodable = report.quarantined_undecodable.saturating_add(1);
            } else {
                report.register_deferred = report.register_deferred.saturating_add(1);
                enqueue_pending(reg, RegistrationDisposition::Quarantine, state, &mut report);
            }
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
        if send_registration(register, pending.disposition, &pending.reg).is_ok() {
            report.registered_from_pending = report.registered_from_pending.saturating_add(1);
            continue;
        }

        pending.attempts = pending.attempts.saturating_add(1);
        // Quarantine pendings fail closed: retain until indexd accepts the
        // quarantined-register verb. The queue bound remains MAX_PENDING.
        if pending.attempts >= MAX_REGISTER_ATTEMPTS
            && pending.disposition == RegistrationDisposition::Live
        {
            report.dropped_poison = report.dropped_poison.saturating_add(1);
            continue;
        }
        retained.push_back(pending);
    }
    state.pending = retained;
}

fn enqueue_pending(
    reg: ArchiveRegistration,
    disposition: RegistrationDisposition,
    state: &mut DriverState,
    report: &mut CycleReport,
) {
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

    state.pending.push_back(PendingRegistration {
        reg,
        attempts: 1,
        disposition,
    });
}

fn send_registration(
    register: &dyn RegisterClient,
    disposition: RegistrationDisposition,
    reg: &ArchiveRegistration,
) -> Result<crate::register_client::RegistrationOk, crate::register_client::RegisterError> {
    match disposition {
        RegistrationDisposition::Live => register.register(reg),
        RegistrationDisposition::Quarantine => register.register_quarantined(reg),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ProbeFailure {
    camera: String,
    reason: ProbeFailureReason,
}

impl ProbeFailure {
    fn to_log_fragment(&self) -> String {
        match self.reason {
            ProbeFailureReason::Unplayable(reason) => {
                format!("camera={} reason={reason:?}", self.camera)
            }
            ProbeFailureReason::ProbeIo => format!("camera={} reason=ProbeIo", self.camera),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ProbeFailureReason {
    Unplayable(UnplayableReason),
    ProbeIo,
}

fn collect_probe_failures(
    store: &dyn ArchiveStore,
    angles: &[ArchiveAngleRef],
) -> Vec<ProbeFailure> {
    let mut failures = Vec::new();
    for angle in angles {
        match store.probe_dest_playability(&angle.file_ref) {
            Ok(ArchivePlayability::Playable) => {}
            Ok(ArchivePlayability::Unplayable(reason)) => failures.push(ProbeFailure {
                camera: angle.camera.clone(),
                reason: ProbeFailureReason::Unplayable(reason),
            }),
            Err(_) => failures.push(ProbeFailure {
                camera: angle.camera.clone(),
                reason: ProbeFailureReason::ProbeIo,
            }),
        }
    }
    failures
}

fn log_quarantine_warning(canonical_key: &str, failure_detail: &str) {
    let mut stderr = io::stderr();
    let _ = writeln!(
        &mut stderr,
        "retentiond archive_recent_only: quarantining_undecodable canonical_key={canonical_key} failures={failure_detail}"
    );
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
        collections::{HashMap, HashSet, VecDeque},
        io,
    };

    use crate::{
        archive::ArchiveStore,
        candidates::{Candidate, CandidateAngle, CandidateSource},
        io::{ContentHash, FileIdentity},
        probe::{ArchivePlayability, UnplayableReason},
        register_client::{ArchiveRegistration, RegisterClient, RegisterError, RegistrationOk},
    };

    use super::{
        DriverState, MAX_REGISTER_ATTEMPTS, RegistrationDisposition, archive_recent_once, basename,
    };

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
        probe_unplayable: RefCell<HashMap<String, UnplayableReason>>,
        probe_error: RefCell<HashSet<String>>,
    }

    impl FakeStore {
        fn fail_once_for(&self, src_rel: &str) {
            *self.fail_once_src.borrow_mut() = Some(src_rel.to_owned());
        }

        fn set_unplayable(&self, dest_rel: &str, reason: UnplayableReason) {
            self.probe_unplayable
                .borrow_mut()
                .insert(dest_rel.to_owned(), reason);
        }

        fn set_probe_error(&self, dest_rel: &str) {
            self.probe_error.borrow_mut().insert(dest_rel.to_owned());
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

        fn probe_dest_playability(&self, dest_rel: &str) -> io::Result<ArchivePlayability> {
            if self.probe_error.borrow().contains(dest_rel) {
                return Err(io::Error::other("probe failed"));
            }
            if let Some(reason) = self.probe_unplayable.borrow().get(dest_rel) {
                return Ok(ArchivePlayability::Unplayable(*reason));
            }
            Ok(ArchivePlayability::Playable)
        }
    }

    #[derive(Default)]
    struct FakeRegister {
        live_calls: RefCell<Vec<ArchiveRegistration>>,
        quarantine_calls: RefCell<Vec<ArchiveRegistration>>,
        live_failures: RefCell<VecDeque<bool>>,
        quarantine_failures: RefCell<VecDeque<bool>>,
        always_fail_live: Cell<bool>,
        always_fail_quarantine: Cell<bool>,
    }

    impl FakeRegister {
        fn with_live_failures(failures: Vec<bool>) -> Self {
            Self {
                live_calls: RefCell::new(Vec::new()),
                quarantine_calls: RefCell::new(Vec::new()),
                live_failures: RefCell::new(failures.into()),
                quarantine_failures: RefCell::new(VecDeque::new()),
                always_fail_live: Cell::new(false),
                always_fail_quarantine: Cell::new(false),
            }
        }

        fn with_quarantine_failures(failures: Vec<bool>) -> Self {
            Self {
                live_calls: RefCell::new(Vec::new()),
                quarantine_calls: RefCell::new(Vec::new()),
                live_failures: RefCell::new(VecDeque::new()),
                quarantine_failures: RefCell::new(failures.into()),
                always_fail_live: Cell::new(false),
                always_fail_quarantine: Cell::new(false),
            }
        }

        fn set_always_fail_live(&self, always_fail: bool) {
            self.always_fail_live.set(always_fail);
        }

        fn set_always_fail_quarantine(&self, always_fail: bool) {
            self.always_fail_quarantine.set(always_fail);
        }
    }

    impl RegisterClient for FakeRegister {
        fn register(&self, reg: &ArchiveRegistration) -> Result<RegistrationOk, RegisterError> {
            self.live_calls.borrow_mut().push(reg.clone());
            let should_fail = {
                let mut failures = self.live_failures.borrow_mut();
                if let Some(next) = failures.pop_front() {
                    next
                } else {
                    self.always_fail_live.get()
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

        fn register_quarantined(
            &self,
            reg: &ArchiveRegistration,
        ) -> Result<RegistrationOk, RegisterError> {
            self.quarantine_calls.borrow_mut().push(reg.clone());
            let should_fail = {
                let mut failures = self.quarantine_failures.borrow_mut();
                if let Some(next) = failures.pop_front() {
                    next
                } else {
                    self.always_fail_quarantine.get()
                }
            };
            if should_fail {
                Err(RegisterError::Io(io::Error::other(
                    "quarantine register failed",
                )))
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
        assert_eq!(report.quarantined_undecodable, 0);
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

        let calls = register.live_calls.borrow();
        assert_eq!(calls.len(), 1);
        assert_eq!(register.quarantine_calls.borrow().len(), 0);
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
        assert_eq!(register.live_calls.borrow().len(), 0);

        let second = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(second.registered, 1);
        assert_eq!(register.live_calls.borrow().len(), 1);
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
        assert_eq!(register.live_calls.borrow().len(), 0);
        assert_eq!(
            store.removed.borrow().as_slice(),
            &[
                "RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-front.mp4"
                    .to_owned()
            ]
        );
        assert!(
            store.landed.borrow().is_empty(),
            "no orphan angle files left"
        );

        let second = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(second.registered, 1);
        assert_eq!(register.live_calls.borrow().len(), 1);
    }

    #[test]
    fn register_failure_defers_then_drains_without_recopied_bytes() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        let register = FakeRegister::with_live_failures(vec![true, false]);
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
    fn unplayable_angle_registers_quarantine_without_remove_dest() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        store.set_unplayable(
            "RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-back.mp4",
            UnplayableReason::NoMoov,
        );
        let register = FakeRegister::default();
        let mut state = DriverState::new();

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(report.quarantined_undecodable, 1);
        assert_eq!(report.copy_failed, 0);
        assert_eq!(register.live_calls.borrow().len(), 0);
        assert_eq!(register.quarantine_calls.borrow().len(), 1);
        assert!(store.removed.borrow().is_empty());
    }

    #[test]
    fn probe_error_routes_to_quarantine_without_copy_failure_or_remove() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        store.set_probe_error(
            "RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-front.mp4",
        );
        let register = FakeRegister::default();
        let mut state = DriverState::new();

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(report.copy_failed, 0);
        assert_eq!(report.quarantined_undecodable, 1);
        assert!(store.removed.borrow().is_empty());
        assert_eq!(register.live_calls.borrow().len(), 0);
        assert_eq!(register.quarantine_calls.borrow().len(), 1);
    }

    #[test]
    fn quarantine_register_failure_defers_pending_and_retry_skips_recopy() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        store.set_unplayable(
            "RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-front.mp4",
            UnplayableReason::NoMoov,
        );
        let register = FakeRegister::with_quarantine_failures(vec![true, true, false]);
        let mut state = DriverState::new();

        let first = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(first.register_deferred, 1);
        assert_eq!(first.pending_len, 1);
        let copies_after_first = store.copies.borrow().len();
        assert_eq!(register.quarantine_calls.borrow().len(), 1);

        let second = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(second.registered_from_pending, 0);
        assert_eq!(second.skipped_already_pending, 1);
        assert_eq!(second.pending_len, 1);
        assert_eq!(store.copies.borrow().len(), copies_after_first);
        assert_eq!(register.quarantine_calls.borrow().len(), 2);

        candidates.set(Vec::new());
        let third = archive_recent_once(&candidates, &store, &register, &mut state, 3).unwrap();
        assert_eq!(third.registered_from_pending, 1);
        assert_eq!(third.pending_len, 0);
        assert_eq!(register.quarantine_calls.borrow().len(), 3);
    }

    #[test]
    fn pending_key_reemit_is_skipped_not_recopied() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        register.set_always_fail_live(true);
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
        register.set_always_fail_live(true);
        let mut state = DriverState::new();

        archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(state.pending.len(), 1);

        candidates.set(Vec::new());
        let mut saw_drop = false;
        for tick in 0..=MAX_REGISTER_ATTEMPTS {
            let report = archive_recent_once(
                &candidates,
                &store,
                &register,
                &mut state,
                2 + i64::from(tick),
            )
            .unwrap();
            if report.dropped_poison > 0 {
                saw_drop = true;
                assert_eq!(report.pending_len, 0);
                break;
            }
        }
        assert!(
            saw_drop,
            "pending registration should eventually poison-drop"
        );
    }

    #[test]
    fn quarantine_pending_is_retained_past_max_attempts_until_register_accepts() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        store.set_unplayable(
            "RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-front.mp4",
            UnplayableReason::NoMoov,
        );
        let register = FakeRegister::default();
        register.set_always_fail_quarantine(true);
        let mut state = DriverState::new();

        let first = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(first.register_deferred, 1);
        assert_eq!(first.pending_len, 1);
        assert_eq!(store.copies.borrow().len(), 2);

        for tick in 0..=(MAX_REGISTER_ATTEMPTS + 1) {
            let report = archive_recent_once(
                &candidates,
                &store,
                &register,
                &mut state,
                2 + i64::from(tick),
            )
            .unwrap();
            assert_eq!(report.skipped_already_pending, 1);
            assert_eq!(report.pending_len, 1);
        }

        assert_eq!(store.copies.borrow().len(), 2);
        assert_eq!(state.pending.len(), 1);
        let pending = state.pending.front().expect("pending retained");
        assert_eq!(pending.disposition, RegistrationDisposition::Quarantine);
        assert!(pending.attempts > MAX_REGISTER_ATTEMPTS);

        candidates.set(Vec::new());
        register.set_always_fail_quarantine(false);
        let drained = archive_recent_once(
            &candidates,
            &store,
            &register,
            &mut state,
            2 + i64::from(MAX_REGISTER_ATTEMPTS) + 2,
        )
        .unwrap();
        assert_eq!(drained.registered_from_pending, 1);
        assert_eq!(drained.pending_len, 0);
        assert_eq!(register.live_calls.borrow().len(), 0);
        assert!(register.quarantine_calls.borrow().len() > 2);
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
