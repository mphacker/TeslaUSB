//! Read-only system + storage probing for the device-status endpoints
//! (`webd.md` §2.2 / §3 "System health" / "Storage"). Every datum is a
//! `/proc`, `/sys`, or `statvfs(3)` read — `webd` never writes and never
//! shells out.
//!
//! Probing is behind the [`SystemProbe`] trait so the handlers stay testable
//! on the non-Linux build host (where `/proc` and `statvfs` do not exist): the
//! live [`LinuxProbe`] reads real kernel files and degrades any reading it
//! cannot take to `None`, while tests inject a fake. Inactive services and
//! car-owned exFAT volumes are reported as **`unknown`** rather than
//! fabricated — the legacy UI's degraded look IS the parity target
//! (`spa.md` §3).
//!
//! Casts here are f64↔u64 on quantities (load, free fractions, uptime) that
//! drive coarse human-readable status, never exact accounting, so the
//! precision/truncation/sign pedantic lints are allowed module-wide.
#![allow(
    clippy::cast_precision_loss,
    clippy::cast_possible_truncation,
    clippy::cast_sign_loss
)]

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// Free-fraction at or above which a filesystem is healthy.
const DISK_OK_FRAC: f64 = 0.15;
/// Free-fraction at or above which a filesystem is merely warned (below =
/// error).
const DISK_WARN_FRAC: f64 = 0.05;
/// Bytes in one GiB, used for human-readable size messages.
const GIB: f64 = (1u64 << 30) as f64;

/// Severity ladder shared by every health block; the string form matches the
/// SPA's `SEV_COLORS` keys (`ok|warn|error|unknown`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    /// Healthy.
    Ok,
    /// Degraded but serving.
    Warn,
    /// Failing.
    Error,
    /// No signal (not probed, inactive service, or car-owned volume).
    Unknown,
}

impl Severity {
    /// The wire string (`ok|warn|error|unknown`).
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Warn => "warn",
            Self::Error => "error",
            Self::Unknown => "unknown",
        }
    }

    /// Severity rank for "worst wins" rollups. `Unknown` ranks lowest so an
    /// all-unknown set rolls up to `unknown`, but any real signal dominates.
    const fn rank(self) -> u8 {
        match self {
            Self::Unknown => 0,
            Self::Ok => 1,
            Self::Warn => 2,
            Self::Error => 3,
        }
    }
}

/// A point-in-time `statvfs` reading (bytes **and** inodes: thumbnails/Recent
/// segments can exhaust inodes long before bytes, `storage.md` §2).
#[derive(Debug, Clone, Copy)]
pub struct FsStat {
    /// Bytes free to an unprivileged writer.
    pub free_bytes: u64,
    /// Total bytes of the filesystem.
    pub total_bytes: u64,
    /// Free inodes.
    pub free_inodes: u64,
    /// Total inodes.
    pub total_inodes: u64,
}

impl FsStat {
    /// Bytes in use (`total - free`, saturating).
    #[must_use]
    pub const fn used_bytes(self) -> u64 {
        self.total_bytes.saturating_sub(self.free_bytes)
    }

    /// Free bytes as a fraction of total (`0.0` when total is 0).
    #[must_use]
    pub fn free_frac(self) -> f64 {
        if self.total_bytes == 0 {
            0.0
        } else {
            self.free_bytes as f64 / self.total_bytes as f64
        }
    }
}

/// Device / filesystem-type / mount-point for one mounted filesystem.
#[derive(Debug, Clone)]
pub struct MountInfo {
    /// Backing device (mounts field 1).
    pub device: String,
    /// Filesystem type (mounts field 3).
    pub fstype: String,
    /// Mount point (mounts field 2).
    pub mount: String,
}

/// The read-only kernel-fact source the endpoints query. Every method returns
/// an `Option`/best-effort value so a probe that cannot read a fact degrades
/// to `unknown` instead of failing the request.
pub trait SystemProbe: Send + Sync {
    /// Read `/proc/<name>` (e.g. `"meminfo"`, `"loadavg"`, `"uptime"`,
    /// `"mounts"`); `None` if it cannot be read.
    fn proc_file(&self, name: &str) -> Option<String>;
    /// `statvfs(3)` for `path`; `None` on any error or non-Unix host.
    fn statvfs(&self, path: &Path) -> Option<FsStat>;
    /// Whether `path` is writable by this process.
    fn writable(&self, path: &Path) -> bool;
    /// The USB device-controller state (`/sys/class/udc/<udc>/state`), e.g.
    /// `"configured"`; `None` when no UDC is present.
    fn udc_state(&self) -> Option<String>;
    /// The [`MountInfo`] of the filesystem that `path` lives on.
    fn mount_for(&self, path: &Path) -> Option<MountInfo>;
    /// Read a text file as UTF-8.
    fn read_file_string(&self, path: &Path) -> Option<String>;
    /// `SoC` temperature in milli-degrees Celsius (e.g. `47000` = 47.0 °C), read
    /// from `/sys/class/thermal/thermal_zone0/temp`; `None` when no thermal zone
    /// is exposed (e.g. the non-Linux build host or a board without a sensor).
    fn cpu_temp_millic(&self) -> Option<i64> {
        None
    }
}

