//! Task supervisor for the worker daemon.
//!
//! Single async entry point that orchestrates the three
//! long-running subsystems:
//!
//! * **Indexer bootstrap** — runs once at startup, on a blocking
//!   thread because rusqlite is sync. Records every clip the
//!   walker can see that the store does not yet know about.
//! * **Clip watcher** — `ClipWatcher::next_batch` blocks on
//!   `inotify` for new `CLOSE_WRITE`/`MOVED_TO` events. Runs on a
//!   dedicated [`tokio::task::spawn_blocking`] thread and hands
//!   batches back via a tokio `mpsc` channel.
//! * **Cleanup sweep** — fires on a [`tokio::time::interval`]
//!   tick; each tick runs `Cleanup::run_once` on the blocking
//!   pool (synchronous `std::fs` + rusqlite).
//!
//! Shutdown is unified: a SIGTERM/SIGINT or a fatal subsystem
//! error wakes the `tokio::select!` and the supervisor drains.
//! See ADR-0013 for the runtime choice.
//!
//! ## Design — why pure-logic helpers
//!
//! The async wiring (`run`) is hard to unit-test without
//! standing up a real inotify FD and a real `SQLite` file. The
//! parts that *can* be tested deterministically are pulled out
//! into small helpers:
//!
//! * [`cleanup_interval_with_floor`] enforces a minimum tick
//!   period so a typo in `interval_seconds` cannot pin a CPU.
//! * [`ShutdownReason`] is an enum with an `is_fatal` method
//!   so the supervisor's "did this subsystem die?" branch is
//!   policy-as-data rather than scattered match arms.

use std::path::PathBuf;
use std::time::Duration;

use anyhow::{Context, Result};
#[cfg(target_os = "linux")]
use tracing::warn;
use tracing::{error, info};

use crate::cleanup::Cleanup;
#[cfg(target_os = "linux")]
use crate::cleanup_sweep;
use crate::config::Config;
use crate::indexer::Indexer;
#[cfg(target_os = "linux")]
use crate::lun_pressure::lun_size_bytes;
#[cfg(target_os = "linux")]
use crate::storage_config::StorageConfig;
use crate::store::Store;

/// Path of the AC.1 shared storage config. The supervisor
/// re-reads this file on every cleanup tick so live edits made
/// through the web UI's `/storage` page propagate to the
/// tier-aware sweep ([`cleanup_sweep`]) within one tick without
/// requiring a worker restart. The file is tiny (~1 KB), so the
/// per-tick read cost is negligible.
#[cfg(target_os = "linux")]
const STORAGE_CONFIG_PATH: &str = "/etc/teslausb/teslausb.toml";

/// Smallest cleanup-tick period we will honour. A misconfigured
/// `interval_seconds = 1` (or `2`) would otherwise spin the
/// cleanup task tight enough to load a Pi noticeably; this
/// floor is defence in depth on top of the config's `> 0`
/// rejection.
pub const MIN_CLEANUP_INTERVAL: Duration = Duration::from_secs(5);

/// Buffered channel capacity for watcher-to-supervisor event
/// hand-off. Sized at "a few batches" — inotify already
/// coalesces, so this buffer absorbs bursts without holding
/// onto the lock pool. Pure-logic; exposed for tests.
pub const WATCHER_CHANNEL_CAPACITY: usize = 64;

/// What woke the supervisor's outer `select!`. Pulled out as
/// an enum so the post-shutdown log line and the integration
/// test can both read the reason without `Debug`-printing an
/// untyped string.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShutdownReason {
    /// SIGTERM received (systemd stop, container kill, etc.).
    Sigterm,
    /// SIGINT received (Ctrl-C from a dev terminal).
    Sigint,
    /// The watcher's blocking thread returned `Err` or its
    /// channel closed unexpectedly. Treated as fatal; systemd
    /// will restart the service.
    WatcherFailed,
    /// The indexer's bootstrap pass failed. Treated as fatal.
    IndexerBootstrapFailed,
    /// A cleanup tick returned `Err`. Treated as fatal so we
    /// don't silently rot through the day.
    CleanupFailed,
    /// Caller asked for a one-shot bootstrap-only run (used by
    /// tests and `--bootstrap-only` operator flag).
    BootstrapOnly,
}

