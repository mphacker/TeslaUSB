//! Phase-1 `RecentClips` archive driver (mount-free).
//!
//! Inventory comes from injected candidates, and source bytes are copied by the
//! injected `ArchiveStore` (live store uses `ReadFile`).

use std::collections::{HashMap, HashSet, VecDeque};
use std::fmt::Write as _;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use crate::archive::ArchiveStore;
use crate::candidates::{Candidate, CandidateSource};
use crate::durability::write_json_durable;
use crate::io::ContentHash;
use crate::probe::{ArchivePlayability, UnplayableReason};
use crate::register_client::{
    ArchiveAngleRef, ArchiveItemRef, ArchiveRegistration, RegisterClient, RegisterError,
};

/// Maximum number of register attempts for one canonical key before dropping it.
pub const MAX_REGISTER_ATTEMPTS: u32 = 5;
/// Maximum number of pending register payloads held in memory.
pub const MAX_PENDING: usize = 256;
const PRUNE_MIN_MISSED_SCANS: u32 = 40;
const PRUNE_GRACE_SECS: i64 = 3600;
const PRUNE_EVERY_CYCLES: u32 = 5;
const PRUNE_MAX_DELETIONS_PER_CYCLE: usize = 16;
const STATE_SCHEMA: u32 = 1;
const MARKER_SCHEMA: u32 = 1;
const MARKER_DIR: &str = ".retentiond/markers";
const OUTBOX_FILE: &str = ".retentiond/register-outbox.json";
const STAGING_DIR: &str = ".retentiond/staging";

/// One queued register payload awaiting retry.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct PendingRegistration {
    reg: ArchiveRegistration,
    attempts: u32,
    disposition: RegistrationDisposition,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
enum RegistrationDisposition {
    Live,
    Quarantine,
}

/// Stateful cross-cycle data for the archive driver.
#[derive(Debug, Default)]
pub struct DriverState {
    pending: VecDeque<PendingRegistration>,
    archive_root: Option<PathBuf>,
    outbox_loaded: bool,
    markers: HashMap<String, MarkerSummary>,
    markers_loaded: bool,
    prune_cycle_counter: u32,
}

impl DriverState {
    /// Construct empty driver state.
    #[must_use]
    pub fn new() -> Self {
        Self {
            pending: VecDeque::new(),
            archive_root: None,
            outbox_loaded: false,
            markers: HashMap::new(),
            markers_loaded: false,
            prune_cycle_counter: 0,
        }
    }

    /// Construct state with a durable archive root for marker/outbox persistence.
    #[must_use]
    pub fn with_archive_root(archive_root: impl Into<PathBuf>) -> Self {
        Self {
            pending: VecDeque::new(),
            archive_root: Some(archive_root.into()),
            outbox_loaded: false,
            markers: HashMap::new(),
            markers_loaded: false,
            prune_cycle_counter: 0,
        }
    }

