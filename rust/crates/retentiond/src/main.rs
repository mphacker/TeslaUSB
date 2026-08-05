//! `retentiond` binary entrypoint.
//!
//! Policy decisions live in the library crate; this binary wires CLI parsing and
//! the unix-only live archive-recent driver loop.

#![allow(clippy::print_stdout, clippy::print_stderr)]

#[cfg(unix)]
mod live;

use std::process::ExitCode;

#[cfg(unix)]
use std::fs::File;
#[cfg(unix)]
use std::io::Read;
#[cfg(unix)]
use std::path::{Path, PathBuf};
#[cfg(unix)]
use std::sync::atomic::{AtomicBool, Ordering};
#[cfg(unix)]
use std::thread;
#[cfg(unix)]
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[cfg(unix)]
use std::rc::Rc;
#[cfg(unix)]
use live::{
    LiveArchiveDeleteOps, LiveArchiveStore, LiveCatalog, LiveClock, LiveIndexClient, LiveRand,
    LiveStatfs,
};
#[cfg(unix)]
use serde::Serialize;
#[cfg(unix)]
use retentiond::archive::{CarDeleteHandoff, CarDeleteRequest, HandoffOutcome};
#[cfg(unix)]
use retentiond::archive_driver::{DriverState, archive_recent_capped};
#[cfg(unix)]
use retentiond::config::RetentionConfig;
#[cfg(unix)]
use retentiond::governor::{self, DiskImgAccounting, FsRole, FsSample, Statfs, Tier};
#[cfg(unix)]
use retentiond::index_delete_client::IndexDeleteClient;
#[cfg(unix)]
use retentiond::read_client::VolumeReadFileClient;
#[cfg(unix)]
use retentiond::register_client::{INDEXD_SOCKET_PATH, UnixRegisterClient};
#[cfg(unix)]
use retentiond::serve::{DrainInput, DrainOutcome, DrainStop, RetentionLoop, Seams};
#[cfg(unix)]
use retentiond::volume_source::VolumeCandidateSource;

#[cfg(unix)]
const DEFAULT_SLOT: u8 = 0;
#[cfg(unix)]
const DEFAULT_INTERVAL_SECS: u64 = 20;
#[cfg(unix)]
const DEFAULT_VOLUME_IMAGE: &str = "/data/teslausb/teslacam.img";
#[cfg(unix)]
const DEFAULT_HEALTH_FILE: &str = "/run/teslausb/retentiond.health.json";
#[cfg(unix)]
const DEFAULT_GOVERNOR_STATUS_FILE: &str = "/run/teslausb/retentiond.governor.json";
#[cfg(unix)]
const MAX_COPIES_PER_CYCLE: usize = 4;

#[cfg(unix)]
static SHUTDOWN: AtomicBool = AtomicBool::new(false);

#[cfg(unix)]
#[derive(Debug, Serialize)]
struct HealthHeartbeat {
    schema: u32,
    updated_at: i64,
    running: bool,
    pending: u64,
    last_progress_at: i64,
}

#[cfg(unix)]
#[derive(Debug, Serialize)]
struct GovernorStatus {
    schema: u32,
    updated_at: i64,
    uploads_allowed: bool,
    seq: u64,
    interval_secs: u64,
    publisher_instance: String,
    /// "armed" (real deletion) | "dryrun" (projection only).
    mode: &'static str,
    /// True when the archive copy pass is skipped (drain-only).
    drain_only: bool,
    free_bytes: u64,
    total_bytes: u64,
    target_free_frac: f64,
    target_exit_frac: f64,
    recency_floor_secs: i64,
    /// Stable snake_case tag of the last drain stop reason.
    last_stop: &'static str,
    last_bytes_freed: u64,
    last_items: u64,
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("version" | "--version" | "-V") => {
            println!("retentiond {}", env!("CARGO_PKG_VERSION"));
            ExitCode::SUCCESS
        }
        Some("--help" | "-h" | "help") | None => {
            println!("{}", usage());
            ExitCode::SUCCESS
        }
        Some("serve") => run_serve(args.get(1..).unwrap_or(&[])),
        Some(other) => {
            eprintln!("retentiond: unknown command `{other}`\n{}", usage());
            ExitCode::FAILURE
        }
    }
}

fn usage() -> String {
    "usage: retentiond <version|serve|help>\n\
     serve mode (phase-1 inert): retentiond serve --archive-recent-only --no-delete \\\n\
      --archive-root <path> [--volume-image <path>] \\\n\
       [--indexd-socket <path>] [--slot <u8>] [--interval-secs <u64>]\n\
     serve mode (phase-2e governor): retentiond serve --archive-recent-only --enable-eviction \\\n\
      [--dry-run] [--allow-permanent-loss] [--drain-only] [--recency-floor-secs <i64>] --archive-root <path> [--volume-image <path>] \\\n\
       [--indexd-socket <path>] [--slot <u8>] [--interval-secs <u64>]\n\
     note: --no-delete and --enable-eviction are mutually exclusive; --drain-only requires --enable-eviction."
        .to_owned()
}

#[cfg(unix)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum EvictionMode {
    Inert,
    DryRun,
    Armed,
}

#[cfg(unix)]
fn resolve_eviction_mode(enable_eviction: bool, dry_run: bool, allow_permanent_loss: bool) -> EvictionMode {
    if !enable_eviction {
        EvictionMode::Inert
    } else if dry_run || !allow_permanent_loss {
        EvictionMode::DryRun
    } else {
        EvictionMode::Armed
    }
}

#[cfg(unix)]
fn cumulative_evict_budget(free_at_startup: u64, target_free_bytes: u64, per_cycle_evict_bytes: u64) -> u64 {
    let startup_deficit = target_free_bytes.saturating_sub(free_at_startup);
    startup_deficit.saturating_add(per_cycle_evict_bytes)
}

#[cfg(unix)]
/// Per-episode blast-radius guard for the armed governor. An "episode" is one
/// unhealthy->healthy convergence. The odometer (`cumulative_freed`) is bounded by
/// the deficit observed when the episode STARTED (plus one per-cycle slack); if a
/// run frees more than that without ever reaching a verified-healthy checkpoint,
/// statfs is presumed to be lying and the drain is latched off. Reaching a healthy
/// checkpoint (TargetReached/AlreadyHealthy) ends the episode and resets the odometer.
///
/// Accepted low-severity residuals (deletion is intrinsically bounded to `RecentClips`
/// older than the recency floor): (a) a single false-healthy statfs reading resets the
/// odometer (no hysteresis); (b) the latch is in-memory and re-arms on process restart.
#[derive(Debug, Default)]
struct EvictBudget {
    cumulative_freed: u64,
    episode_budget: u64,
    episode_active: bool,
    latched: bool,
}

#[cfg(unix)]
impl EvictBudget {
    /// Fold one armed (non-dry-run) drain outcome into the guard. `stop_healthy` is
    /// true iff the pass reached a verified-healthy state (TargetReached/AlreadyHealthy).
    /// Returns `true` the first time the guard latches.
    fn observe(
        &mut self,
        stop_healthy: bool,
        free_before: u64,
        target_free_bytes: u64,
        bytes_freed: u64,
        per_cycle_evict_bytes: u64,
    ) -> bool {
        if self.latched {
            return false;
        }
        if stop_healthy {
            self.cumulative_freed = 0;
            self.episode_active = false;
            return false;
        }
        if !self.episode_active {
            self.episode_budget =
                cumulative_evict_budget(free_before, target_free_bytes, per_cycle_evict_bytes);
            self.episode_active = true;
            self.cumulative_freed = 0;
        }
        self.cumulative_freed = self.cumulative_freed.saturating_add(bytes_freed);
        if self.cumulative_freed >= self.episode_budget {
            self.latched = true;
            return true;
        }
        false
    }
}

