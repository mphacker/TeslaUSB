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

use std::path::PathBuf;
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
}

fn main() -> ExitCode {
    install_tracing();
    let cli = Cli::parse();
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

fn real_main(opts: RunOptions) -> Result<ShutdownReason> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .context("building tokio current-thread runtime")?;
    runtime.block_on(run(opts))
}
