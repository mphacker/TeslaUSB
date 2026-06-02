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

use teslafat::config::DiskConfig;

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

fn load_and_announce(args: &Args) -> Result<DiskConfig> {
    let cfg = DiskConfig::load(&args.config)
        .with_context(|| format!("loading {}", args.config.display()))?;

    info!(
        version = env!("CARGO_PKG_VERSION"),
        config_path = %args.config.display(),
        partitions = cfg.partition.len(),
        disk_signature = format_args!("{:#010x}", cfg.disk_signature),
        nbd_socket = %cfg.nbd.socket_path.display(),
        nbd_handshake_timeout_s = cfg.nbd.handshake_timeout_seconds,
        "started"
    );
    for (index, part) in cfg.partition.iter().enumerate() {
        info!(
            partition = index,
            backing_root = %part.backing_root.display(),
            volume_size_gb = part.volume_size_gb,
            volume_label = %part.volume_label,
            fs_type = if matches!(part.fs_type, teslafat::config::FsType::Fat32) { "fat32" } else { "exfat" },
            retention_hide_after_s = part.retention.recentclips_hide_after_seconds,
            "partition configured"
        );
    }

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
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    use anyhow::{Context, Result};
    use tokio::net::UnixListener;
    use tokio::runtime::Builder;
    use tokio::signal::unix::{SignalKind, signal};
    use tracing::{info, warn};

    use teslafat::backend::{PartitionedDiskBackend, ReloadableBackend, SynthBackend};
    use teslafat::config::{DiskConfig, FsType};
    use teslafat::server;
    use teslausb_core::backend::BlockBackend;
    use teslausb_core::fs::mbr::{
        DEFAULT_ALIGNMENT_SECTORS, DiskLayout, PARTITION_TYPE_EXFAT, PARTITION_TYPE_FAT32_LBA,
        PartitionRequest,
    };

    /// Logical sector size used to convert a backend's byte size into
    /// the MBR partition's sector count.
    const SECTOR_SIZE_BYTES: u64 = 512;

    /// Map a partition's filesystem flavour to its MBR partition-type
    /// byte. Both volumes are exFAT under ADR-0023, but the FAT32 byte
    /// is kept so a partition left on FAT32 during migration still
    /// gets a correct type field.
    const fn partition_type_for(fs: FsType) -> u8 {
        match fs {
            FsType::Exfat => PARTITION_TYPE_EXFAT,
            FsType::Fat32 => PARTITION_TYPE_FAT32_LBA,
        }
    }

    /// Convert a child backend's byte size into a u32 sector count for
    /// the MBR partition table.
    ///
    /// # Errors
    ///
    /// Errors if the size is not a whole number of 512-byte sectors
    /// (it always is — sizes are `volume_size_gb * 2^30`) or exceeds
    /// the `u32` sector ceiling MBR can address (~2 TiB).
    fn partition_sector_count(size_bytes: u64) -> Result<u32> {
        anyhow::ensure!(
            size_bytes % SECTOR_SIZE_BYTES == 0,
            "partition size {size_bytes} is not a multiple of {SECTOR_SIZE_BYTES}",
        );
        u32::try_from(size_bytes / SECTOR_SIZE_BYTES)
            .context("partition sector count exceeds u32::MAX (too large for MBR)")
    }

    /// Build the runtime, bind the listener, compose the partitioned
    /// disk, and run the accept loop until `SIGTERM` or `SIGINT`.
    /// Returns `Ok(())` on a clean shutdown.
    pub fn serve_until_signal(cfg: &DiskConfig) -> Result<()> {
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

            // Build one ReloadableBackend per partition. Each child is
            // warmed before we advertise readiness (same cold-read
            // rationale as the single-LUN path) and contributes one
            // entry to the MBR partition table sized to its fixed
            // export length.
            let mut children: Vec<Arc<ReloadableBackend>> = Vec::with_capacity(cfg.partition.len());
            let mut requests: Vec<PartitionRequest> = Vec::with_capacity(cfg.partition.len());
            for (index, part) in cfg.partition.iter().enumerate() {
                let child = Arc::new(ReloadableBackend::open(part).with_context(|| {
                    format!(
                        "opening partition[{index}] backend ({})",
                        part.backing_root.display()
                    )
                })?);
                let view = child.current();
                info!(
                    partition = index,
                    fs_type = if view.is_fat32() { "fat32" } else { "exfat" },
                    size_bytes = view.volume_size(),
                    file_count = view.file_count(),
                    backing_root = %part.backing_root.display(),
                    "partition SynthBackend ready"
                );
                warm_backend(&view).await;
                drop(view);

                let sector_count = partition_sector_count(child.size())
                    .with_context(|| format!("partition[{index}] geometry"))?;
                requests.push(PartitionRequest {
                    sector_count,
                    partition_type: partition_type_for(part.fs_type),
                });
                children.push(child);
            }

            let layout = DiskLayout::plan(cfg.disk_signature, &requests, DEFAULT_ALIGNMENT_SECTORS)
                .context("planning MBR disk layout")?;

            // The reload handler keeps its own `Arc` clones so it can
            // re-walk each child independently of the disk that routes
            // their bytes; a `try_go_live` swap inside a child is seen
            // by both paths through the child's internal `RwLock`.
            //
            // Only partitions whose config opts into SIGHUP reload are
            // handed to the reload loop. The continuously-recorded
            // `TeslaCam` partition sets `reload_on_sighup = false`: its
            // layout must not be live-swapped while the car is writing,
            // and excluding it keeps exactly one reloadable partition
            // (the media volume) so the single `RELOAD_LIVE_MARKER` the
            // rebind script waits on unambiguously means "the chime view
            // is live".
            let reload_handles: Vec<Arc<ReloadableBackend>> = children
                .iter()
                .zip(cfg.partition.iter())
                .filter(|(_, part)| part.reload_on_sighup)
                .map(|(child, _)| Arc::clone(child))
                .collect();
            info!(
                reloadable_partitions = reload_handles.len(),
                total_partitions = cfg.partition.len(),
                "SIGHUP live-reload enabled for opted-in partitions"
            );
            let disk = PartitionedDiskBackend::new(&layout, children)
                .context("composing partitioned disk backend")?;
            info!(
                partitions = layout.partitions.len(),
                disk_size_bytes = disk.size(),
                "partitioned disk assembled (MBR + partitions)"
            );

            // Tell systemd we are ready. Under `Type=notify` this gates
            // `After=teslafat.service` consumers (e.g. `nbd-attach`) so
            // nbd-client never connects before `bind()` returned and the
            // backends are warm — closing the boot-race window.
            notify_systemd_ready();
            // Serve the accept loop and the SIGHUP live-reload loop
            // concurrently. `reload_on_sighup` never resolves on the
            // happy path (it loops forever), so this `select!` returns
            // when `server::serve` does — i.e. on SIGTERM/SIGINT.
            let result = tokio::select! {
                r = server::serve(
                    listener,
                    &disk,
                    cfg.nbd.handshake_timeout(),
                    shutdown_on_signal(),
                ) => r,
                () = reload_on_sighup(reload_handles) => Ok(()),
            };
            // Best-effort socket cleanup on a clean exit so a restart
            // isn't blocked by a stale file.
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
        if bytes.first() == Some(&b'@') {
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

    /// Whole-millisecond view of a [`Duration`] for structured log
    /// fields. `Duration::as_millis` returns `u128`; a plain `as u64`
    /// cast is lossy (`clippy::cast_possible_truncation`), so saturate
    /// at `u64::MAX` instead. The warm-up and reload intervals we log
    /// are sub-second, so the saturation branch is unreachable in
    /// practice — it exists only to keep the conversion total.
    fn duration_millis_u64(d: Duration) -> u64 {
        u64::try_from(d.as_millis()).unwrap_or(u64::MAX)
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
                let Some(slice) = buf.get_mut(..take) else {
                    continue;
                };
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
                duration_ms = duration_millis_u64(last_pass),
                regions = WARMUP_REGIONS.len(),
                "SynthBackend warmup pass"
            );
        }
        if last_pass > WARMUP_PASS_BUDGET {
            warn!(
                duration_ms = duration_millis_u64(last_pass),
                budget_ms = duration_millis_u64(WARMUP_PASS_BUDGET),
                "SynthBackend warmup final pass exceeded budget; \
                 USB host may see read timeouts during enumeration"
            );
        } else {
            info!(
                duration_ms = duration_millis_u64(last_pass),
                budget_ms = duration_millis_u64(WARMUP_PASS_BUDGET),
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

    /// Number of times [`go_live_when_quiescent`] retries the
    /// quiescence-gated swap before deferring it. The media LUN is
    /// read-mostly (boombox / lock-chime), so it is almost always
    /// quiescent the instant a SIGHUP arrives; this budget only covers
    /// a brief host write that happens to overlap the reload.
    const RELOAD_SWAP_RETRIES: u32 = 20;

    /// Backoff between quiescence-gated swap attempts. `20 * 500 ms`
    /// gives a ~10 s window for an overlapping write to finish before
    /// the reload is deferred.
    const RELOAD_SWAP_BACKOFF: Duration = Duration::from_millis(500);

    /// Stable, machine-greppable marker logged the instant a SIGHUP
    /// reload actually goes live (the quiescence-gated swap succeeded).
    ///
    /// This is a **contract** with `scripts/tesla_gadget_rebind.sh`,
    /// which sends this daemon a SIGHUP and then blocks on this exact
    /// token in the journal before it unbinds/rebinds the USB gadget —
    /// so the host (Tesla) only ever re-enumerates the *fresh* synth
    /// view, never a stale snapshot. Keep this token and the script's
    /// `RELOAD_LIVE_MARKER` grep in lock-step.
    const RELOAD_LIVE_MARKER: &str = "teslafat-reload-live";

    /// Rebuild the synth view on every `SIGHUP` and swap it in once the
    /// LUN is quiescent. Runs concurrently with the accept loop and
    /// never resolves on the happy path (loops forever). If the
    /// `SIGHUP` handler cannot be installed it logs and parks on
    /// `pending()` so the daemon keeps serving the original view.
    ///
    /// The expensive re-walk (directory walk + FAT/`exFAT` layout
    /// planning) runs on a blocking thread via
    /// [`tokio::task::spawn_blocking`] so it never stalls the
    /// single-threaded runtime that is also driving NBD I/O. A failed
    /// or panicking rebuild leaves the live view untouched.
    async fn reload_on_sighup(backends: Vec<Arc<ReloadableBackend>>) {
        let mut hup = match signal(SignalKind::hangup()) {
            Ok(s) => s,
            Err(e) => {
                warn!(error = ?e, "could not install SIGHUP handler; live reload disabled");
                return std::future::pending::<()>().await;
            }
        };
        loop {
            hup.recv().await;
            info!(
                partitions = backends.len(),
                "received SIGHUP; rebuilding synth views from backing trees"
            );
            for (index, backend) in backends.iter().enumerate() {
                let builder = Arc::clone(backend);
                let built = tokio::task::spawn_blocking(move || builder.build_fresh()).await;
                let fresh = match built {
                    Ok(Ok(b)) => Arc::new(b),
                    Ok(Err(e)) => {
                        warn!(partition = index, error = ?e, "reload rebuild failed; keeping current view");
                        continue;
                    }
                    Err(e) => {
                        warn!(partition = index, error = ?e, "reload rebuild task panicked; keeping current view");
                        continue;
                    }
                };
                info!(
                    partition = index,
                    file_count = fresh.file_count(),
                    size_bytes = fresh.volume_size(),
                    "rebuilt synth view from backing tree"
                );
                // Warm the fresh view's read caches before it goes live
                // so the host never sees a cold-read stall right after
                // the swap (same rationale as the startup warmup).
                warm_backend(&fresh).await;
                go_live_when_quiescent(index, backend, fresh).await;
            }
        }
    }

    /// Retry [`ReloadableBackend::try_go_live`] until the partition is
    /// quiescent or the retry budget is exhausted. A full layout swap
    /// abandons the old view's in-flight write state, so it must only
    /// happen while no host write is addressing the current layout. If
    /// the partition stays busy past the budget the swap is deferred and
    /// the operator can re-send `SIGHUP` once writes settle.
    async fn go_live_when_quiescent(
        partition: usize,
        backend: &ReloadableBackend,
        fresh: Arc<SynthBackend>,
    ) {
        for attempt in 1..=RELOAD_SWAP_RETRIES {
            if backend.try_go_live(Arc::clone(&fresh)) {
                info!(
                    partition,
                    attempt,
                    marker = RELOAD_LIVE_MARKER,
                    "reload swap applied; new synth view is live"
                );
                return;
            }
            warn!(
                partition,
                attempt,
                max = RELOAD_SWAP_RETRIES,
                "partition busy with an in-flight write; deferring reload swap"
            );
            tokio::time::sleep(RELOAD_SWAP_BACKOFF).await;
        }
        warn!(
            partition,
            "partition still busy after retry budget; reload deferred — re-send SIGHUP when idle"
        );
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