/// Paths `webd` probes: the Pi-side data/archive root whose ext4 filesystem
/// backs the catalog, archive, and export cache.
#[derive(Debug, Clone)]
pub struct SysPaths {
    /// `WEBD_ARCHIVE_ROOT` — the data filesystem to report as the "SD Card".
    pub archive_root: PathBuf,
    /// Retention worker heartbeat path.
    pub worker_health_file: PathBuf,
}

/// One `{severity, message}` row of `GET /api/system/health`.
#[derive(Debug, Clone, Serialize)]
pub struct HealthBlock {
    /// `ok|warn|error|unknown`.
    pub severity: &'static str,
    /// Human-readable one-line status.
    pub message: String,
}

impl HealthBlock {
    fn new(sev: Severity, message: impl Into<String>) -> Self {
        Self {
            severity: sev.as_str(),
            message: message.into(),
        }
    }
}

/// `GET /api/system/health`: an overall rollup plus the per-subsystem blocks
/// `webd` can probe read-only. Subsystems it cannot observe (car-owned exFAT
/// volumes, inactive services, Wi-Fi tooling) are deliberately omitted so the
/// SPA renders them in the legacy `unknown / —` state.
#[derive(Debug, Serialize)]
pub struct SystemHealth {
    /// Worst severity across the probed subsystems (`unknown` if none probed).
    pub overall: &'static str,
    /// Probed subsystem blocks, keyed by the SPA's subsystem key.
    pub subsystems: BTreeMap<String, HealthBlock>,
}

impl SystemHealth {
    /// A fully-degraded payload (used when the probe task itself cannot run).
    #[must_use]
    pub fn degraded() -> Self {
        Self {
            overall: Severity::Unknown.as_str(),
            subsystems: BTreeMap::new(),
        }
    }
}

/// CPU load averages (1/5/15 minute).
#[derive(Debug, Serialize)]
pub struct LoadDto {
    /// 1-minute load average.
    pub one: f64,
    /// 5-minute load average.
    pub five: f64,
    /// 15-minute load average.
    pub fifteen: f64,
}

/// A memory-or-swap tile: total, available/free, and percent used.
#[derive(Debug, Serialize)]
pub struct MemDto {
    /// Total bytes.
    pub total_bytes: u64,
    /// Bytes available to allocate.
    pub available_bytes: u64,
    /// Percent used (`0.0` when total is 0).
    pub used_pct: f64,
}

impl MemDto {
    fn new(total_bytes: u64, available_bytes: u64) -> Self {
        let used_pct = if total_bytes == 0 {
            0.0
        } else {
            let used = total_bytes.saturating_sub(available_bytes);
            (used as f64 / total_bytes as f64) * 100.0
        };
        Self {
            total_bytes,
            available_bytes,
            used_pct,
        }
    }
}

/// `GET /api/system/metrics`: the Live-Metrics tiles `webd` can read honestly
/// (`load`, `mem`, `swap`, `uptime`, `cpu_temp`). CPU-percent and per-device I/O
/// need sampling deltas and are left to a later slice (the SPA shows `—`).
#[derive(Debug, Serialize)]
pub struct SystemMetrics {
    /// Seconds since boot, or `null`.
    pub uptime_s: Option<u64>,
    /// Load averages, or `null`.
    pub load: Option<LoadDto>,
    /// RAM tile, or `null`.
    pub mem: Option<MemDto>,
    /// Swap tile, or `null` when no swap is configured.
    pub swap: Option<MemDto>,
    /// `SoC` temperature in degrees Celsius (one decimal), or `null` when no
    /// thermal sensor is exposed. A first-class tile on a fanless Pi appliance
    /// where thermal throttling is a real failure mode.
    pub cpu_temp_c: Option<f64>,
    /// When this snapshot was taken (epoch seconds), or `null`.
    pub updated_at: Option<u64>,
}

