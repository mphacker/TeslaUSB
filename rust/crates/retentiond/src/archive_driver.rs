//! Phase-1 `RecentClips` archive driver.
//!
//! This is the non-destructive loop core that:
//! 1. discovers stable complete `RecentClips` segments,
//! 2. copies every angle into the archive, and
//! 3. registers the archived segment with `indexd`.
//!
//! It never deletes bytes and keeps all policy in pure, host-testable code.

use std::collections::VecDeque;
use std::io;

use crate::archive::ArchiveStore;
use crate::recent_facts::{RecentDirReader, RecentFactsGatherer};
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
    /// Count of newly observed complete segments this cycle.
    pub observed: usize,
    /// Count of fresh segments registered immediately this cycle.
    pub registered: usize,
    /// Count of pending registrations successfully drained this cycle.
    pub registered_from_pending: usize,
    /// Count of segments whose copy failed (registration skipped).
    pub copy_failed: usize,
    /// Count of registrations deferred to pending due to register failure.
    pub register_deferred: usize,
    /// Count of observed segments skipped because their key is already pending.
    pub skipped_already_pending: usize,
    /// Count of pending items dropped (poison or queue-bound eviction).
    pub dropped_poison: usize,
    /// Pending queue length at cycle end.
    pub pending_len: usize,
}

