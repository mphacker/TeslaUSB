//! `teslafat` — userspace FAT/`exFAT` synthesizer + NBD server.
//!
//! Speaks the NBD newstyle protocol to a kernel `nbd-client` which
//! in turn backs the `g_mass_storage` USB gadget exposed to the
//! vehicle. The synthesised FAT/`exFAT` view is computed from a
//! real Linux directory tree (`backing_root`) at request time;
//! writes from the vehicle decode back into native file operations.
//!
//! ## Phase 1.1 state (this commit)
//!
//! Bootstrap only: `clap` CLI parsing, JSON tracing to stderr (level
//! via `RUST_LOG`), TOML config loader, and the "started" sentinel
//! log line that the Phase 1.1 integration test asserts on. NBD
//! listen + IPC sockets are deferred to Phases 1.3 / 1.5 — they
//! land alongside the handshake port and the per-connection
//! transmission loop respectively. Exit returns cleanly immediately
//! after the sentinel so `cargo run -p teslafat -- --config <file>`
//! is observable and scriptable end-to-end.

#![cfg_attr(test, allow(clippy::unwrap_used))]

use std::path::PathBuf;
use std::process::ExitCode;

use anyhow::{Context, Result};
use clap::Parser;
use tracing::{error, info};
use tracing_subscriber::{EnvFilter, fmt};

use teslafat::config::Config;

/// `teslafat` (FAT/exFAT synthesizer + NBD server) CLI.
#[derive(Debug, Parser)]
#[command(
    name = "teslafat",
    version,
    about = "FAT/exFAT synthesizer + NBD server (TeslaUSB B-1)",
    long_about = None,
)]
struct Args {
    /// Path to the TOML config file.
    #[arg(short, long, default_value = "/etc/teslausb/teslafat.toml")]
    config: PathBuf,
}

fn install_tracing() {
    // `EnvFilter::try_from_default_env` parses `RUST_LOG`; an unset
    // or malformed value falls back to a sensible default rather
    // than panicking the daemon.
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    fmt()
        .json()
        .with_env_filter(filter)
        .with_writer(std::io::stderr)
        .init();
}

fn run(args: &Args) -> Result<()> {
    let cfg =
        Config::load(&args.config).with_context(|| format!("loading {}", args.config.display()))?;

    info!(
        version = env!("CARGO_PKG_VERSION"),
        config_path = %args.config.display(),
        backing_root = %cfg.backing_root.display(),
        volume_size_gb = cfg.volume_size_gb,
        volume_label = %cfg.volume_label,
        retention_hide_after_s = cfg.retention.recentclips_hide_after_seconds,
        "started"
    );

    // NBD listen + IPC sockets land in Phases 1.3 / 1.5. Phase 1.1
    // verifies only that bootstrap (CLI parse, tracing init, config
    // load, sentinel emit) succeeds end-to-end; exit cleanly here.
    Ok(())
}

fn main() -> ExitCode {
    install_tracing();
    let args = Args::parse();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            // `Debug` formats the full anyhow context chain, so the
            // operator sees both "loading /etc/.../teslafat.toml"
            // and the underlying I/O / parse / validation cause.
            error!(error = ?e, "fatal");
            ExitCode::FAILURE
        }
    }
}