#[cfg(unix)]
#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation, clippy::cast_sign_loss)]
fn frac_bytes(total: u64, frac: f64) -> u64 {
    (total as f64 * frac.clamp(0.0, 1.0)) as u64
}

#[cfg(unix)]
fn validate_phase1_mode(parsed: &ServeArgs) -> Result<(), String> {
    if !parsed.no_delete && !parsed.enable_eviction {
        return Err(
            "retentiond serve: phase-1 requires --no-delete (or --enable-eviction to run the governor)."
                .to_owned(),
        );
    }
    Ok(())
}

#[cfg(unix)]
struct NoCarHandoff;

#[cfg(unix)]
impl CarDeleteHandoff for NoCarHandoff {
    fn request_car_delete(&self, _req: &CarDeleteRequest) -> HandoffOutcome {
        HandoffOutcome::Refused(
            "governor archive-delete path does not use the car handoff".to_owned(),
        )
    }
}

#[cfg(unix)]
#[allow(clippy::cast_precision_loss)]
fn log_drain(outcome: &DrainOutcome, dry_run: bool, cfg: &RetentionConfig, interval_secs: u64) {
    let mode_tag = if dry_run { "DRY-RUN" } else { "ARMED" };
    let total = outcome.total_bytes;
    let target_free = frac_bytes(total, cfg.target_drain.target_free_frac);
    let gap_to_target = target_free.saturating_sub(outcome.free_after);
    let pct = |bytes: u64| {
        if total == 0 {
            0.0
        } else {
            (bytes as f64 * 100.0) / total as f64
        }
    };
    let recency_floor_epoch = now_epoch_s_saturating().saturating_sub(cfg.target_drain.recency_floor_secs);
    let permanent_loss_count = outcome.records.iter().filter(|record| record.permanent_loss).count();
    println!(
        "retentiond governor [{mode_tag}] stop={:?} items={} bytes_freed={} free_before={} ({:.2}%) free_after={} ({:.2}%) total={} target_free≈{} ({:.2}%) gap_to_target={} recency_floor_epoch={}",
        outcome.stop,
        outcome.records.len(),
        outcome.bytes_freed,
        outcome.free_before,
        pct(outcome.free_before),
        outcome.free_after,
        pct(outcome.free_after),
        total,
        target_free,
        cfg.target_drain.target_free_frac * 100.0,
        gap_to_target,
        recency_floor_epoch
    );
    if let Some(oldest) = outcome.records.first() {
        println!(
            "retentiond governor [{mode_tag}] oldest_candidate={} permanent_loss_count={}",
            oldest.source_path, permanent_loss_count
        );
    }
    if gap_to_target > 0
        && !matches!(
            outcome.stop,
            DrainStop::AlreadyHealthy | DrainStop::NoSafeCandidate
        )
    {
        eprintln!(
            "governor: target NOT reached this pass; ARMED mode will CONTINUE deleting oldest RecentClips across future ~{interval_secs}s cycles until free>=exit — cumulative deletion will EXCEED this single pass (remaining≈{gap_to_target} bytes)."
        );
    }
}

#[cfg(not(unix))]
fn run_serve(_args: &[String]) -> ExitCode {
    eprintln!("retentiond serve: live archive-recent-only mode is only supported on unix.");
    ExitCode::FAILURE
}