/// Run one non-destructive archive cycle for one `RecentClips` slot.
///
/// Order is fixed:
/// 1. drain pending registrations first,
/// 2. observe new stable recent segments,
/// 3. copy each complete segment,
/// 4. register copied segments (or defer register-only retry).
///
/// # Errors
///
/// Returns listing/observation I/O failures from [`RecentFactsGatherer::observe`].
#[allow(clippy::too_many_arguments)]
pub fn archive_recent_once(
    gatherer: &mut RecentFactsGatherer,
    slot: u8,
    recentclips_dir: &str,
    reader: &dyn RecentDirReader,
    store: &dyn ArchiveStore,
    register: &dyn RegisterClient,
    state: &mut DriverState,
    now_epoch_s: i64,
) -> io::Result<CycleReport> {
    let mut report = CycleReport::default();
    drain_pending(register, state, &mut report);

    let segs = gatherer.observe(slot, recentclips_dir, reader)?;
    report.observed = segs.len();

    for seg in segs {
        if state
            .pending
            .iter()
            .any(|pending| pending.reg.canonical_key == seg.canonical_key)
        {
            report.skipped_already_pending = report.skipped_already_pending.saturating_add(1);
            continue;
        }

        let mut copied_angles = Vec::with_capacity(seg.angles.len());
        let mut segment_size_bytes = 0_i64;
        let mut copy_failed = false;

        for angle in &seg.angles {
            let file_name = basename(&angle.src_rel);
            let dest_rel = format!("{}/{}", seg.archive_item_path, file_name);
            if store.copy_and_hash_dest(&angle.src_rel, &dest_rel).is_err() {
                gatherer.forget(&seg.canonical_key);
                report.copy_failed = report.copy_failed.saturating_add(1);
                copy_failed = true;
                break;
            }

            let size_bytes = u64_to_i64_saturating(angle.size_bytes);
            segment_size_bytes = segment_size_bytes.saturating_add(size_bytes);
            copied_angles.push(ArchiveAngleRef {
                camera: angle.camera.clone(),
                file_ref: dest_rel,
                offset_ms: 0,
                duration_s: None,
                size_bytes,
            });
        }

        if copy_failed {
            continue;
        }

        let started_at = seg.capture_ms / 1_000;
        let reg = ArchiveRegistration {
            canonical_key: seg.canonical_key.clone(),
            folder_class: "RecentClips".to_owned(),
            partition: seg.partition.clone(),
            started_at,
            ended_at: started_at,
            duration_s: None,
            archive: ArchiveItemRef {
                path: seg.archive_item_path.clone(),
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

    // Queue-bound policy: keep newest work by dropping the oldest pending entry
    // when the queue is full.
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
    clippy::indexing_slicing,
    clippy::cognitive_complexity
)]
mod tests {
    use std::{
        cell::{Cell, RefCell},
        collections::VecDeque,
        io,
    };

    use crate::{
        archive::ArchiveStore,
        io::{ContentHash, FileIdentity},
        recent_facts::{RecentDirReader, RecentFactsGatherer, RecentFileObservation, civil_timestamp_to_ms},
        register_client::{ArchiveRegistration, RegisterClient, RegisterError, RegistrationOk},
    };

    use super::{DriverState, MAX_REGISTER_ATTEMPTS, archive_recent_once, basename};

    const SLOT: u8 = 0;
    const RECENT_DIR: &str = "TeslaCam/RecentClips";
    const TIMESTAMP: &str = "2026-06-19_10-00-00";
    const CANONICAL_KEY: &str = "0:TeslaCam/RecentClips/2026-06-19_10-00-00";
    const ARCHIVE_PATH: &str = "RecentClips/2026-06-19/2026-06-19_10-00-00";

    #[derive(Default)]
    struct FakeReader {
        files: RefCell<Vec<RecentFileObservation>>,
    }

    impl FakeReader {
        fn set_files(&self, files: Vec<RecentFileObservation>) {
            *self.files.borrow_mut() = files;
        }
    }

    impl RecentDirReader for FakeReader {
        fn list(&self, _slot: u8) -> io::Result<Vec<RecentFileObservation>> {
            Ok(self.files.borrow().clone())
        }
    }

    #[derive(Default)]
    struct FakeStore {
        copies: RefCell<Vec<(String, String)>>,
        fail_once_src: RefCell<Option<String>>,
    }

    impl FakeStore {
        fn fail_once_for(&self, src_rel: &str) {
            *self.fail_once_src.borrow_mut() = Some(src_rel.to_owned());
        }

        fn copy_count(&self) -> usize {
            self.copies.borrow().len()
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
            Ok(ContentHash::new([0_u8; 32]))
        }

        fn source_identity(&self, _src_rel: &str) -> io::Result<FileIdentity> {
            Err(io::Error::other("unused by archive driver tests"))
        }

        fn list_source_rel_names(&self, _src_dir: &str) -> io::Result<Vec<String>> {
            Err(io::Error::other("unused by archive driver tests"))
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

        fn call_count(&self) -> usize {
            self.calls.borrow().len()
        }
    }

    impl RegisterClient for FakeRegister {
        fn register(
            &self,
            reg: &crate::register_client::ArchiveRegistration,
        ) -> Result<RegistrationOk, crate::register_client::RegisterError> {
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

    fn obs(name: &str, size: u64, mtime_ms: i64) -> RecentFileObservation {
        RecentFileObservation {
            name: name.to_owned(),
            size,
            mtime_ms,
        }
    }

    fn stable_three_angle_files() -> Vec<RecentFileObservation> {
        vec![
            obs("2026-06-19_10-00-00-front.mp4", 10, 100),
            obs("2026-06-19_10-00-00-back.mp4", 20, 100),
            obs("2026-06-19_10-00-00-left_repeater.mp4", 30, 100),
        ]
    }

    #[test]
    fn happy_path_copies_three_angles_and_registers_once() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        reader.set_files(stable_three_angle_files());
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::new();

        let first = archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            10,
        )
        .expect("first pass should succeed");
        assert_eq!(first.observed, 0);
        assert_eq!(first.registered, 0);

        let second = archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            20,
        )
        .expect("second pass should emit/register");

        assert_eq!(second.observed, 1);
        assert_eq!(second.registered, 1);
        assert_eq!(second.registered_from_pending, 0);
        assert_eq!(second.copy_failed, 0);
        assert_eq!(second.register_deferred, 0);
        assert_eq!(second.dropped_poison, 0);
        assert_eq!(second.pending_len, 0);
        assert_eq!(register.call_count(), 1);

        let copies = store.copies.borrow();
        assert_eq!(copies.len(), 3);
        let mut copied_dests: Vec<String> = copies.iter().map(|(_, dest)| dest.clone()).collect();
        copied_dests.sort_unstable();
        assert_eq!(
            copied_dests,
            vec![
                format!("{ARCHIVE_PATH}/2026-06-19_10-00-00-back.mp4"),
                format!("{ARCHIVE_PATH}/2026-06-19_10-00-00-front.mp4"),
                format!("{ARCHIVE_PATH}/2026-06-19_10-00-00-left_repeater.mp4"),
            ]
        );

        let calls = register.calls.borrow();
        assert_eq!(calls.len(), 1);
        let reg = &calls[0];
        let Some(capture_ms) = civil_timestamp_to_ms(TIMESTAMP) else {
            panic!("known timestamp should parse");
        };
        assert_eq!(reg.canonical_key, CANONICAL_KEY);
        assert_eq!(reg.folder_class, "RecentClips");
        assert_eq!(reg.partition, "slot0");
        assert_eq!(reg.started_at, capture_ms / 1_000);
        assert_eq!(reg.ended_at, reg.started_at);
        assert_eq!(reg.duration_s, None);
        assert_eq!(reg.archive.path, ARCHIVE_PATH);
        assert_eq!(reg.archive.file_count, 3);
        assert_eq!(reg.archive.size_bytes, 60);
        assert_eq!(reg.archive.archived_at, 20);
        assert_eq!(reg.angles.len(), 3);
        for angle in &reg.angles {
            assert!(angle.file_ref.starts_with(ARCHIVE_PATH));
            assert_eq!(angle.offset_ms, 0);
            assert_eq!(angle.duration_s, None);
            match angle.camera.as_str() {
                "back" => assert_eq!(angle.size_bytes, 20),
                "front" => assert_eq!(angle.size_bytes, 10),
                "left_repeater" => assert_eq!(angle.size_bytes, 30),
                other => panic!("unexpected camera {other}"),
            }
        }
    }

    #[test]
    fn copy_failure_forgets_segment_and_reemits_after_stability() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        reader.set_files(stable_three_angle_files());
        let store = FakeStore::default();
        store.fail_once_for("TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4");
        let register = FakeRegister::default();
        let mut state = DriverState::new();

        archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            10,
        )
        .expect("first pass");
        let failed = archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            20,
        )
        .expect("copy-failing pass");
        assert_eq!(failed.copy_failed, 1);
        assert_eq!(register.call_count(), 0);

        let third = archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            30,
        )
        .expect("re-evaluation first pass");
        assert_eq!(third.observed, 0);

        let fourth = archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            40,
        )
        .expect("re-emission pass");
        assert_eq!(fourth.observed, 1);
        assert_eq!(fourth.registered, 1);
        assert_eq!(register.call_count(), 1);
    }

    #[test]
    fn register_failure_defers_then_drains_without_recopied_bytes() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        reader.set_files(stable_three_angle_files());
        let store = FakeStore::default();
        let register = FakeRegister::with_failures(vec![true, false]);
        let mut state = DriverState::new();

        archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            10,
        )
        .expect("first pass");
        let deferred = archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            20,
        )
        .expect("register failing pass");
        assert_eq!(deferred.registered, 0);
        assert_eq!(deferred.register_deferred, 1);
        assert_eq!(deferred.pending_len, 1);
        assert_eq!(store.copy_count(), 3);

        let drained = archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            30,
        )
        .expect("pending drain pass");
        assert_eq!(drained.registered_from_pending, 1);
        assert_eq!(drained.pending_len, 0);
        assert_eq!(store.copy_count(), 3);
    }

    #[test]
    fn pending_is_deduped_by_canonical_key() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        reader.set_files(stable_three_angle_files());
        let store = FakeStore::default();
        let register = FakeRegister::with_failures(vec![true, true, true, true, true, true]);
        let mut state = DriverState::new();

        archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            10,
        )
        .expect("first pass");
        archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            20,
        )
        .expect("initial register failure");
        assert_eq!(state.pending.len(), 1);
        let copies_after_initial_failure = store.copy_count();
        gatherer.forget(CANONICAL_KEY);

        archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            30,
        )
        .expect("re-eval pass one");
        let second_failure = archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            40,
        )
        .expect("re-eval pass two");
        assert_eq!(second_failure.skipped_already_pending, 1);
        assert_eq!(second_failure.register_deferred, 0);
        assert_eq!(second_failure.pending_len, 1);
        assert_eq!(store.copy_count(), copies_after_initial_failure);
        assert_eq!(state.pending.len(), 1);
        assert_eq!(state.pending[0].reg.canonical_key, CANONICAL_KEY);
    }

    #[test]
    fn pending_key_reemit_is_skipped_not_recopied() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        reader.set_files(stable_three_angle_files());
        let store = FakeStore::default();
        let register = FakeRegister::default();
        register.set_always_fail(true);
        let mut state = DriverState::new();

        archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            10,
        )
        .expect("first pass");
        archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            20,
        )
        .expect("initial register failure");
        assert_eq!(state.pending.len(), 1);
        let copies_after_initial_failure = store.copy_count();
        gatherer.forget(CANONICAL_KEY);

        archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            30,
        )
        .expect("re-eval pass one");
        let calls_before_reemit = register.call_count();

        let reemitted = archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            40,
        )
        .expect("re-eval pass two");
        assert_eq!(reemitted.skipped_already_pending, 1);
        assert_eq!(reemitted.register_deferred, 0);
        assert_eq!(store.copy_count(), copies_after_initial_failure);
        assert_eq!(register.call_count(), calls_before_reemit + 1);
        assert_eq!(state.pending.len(), 1);
        assert_eq!(state.pending[0].reg.canonical_key, CANONICAL_KEY);
    }

    #[test]
    fn poison_pending_is_dropped_after_max_attempts() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        reader.set_files(stable_three_angle_files());
        let store = FakeStore::default();
        let register = FakeRegister::default();
        register.set_always_fail(true);
        let mut state = DriverState::new();

        archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            10,
        )
        .expect("first pass");
        archive_recent_once(
            &mut gatherer,
            SLOT,
            RECENT_DIR,
            &reader,
            &store,
            &register,
            &mut state,
            20,
        )
        .expect("enqueue pending");
        assert_eq!(state.pending.len(), 1);

        let mut saw_drop = false;
        for tick in 0..=MAX_REGISTER_ATTEMPTS {
            let report = archive_recent_once(
                &mut gatherer,
                SLOT,
                RECENT_DIR,
                &reader,
                &store,
                &register,
                &mut state,
                30 + i64::from(tick),
            )
            .expect("drain cycle should continue");
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
