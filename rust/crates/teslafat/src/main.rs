//! `teslafat` — userspace FAT/`exFAT` synthesizer + NBD server.
//!
//! Speaks the NBD newstyle protocol to a kernel `nbd-client` which
//! in turn backs the `g_mass_storage` USB gadget exposed to the
//! vehicle. The synthesised FAT/`exFAT` view is computed from a
//! real Linux directory tree (`backing_root`) at request time;
//! writes from the vehicle decode back into native file operations.
//!
//! ## Phase 1.6 state (this commit)
//!
//! Boots, loads its TOML config, emits a JSON "started" sentinel,
//! then on Unix binds the configured Unix-domain socket and enters
//! [`teslafat::server::serve`] until `SIGTERM` or `SIGINT`. The
//! backend is the Phase 1.6 placeholder [`teslafat::backend::ZeroBackend`];
//! it advertises `cfg.volume_size_gb` of zero-backed storage so
//! the Phase 1.7 smoke test can verify the wire path end-to-end
//! before the real `FileBackend` (Phase 2) lands.
//!
//! `--check-config` keeps the Phase 1.1 contract intact: validate
//! the config, emit the sentinel, exit `0` without binding the
//! socket. That's the mode the `setup.sh` installer (Phase 6.4)
//! uses to verify a freshly-written config before enabling the
//! `teslafat@.service` instance.

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

    /// Load + validate the config, emit the "started" sentinel,
    /// and exit. Skips socket bind + accept loop. Use this from
    /// the installer to verify a freshly-written config.
    #[arg(long, default_value_t = false)]
    check_config: bool,
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

fn load_and_announce(args: &Args) -> Result<Config> {
    let cfg =
        Config::load(&args.config).with_context(|| format!("loading {}", args.config.display()))?;

    info!(
        version = env!("CARGO_PKG_VERSION"),
        config_path = %args.config.display(),
        backing_root = %cfg.backing_root.display(),
        volume_size_gb = cfg.volume_size_gb,
        volume_label = %cfg.volume_label,
        retention_hide_after_s = cfg.retention.recentclips_hide_after_seconds,
        nbd_socket = %cfg.nbd.socket_path.display(),
        nbd_handshake_timeout_s = cfg.nbd.handshake_timeout_seconds,
        "started"
    );

    Ok(cfg)
}

fn run(args: &Args) -> Result<()> {
    let cfg = load_and_announce(args)?;

    if args.check_config {
        info!("--check-config set; exiting without binding socket");
        return Ok(());
    }

    #[cfg(unix)]
    {
        unix_serve::serve_until_signal(&cfg)
    }

    #[cfg(not(unix))]
    {
        // The serve loop binds a Unix-domain socket and installs
        // POSIX signal handlers, both of which only exist on Unix.
        // The non-Unix build is dev-only (the deploy target is the
        // Pi), so refuse to start in serve mode and tell the
        // operator how to test the bootstrap path instead.
        let _ = cfg; // silence unused warning
        anyhow::bail!(
            "teslafat serve mode requires a Unix host; \
             use --check-config to validate config on non-Unix dev boxes"
        )
    }
}

#[cfg(unix)]
mod unix_serve {
    use std::fs;
    use std::path::Path;

    use anyhow::{Context, Result};
    use tokio::net::UnixListener;
    use tokio::runtime::Builder;
    use tokio::signal::unix::{SignalKind, signal};
    use tracing::{info, warn};

    use teslafat::backend::ZeroBackend;
    use teslafat::config::Config;
    use teslafat::server;

    /// One GiB in bytes; used to convert `volume_size_gb` (the
    /// operator-facing knob) into the `u64` size the backend
    /// advertises in the NBD handshake.
    const BYTES_PER_GIB: u64 = 1024 * 1024 * 1024;

    /// Build the runtime, bind the listener, and run the accept
    /// loop until `SIGTERM` or `SIGINT`. Returns `Ok(())` on a
    /// clean shutdown.
    pub fn serve_until_signal(cfg: &Config) -> Result<()> {
        // Current-thread runtime is sufficient: the daemon runs at
        // most one active NBD connection (single kernel client per
        // export) plus the signal-handling future, and the Pi Zero
        // 2 W cannot spare the RAM for a multi-thread scheduler.
        let runtime = Builder::new_current_thread()
            .enable_io()
            .enable_time()
            .build()
            .context("building tokio current-thread runtime")?;

        runtime.block_on(async {
            let listener = prepare_listener(&cfg.nbd.socket_path)?;
            let backend = ZeroBackend::new(u64::from(cfg.volume_size_gb) * BYTES_PER_GIB);
            let result = server::serve(
                listener,
                &backend,
                cfg.nbd.handshake_timeout(),
                shutdown_on_signal(),
            )
            .await;
            // Best-effort socket cleanup on a clean exit so a
            // restart isn't blocked by a stale file. We deliberately
            // ignore the error: on a crashed shutdown the OS will
            // not have removed it either, and `prepare_listener`
            // handles a stale socket on the next start.
            if let Err(e) = fs::remove_file(&cfg.nbd.socket_path) {
                if e.kind() != std::io::ErrorKind::NotFound {
                    warn!(error = ?e, "failed to remove socket file on shutdown");
                }
            }
            result
        })
    }

    /// Ensure the socket's parent directory exists, remove any
    /// stale socket file at the bind path, and bind a
    /// [`UnixListener`].
    ///
    /// The unlink-before-bind step matters because `bind` returns
    /// `EADDRINUSE` if the path exists even when no process owns
    /// it (Unix sockets are file-system entries that persist
    /// across a crashed daemon). Systemd's `Restart=on-failure`
    /// would otherwise loop forever on a crashed instance.
    fn prepare_listener(path: &Path) -> Result<UnixListener> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)
                .with_context(|| format!("creating socket parent dir {}", parent.display()))?;
        }
        match fs::remove_file(path) {
            Ok(()) => info!(socket = %path.display(), "removed stale socket file"),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => {
                return Err(anyhow::Error::new(e))
                    .with_context(|| format!("removing stale socket {}", path.display()));
            }
        }
        let listener = UnixListener::bind(path)
            .with_context(|| format!("binding NBD socket {}", path.display()))?;
        info!(socket = %path.display(), "NBD socket bound");
        Ok(listener)
    }

    /// Future that resolves on the first `SIGTERM` or `SIGINT`.
    /// If the kernel refuses to install either handler the future
    /// falls back to `pending` so the daemon at least keeps
    /// serving (rather than exiting straight away on a config-or-
    /// kernel quirk that nobody saw coming).
    async fn shutdown_on_signal() {
        let mut term = match signal(SignalKind::terminate()) {
            Ok(s) => s,
            Err(e) => {
                warn!(error = ?e, "could not install SIGTERM handler");
                // `pending::<()>()` is a future that never resolves.
                // Returning from it would otherwise exit serve on
                // the first poll.
                return std::future::pending::<()>().await;
            }
        };
        let mut int = match signal(SignalKind::interrupt()) {
            Ok(s) => s,
            Err(e) => {
                warn!(error = ?e, "could not install SIGINT handler");
                return std::future::pending::<()>().await;
            }
        };
        tokio::select! {
            _ = term.recv() => info!("received SIGTERM"),
            _ = int.recv() => info!("received SIGINT"),
        }
    }
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