#[cfg(unix)]
#[allow(clippy::too_many_lines)]
// run_serve is the single top-level cycle orchestrator (statfs -> archive gate ->
// drain -> governor-status publish); it is deliberately kept as one loop and is
// already exempt from too_many_lines. Publishing the governor upload-backpressure
// status inline (post-drain re-evaluate) adds branching that trips
// cognitive_complexity; splitting the recording-critical cycle across helpers would
// obscure the ordering guarantees, so it shares the same orchestration exemption.
#[allow(clippy::cognitive_complexity)]
fn run_serve(args: &[String]) -> ExitCode {
    let parsed = match parse_serve_args(args) {
        Ok(parsed) => parsed,
        Err(message) => {
            eprintln!("{message}");
            return ExitCode::FAILURE;
        }
    };

    if !parsed.archive_recent_only {
        eprintln!(
            "retentiond serve: only --archive-recent-only mode is supported in this build \
             (phase-1 non-destructive)."
        );
        return ExitCode::FAILURE;
    }
    if let Err(err) = validate_phase1_mode(&parsed) {
        eprintln!("{err}");
        return ExitCode::FAILURE;
    }
    let Some(archive_root) = parsed.archive_root.clone() else {
        eprintln!("retentiond serve: missing required --archive-root <path>.");
        return ExitCode::FAILURE;
    };

    let mode = resolve_eviction_mode(
        parsed.enable_eviction,
        parsed.dry_run,
        parsed.allow_permanent_loss,
    );

    // Arm the watchdog and send the first keepalive BEFORE any storage I/O
    // (volume open below can block on slow/bad media) so systemd doesn't kill us
    // during startup.
    retentiond::watchdog::init();
    retentiond::watchdog::pet();

    let candidates = match VolumeCandidateSource::open(&parsed.volume_image, parsed.slot) {
        Ok(reader) => reader,
        Err(err) => {
            eprintln!(
                "retentiond serve: cannot initialize volume candidate source at {}: {err}",
                parsed.volume_image.display()
            );
            return ExitCode::FAILURE;
        }
    };

    install_shutdown_handlers();
    retentiond::watchdog::pet();

    let store = LiveArchiveStore::new(
        Box::new(VolumeReadFileClient::new(&parsed.volume_image, parsed.slot)),
        &archive_root,
    );
    let register = UnixRegisterClient::new(&parsed.indexd_socket);
    let mut state = DriverState::with_archive_root(&archive_root);
    let health_file = std::env::var_os("RETENTIOND_HEALTH_FILE")
        .map_or_else(|| PathBuf::from(DEFAULT_HEALTH_FILE), PathBuf::from);
    let governor_status_file = std::env::var_os("RETENTIOND_GOVERNOR_STATUS_FILE")
        .map_or_else(|| PathBuf::from(DEFAULT_GOVERNOR_STATUS_FILE), PathBuf::from);
    let startup_now = now_epoch_s_saturating();
    let mut last_progress_at = startup_now;
    let mut last_pending: u64 = 0;
    let mut health_write_error_logged = false;
    let mut governor_status_write_error_logged = false;
    write_health_heartbeat_best_effort(
        &health_file,
        startup_now,
        last_pending,
        last_progress_at,
        &mut health_write_error_logged,
    );

    if mode == EvictionMode::Inert {
        while !SHUTDOWN.load(Ordering::Relaxed) {
            let now_epoch_s = now_epoch_s_saturating();
            let result = {
                let pending_snapshot = last_pending;
                let mut on_progress = || {
                    let t = now_epoch_s_saturating();
                    write_health_heartbeat_best_effort(
                        &health_file,
                        t,
                        pending_snapshot,
                        t,
                        &mut health_write_error_logged,
                    );
                    retentiond::watchdog::pet();
                };
                archive_recent_capped(
                    &candidates,
                    &store,
                    &register,
                    &mut state,
                    now_epoch_s,
                    Some(MAX_COPIES_PER_CYCLE),
                    true,
                    &mut on_progress,
                )
            };
            match result {
                Ok(report) => {
                    // Stamp the end-of-cycle heartbeat with a FRESH timestamp, not the
                    // loop-entry `now_epoch_s`. A long cycle (e.g. a cold-start batch)
                    // advances real time while it runs; reusing the start time here would
                    // clobber the fresher timestamps written by `on_progress` and could
                    // falsely age the worker toward "stale" during the next sleep.
                    let cycle_end = now_epoch_s_saturating();
                    let has_activity = report.observed > 0
                        || report.registered > 0
                        || report.registered_from_pending > 0
                        || report.copy_failed > 0
                        || report.register_deferred > 0
                        || report.register_rejected > 0
                        || report.quarantined_undecodable > 0
                        || report.skipped_already_pending > 0
                        || report.skipped_rejected > 0
                        || report.dropped_poison > 0
                        || report.pruned_markers > 0
                        || report.pending_len > 0;
                    if has_activity {
                        println!(
                            "retentiond archive_recent_only slot={} observed={} registered={} \
                             registered_from_pending={} copy_failed={} register_deferred={} \
                             register_rejected={} quarantined_undecodable={} \
                             skipped_already_pending={} skipped_rejected={} dropped_poison={} \
                             pruned_markers={} \
                             pending={}",
                            parsed.slot,
                            report.observed,
                            report.registered,
                            report.registered_from_pending,
                            report.copy_failed,
                            report.register_deferred,
                            report.register_rejected,
                            report.quarantined_undecodable,
                            report.skipped_already_pending,
                            report.skipped_rejected,
                            report.dropped_poison,
                            report.pruned_markers,
                            report.pending_len
                        );
                    }
                    if report.observed > 0
                        || report.registered > 0
                        || report.registered_from_pending > 0
                    {
                        last_progress_at = cycle_end;
                    }
                    last_pending = u64::try_from(report.pending_len).unwrap_or(u64::MAX);
                    write_health_heartbeat_best_effort(
                        &health_file,
                        cycle_end,
                        last_pending,
                        last_progress_at,
                        &mut health_write_error_logged,
                    );
                    retentiond::watchdog::pet();
                }
                Err(err) => {
                    // Intentionally do NOT refresh the heartbeat on a failed cycle: a
                    // worker that errors every loop must not keep reporting a fresh
                    // "Idle, queue empty" / "{n} pending" status. Leaving updated_at
                    // frozen lets webd age it into "stale" then "Worker not running",
                    // which is the whole point of this health signal.
                    eprintln!("retentiond archive_recent_only: cycle error: {err}");
                    retentiond::watchdog::pet();
                }
            }
            sleep_interruptible(parsed.interval_secs);
        }
        return ExitCode::SUCCESS;
    }

    let effective_dry_run = mode == EvictionMode::DryRun;
    let mut cfg = RetentionConfig {
        local_only_recent_delete_approved: parsed.enable_eviction,
        ..RetentionConfig::default()
    };
    // Operator override for the recency floor (this test card holds only ~4 days
    // of RecentClips, so the 7-day default would protect 100% of the archive and
    // the governor would free nothing). Propagates to BOTH the L1 candidate query
    // and the L3 atomic claim via set_cycle_context below.
    if let Some(secs) = parsed.recency_floor_secs {
        cfg.target_drain.recency_floor_secs = secs;
    }
    let archive_root_str = archive_root.to_string_lossy().into_owned();
    let trash_dir_path = archive_root.join(".retention-trash");
    if let Err(err) = std::fs::create_dir_all(&trash_dir_path) {
        if err.kind() != std::io::ErrorKind::AlreadyExists {
            eprintln!(
                "retentiond governor: failed to ensure trash dir {}: {err}",
                trash_dir_path.display()
            );
        }
    }
    let trash_dir = trash_dir_path.to_string_lossy().into_owned();

    let shared = Rc::new(IndexDeleteClient::new(&parsed.indexd_socket));
    let clock = LiveClock;
    let rand = LiveRand;
    let statfs = LiveStatfs;
    let handoff = NoCarHandoff;
    let fs = LiveArchiveDeleteOps::new(&archive_root);
    let index = LiveIndexClient::new(Rc::clone(&shared));
    let catalog = LiveCatalog::new(Rc::clone(&shared), &archive_root, trash_dir.clone());
    let seams = Seams {
        clock: &clock,
        store: &store,
        handoff: &handoff,
        statfs: &statfs,
        fs: &fs,
        index: &index,
        catalog: &catalog,
        rand: &rand,
    };
    let governor = RetentionLoop::new(&cfg, seams, trash_dir.clone());

    let mut evict_budget = EvictBudget::default();
    let mut prev_space_tier = Tier::Healthy;
    let mut archive_paused_prev = false;
    let publisher_instance = publisher_instance_hex_128();
    let mut governor_seq: u64 = 0;

    let mut recovered = false;
    maybe_retry_recover(
        || governor.recover().map(|report| report.actions.len()),
        &mut recovered,
        false,
    );
    retentiond::watchdog::pet();

    while !SHUTDOWN.load(Ordering::Relaxed) {
        maybe_retry_recover(
            || governor.recover().map(|report| report.actions.len()),
            &mut recovered,
            true,
        );
        let cycle_prev_tier = prev_space_tier;
        let mut pre_uploads_unsafe = true;
        let mut pre_status_bytes: Option<(u64, u64)> = None;
        // Fail-closed default: if statfs fails below we stay paused for this cycle
        // (the Ok arm overwrites this with the real assessment).
        let mut archive_paused = true;
        match statfs.statfs(archive_root_str.as_str()) {
            Ok(stat) => {
                pre_status_bytes = Some((stat.free_bytes, stat.total_bytes));
                let samples = [FsSample {
                    role: FsRole::Data,
                    stat,
                }];
                let assessment = governor::evaluate(
                    prev_space_tier,
                    &samples,
                    DiskImgAccounting {
                        nominal_bytes: 0,
                        allocated_bytes: 0,
                    },
                    true,
                    &cfg.governor,
                );
                prev_space_tier = assessment.space_tier;
                archive_paused = assessment.tier >= Tier::Critical;
                if assessment.tier < Tier::Emergency {
                    pre_uploads_unsafe = false;
                }
                if archive_paused != archive_paused_prev {
                    eprintln!(
                        "retentiond archive gate: {} at tier={:?} free={} bytes",
                        if archive_paused { "PAUSED" } else { "RESUMED" },
                        assessment.tier,
                        assessment.data_free_bytes
                    );
                }
            }
            Err(err) => {
                // Blind to free space -> fail SAFE (archive_paused stays true):
                // pause the optional archive writer this cycle so it cannot fill
                // the disk while we cannot measure it. The eviction drain below
                // still runs and frees space if it can. A transient statfs hiccup
                // costs one cycle of mirroring; a persistent one must not defeat
                // the ENOSPC guard. prev_space_tier is left unchanged so a blind
                // cycle does not poison the space hysteresis when statfs recovers.
                if !archive_paused_prev {
                    eprintln!("retentiond archive gate: PAUSED (statfs failed: {err})");
                }
            }
        }
        archive_paused_prev = archive_paused;
        if pre_uploads_unsafe {
            let (free_bytes, total_bytes) = pre_status_bytes.unwrap_or((0, 0));
            governor_seq = governor_seq.saturating_add(1);
            write_governor_status_best_effort(
                &governor_status_file,
                false,
                governor_seq,
                parsed.interval_secs,
                &publisher_instance,
                free_bytes,
                total_bytes,
                "skipped",
                0,
                0,
                effective_dry_run,
                parsed.drain_only,
                &cfg,
                &mut governor_status_write_error_logged,
            );
        }
        // --drain-only (emergency): SKIP the archive pass entirely and run ONLY the
        // eviction drain below. At a near-full disk the archive pass hard-blocks on
        // the full filesystem (marker/outbox/candidate I/O) longer than the watchdog
        // window, which starves the drain that actually frees space. The drain is
        // self-contained (statfs + indexd socket + its own candidate cache; recover()
        // already ran before the loop), so skipping the archive pass here is safe.
        let result = if parsed.drain_only || archive_paused {
            Ok(retentiond::archive_driver::CycleReport::default())
        } else {
            let now_epoch_s = now_epoch_s_saturating();
            let pending_snapshot = last_pending;
            let mut on_progress = || {
                let t = now_epoch_s_saturating();
                write_health_heartbeat_best_effort(
                    &health_file,
                    t,
                    pending_snapshot,
                    t,
                    &mut health_write_error_logged,
                );
                retentiond::watchdog::pet();
            };
            archive_recent_capped(
                &candidates,
                &store,
                &register,
                &mut state,
                now_epoch_s,
                Some(MAX_COPIES_PER_CYCLE),
                true,
                &mut on_progress,
            )
        };
        match result {
            Ok(report) => {
                let cycle_end = now_epoch_s_saturating();
                let has_activity = report.observed > 0
                    || report.registered > 0
                    || report.registered_from_pending > 0
                    || report.copy_failed > 0
                    || report.register_deferred > 0
                    || report.register_rejected > 0
                    || report.quarantined_undecodable > 0
                    || report.skipped_already_pending > 0
                    || report.skipped_rejected > 0
                    || report.dropped_poison > 0
                    || report.pruned_markers > 0
                    || report.pending_len > 0;
                if has_activity {
                    println!(
                        "retentiond archive_recent_only slot={} observed={} registered={} \
                         registered_from_pending={} copy_failed={} register_deferred={} \
                         register_rejected={} quarantined_undecodable={} \
                         skipped_already_pending={} skipped_rejected={} dropped_poison={} \
                         pruned_markers={} \
                         pending={}",
                        parsed.slot,
                        report.observed,
                        report.registered,
                        report.registered_from_pending,
                        report.copy_failed,
                        report.register_deferred,
                        report.register_rejected,
                        report.quarantined_undecodable,
                        report.skipped_already_pending,
                        report.skipped_rejected,
                        report.dropped_poison,
                        report.pruned_markers,
                        report.pending_len
                    );
                }
                if report.observed > 0 || report.registered > 0 || report.registered_from_pending > 0 {
                    last_progress_at = cycle_end;
                }
                last_pending = u64::try_from(report.pending_len).unwrap_or(u64::MAX);
                write_health_heartbeat_best_effort(
                    &health_file,
                    cycle_end,
                    last_pending,
                    last_progress_at,
                    &mut health_write_error_logged,
                );
                retentiond::watchdog::pet();
            }
            Err(err) => {
                eprintln!("retentiond archive_recent_only: cycle error: {err}");
                retentiond::watchdog::pet();
            }
        }

        let mut last_outcome: Option<DrainOutcome> = None;
        if !evict_budget.latched {
            // Coupling note: allow_undurable is wired to enable_eviction, so
            // armed eviction currently bypasses indexd's PROVEN_DURABLE gate.
            // Do not flip this path to allow_undurable=false until uploadd's
            // prepare/finalize seam reliably writes finalized COMPLETE parent
            // upload sets, or eviction will stop freeing space.
            shared.set_cycle_context(
                now_epoch_s_saturating().saturating_sub(cfg.target_drain.recency_floor_secs),
                parsed.enable_eviction,
            );
            let should_stop = || SHUTDOWN.load(Ordering::Relaxed);
            let pet = || retentiond::watchdog::pet();
            let input = DrainInput {
                data_fs_path: &archive_root_str,
                dry_run: effective_dry_run,
                should_stop: &should_stop,
                pet_watchdog: &pet,
            };
            match governor.drain_to_target(&input) {
                Ok(outcome) => {
                    log_drain(&outcome, effective_dry_run, &cfg, parsed.interval_secs);
                    if !effective_dry_run {
                        // size the episode budget to the level the drain actually drives free UP to (exit hysteresis), else an honest convergence to exit_frac would exceed a free_frac-sized budget and false-latch
                        let target_free_bytes =
                            frac_bytes(outcome.total_bytes, cfg.target_drain.target_exit_frac);
                        let stop_healthy = matches!(
                            &outcome.stop,
                            DrainStop::TargetReached | DrainStop::AlreadyHealthy
                        );
                        if evict_budget.observe(
                            stop_healthy,
                            outcome.free_before,
                            target_free_bytes,
                            outcome.bytes_freed,
                            cfg.target_drain.per_cycle_evict_bytes,
                        ) {
                            eprintln!(
                                "retentiond governor: EPISODE BLAST-RADIUS BUDGET EXCEEDED (cumulative_freed={} >= episode_budget={}); latching drain OFF — archiving continues. Investigate statfs/consistency.",
                                evict_budget.cumulative_freed, evict_budget.episode_budget
                            );
                        }
                    }
                    last_outcome = Some(outcome);
                }
                Err(err) => eprintln!("retentiond governor: drain cycle error: {err}"),
            }
            retentiond::watchdog::pet();
        }

        let mut uploads_allowed = false;
        let (free_bytes, total_bytes) = match statfs.statfs(archive_root_str.as_str()) {
            Ok(stat) => {
                let post = governor::evaluate(
                    cycle_prev_tier,
                    &[FsSample {
                        role: FsRole::Data,
                        stat,
                    }],
                    DiskImgAccounting {
                        nominal_bytes: 0,
                        allocated_bytes: 0,
                    },
                    true,
                    &cfg.governor,
                );
                uploads_allowed = governor.upload_backpressure(&post).uploads_allowed;
                (stat.free_bytes, stat.total_bytes)
            }
            Err(_) => last_outcome
                .as_ref()
                .map_or((0, 0), |outcome| (outcome.free_after, outcome.total_bytes)),
        };
        if evict_budget.latched {
            uploads_allowed = false;
        }
        let (last_stop, last_bytes_freed, last_items) = match last_outcome.as_ref() {
            Some(outcome) => (
                drain_stop_tag(&outcome.stop),
                outcome.bytes_freed,
                u64::try_from(outcome.records.len()).unwrap_or(u64::MAX),
            ),
            None => ("skipped", 0, 0),
        };
        governor_seq = governor_seq.saturating_add(1);
        write_governor_status_best_effort(
            &governor_status_file,
            uploads_allowed,
            governor_seq,
            parsed.interval_secs,
            &publisher_instance,
            free_bytes,
            total_bytes,
            last_stop,
            last_bytes_freed,
            last_items,
            effective_dry_run,
            parsed.drain_only,
            &cfg,
            &mut governor_status_write_error_logged,
        );

        sleep_interruptible(parsed.interval_secs);
    }

    ExitCode::SUCCESS
}

