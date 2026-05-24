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
    use std::time::{Duration, Instant};

    use anyhow::{Context, Result};
    use tokio::net::UnixListener;
    use tokio::runtime::Builder;
    use tokio::signal::unix::{SignalKind, signal};
    use tracing::{info, warn};

    use teslafat::backend::SynthBackend;
    use teslafat::config::Config;
    use teslafat::server;
    use teslausb_core::backend::BlockBackend;

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
            let backend =
                SynthBackend::open(cfg).context("opening SynthBackend from backing tree")?;
            info!(
                fs_type = if backend.is_fat32() { "fat32" } else { "exfat" },
                size_bytes = backend.volume_size(),
                file_count = backend.file_count(),
                "SynthBackend ready"
            );
            // Warm the backend's read path BEFORE telling systemd we
            // are ready. `SynthBackend::open` completes when the
            // backing tree is parsed and the FAT can answer reads,
            // but the first reads after open are cold: page cache is
            // empty, allocator hasn't sized its arenas, and the
            // exFAT directory walker hasn't materialised the per-
            // cluster maps it lazily caches. On a Pi Zero 2 W with
            // ~2000 files those first reads can take 50–200 ms each,
            // which is too slow for the Tesla USB host: it issues
            // SCSI READs back-to-back during enumeration, sees them
            // time out, and falls back to a read-only mount until
            // the next replug. Sending READY=1 only after the
            // warmup means `nbd-attach@N` → `usb-gadget` → UDC bind
            // are all held back by systemd ordering until the
            // backend can actually answer fast — closing the
            // boot-race window the operator saw on 2026-05-24.
            warm_backend(&backend).await;
            // Tell systemd we are ready. Under `Type=notify` this is
            // what gates `After=teslafat@N.service` consumers (e.g.
            // `nbd-attach@N`) — they will not start until this
            // notification arrives, eliminating the boot race where
            // nbd-client connects before `bind()` has returned. Under
            // any other unit type (or when run outside systemd) this
            // is a no-op because `NOTIFY_SOCKET` is unset.
            notify_systemd_ready();
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

    /// Send `READY=1` to the systemd notify socket if one is
    /// configured via `$NOTIFY_SOCKET`. Best-effort: if the env var
    /// is absent (manual invocation, dev box, `Type=simple` unit),
    /// or the send fails for any reason, we log at warn level and
    /// carry on serving — failing to notify must never take down a
    /// working daemon.
    ///
    /// The implementation is deliberately inlined (no `sd-notify`
    /// crate dependency) because the payload is one line and the
    /// only OS abstraction we need is `UnixDatagram`. Linux abstract
    /// namespace sockets (leading `@`) are not supported — systemd
    /// uses a filesystem path (`/run/systemd/notify`) for ordinary
    /// service units, which is the only case we care about.
    fn notify_systemd_ready() {
        use std::os::unix::ffi::OsStrExt;
        use std::os::unix::net::UnixDatagram;

        let Some(raw) = std::env::var_os("NOTIFY_SOCKET") else {
            return;
        };
        let bytes = raw.as_bytes();
        if bytes.is_empty() {
            return;
        }
        if bytes[0] == b'@' {
            warn!(
                "sd_notify: abstract NOTIFY_SOCKET not supported; \
                 set the unit to use a filesystem notify socket"
            );
            return;
        }
        let sock = match UnixDatagram::unbound() {
            Ok(s) => s,
            Err(e) => {
                warn!(error = ?e, "sd_notify: could not create datagram socket");
                return;
            }
        };
        match sock.send_to(b"READY=1\n", Path::new(&raw)) {
            Ok(_) => info!("sd_notify: READY=1 sent"),
            Err(e) => warn!(error = ?e, "sd_notify: send failed"),
        }
    }

    /// Regions of the synthesised volume that a host SCSI/USB
    /// initiator always touches first during enumeration. The
    /// values are chosen to cover both FAT32 and exFAT layouts:
    /// boot sector at offset 0, reserved/FSInfo around 512 B, the
    /// FAT region (which the synth backend lazily builds in 64 KiB
    /// windows), and the root directory cluster (~1 MiB in for our
    /// typical volume geometry). Reading them up front populates
    /// the synth backend's internal caches before we tell systemd
    /// the daemon is ready.
    const WARMUP_REGIONS: &[(u64, usize)] = &[
        (0, 4096),
        (0x1_0000, 65_536),
        (0x10_0000, 65_536),
        (0x20_0000, 65_536),
    ];

    /// Number of warm-up passes. Two passes is enough to populate
    /// the cluster-map cache and exercise both the cold and warm
    /// code paths of the read dispatcher. The third pass is a
    /// safety margin and its duration is what we log + use to
    /// decide whether to warn.
    const WARMUP_PASSES: usize = 3;

    /// p99 read-latency budget for the final warm-up pass. If a
    /// pass exceeds this we log a `WARN` (so it shows up in
    /// `journalctl -p err` triage) but still proceed to signal
    /// READY=1 — being slow to answer reads is better than never
    /// answering them, and blocking systemd forever would leave
    /// the user with no USB at all.
    const WARMUP_PASS_BUDGET: Duration = Duration::from_millis(250);

    /// Exercise [`BlockBackend::read`] over a small set of regions
    /// the host will touch during USB enumeration. Logs each pass
    /// duration so a regression in cold-read latency is visible in
    /// the journal; never aborts the daemon — a slow backend that
    /// can still answer is strictly better than no LUN.
    async fn warm_backend(backend: &SynthBackend) {
        let mut buf = vec![0_u8; 65_536];
        let mut last_pass = Duration::ZERO;
        for pass in 1..=WARMUP_PASSES {
            let pass_start = Instant::now();
            for &(off, len) in WARMUP_REGIONS {
                let take = len.min(buf.len());
                let slice = &mut buf[..take];
                if let Err(e) = backend.read(off, slice).await {
                    warn!(
                        error = ?e,
                        offset = off,
                        len = take,
                        pass,
                        "warmup read failed; continuing"
                    );
                }
            }
            last_pass = pass_start.elapsed();
            info!(
                pass,
                duration_ms = last_pass.as_millis() as u64,
                regions = WARMUP_REGIONS.len(),
                "SynthBackend warmup pass"
            );
        }
        if last_pass > WARMUP_PASS_BUDGET {
            warn!(
                duration_ms = last_pass.as_millis() as u64,
                budget_ms = WARMUP_PASS_BUDGET.as_millis() as u64,
                "SynthBackend warmup final pass exceeded budget; \
                 USB host may see read timeouts during enumeration"
            );
        } else {
            info!(
                duration_ms = last_pass.as_millis() as u64,
                budget_ms = WARMUP_PASS_BUDGET.as_millis() as u64,
                "SynthBackend warmup complete, within budget"
            );
        }
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