impl ShutdownReason {
    /// Whether this reason warrants a non-zero exit code. The
    /// "graceful" reasons (signals, bootstrap-only) exit zero
    /// so systemd doesn't loop-restart on `systemctl stop`.
    #[must_use]
    pub const fn is_fatal(self) -> bool {
        match self {
            Self::Sigterm | Self::Sigint | Self::BootstrapOnly => false,
            Self::WatcherFailed | Self::IndexerBootstrapFailed | Self::CleanupFailed => true,
        }
    }
}

/// Floor `requested` at [`MIN_CLEANUP_INTERVAL`]. Pure logic;
/// see module docs for rationale.
#[must_use]
pub fn cleanup_interval_with_floor(requested: Duration) -> Duration {
    if requested < MIN_CLEANUP_INTERVAL {
        MIN_CLEANUP_INTERVAL
    } else {
        requested
    }
}

/// Options driving a [`run`] invocation. Carved out so the
/// integration test can flip `bootstrap_only` without touching
/// the on-disk config.
#[derive(Debug, Clone)]
pub struct RunOptions {
    /// Absolute path to the worker config TOML.
    pub config_path: PathBuf,
    /// When `true`, perform the indexer bootstrap pass and
    /// then exit successfully. Operator flag for verifying a
    /// fresh deployment.
    pub bootstrap_only: bool,
}

/// Install the worker's tracing subscriber: JSON to stderr,
/// `RUST_LOG`-style env-filter (default `info`). Idempotent
/// — repeated calls are no-ops because `try_init` swallows
/// the "already set" error.
pub fn install_tracing() {
    use tracing_subscriber::EnvFilter;
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let _ = tracing_subscriber::fmt()
        .json()
        .with_env_filter(filter)
        .with_writer(std::io::stderr)
        .try_init();
}

/// Top-level supervisor entry point. Boots the store, opens
/// the watcher, runs bootstrap, then enters the steady-state
/// loop until a [`ShutdownReason`] fires.
///
/// # Errors
///
/// Returns `Err` if the config fails to load/validate, if the
/// store cannot be opened, or if a subsystem panics. Routine
/// fatal reasons (`WatcherFailed`, etc.) are surfaced as the
/// [`ShutdownReason`] return value and the function still
/// returns `Ok` — the binary's `main` translates the reason
/// into an exit code so systemd sees the right signal.
pub async fn run(opts: RunOptions) -> Result<ShutdownReason> {
    let config = Config::load(&opts.config_path)
        .with_context(|| format!("loading config from {}", opts.config_path.display()))?;

    info!(
        backing_root = %config.backing_root.display(),
        db_path = %config.db_path.display(),
        bootstrap_only = opts.bootstrap_only,
        "supervisor starting",
    );

    let store = Store::open(&config.db_path)
        .with_context(|| format!("opening store at {}", config.db_path.display()))?;

    // Bootstrap pass on the blocking pool. The indexer owns
    // the store while it walks, so we get it back via the
    // `Indexer::into_store` consume-and-return idiom.
    let indexer = Indexer::new(config.clone(), store);
    let (mut indexer, bootstrap_outcome) = tokio::task::spawn_blocking(move || {
        let mut indexer = indexer;
        let outcome = indexer.bootstrap();
        (indexer, outcome)
    })
    .await
    .context("indexer bootstrap task panicked")?;

    match bootstrap_outcome {
        Ok(summary) => {
            info!(
                seen = summary.seen,
                indexed = summary.indexed,
                failed = summary.failed,
                "indexer bootstrap complete",
            );
        }
        Err(e) => {
            error!(error = %e, "indexer bootstrap failed");
            return Ok(ShutdownReason::IndexerBootstrapFailed);
        }
    }

    if opts.bootstrap_only {
        info!("bootstrap-only mode — exiting after bootstrap");
        return Ok(ShutdownReason::BootstrapOnly);
    }

    let cfg_for_cleanup = config.clone();
    let interval = cleanup_interval_with_floor(config.cleanup.interval());
    let cleanup = Cleanup::new(cfg_for_cleanup);

    // Steady-state loop. Watcher events come via mpsc from the
    // blocking thread; cleanup ticks on a tokio interval;
    // signals are tokio::signal handles. The first arm to fire
    // decides the shutdown reason.
    //
    // Indexer and cleanup calls run inline. Both are
    // synchronous and bounded (one MP4 parse / one statvfs +
    // sqlite sweep); the reactor is current-thread with no
    // other in-flight async work, so blocking it briefly is
    // cheaper than the mem-replace + spawn_blocking dance.
    // The *watcher* still runs on its own blocking thread
    // because `next_batch` blocks indefinitely on inotify.
    #[cfg(target_os = "linux")]
    let reason = steady_state_linux(&config, &mut indexer, &cleanup, interval).await?;

    #[cfg(not(target_os = "linux"))]
    let reason = steady_state_non_linux(&mut indexer, &cleanup, interval).await?;

    let _ = indexer; // explicit: keep alive through the loop
    info!(reason = ?reason, "supervisor stopped");
    Ok(reason)
}