#[cfg(unix)]
fn sleep_interruptible(interval_secs: u64) {
    for _ in 0..interval_secs {
        if SHUTDOWN.load(Ordering::Relaxed) {
            break;
        }
        retentiond::watchdog::pet();
        thread::sleep(Duration::from_secs(1));
    }
}

#[cfg(unix)]
fn now_epoch_s_saturating() -> i64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(duration) => i64::try_from(duration.as_secs()).unwrap_or(i64::MAX),
        Err(_) => 0,
    }
}

#[cfg(unix)]
fn publisher_instance_hex_128() -> String {
    let mut bytes = [0_u8; 16];
    if File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .is_ok()
    {
        return format!("{:032x}", u128::from_le_bytes(bytes));
    }
    let fallback = (u128::from(std::process::id()) << 64)
        | u128::try_from(now_epoch_s_saturating()).unwrap_or(0);
    format!("{fallback:032x}")
}

#[cfg(unix)]
fn render_health(now: i64, pending: u64, last_progress_at: i64) -> String {
    let heartbeat = HealthHeartbeat {
        schema: 1,
        updated_at: now,
        running: true,
        pending,
        last_progress_at,
    };
    serde_json::to_string(&heartbeat).unwrap_or_else(|_| {
        format!(
            "{{\"schema\":1,\"updated_at\":{now},\"running\":true,\"pending\":{pending},\"last_progress_at\":{last_progress_at}}}"
        )
    })
}