/// One filesystem entry of `GET /api/storage`.
#[derive(Debug, Serialize)]
pub struct FilesystemDto {
    /// Mount point.
    pub mount: String,
    /// Backing device.
    pub device: String,
    /// Filesystem type.
    pub fstype: String,
    /// Bytes free to an unprivileged writer.
    pub free_bytes: u64,
    /// Total bytes.
    pub total_bytes: u64,
    /// Free inodes.
    pub free_inodes: u64,
    /// Total inodes.
    pub total_inodes: u64,
}

/// `GET /api/storage`: the filesystems `webd` can `statvfs` directly. The
/// governor tier is owned by `retentiond`; while that service is not wired in,
/// `governor` is `null` (not fabricated).
#[derive(Debug, Serialize)]
pub struct Storage {
    /// The probed filesystems (root + the data/archive root).
    pub filesystems: Vec<FilesystemDto>,
    /// Reserved for the `retentiond` governor tier; currently always `null`.
    pub governor: Option<serde_json::Value>,
}

/// `GET /api/storage/health`: the data filesystem's capacity plus the
/// device/fs/mount facts. Wear telemetry (fs errors, I/O errors, TRIM) is not
/// available read-only from SD cards and is reported as `null`.
#[derive(Debug, Serialize)]
pub struct StorageHealth {
    /// Capacity-derived severity.
    pub severity: &'static str,
    /// Human-readable one-line summary.
    pub summary: String,
    /// Backing device, or `null`.
    pub device: Option<String>,
    /// Filesystem type, or `null`.
    pub fstype: Option<String>,
    /// Mount point, or `null`.
    pub mount: Option<String>,
    /// Bytes in use, or `null`.
    pub used_bytes: Option<u64>,
    /// Total bytes, or `null`.
    pub total_bytes: Option<u64>,
    /// Filesystem error count — not available read-only (`null`).
    pub fs_errors: Option<u64>,
    /// I/O errors in the last 24h — not available read-only (`null`).
    pub io_errors_24h: Option<u64>,
    /// TRIM status — not available read-only (`null`).
    pub trim: Option<String>,
}

impl StorageHealth {
    /// A fully-degraded payload (no `statvfs` reading available).
    #[must_use]
    pub fn unavailable() -> Self {
        Self {
            severity: Severity::Unknown.as_str(),
            summary: "Storage health unavailable".to_owned(),
            device: None,
            fstype: None,
            mount: None,
            used_bytes: None,
            total_bytes: None,
            fs_errors: None,
            io_errors_24h: None,
            trim: None,
        }
    }
}

/// Format a byte count as `"12.3 GB"`.
fn human_gb(bytes: u64) -> String {
    format!("{:.1} GB", bytes as f64 / GIB)
}

/// Classify a free-byte fraction into a [`Severity`].
fn classify_frac(frac: f64) -> Severity {
    if frac >= DISK_OK_FRAC {
        Severity::Ok
    } else if frac >= DISK_WARN_FRAC {
        Severity::Warn
    } else {
        Severity::Error
    }
}

/// Build the `disk` (SD Card) block from a `statvfs` of the data root.
fn disk_block(probe: &dyn SystemProbe, root: &Path) -> (Severity, HealthBlock) {
    match probe.statvfs(root) {
        Some(fs) => {
            let frac = fs.free_frac();
            let sev = classify_frac(frac);
            let msg = format!(
                "{} free of {} ({:.0}%)",
                human_gb(fs.free_bytes),
                human_gb(fs.total_bytes),
                frac * 100.0
            );
            (sev, HealthBlock::new(sev, msg))
        }
        None => (
            Severity::Unknown,
            HealthBlock::new(Severity::Unknown, "capacity unavailable"),
        ),
    }
}

/// Build the `storage_writable` (Storage Roots) block.
fn writable_block(probe: &dyn SystemProbe, root: &Path) -> (Severity, HealthBlock) {
    if probe.writable(root) {
        (
            Severity::Ok,
            HealthBlock::new(Severity::Ok, "archive root writable"),
        )
    } else {
        (
            Severity::Warn,
            HealthBlock::new(Severity::Warn, "archive root not writable"),
        )
    }
}

/// Build the `gadget` (USB Gadget) block from the UDC state.
fn gadget_block(probe: &dyn SystemProbe) -> (Severity, HealthBlock) {
    match probe.udc_state() {
        Some(state) if state == "configured" => (
            Severity::Ok,
            HealthBlock::new(Severity::Ok, "USB gadget configured (attached)"),
        ),
        Some(state) => (
            Severity::Warn,
            HealthBlock::new(Severity::Warn, format!("UDC state: {state}")),
        ),
        None => (
            Severity::Unknown,
            HealthBlock::new(Severity::Unknown, "no USB device controller"),
        ),
    }
}