#[cfg(target_os = "linux")]
async fn steady_state_linux(
    config: &Config,
    indexer: &mut Indexer,
    cleanup: &Cleanup,
    interval: Duration,
) -> Result<ShutdownReason> {
    use tokio::signal::unix::{SignalKind, signal};
    use tokio::sync::mpsc;

    let (tx, mut rx) = mpsc::channel::<crate::watcher::WatchEvent>(WATCHER_CHANNEL_CAPACITY);
    let watcher_handle = spawn_watcher(config, tx)?;

    let mut sigterm = signal(SignalKind::terminate()).context("installing SIGTERM handler")?;
    let mut sigint = signal(SignalKind::interrupt()).context("installing SIGINT handler")?;

    let mut tick = tokio::time::interval(interval);
    // First tick fires immediately; skip it so we don't sweep
    // before the bootstrap pass's effects have settled on
    // disk. Subsequent ticks are on the requested cadence.
    tick.tick().await;

    let reason = loop {
        tokio::select! {
            biased;
            _ = sigterm.recv() => break ShutdownReason::Sigterm,
            _ = sigint.recv()  => break ShutdownReason::Sigint,
            // Cleanup tick is polled BEFORE watcher events so a
            // chatty inotify stream (one CLOSE_WRITE per camera per
            // minute = ~5/min sustained) can never starve the
            // periodic cleanup. The mpsc channel buffers any
            // queued events for the next loop iteration; no events
            // are lost. Without this priority a continuously-busy
            // Tesla can lock out cleanup indefinitely — observed
            // on cybertruckusb.local 2026-05-23 (no tick fired in
            // 11+ min while LUN was at 102% fill).
            _ = tick.tick() => {
                // Three-phase cleanup tick (intentionally additive):
                //   1. `cleanup.run_once` — age-based RecentClips
                //      retention (`retention_days` from worker.toml).
                //      Enforces "delete clips older than N days"
                //      regardless of free-space pressure.
                //   2. `cleanup.gc_orphans` — drop index rows whose
                //      backing files vanished.
                //   3. `cleanup_sweep::sweep_to_target_now` (AC.7) —
                //      tier-aware free-space sweep keyed off
                //      `target_free_pct` in /etc/teslausb/teslausb.toml.
                //      Runs AFTER `run_once` so the age-based deletes
                //      are already reflected in the LUN-fill reading;
                //      any NotFound from a race with `run_once` is
                //      logged at WARN and counted in
                //      `SweepSummary::failed`, never fatal.
                // The two delete paths are complementary, not
                // redundant: `run_once` ensures Tesla never sees
                // a clip older than `retention_days` (even when
                // the LUN has plenty of free space), while the
                // sweep keeps a free-space floor for incoming
                // writes (even when no clip is "old" yet).
                //
                // Both phases use the SAME shared StorageConfig
                // snapshot for this tick so they agree on the LUN
                // size — see ADR-0018. A missing/invalid file
                // means lun_size_bytes = 0, which disables BOTH
                // the pressure floor (in cleanup.run_once) and the
                // sweep (it no-ops).
                let storage_cfg = StorageConfig::load(std::path::Path::new(STORAGE_CONFIG_PATH))
                    .unwrap_or_else(|e| {
                        warn!(error = %e,
                            "storage_config load failed; LUN pressure disabled this tick");
                        StorageConfig::default()
                    });
                let lun_bytes = lun_size_bytes(storage_cfg.storage.teslacam_gb);
                match cleanup.run_once(indexer.store(), lun_bytes) {
                    Ok(summary) => {
                        info!(
                            considered = summary.considered,
                            preserved_gps = summary.preserved_gps,
                            deleted = summary.deleted,
                            failed = summary.failed,
                            pressure = summary.pressure,
                            "cleanup tick complete",
                        );
                    }
                    Err(e) => {
                        error!(error = %e, "cleanup tick failed");
                        break ShutdownReason::CleanupFailed;
                    }
                }
                // Second pass on the same tick: drop index
                // rows whose backing files have disappeared
                // (power-cut truncations, manual deletions,
                // exFAT recovery). Per-row failures are
                // non-fatal; only a fundamental list_all_clips
                // failure breaks the supervisor.
                match cleanup.gc_orphans(indexer.store()) {
                    Ok(gc) => {
                        info!(
                            scanned = gc.scanned,
                            removed = gc.removed,
                            failed = gc.failed,
                            skipped_unsafe = gc.skipped_unsafe,
                            "gc_orphans tick complete",
                        );
                    }
                    Err(e) => {
                        error!(error = %e, "gc_orphans tick failed");
                        break ShutdownReason::CleanupFailed;
                    }
                }
                // AC.7: tier-aware continuous sweep
                // (cleanup_sweep) enforces the operator-configured
                // `target_free_pct` floor on the TeslaCam LUN.
                // Re-uses the storage_cfg / lun_bytes snapshot
                // computed above so all three phases of this tick
                // agree on what the LUN looks like.
                match cleanup_sweep::sweep_to_target_now(
                    indexer.store(),
                    &config.backing_root,
                    &storage_cfg,
                    lun_bytes,
                ) {
                    Ok(s) => info!(
                        deleted_tier_a = s.deleted_tier_a,
                        deleted_tier_b = s.deleted_tier_b,
                        deleted_tier_c_age = s.deleted_tier_c_age,
                        deleted_tier_c_last_resort = s.deleted_tier_c_last_resort,
                        failed = s.failed,
                        initial_free_pct = s.initial_free_pct,
                        final_free_pct = s.final_free_pct,
                        target_pct = s.target_pct,
                        target_reached = s.target_reached,
                        "cleanup_sweep tick complete",
                    ),
                    Err(e) => warn!(error = %e, "cleanup_sweep tick failed (non-fatal)"),
                }
            }
            maybe_event = rx.recv() => {
                let Some(event) = maybe_event else {
                    error!("watcher channel closed unexpectedly");
                    break ShutdownReason::WatcherFailed;
                };
                if let Err(e) = indexer.handle_event(&event) {
                    warn!(error = %e, "indexer.handle_event failed (per-clip; not fatal)");
                }
            }
        }
    };

    drop(rx);
    watcher_handle.abort();
    // Best-effort join; the blocking thread may be parked in
    // read_events_blocking and will only notice when the FD
    // is dropped by abort. We don't await it.
    Ok(reason)
}