#[cfg(unix)]
fn write_health_heartbeat_atomic(path: &Path, body: &str) -> std::io::Result<()> {
    let mut tmp = path.as_os_str().to_os_string();
    tmp.push(".tmp");
    let tmp_path = PathBuf::from(tmp);
    std::fs::write(&tmp_path, body)?;
    std::fs::rename(&tmp_path, path)?;
    Ok(())
}

#[cfg(unix)]
fn write_health_heartbeat_best_effort(
    path: &Path,
    now: i64,
    pending: u64,
    last_progress_at: i64,
    write_error_logged: &mut bool,
) {
    let body = render_health(now, pending, last_progress_at);
    if let Err(err) = write_health_heartbeat_atomic(path, &body) {
        if !*write_error_logged {
            eprintln!(
                "retentiond archive_recent_only: health heartbeat write failed at {}: {err}",
                path.display()
            );
            *write_error_logged = true;
        }
    }
}

#[cfg(unix)]
fn drain_stop_tag(stop: &DrainStop) -> &'static str {
    match stop {
        DrainStop::TargetReached => "target_reached",
        DrainStop::ByteCapReached => "byte_cap",
        DrainStop::CountCapReached => "count_cap",
        DrainStop::WallClockCapReached => "wall_cap",
        DrainStop::ShutdownRequested => "shutdown",
        DrainStop::NoSafeCandidate => "no_safe_candidate",
        DrainStop::AlreadyHealthy => "already_healthy",
        DrainStop::AnomalyRefused { .. } => "anomaly_refused",
        DrainStop::DeleteFailed { .. } => "delete_failed",
        DrainStop::StatCheckFailed { .. } => "stat_check_failed",
    }
}

#[cfg(unix)]
fn write_governor_status_atomic(path: &Path, body: &str) -> std::io::Result<()> {
    let mut tmp = path.as_os_str().to_os_string();
    tmp.push(".tmp");
    let tmp_path = PathBuf::from(tmp);
    std::fs::write(&tmp_path, body)?;
    std::fs::rename(&tmp_path, path)?;
    Ok(())
}

#[cfg(unix)]
#[allow(clippy::too_many_arguments)]
fn write_governor_status_best_effort(
    path: &Path,
    uploads_allowed: bool,
    seq: u64,
    interval_secs: u64,
    publisher_instance: &str,
    free_bytes: u64,
    total_bytes: u64,
    last_stop: &'static str,
    last_bytes_freed: u64,
    last_items: u64,
    effective_dry_run: bool,
    drain_only: bool,
    cfg: &RetentionConfig,
    write_error_logged: &mut bool,
) {
    let status = GovernorStatus {
        schema: 2,
        updated_at: now_epoch_s_saturating(),
        uploads_allowed,
        seq,
        interval_secs,
        publisher_instance: publisher_instance.to_owned(),
        mode: if effective_dry_run { "dryrun" } else { "armed" },
        drain_only,
        free_bytes,
        total_bytes,
        target_free_frac: cfg.target_drain.target_free_frac,
        target_exit_frac: cfg.target_drain.target_exit_frac,
        recency_floor_secs: cfg.target_drain.recency_floor_secs,
        last_stop,
        last_bytes_freed,
        last_items,
    };
    let body = match serde_json::to_string(&status) {
        Ok(b) => b,
        Err(err) => {
            if !*write_error_logged {
                eprintln!("retentiond governor: status serialize failed: {err}");
                *write_error_logged = true;
            }
            return;
        }
    };
    if let Err(err) = write_governor_status_atomic(path, &body) {
        if !*write_error_logged {
            eprintln!(
                "retentiond governor: status write failed at {}: {err}",
                path.display()
            );
            *write_error_logged = true;
        }
    }
}

#[cfg(unix)]
#[derive(Debug, Clone)]
#[allow(clippy::struct_excessive_bools)]
struct ServeArgs {
    archive_recent_only: bool,
    no_delete: bool,
    enable_eviction: bool,
    dry_run: bool,
    allow_permanent_loss: bool,
    /// Emergency: skip the archive pass and run ONLY the eviction drain each
    /// cycle. For freeing space on a near-full disk where the archive pass
    /// hard-blocks on the full filesystem (marker/outbox/candidate I/O) longer
    /// than the watchdog window and starves the drain that actually frees space.
    /// Requires `--enable-eviction`; the drain is self-contained so this is safe.
    drain_only: bool,
    /// Operator override for `TargetDrainConfig::recency_floor_secs` (protect
    /// anything recorded within this many seconds). `None` = use the config
    /// default (1 hour / 3600 seconds). Must be > 0 when set.
    recency_floor_secs: Option<i64>,
    archive_root: Option<PathBuf>,
    volume_image: PathBuf,
    indexd_socket: PathBuf,
    slot: u8,
    interval_secs: u64,
}

#[cfg(unix)]
impl Default for ServeArgs {
    fn default() -> Self {
        Self {
            archive_recent_only: false,
            no_delete: false,
            enable_eviction: false,
            dry_run: false,
            allow_permanent_loss: false,
            drain_only: false,
            recency_floor_secs: None,
            archive_root: None,
            volume_image: PathBuf::from(DEFAULT_VOLUME_IMAGE),
            indexd_socket: PathBuf::from(INDEXD_SOCKET_PATH),
            slot: DEFAULT_SLOT,
            interval_secs: DEFAULT_INTERVAL_SECS,
        }
    }
}

#[cfg(unix)]
fn parse_serve_args(args: &[String]) -> Result<ServeArgs, String> {
    let mut parsed = ServeArgs::default();
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--archive-recent-only" => parsed.archive_recent_only = true,
            "--no-delete" => parsed.no_delete = true,
            "--enable-eviction" => parsed.enable_eviction = true,
            "--dry-run" => parsed.dry_run = true,
            "--allow-permanent-loss" => parsed.allow_permanent_loss = true,
            "--drain-only" => parsed.drain_only = true,
            "--recency-floor-secs" => {
                let value = next_arg_value(&mut iter, "--recency-floor-secs")?;
                parsed.recency_floor_secs =
                    Some(parse_arg::<i64>("--recency-floor-secs", &value)?);
            }
            "--archive-root" => {
                let value = next_arg_value(&mut iter, "--archive-root")?;
                parsed.archive_root = Some(PathBuf::from(value));
            }
            "--volume-image" => {
                let value = next_arg_value(&mut iter, "--volume-image")?;
                parsed.volume_image = PathBuf::from(value);
            }
            "--indexd-socket" => {
                let value = next_arg_value(&mut iter, "--indexd-socket")?;
                parsed.indexd_socket = PathBuf::from(value);
            }
            "--slot" => {
                let value = next_arg_value(&mut iter, "--slot")?;
                parsed.slot = parse_arg::<u8>("--slot", &value)?;
            }
            "--interval-secs" => {
                let value = next_arg_value(&mut iter, "--interval-secs")?;
                parsed.interval_secs = parse_arg::<u64>("--interval-secs", &value)?;
            }
            other => return Err(format!("retentiond serve: unknown option `{other}`.\n{}", usage())),
        }
    }
    if parsed.interval_secs == 0 {
        return Err("retentiond serve: --interval-secs must be greater than 0.".to_owned());
    }
    if let Some(secs) = parsed.recency_floor_secs {
        if secs <= 0 {
            return Err(
                "retentiond serve: --recency-floor-secs must be greater than 0.".to_owned(),
            );
        }
    }
    if parsed.no_delete && parsed.enable_eviction {
        return Err(
            "retentiond serve: --no-delete and --enable-eviction are mutually exclusive."
                .to_owned(),
        );
    }
    if parsed.drain_only && !parsed.enable_eviction {
        return Err("retentiond serve: --drain-only requires --enable-eviction.".to_owned());
    }
    Ok(parsed)
}

