//! `teslausb-worker` binary entry point.
//!
//! Thin wrapper around [`teslausb_worker::supervisor::run`] —
//! parses CLI flags, installs tracing, runs the supervisor on
//! a tokio current-thread runtime, translates the
//! [`ShutdownReason`] into a process exit code. All
//! orchestration logic lives in the `supervisor` module so it
//! stays unit-testable; `main.rs` carries only the bits that
//! cannot be tested without spawning a real process.
//!
//! See ADR-0013 for the runtime choice.

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use anyhow::{Context, Result};
use clap::Parser;

use teslausb_worker::supervisor::{RunOptions, ShutdownReason, install_tracing, run};

/// `TeslaUSB` B-1 background worker.
///
/// Indexes new dashcam/sentry clips (SEI → GPS waypoints in
/// `SQLite`) and reaps no-GPS `RecentClips` clips per the
/// configured retention + free-space policy.
#[derive(Debug, Parser)]
#[command(version, about, long_about = None)]
struct Cli {
    /// Path to the worker config TOML.
    #[arg(short, long, value_name = "PATH")]
    config: PathBuf,

    /// Run the indexer bootstrap pass once and exit. Useful
    /// after a fresh deploy or after wiping the `SQLite` db.
    #[arg(long)]
    bootstrap_only: bool,

    /// Load and validate the config, then exit. Used as the
    /// systemd `ExecStartPre` gate so a malformed config
    /// surfaces in journalctl before the supervisor starts
    /// the watcher / opens the store.
    #[arg(long)]
    check_config: bool,

    /// Rebuild the materialised `trips` / `detected_events` /
    /// `clip_trip_map` tables from the current `clips` +
    /// `waypoints` rows, then exit. Idempotent (full rebuild
    /// inside a single transaction). Useful after a schema
    /// migration adds new derived columns, or to backfill on
    /// a device that hasn't completed bootstrap yet.
    #[arg(long)]
    rebuild_trips: bool,
}

fn main() -> ExitCode {
    install_tracing();
    let cli = Cli::parse();
    if cli.check_config {
        match teslausb_worker::config::Config::load(&cli.config) {
            Ok(_) => {
                tracing::info!(
                    config = %cli.config.display(),
                    "--check-config: config OK",
                );
                return ExitCode::SUCCESS;
            }
            Err(e) => {
                tracing::error!(error = ?e, "--check-config: config invalid");
                return ExitCode::from(2);
            }
        }
    }
    if cli.rebuild_trips {
        return run_rebuild_trips(&cli.config);
    }
    let opts = RunOptions {
        config_path: cli.config,
        bootstrap_only: cli.bootstrap_only,
    };
    match real_main(opts) {
        Ok(reason) => {
            if reason.is_fatal() {
                tracing::error!(reason = ?reason, "worker exiting with fatal shutdown");
                ExitCode::from(1)
            } else {
                ExitCode::SUCCESS
            }
        }
        Err(e) => {
            // anyhow's chain format includes the full context
            // chain on one line; readable in journalctl.
            tracing::error!(error = ?e, "worker failed to start");
            ExitCode::from(2)
        }
    }
}

fn run_rebuild_trips(config: &Path) -> ExitCode {
    let cfg = match teslausb_worker::config::Config::load(config) {
        Ok(c) => c,
        Err(e) => {
            tracing::error!(error = ?e, "--rebuild-trips: config invalid");
            return ExitCode::from(2);
        }
    };
    let mut store = match teslausb_worker::store::Store::open(&cfg.db_path) {
        Ok(s) => s,
        Err(e) => {
            tracing::error!(
                error = ?e,
                db_path = %cfg.db_path.display(),
                "--rebuild-trips: failed to open DB",
            );
            return ExitCode::from(2);
        }
    };
    match store.rebuild_trips_now() {
        Ok(stats) => {
            tracing::info!(?stats, "--rebuild-trips: rebuild complete");
            ExitCode::SUCCESS
        }
        Err(e) => {
            tracing::error!(error = ?e, "--rebuild-trips: rebuild failed");
            ExitCode::from(1)
        }
    }
}

fn real_main(opts: RunOptions) -> Result<ShutdownReason> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .context("building tokio current-thread runtime")?;
    runtime.block_on(run(opts))
}