#[cfg(target_os = "linux")]
fn spawn_watcher(
    config: &Config,
    tx: tokio::sync::mpsc::Sender<crate::watcher::WatchEvent>,
) -> Result<tokio::task::JoinHandle<()>> {
    let mut watcher = crate::watcher::ClipWatcher::new(config).context("opening clip watcher")?;
    Ok(tokio::task::spawn_blocking(move || {
        loop {
            match watcher.next_batch() {
                Ok(events) => {
                    for event in events {
                        if tx.blocking_send(event).is_err() {
                            // Supervisor dropped the receiver —
                            // shutdown in progress, exit the thread.
                            return;
                        }
                    }
                }
                Err(e) => {
                    warn!(error = %e, "watcher next_batch failed; exiting watcher thread");
                    return;
                }
            }
        }
    }))
}

#[cfg(not(target_os = "linux"))]
async fn steady_state_non_linux(
    indexer: &mut Indexer,
    cleanup: &Cleanup,
    interval: Duration,
) -> Result<ShutdownReason> {
    // Dev-workstation fallback: no inotify, no SIGTERM. Spin
    // cleanup on the requested cadence and wait for Ctrl-C.
    // This path exists so `cargo run` works on the dev box for
    // sanity checks of the cleanup loop without standing up a
    // Linux VM.
    let mut tick = tokio::time::interval(interval);
    tick.tick().await;
    let reason = loop {
        tokio::select! {
            biased;
            r = tokio::signal::ctrl_c() => {
                r.context("installing Ctrl-C handler")?;
                break ShutdownReason::Sigint;
            }
            _ = tick.tick() => {
                // Non-Linux dev fallback: no /etc config, just
                // pass lun_size_bytes = 0 so pressure is disabled
                // (this code path is not reached on the live Pi).
                if let Err(e) = cleanup.run_once(indexer.store(), 0) {
                    error!(error = %e, "cleanup tick failed");
                    break ShutdownReason::CleanupFailed;
                }
                if let Err(e) = cleanup.gc_orphans(indexer.store()) {
                    error!(error = %e, "gc_orphans tick failed");
                    break ShutdownReason::CleanupFailed;
                }
            }
        }
    };
    Ok(reason)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    #[test]
    fn cleanup_interval_floor_applies_to_low_value() {
        assert_eq!(
            cleanup_interval_with_floor(Duration::from_secs(1)),
            MIN_CLEANUP_INTERVAL,
        );
        assert_eq!(
            cleanup_interval_with_floor(Duration::ZERO),
            MIN_CLEANUP_INTERVAL,
        );
    }

    #[test]
    fn cleanup_interval_floor_preserves_normal_value() {
        let one_min = Duration::from_secs(60);
        assert_eq!(cleanup_interval_with_floor(one_min), one_min);
    }

    #[test]
    fn cleanup_interval_floor_preserves_exact_minimum() {
        assert_eq!(
            cleanup_interval_with_floor(MIN_CLEANUP_INTERVAL),
            MIN_CLEANUP_INTERVAL,
        );
    }

    #[test]
    fn shutdown_reason_signals_are_not_fatal() {
        assert!(!ShutdownReason::Sigterm.is_fatal());
        assert!(!ShutdownReason::Sigint.is_fatal());
    }

    #[test]
    fn shutdown_reason_bootstrap_only_is_not_fatal() {
        assert!(!ShutdownReason::BootstrapOnly.is_fatal());
    }

    #[test]
    fn shutdown_reason_subsystem_failures_are_fatal() {
        assert!(ShutdownReason::WatcherFailed.is_fatal());
        assert!(ShutdownReason::IndexerBootstrapFailed.is_fatal());
        assert!(ShutdownReason::CleanupFailed.is_fatal());
    }

    #[test]
    fn shutdown_reason_is_copy() {
        // Doubles as a guard that we don't accidentally add a
        // non-Copy variant (would break the supervisor's
        // `break reason` style).
        fn assert_copy<T: Copy>() {}
        assert_copy::<ShutdownReason>();
    }

    #[test]
    fn watcher_channel_capacity_is_power_of_two_for_easier_tuning() {
        // Soft invariant: powers of two keep mental math
        // simple when reasoning about high-water marks. If
        // this needs to grow non-power-of-two later, drop the
        // test — it has no functional effect.
        assert!(WATCHER_CHANNEL_CAPACITY.is_power_of_two());
    }

    #[test]
    fn install_tracing_is_idempotent() {
        // First call wins; the second is a no-op (try_init
        // returns Err which we swallow). This test mostly
        // exists to make sure we don't accidentally use
        // `init()` (which would panic on the second call).
        install_tracing();
        install_tracing();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn run_with_missing_config_returns_load_error() {
        let opts = RunOptions {
            config_path: PathBuf::from("/does/not/exist/teslausb-worker.toml"),
            bootstrap_only: true,
        };
        let err = run(opts).await.expect_err("missing config must error");
        let msg = format!("{err:#}");
        assert!(msg.contains("loading config"), "got: {msg}");
    }

    #[cfg(target_os = "linux")]
    #[tokio::test(flavor = "current_thread")]
    async fn run_bootstrap_only_returns_bootstrap_only_reason() {
        // End-to-end: write a minimal config + empty bucket
        // dirs, run the supervisor in bootstrap-only mode, and
        // assert the reason. Skipped on non-Linux because the
        // non-linux steady-state path doesn't bootstrap the
        // store the same way.
        let tmp = tempfile::tempdir().unwrap();
        let backing = tmp.path().join("backing");
        for sub in &["RecentClips", "SavedClips", "SentryClips"] {
            std::fs::create_dir_all(backing.join(sub)).unwrap();
        }
        let db = tmp.path().join("worker.db");
        let cfg_path = tmp.path().join("worker.toml");
        let toml = format!(
            "backing_root = \"{}\"\ndb_path = \"{}\"\n\n[cleanup]\nretention_days = 7\nmin_free_pct = 0\npreserve_with_gps = true\ninterval_seconds = 5\n",
            backing.to_string_lossy(),
            db.to_string_lossy(),
        );
        std::fs::write(&cfg_path, toml).unwrap();
        let reason = run(RunOptions {
            config_path: cfg_path,
            bootstrap_only: true,
        })
        .await
        .unwrap();
        assert_eq!(reason, ShutdownReason::BootstrapOnly);
    }
}