#[cfg(unix)]
fn maybe_retry_recover<E: std::fmt::Display>(
    recover: impl FnOnce() -> Result<usize, E>,
    recovered: &mut bool,
    deferred: bool,
) {
    if *recovered {
        return;
    }
    match recover() {
        Ok(actions) => {
            if deferred {
                println!(
                    "retentiond governor: recover completed (deferred) actions={}",
                    actions
                );
            } else {
                println!("retentiond governor: recover completed actions={}", actions);
            }
            *recovered = true;
        }
        Err(err) => eprintln!("retentiond governor: recover error: {err}"),
    }
}

#[cfg(unix)]
fn next_arg_value(iter: &mut std::slice::Iter<'_, String>, flag: &str) -> Result<String, String> {
    iter.next()
        .cloned()
        .ok_or_else(|| format!("retentiond serve: missing value for {flag}."))
}

#[cfg(unix)]
fn parse_arg<T>(flag: &str, value: &str) -> Result<T, String>
where
    T: std::str::FromStr,
    <T as std::str::FromStr>::Err: std::fmt::Display,
{
    value
        .parse::<T>()
        .map_err(|err| format!("retentiond serve: invalid {flag} `{value}`: {err}"))
}

#[cfg(unix)]
extern "C" fn shutdown_signal_handler(_signal: libc::c_int) {
    SHUTDOWN.store(true, Ordering::Relaxed);
}

#[cfg(unix)]
#[allow(unsafe_code)]
fn install_shutdown_handlers() {
    SHUTDOWN.store(false, Ordering::Relaxed);
    unsafe {
        let handler = shutdown_signal_handler as libc::sighandler_t;
        let _ = libc::signal(libc::SIGTERM, handler);
        let _ = libc::signal(libc::SIGINT, handler);
    }
}

#[cfg(all(test, unix))]
#[allow(clippy::unwrap_used, clippy::panic)]
mod tests {
    use std::rc::Rc;

    use retentiond::config::RetentionConfig;
    use retentiond::governor::{GovernorAssessment, Tier};
    use retentiond::index_delete_client::IndexDeleteClient;
    use retentiond::read_client::VolumeReadFileClient;

    use super::{
        LiveArchiveDeleteOps, LiveArchiveStore, LiveCatalog, LiveClock, LiveIndexClient, LiveRand,
        LiveStatfs, NoCarHandoff, Seams, cumulative_evict_budget, drain_stop_tag, parse_serve_args,
        publisher_instance_hex_128, render_health, resolve_eviction_mode, validate_phase1_mode,
        DrainStop, EvictBudget, EvictionMode, GovernorStatus, RetentionLoop, ServeArgs,
        maybe_retry_recover,
    };

    #[test]
    fn resolve_eviction_mode_maps_all_eight_flag_combinations() {
        let cases = [
            ((false, false, false), EvictionMode::Inert),
            ((false, false, true), EvictionMode::Inert),
            ((false, true, false), EvictionMode::Inert),
            ((false, true, true), EvictionMode::Inert),
            ((true, false, false), EvictionMode::DryRun),
            ((true, false, true), EvictionMode::Armed),
            ((true, true, false), EvictionMode::DryRun),
            ((true, true, true), EvictionMode::DryRun),
        ];
        for ((enable_eviction, dry_run, allow_permanent_loss), expected) in cases {
            let mode = resolve_eviction_mode(enable_eviction, dry_run, allow_permanent_loss);
            assert_eq!(
                mode, expected,
                "unexpected mode for enable_eviction={enable_eviction}, dry_run={dry_run}, allow_permanent_loss={allow_permanent_loss}"
            );
        }
    }

    #[test]
    fn cumulative_evict_budget_adds_startup_deficit_when_below_target() {
        let budget = cumulative_evict_budget(90, 140, 10);
        assert_eq!(budget, 60);
    }

    #[test]
    fn cumulative_evict_budget_uses_cycle_cap_when_at_target() {
        let budget = cumulative_evict_budget(140, 140, 10);
        assert_eq!(budget, 10);
    }

    #[test]
    fn cumulative_evict_budget_uses_cycle_cap_when_above_target() {
        let budget = cumulative_evict_budget(200, 140, 10);
        assert_eq!(budget, 10);
    }

    #[test]
    fn cumulative_evict_budget_saturates_without_panicking() {
        let budget = cumulative_evict_budget(0, u64::MAX, u64::MAX);
        assert_eq!(budget, u64::MAX);
    }

    #[test]
    fn evict_budget_never_latches_during_honest_convergence() {
        let mut budget = EvictBudget::default();
        let target = 100;
        let per_cycle = 10;
        let mut free_before = 60;
        for _ in 0..4 {
            assert!(!budget.observe(false, free_before, target, per_cycle, per_cycle));
            assert!(!budget.latched);
            free_before = free_before.saturating_add(per_cycle);
        }
        assert!(!budget.observe(true, target, target, 0, per_cycle));
        assert!(!budget.latched);
        assert_eq!(budget.cumulative_freed, 0);
        assert!(!budget.episode_active);
    }

    #[test]
    fn evict_budget_sizes_per_episode_not_startup() {
        let mut budget = EvictBudget::default();
        let target = 1_000;
        let free_before = 100;
        let per_cycle = 100;
        assert!(!budget.observe(true, target, target, 0, per_cycle));
        for _ in 0..9 {
            assert!(!budget.observe(false, free_before, target, per_cycle, per_cycle));
            assert!(!budget.latched);
        }
        assert_eq!(budget.cumulative_freed, 900);
        assert_eq!(budget.episode_budget, 1_000);
    }

    #[test]
    fn evict_budget_latches_on_persistent_lying_statfs() {
        let mut budget = EvictBudget::default();
        let target = 100;
        let free_before = 95;
        let per_cycle = 10;
        assert!(!budget.observe(false, free_before, target, per_cycle, per_cycle));
        assert!(budget.observe(false, free_before, target, per_cycle, per_cycle));
        assert!(budget.latched);
        let state_after_latch = (
            budget.cumulative_freed,
            budget.episode_budget,
            budget.episode_active,
            budget.latched,
        );
        assert!(!budget.observe(false, free_before, target, per_cycle, per_cycle));
        assert_eq!(
            (
                budget.cumulative_freed,
                budget.episode_budget,
                budget.episode_active,
                budget.latched
            ),
            state_after_latch
        );
    }

    #[test]
    fn evict_budget_resets_on_healthy_checkpoint() {
        let mut budget = EvictBudget::default();
        let target = 100;
        let free_before = 80;
        let per_cycle = 10;
        assert!(!budget.observe(false, free_before, target, per_cycle, per_cycle));
        assert!(!budget.observe(false, free_before, target, per_cycle, per_cycle));
        assert!(budget.cumulative_freed > 0);
        assert!(budget.episode_active);
        assert!(!budget.observe(true, target, target, 0, per_cycle));
        assert_eq!(budget.cumulative_freed, 0);
        assert!(!budget.episode_active);
        assert!(!budget.latched);
    }

    #[test]
    fn evict_budget_no_safe_candidate_does_not_latch() {
        let mut budget = EvictBudget::default();
        let target = 100;
        let free_before = 60;
        let per_cycle = 10;
        for _ in 0..25 {
            assert!(!budget.observe(false, free_before, target, 0, per_cycle));
            assert!(!budget.latched);
        }
        assert_eq!(budget.cumulative_freed, 0);
    }