fn severity_from_wire(severity: &str) -> Severity {
    match severity {
        "ok" => Severity::Ok,
        "warn" => Severity::Warn,
        "error" => Severity::Error,
        _ => Severity::Unknown,
    }
}

fn worker_block(raw: Option<String>, now: i64) -> HealthBlock {
    const STALE_SECS: i64 = 180;
    const DEAD_SECS: i64 = 600;
    const PROGRESS_STALE: i64 = 300;
    const CATCHUP: u64 = 200;

    #[derive(Debug, Deserialize)]
    struct WorkerHeartbeat {
        #[serde(rename = "schema")]
        _schema: u32,
        updated_at: i64,
        running: bool,
        pending: u64,
        #[serde(default)]
        last_progress_at: Option<i64>,
    }

    let Some(raw) = raw else {
        return HealthBlock::new(Severity::Unknown, "Worker status unavailable");
    };
    let parsed: WorkerHeartbeat = match serde_json::from_str(&raw) {
        Ok(parsed) => parsed,
        Err(_) => return HealthBlock::new(Severity::Unknown, "Worker status unavailable"),
    };
    let age = if now >= parsed.updated_at {
        now - parsed.updated_at
    } else {
        0
    };
    if age > DEAD_SECS {
        return HealthBlock::new(Severity::Error, "Worker not running");
    }
    if age > STALE_SECS {
        return HealthBlock::new(Severity::Warn, "Worker heartbeat stale");
    }
    if !parsed.running {
        return HealthBlock::new(Severity::Error, "Worker not running");
    }
    if parsed.pending == 0 {
        return HealthBlock::new(Severity::Ok, "Idle, queue empty");
    }
    let last_progress_at = parsed.last_progress_at.unwrap_or(parsed.updated_at);
    let since_progress = if now >= last_progress_at {
        now - last_progress_at
    } else {
        0
    };
    if since_progress > PROGRESS_STALE {
        return HealthBlock::new(
            Severity::Warn,
            format!("{} pending — not draining", parsed.pending),
        );
    }
    if parsed.pending > CATCHUP {
        return HealthBlock::new(
            Severity::Warn,
            format!("{} pending (catch-up)", parsed.pending),
        );
    }
    HealthBlock::new(Severity::Ok, format!("{} pending", parsed.pending))
}

/// Compose `GET /api/system/health` from the probe.
#[must_use]
pub fn system_health(probe: &dyn SystemProbe, paths: &SysPaths, now: i64) -> SystemHealth {
    let root = paths.archive_root.as_path();
    let worker = worker_block(probe.read_file_string(&paths.worker_health_file), now);
    let blocks = [
        ("gadget", gadget_block(probe)),
        ("worker", (severity_from_wire(worker.severity), worker)),
        ("disk", disk_block(probe, root)),
        ("storage_writable", writable_block(probe, root)),
    ];

    let overall = blocks
        .iter()
        .map(|(_, (sev, _))| *sev)
        .max_by_key(|sev| sev.rank())
        .unwrap_or(Severity::Unknown);

    let subsystems = blocks
        .into_iter()
        .map(|(key, (_, block))| (key.to_owned(), block))
        .collect();

    SystemHealth {
        overall: overall.as_str(),
        subsystems,
    }
}

/// Parse the first three whitespace-separated floats of `/proc/loadavg`.
fn parse_loadavg(s: &str) -> Option<LoadDto> {
    let mut it = s.split_whitespace();
    let one = it.next()?.parse().ok()?;
    let five = it.next()?.parse().ok()?;
    let fifteen = it.next()?.parse().ok()?;
    Some(LoadDto { one, five, fifteen })
}

/// Parse the first float of `/proc/uptime` (seconds since boot).
fn parse_uptime(s: &str) -> Option<u64> {
    let secs: f64 = s.split_whitespace().next()?.parse().ok()?;
    if secs.is_finite() && secs >= 0.0 {
        Some(secs as u64)
    } else {
        None
    }
}

/// Read one `key:` line from `/proc/meminfo` as bytes (the file reports kB).
fn meminfo_bytes(s: &str, key: &str) -> Option<u64> {
    s.lines().find_map(|line| {
        let rest = line.strip_prefix(key)?;
        let rest = rest.trim_start();
        let rest = rest.strip_prefix(':')?;
        let kb: u64 = rest.split_whitespace().next()?.parse().ok()?;
        Some(kb.saturating_mul(1024))
    })
}