    /// Update the durable archive root used for marker/outbox persistence.
    pub fn set_archive_root(&mut self, archive_root: impl Into<PathBuf>) {
        self.archive_root = Some(archive_root.into());
        self.outbox_loaded = false;
        self.markers_loaded = false;
        self.markers.clear();
        self.prune_cycle_counter = 0;
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
struct PersistedOutbox {
    schema: u32,
    pending: Vec<PendingRegistration>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
enum MarkerStatus {
    CompleteLive,
    Quarantined,
    Partial,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
struct MarkerAngle {
    camera: String,
    file_ref: String,
    valid_data_length: u64,
    set_checksum_ok: bool,
    destination_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
struct ClipMarker {
    schema: u32,
    canonical_key: String,
    source_fingerprint: String,
    volume_serial: u32,
    partition: String,
    status: MarkerStatus,
    updated_at: i64,
    angles: Vec<MarkerAngle>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct MarkerSummary {
    source_fingerprint: String,
    status: MarkerStatus,
    last_seen_epoch: i64,
    missed_scans: u32,
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
    /// Count of registrations indexd rejected deterministically (not retried).
    pub register_rejected: usize,
    /// Count of copied candidates quarantined as undecodable.
    pub quarantined_undecodable: usize,
    /// Count of observed candidates skipped because their key is already pending.
    pub skipped_already_pending: usize,
    /// Count of candidates skipped because their key was deterministically rejected.
    pub skipped_rejected: usize,
    /// Count of pending items dropped (poison or queue-bound eviction).
    pub dropped_poison: usize,
    /// Pending queue length at cycle end.
    pub pending_len: usize,
    /// Count of marker files pruned this cycle.
    pub pruned_markers: usize,
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
    archive_recent_capped(
        candidates,
        store,
        register,
        state,
        now_epoch_s,
        None,
        false,
        &mut || {},
    )
}

/// Run one non-destructive archive cycle with an optional per-cycle copy cap.
///
/// `max_copies` limits how many candidates that enter the copy phase are
/// processed in this cycle; `None` keeps the cycle unbounded. `on_progress` is
/// invoked once after each processed candidate that entered copy work.
///
/// # Errors
///
/// Returns candidate-inventory failures from [`CandidateSource::list_candidates`].
#[allow(clippy::too_many_lines, clippy::too_many_arguments)]
pub fn archive_recent_capped(
    candidates: &dyn CandidateSource,
    store: &dyn ArchiveStore,
    register: &dyn RegisterClient,
    state: &mut DriverState,
    now_epoch_s: i64,
    max_copies: Option<usize>,
    prune_enabled: bool,
    on_progress: &mut dyn FnMut(),
) -> io::Result<CycleReport> {
    let mut report = CycleReport::default();
    load_markers_if_needed(state);
    load_outbox_if_needed(state);
    drain_pending(register, state, &mut report);

    let clips = candidates.list_candidates()?;
    report.observed = clips.len();
    refresh_marker_scan_state(state, &clips, now_epoch_s);
    state.prune_cycle_counter = state.prune_cycle_counter.saturating_add(1);
    if prune_enabled && state.prune_cycle_counter % PRUNE_EVERY_CYCLES == 0 {
        report.pruned_markers = prune_markers(state, now_epoch_s);
    }
    let mut copies_done: usize = 0;

    for candidate in clips {
        if max_copies.is_some_and(|max| copies_done >= max) {
            break;
        }
        if state
            .pending
            .iter()
            .any(|pending| pending.reg.canonical_key == candidate.canonical_key)
        {
            report.skipped_already_pending = report.skipped_already_pending.saturating_add(1);
            continue;
        }

        if marker_is_complete_live(state, &candidate) {
            continue;
        }

        // Defensive: never stage/register a candidate with no angles — it would
        // otherwise be marked CompleteLive as an empty (zero-file) archive item.
        if candidate.angles.is_empty() {
            report.copy_failed = report.copy_failed.saturating_add(1);
            continue;
        }

        if let Some(archive_item_path) = archive_item_path_for_candidate(&candidate) {
            let mut staged_angles = Vec::with_capacity(candidate.angles.len());
            let mut marker_angles = Vec::with_capacity(candidate.angles.len());
            let mut segment_size_bytes = 0_i64;
            let mut copy_failed = false;

            for angle in &candidate.angles {
                let file_name = basename(&angle.file_ref);
                let final_rel = format!("{archive_item_path}/{file_name}");
                let staging_rel = format!("{STAGING_DIR}/{archive_item_path}/{file_name}");
                let Ok(copy_hash) = store.copy_and_hash_dest(&angle.file_ref, &staging_rel) else {
                    report.copy_failed = report.copy_failed.saturating_add(1);
                    copy_failed = true;
                    break;
                };

                let size_bytes = u64_to_i64_saturating(angle.size_bytes);
                segment_size_bytes = segment_size_bytes.saturating_add(size_bytes);
                staged_angles.push(ArchiveAngleRef {
                    camera: angle.camera.clone(),
                    file_ref: final_rel.clone(),
                    offset_ms: angle.offset_ms,
                    duration_s: angle.duration_s,
                    size_bytes,
                });
                marker_angles.push(MarkerAngle {
                    camera: angle.camera.clone(),
                    file_ref: final_rel,
                    valid_data_length: angle.size_bytes,
                    set_checksum_ok: true,
                    destination_sha256: hash_hex(copy_hash),
                });
            }

            if copy_failed {
                discard_staged_files(store, &staged_angles, 0);
                write_marker(
                    state,
                    &candidate,
                    MarkerStatus::Partial,
                    marker_angles,
                    now_epoch_s,
                );
            } else {
                let mut promoted_angles = Vec::with_capacity(staged_angles.len());
                let mut promote_failed_at = None;
                for (idx, staged) in staged_angles.iter().enumerate() {
                    let staging_rel = format!("{STAGING_DIR}/{}", staged.file_ref);
                    if store.promote_dest(&staging_rel, &staged.file_ref).is_err() {
                        report.copy_failed = report.copy_failed.saturating_add(1);
                        promote_failed_at = Some(idx);
                        break;
                    }
                    promoted_angles.push(staged.clone());
                }
                if let Some(failed_idx) = promote_failed_at {
                    discard_staged_files(store, &staged_angles, failed_idx);
                    write_marker(
                        state,
                        &candidate,
                        MarkerStatus::Partial,
                        marker_angles,
                        now_epoch_s,
                    );
                } else {
                    remove_empty_staging_dirs_best_effort(state, &archive_item_path);
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
                            file_count: usize_to_i64_saturating(promoted_angles.len()),
                            archived_at: now_epoch_s,
                        },
                        angles: promoted_angles,
                    };

                    finalize_registration(
                        store,
                        register,
                        reg,
                        state,
                        &mut report,
                        RegistrationContext {
                            candidate: &candidate,
                            marker_angles,
                            now_epoch_s,
                        },
                    );
                }
            }
        } else {
            report.copy_failed = report.copy_failed.saturating_add(1);
        }

        copies_done = copies_done.saturating_add(1);
        on_progress();
        if max_copies.is_some_and(|max| copies_done >= max) {
            break;
        }
    }

    report.pending_len = state.pending.len();
    Ok(report)
}

/// Probe a copied candidate and register it (or defer/quarantine/reject).
///
/// Deterministic indexd rejections ([`RegisterError::Rejected`]) are logged and
/// counted as `register_rejected` without being enqueued for retry; transient
/// failures are deferred to the pending queue.
struct RegistrationContext<'a> {
    candidate: &'a Candidate,
    marker_angles: Vec<MarkerAngle>,
    now_epoch_s: i64,
}

fn finalize_registration(
    store: &dyn ArchiveStore,
    register: &dyn RegisterClient,
    reg: ArchiveRegistration,
    state: &mut DriverState,
    report: &mut CycleReport,
    registration: RegistrationContext<'_>,
) {
    let probe_failures = collect_probe_failures(store, &reg.angles);
    if probe_failures.is_empty() {
        if let Err(err) = stage_outbox_registration(state, &reg, RegistrationDisposition::Live) {
            log_outbox_stage_failure(&reg.canonical_key, &err);
            report.copy_failed = report.copy_failed.saturating_add(1);
            return;
        }
        write_marker(
            state,
            registration.candidate,
            MarkerStatus::CompleteLive,
            registration.marker_angles,
            registration.now_epoch_s,
        );
        match register.register(&reg) {
            Ok(_) => {
                persist_outbox(state);
                report.registered = report.registered.saturating_add(1);
            }
            Err(RegisterError::Rejected { message }) => {
                persist_outbox(state);
                log_register_rejected_warning(&reg.canonical_key, &message);
                report.register_rejected = report.register_rejected.saturating_add(1);
            }
            Err(_) => {
                report.register_deferred = report.register_deferred.saturating_add(1);
                enqueue_pending(reg, RegistrationDisposition::Live, state, report);
            }
        }
    } else {
        let failure_detail = probe_failures
            .iter()
            .map(ProbeFailure::to_log_fragment)
            .collect::<Vec<_>>()
            .join(",");
        log_quarantine_warning(&reg.canonical_key, &failure_detail);
        report.quarantined_undecodable = report.quarantined_undecodable.saturating_add(1);
        if let Err(err) = stage_outbox_registration(state, &reg, RegistrationDisposition::Quarantine)
        {
            log_outbox_stage_failure(&reg.canonical_key, &err);
            report.copy_failed = report.copy_failed.saturating_add(1);
            return;
        }
        write_marker(
            state,
            registration.candidate,
            MarkerStatus::Quarantined,
            registration.marker_angles,
            registration.now_epoch_s,
        );
        match register.register_quarantined(&reg) {
            Ok(_) => {
                persist_outbox(state);
            }
            Err(RegisterError::Rejected { message }) => {
                persist_outbox(state);
                log_register_rejected_warning(&reg.canonical_key, &message);
                report.register_rejected = report.register_rejected.saturating_add(1);
            }
            Err(_) => {
                report.register_deferred = report.register_deferred.saturating_add(1);
                enqueue_pending(reg, RegistrationDisposition::Quarantine, state, report);
            }
        }
    }
}

fn archive_item_path_for_candidate(candidate: &Candidate) -> Option<String> {
    let timestamp = candidate.canonical_key.rsplit('/').next()?;
    if timestamp.len() < 10 {
        return None;
    }
    let date = timestamp.get(..10)?;
    Some(format!("RecentClips/{date}/{timestamp}"))
}

fn refresh_marker_scan_state(state: &mut DriverState, clips: &[Candidate], now_epoch_s: i64) {
    if state.markers.is_empty() {
        return;
    }
    let observed_keys: HashSet<&str> = clips.iter().map(|clip| clip.canonical_key.as_str()).collect();
    for (canonical_key, marker) in &mut state.markers {
        if observed_keys.contains(canonical_key.as_str()) {
            marker.missed_scans = 0;
            marker.last_seen_epoch = now_epoch_s;
        } else {
            marker.missed_scans = marker.missed_scans.saturating_add(1);
        }
    }
}

fn prune_markers(state: &mut DriverState, now_epoch_s: i64) -> usize {
    let Some(root) = state.archive_root.as_ref() else {
        return 0;
    };
    let to_prune: Vec<String> = state
        .markers
        .iter()
        .filter(|(_, marker)| {
            marker.missed_scans >= PRUNE_MIN_MISSED_SCANS
                && now_epoch_s.saturating_sub(marker.last_seen_epoch) >= PRUNE_GRACE_SECS
        })
        .map(|(key, _)| key.clone())
        .take(PRUNE_MAX_DELETIONS_PER_CYCLE)
        .collect();
    let mut pruned = 0_usize;
    for key in &to_prune {
        let marker_file = marker_path(root, key);
        match fs::remove_file(&marker_file) {
            Ok(()) => {
                state.markers.remove(key);
                pruned = pruned.saturating_add(1);
            }
            Err(err) if err.kind() == io::ErrorKind::NotFound => {
                // Already absent: index and disk now agree.
                state.markers.remove(key);
                pruned = pruned.saturating_add(1);
            }
            Err(err) => {
                // Deletion failed and the file still exists: keep the index entry
                // so the in-memory map never claims a marker is gone while it is
                // still on disk. It is re-evaluated next prune pass (and rebuilt
                // from disk on restart).
                let mut stderr = io::stderr();
                let _ = writeln!(
                    &mut stderr,
                    "retentiond archive_recent_only: failed to prune marker {}: {err}",
                    marker_file.display()
                );
            }
        }
    }
    pruned
}

fn discard_staged_files(store: &dyn ArchiveStore, staged: &[ArchiveAngleRef], start_idx: usize) {
    for angle in staged.iter().skip(start_idx) {
        let staging_rel = format!("{STAGING_DIR}/{}", angle.file_ref);
        let _ = store.remove_dest(&staging_rel);
    }
}

fn remove_empty_staging_dirs_best_effort(state: &DriverState, archive_item_path: &str) {
    let Some(root) = state.archive_root.as_ref() else {
        return;
    };
    let mut current = root.join(STAGING_DIR).join(archive_item_path);
    let staging_root = root.join(STAGING_DIR);
    while let Ok(()) = fs::remove_dir(&current) {
        if current == staging_root {
            break;
        }
        if !current.pop() {
            break;
        }
    }
}

fn drain_pending(register: &dyn RegisterClient, state: &mut DriverState, report: &mut CycleReport) {
    let mut retained = VecDeque::with_capacity(state.pending.len());
    for mut pending in std::mem::take(&mut state.pending) {
        match send_registration(register, pending.disposition, &pending.reg) {
            Ok(_) => {
                report.registered_from_pending = report.registered_from_pending.saturating_add(1);
                continue;
            }
            Err(RegisterError::Rejected { message }) => {
                log_register_rejected_warning(&pending.reg.canonical_key, &message);
                report.register_rejected = report.register_rejected.saturating_add(1);
                continue;
            }
            Err(_) => {
                pending.attempts = pending.attempts.saturating_add(1);
                // Quarantine pendings fail closed: retain until indexd accepts the
                // quarantined-register verb. The queue bound remains MAX_PENDING.
                if pending.attempts >= MAX_REGISTER_ATTEMPTS
                    && pending.disposition == RegistrationDisposition::Live
                {
                    report.dropped_poison = report.dropped_poison.saturating_add(1);
                    continue;
                }
            }
        }
        retained.push_back(pending);
    }
    state.pending = retained;
    persist_outbox(state);
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
    persist_outbox(state);
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

fn load_outbox_if_needed(state: &mut DriverState) {
    if state.outbox_loaded {
        return;
    }
    state.outbox_loaded = true;
    let Some(root) = state.archive_root.as_ref() else {
        return;
    };
    let path = root.join(OUTBOX_FILE);
    let raw = match fs::read_to_string(&path) {
        Ok(raw) => raw,
        Err(err) if err.kind() == io::ErrorKind::NotFound => return,
        Err(err) => {
            let mut stderr = io::stderr();
            let _ = writeln!(
                &mut stderr,
                "retentiond archive_recent_only: failed to read durable outbox {}: {err}",
                path.display()
            );
            return;
        }
    };
    match serde_json::from_str::<PersistedOutbox>(&raw) {
        Ok(persisted) if persisted.schema == STATE_SCHEMA => {
            state.pending = persisted.pending.into_iter().take(MAX_PENDING).collect();
        }
        Ok(_) => {
            let mut stderr = io::stderr();
            let _ = writeln!(
                &mut stderr,
                "retentiond archive_recent_only: ignoring outbox with schema mismatch at {}",
                path.display()
            );
        }
        Err(err) => {
            let mut stderr = io::stderr();
            let _ = writeln!(
                &mut stderr,
                "retentiond archive_recent_only: failed to decode durable outbox {}: {err}",
                path.display()
            );
        }
    }
}

fn load_markers_if_needed(state: &mut DriverState) {
    if state.markers_loaded {
        return;
    }
    state.markers_loaded = true;
    let Some(root) = state.archive_root.as_ref() else {
        return;
    };
    let _ = fs::remove_dir_all(root.join(STAGING_DIR));

    let marker_dir = root.join(MARKER_DIR);
    let entries = match fs::read_dir(&marker_dir) {
        Ok(entries) => entries,
        Err(err) if err.kind() == io::ErrorKind::NotFound => return,
        Err(err) => {
            let mut stderr = io::stderr();
            let _ = writeln!(
                &mut stderr,
                "retentiond archive_recent_only: failed to scan marker directory {}: {err}",
                marker_dir.display()
            );
            return;
        }
    };

    for entry in entries {
        let path = match entry {
            Ok(entry) => entry.path(),
            Err(err) => {
                let mut stderr = io::stderr();
                let _ = writeln!(
                    &mut stderr,
                    "retentiond archive_recent_only: failed to enumerate marker directory {}: {err}",
                    marker_dir.display()
                );
                continue;
            }
        };
        if path.extension().and_then(|ext| ext.to_str()) != Some("json") {
            continue;
        }
        let raw = match fs::read_to_string(&path) {
            Ok(raw) => raw,
            Err(err) => {
                let mut stderr = io::stderr();
                let _ = writeln!(
                    &mut stderr,
                    "retentiond archive_recent_only: failed to read marker {}: {err}",
                    path.display()
                );
                continue;
            }
        };
        let Ok(marker) = serde_json::from_str::<ClipMarker>(&raw) else {
            continue;
        };
        if marker.schema != MARKER_SCHEMA {
            continue;
        }
        // Defense-in-depth: trust a marker's `canonical_key` only when the file
        // name matches the canonical `stable_hex(canonical_key).json` we would
        // have written. This rejects a stray/duplicate/tampered marker whose
        // body claims a different clip, which could otherwise suppress a real
        // copy via the in-memory dedup index.
        let expected_stem = stable_hex(marker.canonical_key.as_bytes());
        if path.file_stem().and_then(|stem| stem.to_str()) != Some(expected_stem.as_str()) {
            let mut stderr = io::stderr();
            let _ = writeln!(
                &mut stderr,
                "retentiond archive_recent_only: ignoring off-path marker {} (key/name mismatch)",
                path.display()
            );
            continue;
        }
        state.markers.insert(
            marker.canonical_key,
            MarkerSummary {
                source_fingerprint: marker.source_fingerprint,
                status: marker.status,
                last_seen_epoch: marker.updated_at,
                missed_scans: 0,
            },
        );
    }
}

fn persist_outbox(state: &DriverState) {
    let Some(root) = state.archive_root.as_ref() else {
        return;
    };
    let path = root.join(OUTBOX_FILE);
    let persisted = PersistedOutbox {
        schema: STATE_SCHEMA,
        pending: state.pending.iter().cloned().collect(),
    };
    if let Err(err) = write_json_durable(&path, &persisted) {
        let mut stderr = io::stderr();
        let _ = writeln!(
            &mut stderr,
            "retentiond archive_recent_only: failed to persist durable outbox {}: {err}",
            path.display()
        );
    }
}

fn stage_outbox_registration(
    state: &DriverState,
    reg: &ArchiveRegistration,
    disposition: RegistrationDisposition,
) -> io::Result<()> {
    let Some(root) = state.archive_root.as_ref() else {
        return Ok(());
    };
    let path = root.join(OUTBOX_FILE);
    let mut pending: Vec<PendingRegistration> = state.pending.iter().cloned().collect();
    if !pending
        .iter()
        .any(|entry| entry.reg.canonical_key == reg.canonical_key)
    {
        pending.push(PendingRegistration {
            reg: reg.clone(),
            attempts: 0,
            disposition,
        });
    }
    let persisted = PersistedOutbox {
        schema: STATE_SCHEMA,
        pending,
    };
    write_json_durable(&path, &persisted)
}

fn marker_is_complete_live(state: &DriverState, candidate: &Candidate) -> bool {
    let Some(marker) = state.markers.get(&candidate.canonical_key) else {
        return false;
    };
    marker.status == MarkerStatus::CompleteLive
        && marker.source_fingerprint == candidate.source_fingerprint
}

#[cfg(test)]
fn read_marker(state: &DriverState, candidate: &Candidate) -> Option<ClipMarker> {
    let root = state.archive_root.as_ref()?;
    let path = marker_path(root, &candidate.canonical_key);
    let raw = fs::read_to_string(path).ok()?;
    let marker: ClipMarker = serde_json::from_str(&raw).ok()?;
    if marker.schema != MARKER_SCHEMA || marker.canonical_key != candidate.canonical_key {
        return None;
    }
    Some(marker)
}

fn write_marker(
    state: &mut DriverState,
    candidate: &Candidate,
    status: MarkerStatus,
    angles: Vec<MarkerAngle>,
    now_epoch_s: i64,
) {
    let Some(root) = state.archive_root.as_ref() else {
        return;
    };
    let path = marker_path(root, &candidate.canonical_key);
    let marker = ClipMarker {
        schema: MARKER_SCHEMA,
        canonical_key: candidate.canonical_key.clone(),
        source_fingerprint: candidate.source_fingerprint.clone(),
        volume_serial: candidate.source_volume_serial,
        partition: candidate.partition.clone(),
        status,
        updated_at: now_epoch_s,
        angles,
    };
    if let Err(err) = write_json_durable(&path, &marker) {
        let mut stderr = io::stderr();
        let _ = writeln!(
            &mut stderr,
            "retentiond archive_recent_only: failed to persist marker {}: {err}",
            path.display()
        );
        return;
    }
    state.markers.insert(
        candidate.canonical_key.clone(),
        MarkerSummary {
            source_fingerprint: candidate.source_fingerprint.clone(),
            status,
            last_seen_epoch: now_epoch_s,
            missed_scans: 0,
        },
    );
}

fn marker_path(root: &Path, canonical_key: &str) -> PathBuf {
    let file = format!("{}.json", stable_hex(canonical_key.as_bytes()));
    root.join(MARKER_DIR).join(file)
}

fn stable_hex(bytes: &[u8]) -> String {
    let mut hash = 0xcbf2_9ce4_8422_2325_u64;
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{hash:016x}")
}

fn hash_hex(hash: ContentHash) -> String {
    let mut out = String::with_capacity(hash.0.len().saturating_mul(2));
    for byte in hash.0 {
        let _ = write!(&mut out, "{byte:02x}");
    }
    out
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

fn log_register_rejected_warning(canonical_key: &str, reason: &str) {
    let mut stderr = io::stderr();
    let _ = writeln!(
        &mut stderr,
        "retentiond register_rejected key={canonical_key} reason={reason}"
    );
}

fn log_outbox_stage_failure(canonical_key: &str, err: &io::Error) {
    let mut stderr = io::stderr();
    let _ = writeln!(
        &mut stderr,
        "retentiond archive_recent_only: failed to stage durable outbox key={canonical_key}: {err}"
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
        fs,
        io,
        path::PathBuf,
        sync::atomic::{AtomicU64, Ordering},
    };

    use crate::{
        archive::ArchiveStore,
        candidates::{Candidate, CandidateAngle, CandidateSource},
        durability::write_json_durable,
        io::{ContentHash, FileIdentity},
        probe::{ArchivePlayability, UnplayableReason},
        register_client::{ArchiveRegistration, RegisterClient, RegisterError, RegistrationOk},
    };

    use super::{
        ClipMarker, CycleReport, DriverState, MARKER_SCHEMA, MAX_REGISTER_ATTEMPTS, MarkerAngle,
        MarkerStatus, MarkerSummary, OUTBOX_FILE, PRUNE_EVERY_CYCLES, PRUNE_GRACE_SECS,
        PRUNE_MIN_MISSED_SCANS, PersistedOutbox, RegistrationDisposition, archive_recent_capped,
        archive_recent_once, basename, marker_path, read_marker,
    };

    const KEY: &str = "0:TeslaCam/RecentClips/2026-06-19_10-00-00";
    const PATH: &str = "RecentClips/2026-06-19/2026-06-19_10-00-00";
    static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn new_archive_root() -> PathBuf {
        let unique = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::current_dir()
            .expect("cwd")
            .join(format!("retentiond-archive-driver-test-{}-{unique}", std::process::id()));
        fs::create_dir_all(&dir).expect("create archive root");
        dir
    }

    #[derive(Debug, Clone, PartialEq, Eq)]
    enum RegisterFailure {
        Io(&'static str),
        Server(&'static str),
        Rejected(&'static str),
    }

    fn sample_candidate() -> Candidate {
        Candidate {
            clip_id: 1,
            canonical_key: KEY.to_owned(),
            partition: "slot0".to_owned(),
            started_at: 1_700_000_000,
            ended_at: 1_700_000_060,
            duration_s: Some(60),
            source_volume_serial: 0x1234_5678,
            source_fingerprint: "test-fingerprint-a".to_owned(),
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

    fn unique_candidate(id: usize, started_at: i64) -> Candidate {
        let mut candidate = sample_candidate();
        let clip_id = i64::try_from(id).expect("id fits in i64");
        let timestamp = format!("2026-06-19_10-00-{id:02}");
        candidate.clip_id = clip_id;
        candidate.canonical_key = format!("0:TeslaCam/RecentClips/{timestamp}");
        candidate.started_at = started_at;
        candidate.ended_at = started_at.saturating_add(60);
        candidate.source_fingerprint = format!("test-fingerprint-{id}");
        for angle in &mut candidate.angles {
            angle.file_ref = format!("TeslaCam/RecentClips/{timestamp}-{}-{id}.mp4", angle.camera);
        }
        candidate
    }

    fn replacement_candidate_with_fingerprint(candidate: &Candidate, fingerprint: &str) -> Candidate {
        let mut replacement = candidate.clone();
        replacement.source_fingerprint = fingerprint.to_owned();
        replacement
    }

    fn write_test_marker(
        archive_root: &std::path::Path,
        candidate: &Candidate,
        status: MarkerStatus,
        updated_at: i64,
        schema: u32,
    ) {
        let marker = ClipMarker {
            schema,
            canonical_key: candidate.canonical_key.clone(),
            source_fingerprint: candidate.source_fingerprint.clone(),
            volume_serial: candidate.source_volume_serial,
            partition: candidate.partition.clone(),
            status,
            updated_at,
            angles: vec![MarkerAngle {
                camera: "front".to_owned(),
                file_ref: format!("{PATH}/2026-06-19_10-00-00-front.mp4"),
                valid_data_length: 10,
                set_checksum_ok: true,
                destination_sha256: "00".repeat(32),
            }],
        };
        let marker_file = marker_path(archive_root, &candidate.canonical_key);
        write_json_durable(&marker_file, &marker).expect("write test marker");
    }

    fn final_front_path() -> String {
        "RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-front.mp4".to_owned()
    }

    fn final_back_path() -> String {
        "RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-back.mp4".to_owned()
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
        promotions: RefCell<Vec<(String, String)>>,
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

        fn promote_dest(&self, staging_rel: &str, final_rel: &str) -> io::Result<()> {
            let mut landed = self.landed.borrow_mut();
            if !landed.remove(staging_rel) {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    format!("staging file not found: {staging_rel}"),
                ));
            }
            landed.insert(final_rel.to_owned());
            self.promotions
                .borrow_mut()
                .push((staging_rel.to_owned(), final_rel.to_owned()));
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
        live_outcomes: RefCell<VecDeque<Option<RegisterFailure>>>,
        quarantine_outcomes: RefCell<VecDeque<Option<RegisterFailure>>>,
        always_fail_live: Cell<bool>,
        always_fail_quarantine: Cell<bool>,
    }

    impl FakeRegister {
        fn with_live_failures(failures: Vec<bool>) -> Self {
            Self {
                live_calls: RefCell::new(Vec::new()),
                quarantine_calls: RefCell::new(Vec::new()),
                live_outcomes: RefCell::new(
                    failures
                        .into_iter()
                        .map(|fail| fail.then_some(RegisterFailure::Io("register failed")))
                        .collect(),
                ),
                quarantine_outcomes: RefCell::new(VecDeque::new()),
                always_fail_live: Cell::new(false),
                always_fail_quarantine: Cell::new(false),
            }
        }

        fn with_quarantine_failures(failures: Vec<bool>) -> Self {
            Self {
                live_calls: RefCell::new(Vec::new()),
                quarantine_calls: RefCell::new(Vec::new()),
                live_outcomes: RefCell::new(VecDeque::new()),
                quarantine_outcomes: RefCell::new(
                    failures
                        .into_iter()
                        .map(|fail| {
                            fail.then_some(RegisterFailure::Io("quarantine register failed"))
                        })
                        .collect(),
                ),
                always_fail_live: Cell::new(false),
                always_fail_quarantine: Cell::new(false),
            }
        }

        fn with_live_rejection(message: &'static str) -> Self {
            Self {
                live_calls: RefCell::new(Vec::new()),
                quarantine_calls: RefCell::new(Vec::new()),
                live_outcomes: RefCell::new(VecDeque::from([Some(RegisterFailure::Rejected(
                    message,
                ))])),
                quarantine_outcomes: RefCell::new(VecDeque::new()),
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
            let outcome = {
                let mut outcomes = self.live_outcomes.borrow_mut();
                if let Some(next) = outcomes.pop_front() {
                    next
                } else if self.always_fail_live.get() {
                    Some(RegisterFailure::Io("register failed"))
                } else {
                    None
                }
            };
            match outcome {
                Some(RegisterFailure::Io(message)) => {
                    Err(RegisterError::Io(io::Error::other(message)))
                }
                Some(RegisterFailure::Server(message)) => Err(RegisterError::Server {
                    message: message.to_owned(),
                }),
                Some(RegisterFailure::Rejected(message)) => Err(RegisterError::Rejected {
                    message: message.to_owned(),
                }),
                None => Ok(RegistrationOk {
                    clip_id: 1,
                    archive_item_id: 1,
                }),
            }
        }

        fn register_quarantined(
            &self,
            reg: &ArchiveRegistration,
        ) -> Result<RegistrationOk, RegisterError> {
            self.quarantine_calls.borrow_mut().push(reg.clone());
            let outcome = {
                let mut outcomes = self.quarantine_outcomes.borrow_mut();
                if let Some(next) = outcomes.pop_front() {
                    next
                } else if self.always_fail_quarantine.get() {
                    Some(RegisterFailure::Io("quarantine register failed"))
                } else {
                    None
                }
            };
            match outcome {
                Some(RegisterFailure::Io(message)) => {
                    Err(RegisterError::Io(io::Error::other(message)))
                }
                Some(RegisterFailure::Server(message)) => Err(RegisterError::Server {
                    message: message.to_owned(),
                }),
                Some(RegisterFailure::Rejected(message)) => Err(RegisterError::Rejected {
                    message: message.to_owned(),
                }),
                None => Ok(RegistrationOk {
                    clip_id: 1,
                    archive_item_id: 1,
                }),
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
            ".retentiond/staging/RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-front.mp4"
        );
        assert_eq!(
            copies[1].1,
            ".retentiond/staging/RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-back.mp4"
        );
        assert_eq!(store.promotions.borrow().len(), 2);

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
    fn copy_failure_after_first_angle_discards_staged_dest_and_retries_cleanly() {
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
                ".retentiond/staging/RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-front.mp4"
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
    fn deterministic_rejection_is_not_deferred_or_pending() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        let register = FakeRegister::with_live_rejection("invalid camera: left");
        let mut state = DriverState::new();

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(report.registered, 0);
        assert_eq!(report.register_rejected, 1);
        assert_eq!(report.register_deferred, 0);
        assert_eq!(report.dropped_poison, 0);
        assert_eq!(report.pending_len, 0);
        assert!(state.pending.is_empty());
    }

    #[test]
    fn complete_live_marker_suppresses_recopy_after_rejected_registration() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        let register = FakeRegister::with_live_rejection("invalid camera: left");
        let archive_root = new_archive_root();
        let mut state = DriverState::with_archive_root(&archive_root);

        let first = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(first.register_rejected, 1);
        assert_eq!(store.copies.borrow().len(), 2);
        let live_calls_after_first = register.live_calls.borrow().len();

        let second = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(second.skipped_rejected, 0);
        assert_eq!(second.register_rejected, 0);
        assert_eq!(register.live_calls.borrow().len(), live_calls_after_first);
        assert_eq!(store.copies.borrow().len(), 2);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn rejected_registration_keeps_landed_bytes() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        let register = FakeRegister::with_live_rejection("invalid camera: left");
        let mut state = DriverState::new();

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(report.register_rejected, 1);
        assert!(store.removed.borrow().is_empty());
        assert!(
            !store.landed.borrow().is_empty(),
            "register rejection must not roll back archived bytes"
        );
    }

    #[test]
    fn deterministic_rejection_from_pending_drops_without_poison() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        let register = FakeRegister::with_live_failures(vec![true]);
        let mut state = DriverState::new();

        let first = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(first.register_deferred, 1);
        assert_eq!(first.pending_len, 1);

        register
            .live_outcomes
            .borrow_mut()
            .push_back(Some(RegisterFailure::Rejected("invalid camera: left")));
        candidates.set(Vec::new());
        let second = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(second.register_rejected, 1);
        assert_eq!(second.register_deferred, 0);
        assert_eq!(second.dropped_poison, 0);
        assert_eq!(second.pending_len, 0);
        assert!(state.pending.is_empty());
    }

    #[test]
    fn operational_server_error_is_deferred_not_rejected() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        let register = FakeRegister {
            live_calls: RefCell::new(Vec::new()),
            quarantine_calls: RefCell::new(Vec::new()),
            live_outcomes: RefCell::new(VecDeque::from([Some(RegisterFailure::Server(
                "index database mutex is poisoned",
            ))])),
            quarantine_outcomes: RefCell::new(VecDeque::new()),
            always_fail_live: Cell::new(false),
            always_fail_quarantine: Cell::new(false),
        };
        let mut state = DriverState::new();

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(report.register_deferred, 1);
        assert_eq!(report.pending_len, 1);
        assert_eq!(report.register_rejected, 0);
        assert_eq!(report.dropped_poison, 0);
        assert_eq!(state.pending.len(), 1);
    }

    #[test]
    fn operational_server_error_does_not_tombstone() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        let register = FakeRegister {
            live_calls: RefCell::new(Vec::new()),
            quarantine_calls: RefCell::new(Vec::new()),
            live_outcomes: RefCell::new(VecDeque::from([Some(RegisterFailure::Server(
                "index database mutex is poisoned",
            ))])),
            quarantine_outcomes: RefCell::new(VecDeque::new()),
            always_fail_live: Cell::new(false),
            always_fail_quarantine: Cell::new(false),
        };
        let mut state = DriverState::new();

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(report.register_deferred, 1);
        assert_eq!(report.pending_len, 1);
        assert_eq!(report.skipped_rejected, 0);
    }

    #[test]
    fn operational_server_error_on_pending_is_retained() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![sample_candidate()]);
        let store = FakeStore::default();
        let register = FakeRegister::with_live_failures(vec![true]);
        let mut state = DriverState::new();

        let first = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(first.register_deferred, 1);
        assert_eq!(first.pending_len, 1);

        register
            .live_outcomes
            .borrow_mut()
            .push_back(Some(RegisterFailure::Server("database is busy")));
        candidates.set(Vec::new());
        let second = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(second.register_rejected, 0);
        assert_eq!(second.pending_len, 1);
        assert_eq!(state.pending.len(), 1);
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
    fn probe_failure_writes_quarantined_marker_and_retries_copy() {
        let candidates = FakeCandidates::default();
        let candidate = sample_candidate();
        candidates.set(vec![candidate.clone()]);
        let store = FakeStore::default();
        store.set_unplayable(
            "RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-back.mp4",
            UnplayableReason::NoMoov,
        );
        let register = FakeRegister::default();
        let archive_root = new_archive_root();
        let mut state = DriverState::with_archive_root(&archive_root);

        let first = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(first.quarantined_undecodable, 1);
        let marker = read_marker(&state, &candidate).expect("quarantined marker");
        assert_eq!(marker.status, MarkerStatus::Quarantined);

        let copies_after_first = store.copies.borrow().len();
        let second = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(second.quarantined_undecodable, 1);
        assert!(
            store.copies.borrow().len() > copies_after_first,
            "quarantined marker must not suppress retry copy"
        );
        let marker2 = read_marker(&state, &candidate).expect("quarantined marker");
        assert_ne!(marker2.status, MarkerStatus::CompleteLive);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn dedup_marker_is_content_addressed_not_size_only() {
        let candidates = FakeCandidates::default();
        let mut first_candidate = sample_candidate();
        first_candidate.source_fingerprint = "fingerprint-a".to_owned();
        candidates.set(vec![first_candidate.clone()]);

        let store = FakeStore::default();
        let register = FakeRegister::default();
        let archive_root = new_archive_root();
        let mut state = DriverState::with_archive_root(&archive_root);

        let first = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(first.registered, 1);
        assert_eq!(store.copies.borrow().len(), 2);

        let second = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(second.registered, 0);
        assert_eq!(store.copies.borrow().len(), 2);

        let mut replacement = sample_candidate();
        replacement.source_fingerprint = "fingerprint-b".to_owned();
        candidates.set(vec![replacement]);
        let third = archive_recent_once(&candidates, &store, &register, &mut state, 3).unwrap();
        assert_eq!(third.registered, 1);
        assert_eq!(store.copies.borrow().len(), 4);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn index_load() {
        let complete_candidate = sample_candidate();
        let partial_candidate = unique_candidate(2, 20);
        let archive_root = new_archive_root();
        write_test_marker(
            &archive_root,
            &complete_candidate,
            MarkerStatus::CompleteLive,
            10,
            MARKER_SCHEMA,
        );
        write_test_marker(
            &archive_root,
            &partial_candidate,
            MarkerStatus::Partial,
            10,
            MARKER_SCHEMA,
        );

        let candidates = FakeCandidates::default();
        candidates.set(vec![complete_candidate, partial_candidate.clone()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::with_archive_root(&archive_root);

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 100).unwrap();
        assert_eq!(report.registered, 1);
        assert_eq!(store.copies.borrow().len(), 2);
        assert_eq!(register.live_calls.borrow().len(), 1);
        assert_eq!(
            register.live_calls.borrow()[0].canonical_key,
            partial_candidate.canonical_key
        );

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn index_load_skips_bad_schema() {
        let candidate = sample_candidate();
        let archive_root = new_archive_root();
        write_test_marker(
            &archive_root,
            &candidate,
            MarkerStatus::CompleteLive,
            10,
            999,
        );

        let candidates = FakeCandidates::default();
        candidates.set(vec![candidate]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::with_archive_root(&archive_root);

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 100).unwrap();
        assert_eq!(report.registered, 1);
        assert_eq!(store.copies.borrow().len(), 2);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn write_then_dedup_via_map() {
        let candidate = sample_candidate();
        let archive_root = new_archive_root();
        let candidates = FakeCandidates::default();
        candidates.set(vec![candidate.clone()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::with_archive_root(&archive_root);

        let first = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(first.registered, 1);
        assert_eq!(store.copies.borrow().len(), 2);

        let marker_file = marker_path(&archive_root, &candidate.canonical_key);
        fs::remove_file(marker_file).expect("remove marker file");

        let second = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(second.registered, 0);
        assert_eq!(store.copies.borrow().len(), 2);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn write_marker_file_failure_keeps_map_clean() {
        let candidate = sample_candidate();
        let archive_root = new_archive_root();
        fs::create_dir_all(archive_root.join(".retentiond")).expect("create retentiond dir");
        fs::write(archive_root.join(".retentiond/markers"), "occupied-by-file")
            .expect("occupy markers path");

        let candidates = FakeCandidates::default();
        candidates.set(vec![candidate.clone()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::with_archive_root(&archive_root);

        let first = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(first.registered, 1);
        assert!(
            !state.markers.contains_key(&candidate.canonical_key),
            "map must not gain a complete marker when durable marker write fails"
        );

        let second = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(second.registered, 1);
        assert_eq!(store.copies.borrow().len(), 4);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn prune_after_absence() {
        let candidate = sample_candidate();
        let archive_root = new_archive_root();
        write_test_marker(
            &archive_root,
            &candidate,
            MarkerStatus::CompleteLive,
            0,
            MARKER_SCHEMA,
        );
        let marker_file = marker_path(&archive_root, &candidate.canonical_key);

        let candidates = FakeCandidates::default();
        candidates.set(vec![candidate.clone()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::with_archive_root(&archive_root);

        let first = archive_recent_capped(
            &candidates,
            &store,
            &register,
            &mut state,
            0,
            None,
            true,
            &mut || {},
        )
        .unwrap();
        assert_eq!(first.pruned_markers, 0);

        candidates.set(Vec::new());
        for scan in 1..=44 {
            let report = archive_recent_capped(
                &candidates,
                &store,
                &register,
                &mut state,
                i64::from(scan),
                None,
                true,
                &mut || {},
            )
            .unwrap();
            assert_eq!(report.pruned_markers, 0);
            assert!(marker_file.exists(), "marker should not prune before grace");
        }

        let mut final_report = CycleReport::default();
        for scan in 45..=49 {
            final_report = archive_recent_capped(
                &candidates,
                &store,
                &register,
                &mut state,
                PRUNE_GRACE_SECS + i64::from(scan),
                None,
                true,
                &mut || {},
            )
            .unwrap();
        }
        assert_eq!(final_report.pruned_markers, 1);
        assert!(
            !state.markers.contains_key(&candidate.canonical_key),
            "prune removes marker from map"
        );
        assert!(!marker_file.exists(), "prune removes marker file");

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn prune_disabled() {
        let candidate = sample_candidate();
        let archive_root = new_archive_root();
        write_test_marker(
            &archive_root,
            &candidate,
            MarkerStatus::CompleteLive,
            0,
            MARKER_SCHEMA,
        );
        let marker_file = marker_path(&archive_root, &candidate.canonical_key);

        let candidates = FakeCandidates::default();
        candidates.set(Vec::new());
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::with_archive_root(&archive_root);

        for scan in 0..60 {
            let report = archive_recent_capped(
                &candidates,
                &store,
                &register,
                &mut state,
                PRUNE_GRACE_SECS + i64::from(scan),
                None,
                false,
                &mut || {},
            )
            .unwrap();
            assert_eq!(report.pruned_markers, 0);
        }
        assert!(marker_file.exists());
        assert!(state.markers.contains_key(&candidate.canonical_key));

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn prune_refresh_keeps_live() {
        let candidate = sample_candidate();
        let archive_root = new_archive_root();
        write_test_marker(
            &archive_root,
            &candidate,
            MarkerStatus::CompleteLive,
            0,
            MARKER_SCHEMA,
        );
        let marker_file = marker_path(&archive_root, &candidate.canonical_key);

        let candidates = FakeCandidates::default();
        candidates.set(vec![candidate.clone()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::with_archive_root(&archive_root);

        for scan in 0..(PRUNE_MIN_MISSED_SCANS + PRUNE_EVERY_CYCLES + 10) {
            let report = archive_recent_capped(
                &candidates,
                &store,
                &register,
                &mut state,
                PRUNE_GRACE_SECS + i64::from(scan),
                None,
                true,
                &mut || {},
            )
            .unwrap();
            assert_eq!(report.pruned_markers, 0);
        }

        assert!(marker_file.exists());
        let summary = state
            .markers
            .get(&candidate.canonical_key)
            .expect("marker summary present");
        assert_eq!(summary.missed_scans, 0);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn copy_nondestructive_on_partial_failure() {
        let base_candidate = sample_candidate();
        let replacement = replacement_candidate_with_fingerprint(&base_candidate, "fingerprint-b");
        let archive_root = new_archive_root();
        let candidates = FakeCandidates::default();
        candidates.set(vec![base_candidate.clone()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::with_archive_root(&archive_root);

        let seeded = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(seeded.registered, 1);
        assert!(store.landed.borrow().contains(&final_front_path()));
        assert!(store.landed.borrow().contains(&final_back_path()));

        candidates.set(vec![replacement.clone()]);
        store.fail_once_for("TeslaCam/RecentClips/2026-06-19_10-00-00-back.mp4");
        let failed = archive_recent_once(&candidates, &store, &register, &mut state, 2).unwrap();
        assert_eq!(failed.copy_failed, 1);
        assert!(
            store.landed.borrow().contains(&final_front_path()),
            "existing final front must survive failed recopy"
        );
        assert!(
            store.landed.borrow().contains(&final_back_path()),
            "existing final back must survive failed recopy"
        );
        let marker = read_marker(&state, &replacement).expect("partial marker");
        assert_eq!(marker.status, MarkerStatus::Partial);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn staged_promote_happy() {
        let candidate = sample_candidate();
        let archive_root = new_archive_root();
        let candidates = FakeCandidates::default();
        candidates.set(vec![candidate.clone()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::with_archive_root(&archive_root);

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(report.registered, 1);
        assert_eq!(store.promotions.borrow().len(), 2);
        assert!(store.landed.borrow().contains(&final_front_path()));
        assert!(store.landed.borrow().contains(&final_back_path()));
        assert!(
            store
                .landed
                .borrow()
                .iter()
                .all(|path| !path.starts_with(".retentiond/staging/")),
            "staging files should be gone after promote"
        );
        let marker = read_marker(&state, &candidate).expect("complete marker");
        assert_eq!(marker.status, MarkerStatus::CompleteLive);
        let summary = state
            .markers
            .get(&candidate.canonical_key)
            .expect("marker summary");
        assert_eq!(summary.status, MarkerStatus::CompleteLive);
        assert_eq!(summary.source_fingerprint, candidate.source_fingerprint);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn index_load_skips_off_path_marker() {
        // A marker whose body claims a clip but whose FILE NAME is not
        // stable_hex(canonical_key).json must be ignored, so it cannot suppress a
        // real copy.
        let candidate = sample_candidate();
        let archive_root = new_archive_root();
        let marker = ClipMarker {
            schema: MARKER_SCHEMA,
            canonical_key: candidate.canonical_key.clone(),
            source_fingerprint: candidate.source_fingerprint.clone(),
            volume_serial: candidate.source_volume_serial,
            partition: candidate.partition.clone(),
            status: MarkerStatus::CompleteLive,
            updated_at: 10,
            angles: Vec::new(),
        };
        // Write it under a deliberately WRONG file name (not the canonical hash).
        let wrong_path = archive_root
            .join(".retentiond/markers")
            .join("deadbeefdeadbeef.json");
        write_json_durable(&wrong_path, &marker).expect("write off-path marker");
        // Sanity: the wrong name is not the canonical one.
        assert_ne!(
            wrong_path,
            marker_path(&archive_root, &candidate.canonical_key)
        );

        let candidates = FakeCandidates::default();
        candidates.set(vec![candidate]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::with_archive_root(&archive_root);

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 100).unwrap();
        assert_eq!(report.registered, 1, "off-path marker must not dedup");
        assert_eq!(store.copies.borrow().len(), 2);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn prune_keeps_entry_when_file_delete_fails() {
        // If the marker file cannot be removed (and still exists), the in-memory
        // index entry must be retained so the map never diverges from disk.
        let candidate = sample_candidate();
        let archive_root = new_archive_root();
        // Occupy the marker path with a NON-EMPTY directory so remove_file fails
        // with a non-NotFound error.
        let marker_file = marker_path(&archive_root, &candidate.canonical_key);
        fs::create_dir_all(marker_file.join("occupied")).expect("occupy marker path with dir");

        let candidates = FakeCandidates::default();
        candidates.set(Vec::new());
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::with_archive_root(&archive_root);
        // Seed an aged index entry directly (well past both prune thresholds).
        state.markers.insert(
            candidate.canonical_key.clone(),
            MarkerSummary {
                source_fingerprint: candidate.source_fingerprint.clone(),
                status: MarkerStatus::CompleteLive,
                last_seen_epoch: 0,
                missed_scans: PRUNE_MIN_MISSED_SCANS,
            },
        );

        let mut report = CycleReport::default();
        for scan in 0..(PRUNE_EVERY_CYCLES + 1) {
            report = archive_recent_capped(
                &candidates,
                &store,
                &register,
                &mut state,
                PRUNE_GRACE_SECS + i64::from(scan),
                None,
                true,
                &mut || {},
            )
            .unwrap();
        }
        assert_eq!(report.pruned_markers, 0, "delete failure is not counted");
        assert!(
            state.markers.contains_key(&candidate.canonical_key),
            "index entry retained when file delete fails"
        );
        assert!(marker_file.is_dir(), "occupying dir still present");

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn zero_angle_candidate_not_marked_complete() {
        let mut candidate = sample_candidate();
        candidate.angles.clear();
        let archive_root = new_archive_root();
        let candidates = FakeCandidates::default();
        candidates.set(vec![candidate.clone()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::with_archive_root(&archive_root);

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 100).unwrap();
        assert_eq!(report.registered, 0);
        assert_eq!(report.copy_failed, 1);
        assert!(store.copies.borrow().is_empty(), "no staging for zero angles");
        assert!(
            !state.markers.contains_key(&candidate.canonical_key),
            "zero-angle candidate must not gain a complete marker"
        );

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn durable_outbox_replays_after_restart_without_recopy() {
        let candidates = FakeCandidates::default();
        let candidate = sample_candidate();
        candidates.set(vec![candidate.clone()]);
        let store = FakeStore::default();
        let register = FakeRegister::with_live_failures(vec![true, false]);
        let archive_root = new_archive_root();
        let mut state = DriverState::with_archive_root(&archive_root);

        let first = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(first.register_deferred, 1);
        assert_eq!(first.pending_len, 1);
        assert_eq!(store.copies.borrow().len(), 2);
        let marker = read_marker(&state, &candidate).expect("complete marker");
        assert_eq!(marker.status, MarkerStatus::CompleteLive);

        let outbox_raw =
            fs::read_to_string(archive_root.join(OUTBOX_FILE)).expect("durable outbox must exist");
        let outbox: PersistedOutbox = serde_json::from_str(&outbox_raw).expect("decode outbox");
        assert_eq!(outbox.pending.len(), 1);
        assert_eq!(outbox.pending[0].reg.canonical_key, KEY);

        let mut restarted_state = DriverState::with_archive_root(&archive_root);
        candidates.set(Vec::new());
        let second = archive_recent_once(
            &candidates,
            &store,
            &register,
            &mut restarted_state,
            2,
        )
        .unwrap();
        assert_eq!(second.registered_from_pending, 1);
        assert_eq!(second.pending_len, 0);
        assert_eq!(store.copies.borrow().len(), 2);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn outbox_stage_failure_prevents_complete_live_marker_and_register_attempt() {
        let candidates = FakeCandidates::default();
        let candidate = sample_candidate();
        candidates.set(vec![candidate.clone()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let archive_root = new_archive_root();
        fs::create_dir_all(archive_root.join(".retentiond/register-outbox.json"))
            .expect("reserve outbox path as directory to force stage failure");
        let mut state = DriverState::with_archive_root(&archive_root);

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(report.copy_failed, 1);
        assert_eq!(report.registered, 0);
        assert_eq!(report.register_deferred, 0);
        assert_eq!(register.live_calls.borrow().len(), 0);
        assert!(
            read_marker(&state, &candidate).is_none(),
            "complete marker must not be written when durable outbox staging fails"
        );

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn capped_stops_after_max_copies_oldest_first() {
        let candidates = FakeCandidates::default();
        let clips = vec![
            unique_candidate(1, 10),
            unique_candidate(2, 20),
            unique_candidate(3, 30),
            unique_candidate(4, 40),
            unique_candidate(5, 50),
        ];
        candidates.set(clips.clone());
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let archive_root = new_archive_root();
        let mut state = DriverState::with_archive_root(&archive_root);

        let first = archive_recent_capped(
            &candidates,
            &store,
            &register,
            &mut state,
            1,
            Some(2),
            false,
            &mut || {},
        )
        .unwrap();
        assert_eq!(first.registered, 2);
        assert_eq!(store.copies.borrow().len(), 4);
        let calls = register.live_calls.borrow();
        assert_eq!(calls[0].canonical_key, clips[0].canonical_key);
        assert_eq!(calls[1].canonical_key, clips[1].canonical_key);
        drop(calls);

        let second = archive_recent_capped(
            &candidates,
            &store,
            &register,
            &mut state,
            2,
            Some(2),
            false,
            &mut || {},
        )
        .unwrap();
        assert_eq!(second.registered, 2);
        assert_eq!(store.copies.borrow().len(), 8);
        let calls = register.live_calls.borrow();
        assert_eq!(calls[2].canonical_key, clips[2].canonical_key);
        assert_eq!(calls[3].canonical_key, clips[3].canonical_key);
        drop(calls);

        let third = archive_recent_capped(
            &candidates,
            &store,
            &register,
            &mut state,
            3,
            Some(2),
            false,
            &mut || {},
        )
        .unwrap();
        assert_eq!(third.registered, 1);
        assert_eq!(store.copies.borrow().len(), 10);
        let calls = register.live_calls.borrow();
        assert_eq!(calls.len(), 5);
        assert_eq!(calls[4].canonical_key, clips[4].canonical_key);
        drop(calls);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn capped_skips_do_not_consume_budget() {
        let candidates = FakeCandidates::default();
        let complete_1 = unique_candidate(1, 10);
        let complete_2 = unique_candidate(2, 20);
        let fresh_1 = unique_candidate(3, 30);
        let fresh_2 = unique_candidate(4, 40);
        let fresh_3 = unique_candidate(5, 50);
        candidates.set(vec![complete_1.clone(), complete_2.clone()]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let archive_root = new_archive_root();
        let mut state = DriverState::with_archive_root(&archive_root);

        let seeded = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(seeded.registered, 2);
        store.copies.borrow_mut().clear();
        register.live_calls.borrow_mut().clear();

        candidates.set(vec![
            complete_1,
            complete_2,
            fresh_1.clone(),
            fresh_2.clone(),
            fresh_3,
        ]);
        let report = archive_recent_capped(
            &candidates,
            &store,
            &register,
            &mut state,
            2,
            Some(2),
            false,
            &mut || {},
        )
        .unwrap();
        assert_eq!(report.registered, 2);
        assert_eq!(store.copies.borrow().len(), 4);
        let calls = register.live_calls.borrow();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].canonical_key, fresh_1.canonical_key);
        assert_eq!(calls[1].canonical_key, fresh_2.canonical_key);
        drop(calls);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn capped_invokes_on_progress_once_per_copied_clip() {
        let candidates = FakeCandidates::default();
        let complete = unique_candidate(1, 10);
        let fresh_1 = unique_candidate(2, 20);
        let fresh_2 = unique_candidate(3, 30);
        let fresh_3 = unique_candidate(4, 40);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let archive_root = new_archive_root();
        let mut state = DriverState::with_archive_root(&archive_root);

        candidates.set(vec![complete.clone()]);
        let seeded = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(seeded.registered, 1);

        candidates.set(vec![complete, fresh_1, fresh_2, fresh_3]);
        let progress_ticks = Cell::new(0_usize);
        let report = archive_recent_capped(
            &candidates,
            &store,
            &register,
            &mut state,
            2,
            Some(3),
            false,
            &mut || progress_ticks.set(progress_ticks.get().saturating_add(1)),
        )
        .unwrap();

        assert_eq!(report.registered, 3);
        assert_eq!(progress_ticks.get(), 3);
        assert_eq!(register.live_calls.borrow().len(), 4);

        let _ = fs::remove_dir_all(archive_root);
    }

    #[test]
    fn archive_recent_once_unbounded_preserves_behavior() {
        let candidates = FakeCandidates::default();
        candidates.set(vec![
            unique_candidate(1, 10),
            unique_candidate(2, 20),
            unique_candidate(3, 30),
            unique_candidate(4, 40),
            unique_candidate(5, 50),
        ]);
        let store = FakeStore::default();
        let register = FakeRegister::default();
        let mut state = DriverState::new();

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(report.registered, 5);
        assert_eq!(store.copies.borrow().len(), 10);
        assert_eq!(register.live_calls.borrow().len(), 5);
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