    #[test]
    fn evict_budget_sizes_to_exit_watermark_not_entry_no_false_latch_on_large_disk() {
        let gib = 1_u64 << 30;
        let total = 469_u64.saturating_mul(gib);
        let free_before = total.saturating_mul(14) / 100;
        let per_cycle = 8_u64.saturating_mul(gib);
        let exit_bytes = total.saturating_mul(17) / 100;
        let free_bytes = total.saturating_mul(15) / 100;
        let expected_freed = exit_bytes.saturating_sub(free_before);

        let mut good_target_budget = EvictBudget::default();
        let mut remaining = expected_freed;
        while remaining > 0 {
            let chunk = remaining.min(per_cycle);
            assert!(!good_target_budget.observe(false, free_before, exit_bytes, chunk, per_cycle));
            assert!(!good_target_budget.latched);
            remaining = remaining.saturating_sub(chunk);
        }
        assert_eq!(good_target_budget.cumulative_freed, expected_freed);
        assert!(!good_target_budget.observe(true, exit_bytes, exit_bytes, 0, per_cycle));
        assert!(!good_target_budget.latched);
        assert_eq!(good_target_budget.cumulative_freed, 0);
        assert!(!good_target_budget.episode_active);

        let mut entry_target_budget = EvictBudget::default();
        let mut remaining = expected_freed;
        let mut latched = false;
        while remaining > 0 {
            let chunk = remaining.min(per_cycle);
            if entry_target_budget.observe(false, free_before, free_bytes, chunk, per_cycle) {
                latched = true;
            }
            remaining = remaining.saturating_sub(chunk);
        }
        assert!(latched);
        assert!(entry_target_budget.latched);
    }

    #[test]
    fn parse_serve_args_rejects_zero_interval_secs() {
        let args = vec!["--interval-secs".to_owned(), "0".to_owned()];
        let err = parse_serve_args(&args).err();
        assert!(err.is_some());
        assert!(
            err.as_deref()
                .is_some_and(|message| message.contains("--interval-secs"))
        );
    }

    #[test]
    fn parse_serve_args_parses_new_phase1_flags() {
        let args = vec![
            "--archive-recent-only".to_owned(),
            "--no-delete".to_owned(),
            "--archive-root".to_owned(),
            "/data/teslausb/archive".to_owned(),
            "--volume-image".to_owned(),
            "/data/teslausb/teslacam.img".to_owned(),
        ];
        let parsed = match parse_serve_args(&args) {
            Ok(parsed) => parsed,
            Err(err) => panic!("parse args: {err}"),
        };
        assert!(parsed.archive_recent_only);
        assert!(parsed.no_delete);
        assert_eq!(
            parsed.archive_root.as_deref().and_then(std::path::Path::to_str),
            Some("/data/teslausb/archive")
        );
        assert_eq!(
            parsed.volume_image.to_str(),
            Some("/data/teslausb/teslacam.img")
        );
    }

    #[test]
    fn parse_serve_args_parses_eviction_arming_flags() {
        let args = vec![
            "--archive-recent-only".to_owned(),
            "--enable-eviction".to_owned(),
            "--dry-run".to_owned(),
            "--allow-permanent-loss".to_owned(),
            "--archive-root".to_owned(),
            "/data/teslausb/archive".to_owned(),
        ];
        let parsed = match parse_serve_args(&args) {
            Ok(parsed) => parsed,
            Err(err) => panic!("parse args: {err}"),
        };
        assert!(parsed.enable_eviction);
        assert!(parsed.dry_run);
        assert!(parsed.allow_permanent_loss);
        assert_eq!(parsed.recency_floor_secs, None);
    }

    #[test]
    fn parse_serve_args_parses_recency_floor_override() {
        let args = vec![
            "--archive-recent-only".to_owned(),
            "--enable-eviction".to_owned(),
            "--allow-permanent-loss".to_owned(),
            "--archive-root".to_owned(),
            "/data/teslausb/archive".to_owned(),
            "--recency-floor-secs".to_owned(),
            "259200".to_owned(),
        ];
        let parsed = match parse_serve_args(&args) {
            Ok(parsed) => parsed,
            Err(err) => panic!("parse args: {err}"),
        };
        assert_eq!(parsed.recency_floor_secs, Some(259_200));
    }

    #[test]
    fn parse_serve_args_rejects_nonpositive_recency_floor() {
        for bad in ["0", "-1"] {
            let args = vec![
                "--archive-recent-only".to_owned(),
                "--enable-eviction".to_owned(),
                "--recency-floor-secs".to_owned(),
                bad.to_owned(),
            ];
            let err = match parse_serve_args(&args) {
                Ok(_) => panic!("expected parse failure for {bad}"),
                Err(err) => err,
            };
            assert!(
                err.contains("--recency-floor-secs"),
                "unexpected error for {bad}: {err}"
            );
        }
    }

    #[test]
    fn parse_serve_args_parses_drain_only() {
        let args = vec![
            "--archive-recent-only".to_owned(),
            "--enable-eviction".to_owned(),
            "--allow-permanent-loss".to_owned(),
            "--drain-only".to_owned(),
            "--archive-root".to_owned(),
            "/data/teslausb/archive".to_owned(),
        ];
        let parsed = match parse_serve_args(&args) {
            Ok(parsed) => parsed,
            Err(err) => panic!("parse args: {err}"),
        };
        assert!(parsed.drain_only, "--drain-only should set drain_only");
        // Default is off when the flag is absent.
        let without = vec![
            "--archive-recent-only".to_owned(),
            "--enable-eviction".to_owned(),
        ];
        let parsed_without = match parse_serve_args(&without) {
            Ok(parsed) => parsed,
            Err(err) => panic!("parse args: {err}"),
        };
        assert!(
            !parsed_without.drain_only,
            "drain_only should default to false"
        );
    }

    #[test]
    fn parse_serve_args_rejects_drain_only_without_enable_eviction() {
        let args = vec![
            "--archive-recent-only".to_owned(),
            "--drain-only".to_owned(),
            "--archive-root".to_owned(),
            "/data/teslausb/archive".to_owned(),
        ];
        let err = match parse_serve_args(&args) {
            Ok(_) => panic!("expected parse failure"),
            Err(err) => err,
        };
        assert_eq!(
            err,
            "retentiond serve: --drain-only requires --enable-eviction."
        );
    }

    #[test]
    fn parse_serve_args_rejects_no_delete_with_enable_eviction() {
        let args = vec!["--no-delete".to_owned(), "--enable-eviction".to_owned()];
        let err = match parse_serve_args(&args) {
            Ok(_) => panic!("expected parse failure"),
            Err(err) => err,
        };
        assert_eq!(
            err,
            "retentiond serve: --no-delete and --enable-eviction are mutually exclusive."
        );
    }

    #[test]
    fn validate_phase1_mode_accepts_enable_eviction_without_no_delete() {
        let parsed = ServeArgs {
            archive_recent_only: true,
            no_delete: false,
            enable_eviction: true,
            dry_run: true,
            allow_permanent_loss: false,
            drain_only: false,
            recency_floor_secs: None,
            archive_root: Some(std::path::PathBuf::from("/data/teslausb/archive")),
            volume_image: std::path::PathBuf::from("/data/teslausb/teslacam.img"),
            indexd_socket: std::path::PathBuf::from("/run/teslausb/indexd.sock"),
            slot: 0,
            interval_secs: 20,
        };
        assert!(validate_phase1_mode(&parsed).is_ok());
    }

    #[test]
    fn render_health_serializes_expected_fields() {
        let raw = render_health(1234, 42, 1200);
        let value: serde_json::Value = match serde_json::from_str(&raw) {
            Ok(value) => value,
            Err(err) => panic!("render_health should produce valid json: {err}"),
        };
        assert_eq!(value["schema"], 1);
        assert_eq!(value["updated_at"], 1234);
        assert_eq!(value["running"], true);
        assert_eq!(value["pending"], 42);
        assert_eq!(value["last_progress_at"], 1200);
    }