/// Compose `GET /api/system/metrics` from the probe.
#[must_use]
pub fn system_metrics(probe: &dyn SystemProbe, now: Option<u64>) -> SystemMetrics {
    let load = probe
        .proc_file("loadavg")
        .as_deref()
        .and_then(parse_loadavg);
    let uptime_s = probe.proc_file("uptime").as_deref().and_then(parse_uptime);

    let meminfo = probe.proc_file("meminfo");
    let mem = meminfo.as_deref().and_then(|s| {
        let total = meminfo_bytes(s, "MemTotal")?;
        let avail = meminfo_bytes(s, "MemAvailable")?;
        Some(MemDto::new(total, avail))
    });
    let swap = meminfo.as_deref().and_then(|s| {
        let total = meminfo_bytes(s, "SwapTotal")?;
        if total == 0 {
            return None;
        }
        let free = meminfo_bytes(s, "SwapFree")?;
        Some(MemDto::new(total, free))
    });

    SystemMetrics {
        uptime_s,
        load,
        mem,
        swap,
        cpu_temp_c: probe.cpu_temp_millic().map(millic_to_celsius),
        updated_at: now,
    }
}

/// Convert milli-degrees Celsius (as the kernel reports thermal-zone temps) to
/// whole degrees with one decimal place. Pure, so it is host-testable.
fn millic_to_celsius(millic: i64) -> f64 {
    (millic as f64 / 100.0).round() / 10.0
}

/// Find the mounted filesystem whose mount point is the longest prefix of
/// `path`. Pure (operates on `/proc/mounts` text) so it is host-testable.
#[must_use]
pub fn parse_best_mount(mounts: &str, path: &Path) -> Option<MountInfo> {
    let target = path.to_string_lossy();
    let mut best: Option<MountInfo> = None;
    for line in mounts.lines() {
        let mut it = line.split_whitespace();
        let device = it.next()?;
        let mount = it.next()?;
        let fstype = it.next()?;
        if !path_under(&target, mount) {
            continue;
        }
        let better = best.as_ref().is_none_or(|b| mount.len() > b.mount.len());
        if better {
            best = Some(MountInfo {
                device: device.to_owned(),
                fstype: fstype.to_owned(),
                mount: mount.to_owned(),
            });
        }
    }
    best
}

/// Whether `target` lives under `mount` (exact, root, or `mount/...`).
fn path_under(target: &str, mount: &str) -> bool {
    if mount == "/" {
        return true;
    }
    target == mount
        || target
            .strip_prefix(mount)
            .is_some_and(|r| r.starts_with('/'))
}

/// Build one [`FilesystemDto`] for `path` from the probe.
fn filesystem_dto(probe: &dyn SystemProbe, path: &Path) -> Option<FilesystemDto> {
    let fs = probe.statvfs(path)?;
    let mount = probe.mount_for(path);
    let (device, fstype, mount) = mount.map_or_else(
        || {
            (
                String::new(),
                String::new(),
                path.to_string_lossy().into_owned(),
            )
        },
        |m| (m.device, m.fstype, m.mount),
    );
    Some(FilesystemDto {
        mount,
        device,
        fstype,
        free_bytes: fs.free_bytes,
        total_bytes: fs.total_bytes,
        free_inodes: fs.free_inodes,
        total_inodes: fs.total_inodes,
    })
}

/// Compose `GET /api/storage` from the probe (root + the data root, deduped by
/// mount point).
#[must_use]
pub fn storage(probe: &dyn SystemProbe, paths: &SysPaths) -> Storage {
    let candidates = [Path::new("/"), paths.archive_root.as_path()];
    let mut filesystems: Vec<FilesystemDto> = Vec::new();
    for path in candidates {
        if let Some(dto) = filesystem_dto(probe, path) {
            if !filesystems.iter().any(|f| f.mount == dto.mount) {
                filesystems.push(dto);
            }
        }
    }
    Storage {
        filesystems,
        governor: None,
    }
}

/// Compose `GET /api/storage/health` for the data filesystem.
#[must_use]
pub fn storage_health(probe: &dyn SystemProbe, paths: &SysPaths) -> StorageHealth {
    let root = paths.archive_root.as_path();
    let Some(fs) = probe.statvfs(root) else {
        return StorageHealth::unavailable();
    };
    let mount = probe.mount_for(root);
    let sev = classify_frac(fs.free_frac());
    StorageHealth {
        severity: sev.as_str(),
        summary: format!(
            "{} free of {}",
            human_gb(fs.free_bytes),
            human_gb(fs.total_bytes)
        ),
        device: mount.as_ref().map(|m| m.device.clone()),
        fstype: mount.as_ref().map(|m| m.fstype.clone()),
        mount: mount.map(|m| m.mount),
        used_bytes: Some(fs.used_bytes()),
        total_bytes: Some(fs.total_bytes),
        fs_errors: None,
        io_errors_24h: None,
        trim: None,
    }
}

