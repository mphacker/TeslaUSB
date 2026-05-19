//! teslafat — userspace FAT32 synthesizer + NBD server
//!
//! Entry point. Parses CLI, loads config, sets up logging, opens
//! the NBD listen socket, and serves the kernel NBD client until
//! shutdown.
//!
//! See `docs/architecture.md` for the big picture and
//! `docs/fat32-synthesis.md` for design choices in the synthesizer.

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::Parser;
use tracing::{info, warn};

mod backend;
mod config;
mod fat32;
mod ipc;
mod nbd;
mod retention;

use backend::dir_tree::DirTreeBackend;
use config::Config;

/// Default location for the NBD listen socket. The kernel NBD
/// client (via nbd-client(8)) connects here, and `g_mass_storage`
/// is configured with `file=/dev/nbd0`.
const DEFAULT_NBD_SOCKET: &str = "/run/teslafat/nbd.sock";

/// Default location for the control/IPC socket the Python web UI
/// uses to query status and request retention policy changes.
const DEFAULT_IPC_SOCKET: &str = "/run/teslafat/control.sock";

#[derive(Debug, Parser)]
#[command(
    name = "teslafat",
    about = "FAT32 synthesizer + NBD server (TeslaUSB B-1)"
)]
struct Args {
    /// Path to the YAML config file
    #[arg(short, long, default_value = "/etc/teslausb/teslafat.yaml")]
    config: PathBuf,

    /// Override the NBD listen socket
    #[arg(long)]
    nbd_socket: Option<PathBuf>,

    /// Override the IPC control socket
    #[arg(long)]
    ipc_socket: Option<PathBuf>,

    /// Override the backing tree root (normally /var/teslacam)
    #[arg(long)]
    backing_root: Option<PathBuf>,

    /// Run synthesis-only (no NBD listen) for dev/test
    #[arg(long)]
    synth_only: bool,
}

fn install_tracing() {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("teslafat=info,warn"));
    fmt()
        .with_env_filter(filter)
        .with_target(false)
        .with_thread_ids(false)
        .with_thread_names(false)
        .with_writer(std::io::stderr)
        .init();
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    install_tracing();
    let args = Args::parse();

    info!(
        version = env!("CARGO_PKG_VERSION"),
        "teslafat starting"
    );

    let cfg = Config::load(&args.config)
        .with_context(|| format!("loading {}", args.config.display()))?;

    let backing_root = args
        .backing_root
        .unwrap_or_else(|| cfg.backing_root.clone());
    let nbd_socket = args
        .nbd_socket
        .unwrap_or_else(|| PathBuf::from(DEFAULT_NBD_SOCKET));
    let ipc_socket = args
        .ipc_socket
        .unwrap_or_else(|| PathBuf::from(DEFAULT_IPC_SOCKET));

    info!(
        backing_root = %backing_root.display(),
        nbd_socket = %nbd_socket.display(),
        ipc_socket = %ipc_socket.display(),
        volume_size_gb = cfg.volume_size_gb,
        "configuration"
    );

    let backend = Arc::new(
        DirTreeBackend::new(&backing_root, cfg.volume_size_gb)
            .context("initialising backing-tree backend")?,
    );

    if args.synth_only {
        info!("synth-only mode requested; not opening sockets");
        return Ok(());
    }

    // Ensure socket directories exist with restrictive perms.
    ensure_socket_dir(&nbd_socket)?;
    ensure_socket_dir(&ipc_socket)?;

    // Install signal handlers so we shut down cleanly.
    let shutdown = install_signal_handlers();

    // Spawn the control/IPC listener.
    let ipc_handle = tokio::spawn(ipc::serve(
        ipc_socket.clone(),
        backend.clone(),
        shutdown.clone(),
    ));

    // Run the NBD server in the foreground.
    let nbd_result = nbd::serve(nbd_socket.clone(), backend.clone(), shutdown.clone()).await;

    if let Err(e) = nbd_result {
        warn!(error = %e, "NBD server exited with error");
    }

    // Wait for IPC to finish cleanup.
    let _ = ipc_handle.await;

    info!("teslafat shut down cleanly");
    Ok(())
}

fn ensure_socket_dir(socket: &std::path::Path) -> Result<()> {
    if let Some(parent) = socket.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("creating {}", parent.display()))?;
    }
    // Best-effort: remove a stale socket file from a prior run.
    let _ = std::fs::remove_file(socket);
    Ok(())
}

/// A token broadcast to all subsystems when the daemon should
/// shut down. Cloned freely; tasks `recv()` to wait for shutdown.
#[derive(Clone)]
pub struct Shutdown {
    notify: Arc<tokio::sync::Notify>,
}

impl Shutdown {
    pub fn new() -> Self {
        Self {
            notify: Arc::new(tokio::sync::Notify::new()),
        }
    }
    pub fn trigger(&self) {
        self.notify.notify_waiters();
    }
    pub async fn recv(&self) {
        self.notify.notified().await;
    }
}

fn install_signal_handlers() -> Shutdown {
    let shutdown = Shutdown::new();
    let s = shutdown.clone();
    tokio::spawn(async move {
        use tokio::signal::unix::{signal, SignalKind};
        let mut term = signal(SignalKind::terminate()).expect("SIGTERM handler");
        let mut int = signal(SignalKind::interrupt()).expect("SIGINT handler");
        tokio::select! {
            _ = term.recv() => info!("SIGTERM received"),
            _ = int.recv() => info!("SIGINT received"),
        }
        s.trigger();
    });
    shutdown
}