    #[test]
    fn governor_status_serializes_expected_shape() {
        let publisher_instance = publisher_instance_hex_128();
        let status = GovernorStatus {
            schema: 2,
            updated_at: 1_700_000_000,
            uploads_allowed: false,
            seq: 7,
            interval_secs: 20,
            publisher_instance: publisher_instance.clone(),
            mode: "armed",
            drain_only: true,
            free_bytes: 50,
            total_bytes: 470,
            target_free_frac: 0.08,
            target_exit_frac: 0.10,
            recency_floor_secs: 3_600,
            last_stop: "already_healthy",
            last_bytes_freed: 0,
            last_items: 0,
        };
        let raw = match serde_json::to_string(&status) {
            Ok(raw) => raw,
            Err(err) => panic!("governor_status should serialize: {err}"),
        };
        let value: serde_json::Value = match serde_json::from_str(&raw) {
            Ok(value) => value,
            Err(err) => panic!("governor_status should be valid json: {err}"),
        };
        assert_eq!(value["schema"], 2);
        assert_eq!(value["updated_at"], 1_700_000_000);
        assert_eq!(value["uploads_allowed"], false);
        assert_eq!(value["seq"], 7);
        assert_eq!(value["interval_secs"], 20);
        assert_eq!(value["publisher_instance"], publisher_instance);
        assert_eq!(value["mode"], "armed");
        assert_eq!(value["drain_only"], true);
        assert_eq!(value["free_bytes"], 50);
        assert_eq!(value["total_bytes"], 470);
        assert_eq!(value["target_free_frac"], 0.08);
        assert_eq!(value["target_exit_frac"], 0.10);
        assert_eq!(value["recency_floor_secs"], 3_600);
        assert_eq!(value["last_stop"], "already_healthy");
        assert_eq!(value["last_bytes_freed"], 0);
        assert_eq!(value["last_items"], 0);
    }

    fn assessment_with_tier(tier: Tier) -> GovernorAssessment {
        GovernorAssessment {
            tier,
            shared_device: false,
            root_reserve_breached: false,
            sparse_image_warning: false,
            space_tier: tier,
            inode_tier: Tier::Healthy,
            data_free_bytes: 0,
            data_free_inodes: 0,
            usable_for_archive_bytes: 0,
        }
    }

    #[test]
    fn upload_backpressure_tracks_emergency_threshold() {
        let cfg = RetentionConfig::default();
        let archive_root = "/data/teslausb/archive";
        let read = VolumeReadFileClient::new("/data/teslausb/teslacam.img", 0);
        let store = LiveArchiveStore::new(Box::new(read), archive_root);
        let shared = Rc::new(IndexDeleteClient::new("/run/teslausb/indexd.sock"));
        let clock = LiveClock;
        let rand = LiveRand;
        let statfs = LiveStatfs;
        let handoff = NoCarHandoff;
        let fs = LiveArchiveDeleteOps::new(archive_root);
        let index = LiveIndexClient::new(Rc::clone(&shared));
        let catalog = LiveCatalog::new(
            Rc::clone(&shared),
            archive_root,
            format!("{archive_root}/.retention-trash"),
        );
        let seams = Seams {
            clock: &clock,
            store: &store,
            handoff: &handoff,
            statfs: &statfs,
            fs: &fs,
            index: &index,
            catalog: &catalog,
            rand: &rand,
        };
        let rl = RetentionLoop::new(&cfg, seams, format!("{archive_root}/.retention-trash"));

        for tier in [
            Tier::Healthy,
            Tier::Low,
            Tier::Critical,
            Tier::Emergency,
            Tier::Exhausted,
        ] {
            let assessment = assessment_with_tier(tier);
            assert_eq!(
                rl.upload_backpressure(&assessment).uploads_allowed,
                tier < Tier::Emergency
            );
        }
    }

    #[test]
    fn governor_status_seq_increases_and_publisher_instance_is_stable() {
        let publisher_instance = publisher_instance_hex_128();
        assert!(!publisher_instance.is_empty());

        let first = GovernorStatus {
            schema: 2,
            updated_at: 1_700_000_000,
            uploads_allowed: true,
            seq: 1,
            interval_secs: 20,
            publisher_instance: publisher_instance.clone(),
            mode: "armed",
            drain_only: false,
            free_bytes: 100,
            total_bytes: 200,
            target_free_frac: 0.08,
            target_exit_frac: 0.10,
            recency_floor_secs: 3_600,
            last_stop: "target_reached",
            last_bytes_freed: 50,
            last_items: 1,
        };
        let second = GovernorStatus {
            schema: first.schema,
            updated_at: first.updated_at,
            uploads_allowed: first.uploads_allowed,
            seq: first.seq.saturating_add(1),
            interval_secs: first.interval_secs,
            publisher_instance: publisher_instance.clone(),
            mode: first.mode,
            drain_only: first.drain_only,
            free_bytes: first.free_bytes,
            total_bytes: first.total_bytes,
            target_free_frac: first.target_free_frac,
            target_exit_frac: first.target_exit_frac,
            recency_floor_secs: first.recency_floor_secs,
            last_stop: first.last_stop,
            last_bytes_freed: first.last_bytes_freed,
            last_items: first.last_items,
        };

        assert!(second.seq > first.seq);
        assert_eq!(first.publisher_instance, second.publisher_instance);
    }

    #[test]
    fn drain_stop_tag_maps_all_variants() {
        assert_eq!(drain_stop_tag(&DrainStop::TargetReached), "target_reached");
        assert_eq!(drain_stop_tag(&DrainStop::ByteCapReached), "byte_cap");
        assert_eq!(drain_stop_tag(&DrainStop::CountCapReached), "count_cap");
        assert_eq!(drain_stop_tag(&DrainStop::WallClockCapReached), "wall_cap");
        assert_eq!(drain_stop_tag(&DrainStop::ShutdownRequested), "shutdown");
        assert_eq!(
            drain_stop_tag(&DrainStop::NoSafeCandidate),
            "no_safe_candidate"
        );
        assert_eq!(drain_stop_tag(&DrainStop::AlreadyHealthy), "already_healthy");
        assert_eq!(
            drain_stop_tag(&DrainStop::AnomalyRefused {
                bytes_to_free: 1,
                total_bytes: 2
            }),
            "anomaly_refused"
        );
        assert_eq!(
            drain_stop_tag(&DrainStop::DeleteFailed {
                reason: "x".to_owned()
            }),
            "delete_failed"
        );
        assert_eq!(
            drain_stop_tag(&DrainStop::StatCheckFailed {
                reason: "x".to_owned()
            }),
            "stat_check_failed"
        );
    }

    #[test]
    fn maybe_retry_recover_retries_until_success_then_stops() {
        let calls = std::cell::Cell::new(0_u32);
        let mut recovered = false;

        maybe_retry_recover(
            || {
                calls.set(calls.get().saturating_add(1));
                Err("first fail")
            },
            &mut recovered,
            false,
        );
        assert!(!recovered);
        assert_eq!(calls.get(), 1);

        maybe_retry_recover(
            || {
                calls.set(calls.get().saturating_add(1));
                Err("second fail")
            },
            &mut recovered,
            true,
        );
        assert!(!recovered);
        assert_eq!(calls.get(), 2);

        maybe_retry_recover(
            || {
                calls.set(calls.get().saturating_add(1));
                Ok::<usize, &'static str>(7)
            },
            &mut recovered,
            true,
        );
        assert!(recovered);
        assert_eq!(calls.get(), 3);

        maybe_retry_recover(
            || {
                calls.set(calls.get().saturating_add(1));
                Ok::<usize, &'static str>(99)
            },
            &mut recovered,
            true,
        );
        assert!(recovered);
        assert_eq!(calls.get(), 3);
    }
}