/// The live probe: real `/proc`, `/sys`, and `statvfs` reads. On a non-Unix
/// build host every Linux-only reading degrades to `None`/`false`.
#[derive(Debug, Clone, Copy, Default)]
pub struct LinuxProbe;

impl SystemProbe for LinuxProbe {
    fn proc_file(&self, name: &str) -> Option<String> {
        std::fs::read_to_string(format!("/proc/{name}")).ok()
    }

    fn statvfs(&self, path: &Path) -> Option<FsStat> {
        statvfs_impl(path)
    }

    fn writable(&self, path: &Path) -> bool {
        writable_impl(path)
    }

    fn udc_state(&self) -> Option<String> {
        let mut entries = std::fs::read_dir("/sys/class/udc").ok()?;
        let first = entries.next()?.ok()?;
        let state = std::fs::read_to_string(first.path().join("state")).ok()?;
        Some(state.trim().to_owned())
    }

    fn mount_for(&self, path: &Path) -> Option<MountInfo> {
        let mounts = self.proc_file("mounts")?;
        parse_best_mount(&mounts, path)
    }

    fn read_file_string(&self, path: &Path) -> Option<String> {
        use std::io::Read;
        // Bounded read: the heartbeat is ~100 bytes. Cap the read so a network-
        // facing health probe can never be made to allocate/block on a huge or
        // never-ending file. (The file lives in root-owned tmpfs /run, so symlink
        // TOCTOU is outside our threat model.)
        const MAX_BYTES: u64 = 64 * 1024;
        let file = std::fs::File::open(path).ok()?;
        let mut buf = String::new();
        file.take(MAX_BYTES).read_to_string(&mut buf).ok()?;
        Some(buf)
    }

    fn cpu_temp_millic(&self) -> Option<i64> {
        let raw = std::fs::read_to_string("/sys/class/thermal/thermal_zone0/temp").ok()?;
        raw.trim().parse::<i64>().ok()
    }
}

#[cfg(unix)]
fn statvfs_impl(path: &Path) -> Option<FsStat> {
    let s = rustix::fs::statvfs(path).ok()?;
    let frsize = s.f_frsize;
    Some(FsStat {
        free_bytes: s.f_bavail.saturating_mul(frsize),
        total_bytes: s.f_blocks.saturating_mul(frsize),
        free_inodes: s.f_favail,
        total_inodes: s.f_files,
    })
}

#[cfg(not(unix))]
fn statvfs_impl(_path: &Path) -> Option<FsStat> {
    None
}

#[cfg(unix)]
fn writable_impl(path: &Path) -> bool {
    rustix::fs::access(path, rustix::fs::Access::WRITE_OK).is_ok()
}

#[cfg(not(unix))]
fn writable_impl(path: &Path) -> bool {
    std::fs::metadata(path).is_ok_and(|m| !m.permissions().readonly())
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::panic,
        clippy::expect_used,
        clippy::indexing_slicing
    )]
    use super::*;
    use std::collections::HashMap;

    #[derive(Default)]
    struct FakeProbe {
        proc: HashMap<String, String>,
        stat: Option<FsStat>,
        writable: bool,
        udc: Option<String>,
        mount: Option<MountInfo>,
        file: Option<String>,
        cpu_temp: Option<i64>,
    }

    impl SystemProbe for FakeProbe {
        fn proc_file(&self, name: &str) -> Option<String> {
            self.proc.get(name).cloned()
        }
        fn statvfs(&self, _path: &Path) -> Option<FsStat> {
            self.stat
        }
        fn writable(&self, _path: &Path) -> bool {
            self.writable
        }
        fn udc_state(&self) -> Option<String> {
            self.udc.clone()
        }
        fn mount_for(&self, _path: &Path) -> Option<MountInfo> {
            self.mount.clone()
        }
        fn read_file_string(&self, _path: &Path) -> Option<String> {
            self.file.clone()
        }
        fn cpu_temp_millic(&self) -> Option<i64> {
            self.cpu_temp
        }
    }

    fn paths() -> SysPaths {
        SysPaths {
            archive_root: PathBuf::from("/data/teslausb/archive"),
            worker_health_file: PathBuf::from("/run/teslausb/retentiond.health.json"),
        }
    }

    #[test]
    fn classify_frac_thresholds() {
        assert_eq!(classify_frac(0.50), Severity::Ok);
        assert_eq!(classify_frac(0.15), Severity::Ok);
        assert_eq!(classify_frac(0.10), Severity::Warn);
        assert_eq!(classify_frac(0.05), Severity::Warn);
        assert_eq!(classify_frac(0.01), Severity::Error);
    }

    #[test]
    fn health_rolls_up_worst_known_severity() {
        let probe = FakeProbe {
            stat: Some(FsStat {
                free_bytes: 1 << 30,
                total_bytes: 100 << 30,
                free_inodes: 1000,
                total_inodes: 10_000,
            }),
            writable: true,
            udc: Some("configured".to_owned()),
            ..FakeProbe::default()
        };
        let health = system_health(&probe, &paths(), 1_000);
        assert_eq!(health.overall, "error");
        assert_eq!(health.subsystems["gadget"].severity, "ok");
        assert_eq!(health.subsystems["worker"].severity, "unknown");
        assert_eq!(health.subsystems["disk"].severity, "error");
        assert_eq!(health.subsystems["storage_writable"].severity, "ok");
    }

    #[test]
    fn health_all_unknown_when_nothing_probed() {
        let probe = FakeProbe {
            writable: true,
            ..FakeProbe::default()
        };
        let health = system_health(&probe, &paths(), 1_000);
        // gadget=unknown, disk=unknown, storage_writable=ok → overall ok.
        assert_eq!(health.overall, "ok");
        assert_eq!(health.subsystems["disk"].severity, "unknown");
        assert_eq!(health.subsystems["gadget"].severity, "unknown");
    }

    #[test]
    fn worker_block_none_is_unknown() {
        let block = worker_block(None, 1_000);
        assert_eq!(block.severity, "unknown");
        assert_eq!(block.message, "Worker status unavailable");
    }

    #[test]
    fn worker_block_parse_fail_is_unknown() {
        let block = worker_block(Some("{oops".to_owned()), 1_000);
        assert_eq!(block.severity, "unknown");
        assert_eq!(block.message, "Worker status unavailable");
    }

    #[test]
    fn worker_block_fresh_not_running_is_error() {
        let block = worker_block(
            Some(
                r#"{"schema":1,"updated_at":990,"running":false,"pending":0,"last_progress_at":990}"#
                    .to_owned(),
            ),
            1_000,
        );
        assert_eq!(block.severity, "error");
        assert_eq!(block.message, "Worker not running");
    }

    #[test]
    fn worker_block_fresh_idle_is_ok() {
        let block = worker_block(
            Some(
                r#"{"schema":1,"updated_at":990,"running":true,"pending":0,"last_progress_at":990}"#
                    .to_owned(),
            ),
            1_000,
        );
        assert_eq!(block.severity, "ok");
        assert_eq!(block.message, "Idle, queue empty");
    }

    #[test]
    fn worker_block_fresh_pending_draining_is_ok() {
        let block = worker_block(
            Some(
                r#"{"schema":1,"updated_at":990,"running":true,"pending":200,"last_progress_at":950}"#
                    .to_owned(),
            ),
            1_000,
        );
        assert_eq!(block.severity, "ok");
        assert_eq!(block.message, "200 pending");
    }

    #[test]
    fn worker_block_fresh_pending_catchup_is_warn() {
        let block = worker_block(
            Some(
                r#"{"schema":1,"updated_at":990,"running":true,"pending":201,"last_progress_at":980}"#
                    .to_owned(),
            ),
            1_000,
        );
        assert_eq!(block.severity, "warn");
        assert_eq!(block.message, "201 pending (catch-up)");
    }

    #[test]
    fn worker_block_fresh_pending_not_draining_is_warn() {
        let block = worker_block(
            Some(
                r#"{"schema":1,"updated_at":990,"running":true,"pending":9,"last_progress_at":600}"#
                    .to_owned(),
            ),
            1_000,
        );
        assert_eq!(block.severity, "warn");
        assert_eq!(block.message, "9 pending — not draining");
    }

    #[test]
    fn worker_block_stale_is_warn() {
        let block = worker_block(
            Some(
                r#"{"schema":1,"updated_at":810,"running":true,"pending":1,"last_progress_at":810}"#
                    .to_owned(),
            ),
            1_000,
        );
        assert_eq!(block.severity, "warn");
        assert_eq!(block.message, "Worker heartbeat stale");
    }

    #[test]
    fn worker_block_dead_is_error() {
        let block = worker_block(
            Some(
                r#"{"schema":1,"updated_at":399,"running":true,"pending":1,"last_progress_at":399}"#
                    .to_owned(),
            ),
            1_000,
        );
        assert_eq!(block.severity, "error");
        assert_eq!(block.message, "Worker not running");
    }

    #[test]
    fn metrics_parse_proc_fixtures() {
        let mut proc = HashMap::new();
        proc.insert(
            "loadavg".to_owned(),
            "0.10 0.20 0.30 1/200 1234\n".to_owned(),
        );
        proc.insert("uptime".to_owned(), "98765.43 1234.00\n".to_owned());
        proc.insert(
            "meminfo".to_owned(),
            "MemTotal:        512000 kB\nMemAvailable:    256000 kB\nSwapTotal:       102400 kB\nSwapFree:         51200 kB\n".to_owned(),
        );
        let probe = FakeProbe {
            proc,
            cpu_temp: Some(47239),
            ..FakeProbe::default()
        };
        let m = system_metrics(&probe, Some(42));
        let load = m.load.expect("load");
        assert!((load.one - 0.10).abs() < 1e-9);
        assert!((load.fifteen - 0.30).abs() < 1e-9);
        assert_eq!(m.uptime_s, Some(98765));
        let mem = m.mem.expect("mem");
        assert_eq!(mem.total_bytes, 512_000 * 1024);
        assert!((mem.used_pct - 50.0).abs() < 1e-6);
        let swap = m.swap.expect("swap");
        assert_eq!(swap.total_bytes, 102_400 * 1024);
        assert_eq!(m.updated_at, Some(42));
        // 47239 milli-°C rounds to 47.2 °C.
        assert!((m.cpu_temp_c.expect("cpu_temp") - 47.2).abs() < 1e-6);
    }

    #[test]
    fn cpu_temp_absent_when_no_sensor() {
        let m = system_metrics(&FakeProbe::default(), None);
        assert!(m.cpu_temp_c.is_none());
    }

    #[test]
    fn millic_to_celsius_rounds_to_one_decimal() {
        assert!((millic_to_celsius(47000) - 47.0).abs() < 1e-9);
        assert!((millic_to_celsius(47239) - 47.2).abs() < 1e-9);
        assert!((millic_to_celsius(47250) - 47.3).abs() < 1e-9);
        assert!((millic_to_celsius(0) - 0.0).abs() < 1e-9);
    }

    #[test]
    fn metrics_swap_absent_when_zero() {
        let mut proc = HashMap::new();
        proc.insert(
            "meminfo".to_owned(),
            "MemTotal: 1000 kB\nMemAvailable: 500 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n".to_owned(),
        );
        let probe = FakeProbe {
            proc,
            ..FakeProbe::default()
        };
        let m = system_metrics(&probe, None);
        assert!(m.swap.is_none());
        assert!(m.updated_at.is_none());
    }

    #[test]
    fn best_mount_picks_longest_prefix() {
        let mounts = "\
/dev/root / ext4 rw 0 0
/dev/mmcblk0p3 /data ext4 rw 0 0
tmpfs /run tmpfs rw 0 0
";
        let m = parse_best_mount(mounts, Path::new("/data/teslausb/archive")).expect("mount");
        assert_eq!(m.mount, "/data");
        assert_eq!(m.device, "/dev/mmcblk0p3");
        assert_eq!(m.fstype, "ext4");

        let root = parse_best_mount(mounts, Path::new("/var/lib/x")).expect("root");
        assert_eq!(root.mount, "/");
    }

    #[test]
    fn storage_dedupes_by_mount() {
        // Same mount returned for both candidates → a single entry.
        let probe = FakeProbe {
            stat: Some(FsStat {
                free_bytes: 1,
                total_bytes: 2,
                free_inodes: 1,
                total_inodes: 2,
            }),
            mount: Some(MountInfo {
                device: "/dev/root".to_owned(),
                fstype: "ext4".to_owned(),
                mount: "/".to_owned(),
            }),
            ..FakeProbe::default()
        };
        let s = storage(&probe, &paths());
        assert_eq!(s.filesystems.len(), 1);
        assert!(s.governor.is_none());
    }

    #[test]
    fn storage_health_unknown_without_statvfs() {
        let probe = FakeProbe::default();
        let h = storage_health(&probe, &paths());
        assert_eq!(h.severity, "unknown");
        assert!(h.total_bytes.is_none());
        assert!(h.fs_errors.is_none());
    }
}
