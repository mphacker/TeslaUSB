//! Read-only system + storage probing for the device-status endpoints
//! (`webd.md` §2.2 / §3 "System health" / "Storage"). Every datum is a
//! `/proc`, `/sys`, or `statvfs(3)` read. A few best-effort probes use
//! read-only system commands (`nmcli`, `journalctl`) and degrade to `unknown`
//! when unavailable.
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

use crate::stats_client::{VolumeStatsClient, VolumeStatsOutcome};

/// Free-fraction at or above which a filesystem is healthy.
const DISK_OK_FRAC: f64 = 0.15;
/// Free-fraction at or above which a filesystem is merely warned (below =
/// error).
const DISK_WARN_FRAC: f64 = 0.05;
/// Bytes in one GiB, used for human-readable size messages.
const GIB: f64 = (1u64 << 30) as f64;
/// A healthy governor rewrites roughly every ~20s; 1h bounds stale "armed"
/// state without false positives on a live daemon.
const GOVERNOR_MAX_AGE_SECS: i64 = 3600;
/// Tolerated positive clock skew when validating governor timestamps.
const GOVERNOR_MAX_SKEW_SECS: i64 = 300;
const FSTRIM_TIMER_WANTS: &str = "/etc/systemd/system/timers.target.wants/fstrim.timer";

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

/// Current `WiFi` association, for the `network` health row. Best-effort.
#[derive(Debug, Clone, Default)]
pub struct WifiLink {
    /// Whether wlan0 is associated with an access point.
    pub connected: bool,
    /// The SSID when connected.
    pub ssid: Option<String>,
    /// Signal strength percent (0-100) when known.
    pub signal: Option<u8>,
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

    /// The host's primary IPv4 address (the source address the kernel selects
    /// for the default route), e.g. `"192.168.1.42"`; `None` when it cannot be
    /// determined. Best-effort, for the read-only System card only. Default
    /// `None` for probes without a live network stack (the test double).
    fn primary_ipv4(&self) -> Option<String> {
        None
    }

    /// The current `WiFi` association on wlan0 (from `nmcli`). `None` when it
    /// cannot be determined (nmcli absent/failed) → the row degrades to
    /// unknown. Default `None` for the test double.
    fn wifi_link(&self) -> Option<WifiLink> {
        None
    }

    /// Count of journal entries at error priority (`-p err`) in the last 10
    /// minutes, system-wide. `None` when the journal cannot be read (no
    /// permission / journalctl absent) → the row degrades to unknown. Default
    /// `None` for the test double.
    fn recent_error_count(&self) -> Option<u64> {
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
    /// `indexd` heartbeat path.
    pub indexer_health_file: PathBuf,
    /// Retention governor status file (`retentiond.governor.json`).
    pub governor_status_file: PathBuf,
    /// FSCK status snapshot (`fsck_status.json`).
    pub fsck_status_file: PathBuf,
    /// FSCK history snapshot (`fsck_history.json`).
    pub fsck_history_file: PathBuf,
    /// Read-only mount of the MEDIA exFAT volume.
    pub media_ro_mount: PathBuf,
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
/// volumes) degrade to `unknown / —` when unavailable.
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

/// Aggregate CPU time counters from `/proc/stat`'s `cpu` line, exposed raw so
/// the SPA can compute utilization as a delta between two polls
/// (`100 * (1 - Δidle/Δtotal)`); a single sample carries no utilization.
#[derive(Debug, Serialize)]
pub struct CpuTimes {
    /// Sum of all fields on the aggregate `cpu` line (jiffies).
    pub total: u64,
    /// Idle jiffies (`idle` + `iowait`).
    pub idle: u64,
}

/// Cumulative block-device byte counters (from `/proc/diskstats` sectors ×512),
/// exposed raw so the SPA can compute throughput as a delta between two polls.
#[derive(Debug, Serialize)]
pub struct DiskIo {
    /// Cumulative bytes read since boot.
    pub read_bytes: u64,
    /// Cumulative bytes written since boot.
    pub write_bytes: u64,
}

/// `GET /api/system/metrics`: the Live-Metrics tiles `webd` can read honestly
/// (`load`, `mem`, `swap`, `uptime`, `cpu_temp`) plus raw CPU/SD counters so the
/// SPA can compute client-side deltas for utilization/throughput.
#[derive(Debug, Serialize)]
pub struct SystemMetrics {
    /// Host name (`/proc/sys/kernel/hostname`), or `null`.
    pub hostname: Option<String>,
    /// Primary IPv4 address (the default-route source address), or `null`.
    pub ip_address: Option<String>,
    /// Hardware platform string (device-tree `model`, e.g. the Pi model), or
    /// `null` on hosts without a device tree.
    pub platform: Option<String>,
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
    /// Aggregate CPU time counters for client-side utilization deltas, or
    /// `null` when `/proc/stat` cannot be read.
    pub cpu_times: Option<CpuTimes>,
    /// SD-card (mmcblk0) cumulative byte counters for client-side throughput
    /// deltas, or `null` when `/proc/diskstats` lacks the device.
    pub sd_io: Option<DiskIo>,
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

/// One of the two USB drives the car sees (TESLACAM / MEDIA), reported with
/// honest optionality: fields are `null` when their source is unavailable.
#[derive(Debug, Serialize)]
pub struct UsbVolumeDto {
    /// "dashcam" | "media".
    pub role: &'static str,
    /// "TESLACAM" | "MEDIA".
    pub label: &'static str,
    /// Always "exfat".
    pub fstype: &'static str,
    /// Usable capacity.
    pub total_bytes: Option<u64>,
    pub free_bytes: Option<u64>,
    pub used_bytes: Option<u64>,
    /// "bitmap" (dashcam) | "statvfs" (media).
    pub source: &'static str,
    /// Freshness of the figure.
    pub stable: bool,
}

/// Quarantined-data accounting for `GET /api/storage`. Quarantined archive items
/// are genuinely-corrupt clips the device keeps (never auto-deleted) and that may
/// still self-heal to LIVE. `null`/`None` in the response when the catalog read
/// failed — honest optionality, never fabricated as zero.
#[derive(Debug, Serialize)]
pub struct QuarantinedDto {
    /// Number of `delete_state = 'QUARANTINED'` archive items.
    pub count: u64,
    /// Their total on-card footprint in bytes.
    pub bytes: u64,
}

/// `GET /api/storage`: the filesystems `webd` can `statvfs` directly. The
/// governor tier is owned by `retentiond`; unreadable/invalid status degrades
/// to `governor: null` (not fabricated).
#[derive(Debug, Serialize)]
pub struct Storage {
    /// The probed filesystems (root + the data/archive root).
    pub filesystems: Vec<FilesystemDto>,
    /// TESLACAM + MEDIA logical volumes.
    pub volumes: Vec<UsbVolumeDto>,
    /// `retentiond` governor status when available and schema-valid.
    pub governor: Option<serde_json::Value>,
    /// Quarantined-data accounting (catalog-sourced). `None` when the catalog
    /// read failed.
    pub quarantined: Option<QuarantinedDto>,
}

/// Mirror of retentiond's `retentiond.governor.json` (schema 1/2). Parsed then
/// re-serialized into `Storage.governor` so the SPA gets a validated, typed
/// object (garbage/absent file → `governor: None`).
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct GovernorDto {
    schema: u32,
    updated_at: i64,
    #[serde(default)]
    uploads_allowed: bool,
    #[serde(default)]
    seq: u64,
    #[serde(default)]
    interval_secs: u64,
    #[serde(default)]
    publisher_instance: String,
    mode: String,
    drain_only: bool,
    free_bytes: u64,
    total_bytes: u64,
    target_free_frac: f64,
    target_exit_frac: f64,
    recency_floor_secs: i64,
    last_stop: String,
    last_bytes_freed: u64,
    last_items: u64,
}

/// `GET /api/storage/health`: the data filesystem's capacity plus the
/// device/fs/mount facts. Wear telemetry is read-only and best-effort:
/// ext4 `errors_count` for filesystem errors and block discard support +
/// `fstrim.timer` enablement for TRIM status.
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
    /// ext4 filesystem error count from `/sys/fs/ext4/<dev>/errors_count`
    /// (`null` when unreadable or non-ext4).
    pub fs_errors: Option<u64>,
    /// TRIM status from `/sys/class/block/<dev>/../queue/discard_max_bytes`
    /// and whether `fstrim.timer` is enabled (`null` when unreadable).
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

/// Classify the archive volume's free fraction. When the retention governor is
/// ARMED the archive is a space-bounded ring buffer intentionally held near
/// `target_free_frac`, so free >= that target is healthy; below it the governor
/// is falling behind (warn), and below the hard danger floor recording loss is
/// imminent (error). Without an armed governor, fall back to generic capacity
/// thresholds.
fn classify_archive_frac(frac: f64, gov: Option<&GovernorDto>) -> Severity {
    match gov {
        Some(g) if g.mode == "armed" => {
            if frac >= g.target_free_frac {
                Severity::Ok
            } else if frac >= DISK_WARN_FRAC {
                Severity::Warn
            } else {
                Severity::Error
            }
        }
        _ => classify_frac(frac),
    }
}

/// Parse a small unsigned sysfs counter value.
fn parse_count(raw: &str) -> Option<u64> {
    raw.trim().parse::<u64>().ok()
}

/// Human TRIM status from discard support + fstrim.timer enablement.
fn trim_status(discard_max_bytes: Option<u64>, fstrim_timer_enabled: bool) -> &'static str {
    match discard_max_bytes {
        Some(n) if n > 0 => {
            if fstrim_timer_enabled {
                "Enabled (scheduled)"
            } else {
                "Supported"
            }
        }
        _ => "Not supported",
    }
}

/// Pure derivation of (fs_errors, trim) from raw sysfs strings already read by
/// the probe.
fn wear_telemetry(
    fstype: &str,
    errors_count_raw: Option<String>,
    discard_max_raw: Option<String>,
    fstrim_timer_enabled: bool,
) -> (Option<u64>, Option<String>) {
    let fs_errors = if fstype == "ext4" {
        errors_count_raw.as_deref().and_then(parse_count)
    } else {
        None
    };
    let trim = discard_max_raw
        .as_deref()
        .map(|raw| trim_status(parse_count(raw), fstrim_timer_enabled).to_owned());
    (fs_errors, trim)
}

/// Build the `disk` (SD Card) block from a `statvfs` of the data root.
fn disk_block(
    probe: &dyn SystemProbe,
    root: &Path,
    gov: Option<&GovernorDto>,
) -> (Severity, HealthBlock) {
    match probe.statvfs(root) {
        Some(fs) => {
            let frac = fs.free_frac();
            let sev = classify_archive_frac(frac, gov);
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

/// Build the `network` (`WiFi`) block from the live association.
fn network_block(probe: &dyn SystemProbe) -> (Severity, HealthBlock) {
    match probe.wifi_link() {
        Some(link) if link.connected => {
            let msg = match (link.ssid.as_deref(), link.signal) {
                (Some(ssid), Some(sig)) => format!("Connected to {ssid} ({sig}%)"),
                (Some(ssid), None) => format!("Connected to {ssid}"),
                (None, _) => "Connected".to_owned(),
            };
            (Severity::Ok, HealthBlock::new(Severity::Ok, msg))
        }
        Some(_) => (
            Severity::Warn,
            HealthBlock::new(Severity::Warn, "WiFi disconnected"),
        ),
        None => (
            Severity::Unknown,
            HealthBlock::new(Severity::Unknown, "WiFi state unavailable"),
        ),
    }
}

/// Build the `journal` (Recent Errors) block from the error-priority journal.
fn journal_block(probe: &dyn SystemProbe) -> (Severity, HealthBlock) {
    match probe.recent_error_count() {
        Some(0) => (
            Severity::Ok,
            HealthBlock::new(Severity::Ok, "No errors in last 10 min"),
        ),
        Some(n) => (
            Severity::Warn,
            HealthBlock::new(Severity::Warn, format!("{n} error(s) in last 10 min")),
        ),
        None => (
            Severity::Unknown,
            HealthBlock::new(Severity::Unknown, "Journal unavailable"),
        ),
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
    // saturating_sub guards against a corrupt-but-parseable updated_at
    // (e.g. i64::MIN) overflowing the subtraction; .max(0) clamps negative
    // clock skew (updated_at in the future) to a zero age.
    let age = now.saturating_sub(parsed.updated_at).max(0);
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
    let since_progress = now.saturating_sub(last_progress_at).max(0);
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

fn indexer_block(raw: Option<String>, now: i64) -> HealthBlock {
    const STALE_SECS: i64 = 180;
    const DEAD_SECS: i64 = 600;

    #[derive(Debug, Deserialize)]
    struct IndexerHeartbeat {
        #[serde(rename = "schema")]
        _schema: u32,
        updated_at: i64,
        running: bool,
    }

    let Some(raw) = raw else {
        return HealthBlock::new(Severity::Unknown, "Indexer status unavailable");
    };
    let parsed: IndexerHeartbeat = match serde_json::from_str(&raw) {
        Ok(parsed) => parsed,
        Err(_) => return HealthBlock::new(Severity::Unknown, "Indexer status unavailable"),
    };
    // saturating_sub guards a corrupt updated_at from overflowing; .max(0)
    // clamps future-dated (clock-skew) heartbeats to a zero age.
    let age = now.saturating_sub(parsed.updated_at).max(0);
    if age > DEAD_SECS {
        return HealthBlock::new(Severity::Error, "Indexer not running");
    }
    if age > STALE_SECS {
        return HealthBlock::new(Severity::Warn, "Indexer stalled");
    }
    if !parsed.running {
        return HealthBlock::new(Severity::Error, "Indexer not running");
    }
    HealthBlock::new(Severity::Ok, "Indexer healthy")
}

/// Compose `GET /api/system/health` from the probe.
#[must_use]
pub fn system_health(probe: &dyn SystemProbe, paths: &SysPaths, now: i64) -> SystemHealth {
    let root = paths.archive_root.as_path();
    let gov = read_governor_dto(probe, paths);
    let worker = worker_block(probe.read_file_string(&paths.worker_health_file), now);
    let indexer = indexer_block(probe.read_file_string(&paths.indexer_health_file), now);
    let blocks = [
        ("gadget", gadget_block(probe)),
        ("worker", (severity_from_wire(worker.severity), worker)),
        ("indexer", (severity_from_wire(indexer.severity), indexer)),
        ("disk", disk_block(probe, root, gov.as_ref())),
        ("storage_writable", writable_block(probe, root)),
        ("network", network_block(probe)),
        ("journal", journal_block(probe)),
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

/// Parse `/proc/stat`'s aggregate `cpu` line into total/idle jiffies.
fn parse_cpu_times(s: &str) -> Option<CpuTimes> {
    let line = s.lines().next()?;
    let mut it = line.split_whitespace();
    if it.next()? != "cpu" {
        return None;
    }
    let vals: Vec<u64> = it.filter_map(|t| t.parse::<u64>().ok()).collect();
    // Need at least user,nice,system,idle,iowait.
    if vals.len() < 5 {
        return None;
    }
    let total: u64 = vals.iter().copied().sum();
    let idle = vals[3].saturating_add(vals[4]);
    Some(CpuTimes { total, idle })
}

/// Parse cumulative read/write bytes for `dev` from `/proc/diskstats`
/// (sectors ×512). `None` when the device is absent.
fn parse_disk_io(s: &str, dev: &str) -> Option<DiskIo> {
    for line in s.lines() {
        let f: Vec<&str> = line.split_whitespace().collect();
        if f.len() > 9 && f[2] == dev {
            let read_sectors: u64 = f[5].parse().ok()?;
            let write_sectors: u64 = f[9].parse().ok()?;
            return Some(DiskIo {
                read_bytes: read_sectors.saturating_mul(512),
                write_bytes: write_sectors.saturating_mul(512),
            });
        }
    }
    None
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

    let hostname = probe
        .read_file_string(Path::new(HOSTNAME_PATH))
        .as_deref()
        .and_then(clean_host_string);
    let platform = probe
        .read_file_string(Path::new(PLATFORM_MODEL_PATH))
        .as_deref()
        .and_then(clean_host_string);
    let ip_address = probe.primary_ipv4();
    let cpu_times = probe.proc_file("stat").as_deref().and_then(parse_cpu_times);
    let sd_io = probe
        .proc_file("diskstats")
        .as_deref()
        .and_then(|s| parse_disk_io(s, SD_DISK_DEV));

    SystemMetrics {
        hostname,
        ip_address,
        platform,
        uptime_s,
        load,
        mem,
        swap,
        cpu_temp_c: probe.cpu_temp_millic().map(millic_to_celsius),
        cpu_times,
        sd_io,
        updated_at: now,
    }
}

/// Linux block-device name for the Pi SD card.
const SD_DISK_DEV: &str = "mmcblk0";
/// Kernel hostname, exposed as a plain string.
const HOSTNAME_PATH: &str = "/proc/sys/kernel/hostname";
/// Device-tree node carrying the board model string (Raspberry Pi models expose
/// it; the value is NUL-terminated). Absent on non-device-tree hosts.
const PLATFORM_MODEL_PATH: &str = "/sys/firmware/devicetree/base/model";

/// Normalize a host-fact string read from `/proc` or the device tree: drop NUL
/// bytes (the device-tree `model` node is NUL-terminated) and trim surrounding
/// whitespace; `None` if nothing meaningful is left. Pure, so it is testable.
fn clean_host_string(raw: &str) -> Option<String> {
    let cleaned: String = raw.chars().filter(|c| *c != '\0').collect();
    let trimmed = cleaned.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_owned())
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

fn validate_governor(dto: GovernorDto, now: i64) -> Option<GovernorDto> {
    if dto.schema != 1 && dto.schema != 2 {
        return None;
    }
    let age = now.saturating_sub(dto.updated_at);
    if age > GOVERNOR_MAX_AGE_SECS {
        return None;
    }
    if age < -GOVERNOR_MAX_SKEW_SECS {
        return None;
    }
    if dto.mode != "armed" && dto.mode != "dryrun" {
        return None;
    }
    if dto.total_bytes == 0 || dto.free_bytes > dto.total_bytes {
        return None;
    }
    if !(dto.target_free_frac > 0.0 && dto.target_free_frac <= 1.0) {
        return None;
    }
    if !(dto.target_exit_frac > 0.0 && dto.target_exit_frac <= 1.0) {
        return None;
    }
    if dto.recency_floor_secs < 0 {
        return None;
    }
    if dto.last_stop.is_empty() {
        return None;
    }
    Some(dto)
}

fn read_governor_dto(probe: &dyn SystemProbe, paths: &SysPaths) -> Option<GovernorDto> {
    let raw = probe.read_file_string(&paths.governor_status_file)?;
    let dto: GovernorDto = serde_json::from_str(&raw).ok()?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(dto.updated_at);
    validate_governor(dto, now)
}

pub(crate) fn retention_governor(
    probe: &dyn SystemProbe,
    paths: &SysPaths,
) -> Option<serde_json::Value> {
    read_governor_dto(probe, paths).and_then(|dto| serde_json::to_value(dto).ok())
}

/// Compose `GET /api/storage` from the probe (root + the data root, deduped by
/// mount point).
#[must_use]
pub fn storage(
    probe: &dyn SystemProbe,
    paths: &SysPaths,
    stats: &dyn VolumeStatsClient,
    quarantined: Option<QuarantinedDto>,
) -> Storage {
    let candidates = [Path::new("/"), paths.archive_root.as_path()];
    let mut filesystems: Vec<FilesystemDto> = Vec::new();
    for path in candidates {
        if let Some(dto) = filesystem_dto(probe, path) {
            if !filesystems.iter().any(|f| f.mount == dto.mount) {
                filesystems.push(dto);
            }
        }
    }
    let dashcam = match stats.volume_stats() {
        Ok(VolumeStatsOutcome::Stats(v)) => UsbVolumeDto {
            role: "dashcam",
            label: "TESLACAM",
            fstype: "exfat",
            total_bytes: Some(v.total_bytes),
            free_bytes: Some(v.free_bytes),
            used_bytes: Some(v.used_bytes),
            source: "bitmap",
            stable: v.stable,
        },
        Ok(VolumeStatsOutcome::Unavailable) | Err(_) => UsbVolumeDto {
            role: "dashcam",
            label: "TESLACAM",
            fstype: "exfat",
            total_bytes: None,
            free_bytes: None,
            used_bytes: None,
            source: "bitmap",
            stable: false,
        },
    };
    let media = probe.statvfs(paths.media_ro_mount.as_path()).map_or(
        UsbVolumeDto {
            role: "media",
            label: "MEDIA",
            fstype: "exfat",
            total_bytes: None,
            free_bytes: None,
            used_bytes: None,
            source: "statvfs",
            stable: false,
        },
        |fs| UsbVolumeDto {
            role: "media",
            label: "MEDIA",
            fstype: "exfat",
            total_bytes: Some(fs.total_bytes),
            free_bytes: Some(fs.free_bytes),
            used_bytes: Some(fs.used_bytes()),
            source: "statvfs",
            stable: true,
        },
    );
    Storage {
        filesystems,
        volumes: vec![dashcam, media],
        governor: retention_governor(probe, paths),
        quarantined,
    }
}

/// Compose `GET /api/storage/health` for the data filesystem.
#[must_use]
pub fn storage_health(probe: &dyn SystemProbe, paths: &SysPaths) -> StorageHealth {
    let root = paths.archive_root.as_path();
    let Some(fs) = probe.statvfs(root) else {
        return StorageHealth::unavailable();
    };
    let gov = read_governor_dto(probe, paths);
    let mount = probe.mount_for(root);
    let (fs_errors, trim) = match mount.as_ref() {
        Some(m) => {
            let dev_base = Path::new(&m.device)
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("");
            let errors_raw =
                probe.read_file_string(Path::new(&format!("/sys/fs/ext4/{dev_base}/errors_count")));
            let discard_raw = probe.read_file_string(Path::new(&format!(
                "/sys/class/block/{dev_base}/../queue/discard_max_bytes"
            )));
            let timer_enabled = probe
                .read_file_string(Path::new(FSTRIM_TIMER_WANTS))
                .is_some();
            wear_telemetry(&m.fstype, errors_raw, discard_raw, timer_enabled)
        }
        None => (None, None),
    };
    let sev = classify_archive_frac(fs.free_frac(), gov.as_ref());
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
        fs_errors,
        trim,
    }
}

/// The live probe: real `/proc`, `/sys`, and `statvfs` reads. On a non-Unix
/// build host every Linux-only reading degrades to `None`/`false`.
#[derive(Debug, Clone, Copy, Default)]
pub struct LinuxProbe;

fn split_nmcli_terse_fields(line: &str) -> Option<(String, String, String)> {
    let mut fields = Vec::with_capacity(3);
    let mut current = String::new();
    let mut chars = line.chars();
    while let Some(ch) = chars.next() {
        if ch == '\\' {
            if let Some(next) = chars.next() {
                current.push(next);
            } else {
                current.push('\\');
            }
            continue;
        }
        if ch == ':' && fields.len() < 2 {
            fields.push(current.trim().to_owned());
            current.clear();
            continue;
        }
        current.push(ch);
    }
    fields.push(current.trim().to_owned());
    if fields.len() != 3 {
        return None;
    }
    let mut fields = fields.into_iter();
    Some((fields.next()?, fields.next()?, fields.next()?))
}

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

    fn primary_ipv4(&self) -> Option<String> {
        // connect() on a UDP socket sends no packet; it only asks the kernel to
        // select the egress interface + source address for the default route, so
        // this returns the address the Pi is reached at (wlan0) and works with no
        // internet. TEST-NET-1 (RFC 5737) is inert even if a datagram were sent.
        let sock = std::net::UdpSocket::bind("0.0.0.0:0").ok()?;
        sock.connect("192.0.2.1:9").ok()?;
        let ip = sock.local_addr().ok()?.ip();
        if ip.is_unspecified() || ip.is_loopback() {
            return None;
        }
        Some(ip.to_string())
    }

    fn wifi_link(&self) -> Option<WifiLink> {
        // Bound the probe: the SPA polls /api/system/health repeatedly, so a hung
        // nmcli must not pile up blocking threads. GNU `timeout` exits 124 on
        // timeout -> status.success() is false -> degrades to "unknown" like any
        // other probe failure. Mirrors the bounded runner in wifi_mutate.rs.
        let out = std::process::Command::new("timeout")
            .args([
                "-k",
                "2",
                "5",
                "nmcli",
                "-t",
                "-f",
                "ACTIVE,SSID,SIGNAL",
                "dev",
                "wifi",
            ])
            .output()
            .ok()?;
        if !out.status.success() {
            return None;
        }
        for line in String::from_utf8_lossy(&out.stdout).lines() {
            let Some((active, ssid, signal)) = split_nmcli_terse_fields(line) else {
                continue;
            };
            if active != "yes" {
                continue;
            }
            return Some(WifiLink {
                connected: true,
                ssid: (!ssid.is_empty()).then_some(ssid),
                signal: signal.parse::<u8>().ok(),
            });
        }
        Some(WifiLink::default())
    }

    fn recent_error_count(&self) -> Option<u64> {
        let out = std::process::Command::new("timeout")
            .args([
                "-k",
                "2",
                "5",
                "journalctl",
                "--since=-10min",
                "--priority=err",
                "--no-pager",
                "--quiet",
                "--output=cat",
            ])
            .output()
            .ok()?;
        if !out.status.success() {
            return None;
        }
        let count = String::from_utf8_lossy(&out.stdout)
            .lines()
            .filter(|line| !line.trim().is_empty())
            .count();
        u64::try_from(count).ok()
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
    use crate::stats_client::{
        VolumeStats, VolumeStatsClient, VolumeStatsError, VolumeStatsOutcome,
    };
    use std::collections::HashMap;

    #[derive(Default)]
    struct FakeProbe {
        proc: HashMap<String, String>,
        stat: Option<FsStat>,
        writable: bool,
        udc: Option<String>,
        mount: Option<MountInfo>,
        worker_file: Option<String>,
        indexer_file: Option<String>,
        governor_file: Option<String>,
        cpu_temp: Option<i64>,
        hostname_file: Option<String>,
        platform_file: Option<String>,
        ipv4: Option<String>,
        wifi_link: Option<WifiLink>,
        recent_error_count: Option<u64>,
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
        fn read_file_string(&self, path: &Path) -> Option<String> {
            if path == paths().worker_health_file.as_path() {
                return self.worker_file.clone();
            }
            if path == paths().indexer_health_file.as_path() {
                return self.indexer_file.clone();
            }
            if path == paths().governor_status_file.as_path() {
                return self.governor_file.clone();
            }
            if path == Path::new(HOSTNAME_PATH) {
                return self.hostname_file.clone();
            }
            if path == Path::new(PLATFORM_MODEL_PATH) {
                return self.platform_file.clone();
            }
            None
        }
        fn cpu_temp_millic(&self) -> Option<i64> {
            self.cpu_temp
        }
        fn primary_ipv4(&self) -> Option<String> {
            self.ipv4.clone()
        }
        fn wifi_link(&self) -> Option<WifiLink> {
            self.wifi_link.clone()
        }
        fn recent_error_count(&self) -> Option<u64> {
            self.recent_error_count
        }
    }

    fn paths() -> SysPaths {
        SysPaths {
            archive_root: PathBuf::from("/data/teslausb/archive"),
            worker_health_file: PathBuf::from("/run/teslausb/retentiond.health.json"),
            indexer_health_file: PathBuf::from("/run/teslausb/indexd.health.json"),
            governor_status_file: PathBuf::from("/run/teslausb/retentiond.governor.json"),
            fsck_status_file: PathBuf::from("/data/teslausb/fsck_status.json"),
            fsck_history_file: PathBuf::from("/data/teslausb/fsck_history.json"),
            media_ro_mount: PathBuf::from("/run/teslausb/media-ro"),
        }
    }

    fn valid_governor_dto(updated_at: i64) -> GovernorDto {
        GovernorDto {
            schema: 1,
            updated_at,
            uploads_allowed: true,
            seq: 1,
            interval_secs: 20,
            publisher_instance: "publisher".to_owned(),
            mode: "armed".to_owned(),
            drain_only: true,
            free_bytes: 53_687_091_200,
            total_bytes: 504_658_657_280,
            target_free_frac: 0.08,
            target_exit_frac: 0.1,
            recency_floor_secs: 3600,
            last_stop: "already_healthy".to_owned(),
            last_bytes_freed: 0,
            last_items: 0,
        }
    }

    struct FakeStatsClient {
        outcome: Option<VolumeStatsOutcome>,
    }

    impl VolumeStatsClient for FakeStatsClient {
        fn volume_stats(&self) -> Result<VolumeStatsOutcome, VolumeStatsError> {
            self.outcome.ok_or_else(|| {
                VolumeStatsError::Io(std::io::Error::new(
                    std::io::ErrorKind::Unsupported,
                    "stats unavailable",
                ))
            })
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
    fn classify_archive_frac_thresholds() {
        let armed = GovernorDto {
            mode: "armed".into(),
            target_free_frac: 0.08,
            target_exit_frac: 0.10,
            ..Default::default()
        };
        assert_eq!(classify_archive_frac(0.10, Some(&armed)), Severity::Ok);
        assert_eq!(classify_archive_frac(0.08, Some(&armed)), Severity::Ok);
        assert_eq!(classify_archive_frac(0.079, Some(&armed)), Severity::Warn);
        assert_eq!(classify_archive_frac(0.05, Some(&armed)), Severity::Warn);
        assert_eq!(classify_archive_frac(0.049, Some(&armed)), Severity::Error);

        assert_eq!(classify_archive_frac(0.10, None), Severity::Warn);
        assert_eq!(classify_archive_frac(0.20, None), Severity::Ok);

        let dryrun = GovernorDto {
            mode: "dryrun".into(),
            target_free_frac: 0.08,
            target_exit_frac: 0.10,
            ..Default::default()
        };
        assert_eq!(classify_archive_frac(0.10, Some(&dryrun)), Severity::Warn);
    }

    #[test]
    fn parse_count_parses_uints() {
        assert_eq!(parse_count("0"), Some(0));
        assert_eq!(parse_count(" 3\n"), Some(3));
        assert_eq!(parse_count("x"), None);
        assert_eq!(parse_count(""), None);
    }

    #[test]
    fn trim_status_matrix() {
        assert_eq!(trim_status(Some(171966464), true), "Enabled (scheduled)");
        assert_eq!(trim_status(Some(171966464), false), "Supported");
        assert_eq!(trim_status(Some(0), true), "Not supported");
        assert_eq!(trim_status(None, true), "Not supported");
    }

    #[test]
    fn wear_telemetry_derives_fields() {
        assert_eq!(
            wear_telemetry(
                "ext4",
                Some("3".to_owned()),
                Some("171966464".to_owned()),
                true
            ),
            (Some(3), Some("Enabled (scheduled)".to_owned()))
        );
        assert_eq!(
            wear_telemetry("vfat", Some("5".to_owned()), None, false),
            (None, None)
        );
        assert_eq!(
            wear_telemetry("ext4", None, Some("0".to_owned()), true),
            (None, Some("Not supported".to_owned()))
        );
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
    fn network_block_connected_with_signal_is_ok() {
        let probe = FakeProbe {
            wifi_link: Some(WifiLink {
                connected: true,
                ssid: Some("Trez".to_owned()),
                signal: Some(51),
            }),
            ..FakeProbe::default()
        };
        let (sev, block) = network_block(&probe);
        assert_eq!(sev.as_str(), "ok");
        assert_eq!(block.severity, "ok");
        assert_eq!(block.message, "Connected to Trez (51%)");
    }

    #[test]
    fn network_block_connected_without_signal_is_ok() {
        let probe = FakeProbe {
            wifi_link: Some(WifiLink {
                connected: true,
                ssid: Some("Trez".to_owned()),
                signal: None,
            }),
            ..FakeProbe::default()
        };
        let (sev, block) = network_block(&probe);
        assert_eq!(sev.as_str(), "ok");
        assert_eq!(block.severity, "ok");
        assert_eq!(block.message, "Connected to Trez");
    }

    #[test]
    fn network_block_disconnected_is_warn() {
        let probe = FakeProbe {
            wifi_link: Some(WifiLink {
                connected: false,
                ..WifiLink::default()
            }),
            ..FakeProbe::default()
        };
        let (sev, block) = network_block(&probe);
        assert_eq!(sev.as_str(), "warn");
        assert_eq!(block.severity, "warn");
        assert_eq!(block.message, "WiFi disconnected");
    }

    #[test]
    fn network_block_unprobeable_is_unknown() {
        let probe = FakeProbe::default();
        let (sev, block) = network_block(&probe);
        assert_eq!(sev.as_str(), "unknown");
        assert_eq!(block.severity, "unknown");
        assert_eq!(block.message, "WiFi state unavailable");
    }

    #[test]
    fn journal_block_zero_is_ok() {
        let probe = FakeProbe {
            recent_error_count: Some(0),
            ..FakeProbe::default()
        };
        let (sev, block) = journal_block(&probe);
        assert_eq!(sev.as_str(), "ok");
        assert_eq!(block.severity, "ok");
        assert_eq!(block.message, "No errors in last 10 min");
    }

    #[test]
    fn journal_block_nonzero_is_warn() {
        let probe = FakeProbe {
            recent_error_count: Some(3),
            ..FakeProbe::default()
        };
        let (sev, block) = journal_block(&probe);
        assert_eq!(sev.as_str(), "warn");
        assert_eq!(block.severity, "warn");
        assert!(block.message.contains('3'));
    }

    #[test]
    fn journal_block_unprobeable_is_unknown() {
        let probe = FakeProbe::default();
        let (sev, block) = journal_block(&probe);
        assert_eq!(sev.as_str(), "unknown");
        assert_eq!(block.severity, "unknown");
        assert_eq!(block.message, "Journal unavailable");
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
    fn worker_block_corrupt_updated_at_does_not_overflow() {
        // A parseable-but-garbage updated_at must not panic/overflow the age
        // subtraction; i64::MIN saturates to a huge age → dead.
        let block = worker_block(
            Some(
                r#"{"schema":1,"updated_at":-9223372036854775808,"running":true,"pending":0,"last_progress_at":0}"#
                    .to_owned(),
            ),
            1_000,
        );
        assert_eq!(block.severity, "error");
        assert_eq!(block.message, "Worker not running");
    }

    #[test]
    fn indexer_block_corrupt_updated_at_does_not_overflow() {
        let block = indexer_block(
            Some(r#"{"schema":1,"updated_at":-9223372036854775808,"running":true}"#.to_owned()),
            1_000,
        );
        assert_eq!(block.severity, "error");
        assert_eq!(block.message, "Indexer not running");
    }

    #[test]
    fn indexer_block_future_skew_is_ok() {
        // updated_at in the future (clock skew) clamps to age 0 → healthy.
        let block = indexer_block(
            Some(r#"{"schema":1,"updated_at":5000,"running":true}"#.to_owned()),
            1_000,
        );
        assert_eq!(block.severity, "ok");
        assert_eq!(block.message, "Indexer healthy");
    }

    #[test]
    fn indexer_block_none_is_unknown() {
        let block = indexer_block(None, 1_000);
        assert_eq!(block.severity, "unknown");
        assert_eq!(block.message, "Indexer status unavailable");
    }

    #[test]
    fn indexer_block_parse_fail_is_unknown() {
        let block = indexer_block(Some("{oops".to_owned()), 1_000);
        assert_eq!(block.severity, "unknown");
        assert_eq!(block.message, "Indexer status unavailable");
    }

    #[test]
    fn indexer_block_dead_is_error() {
        let block = indexer_block(
            Some(r#"{"schema":1,"updated_at":399,"running":true}"#.to_owned()),
            1_000,
        );
        assert_eq!(block.severity, "error");
        assert_eq!(block.message, "Indexer not running");
    }

    #[test]
    fn indexer_block_stale_is_warn() {
        let block = indexer_block(
            Some(r#"{"schema":1,"updated_at":810,"running":true}"#.to_owned()),
            1_000,
        );
        assert_eq!(block.severity, "warn");
        assert_eq!(block.message, "Indexer stalled");
    }

    #[test]
    fn indexer_block_not_running_is_error() {
        let block = indexer_block(
            Some(r#"{"schema":1,"updated_at":990,"running":false}"#.to_owned()),
            1_000,
        );
        assert_eq!(block.severity, "error");
        assert_eq!(block.message, "Indexer not running");
    }

    #[test]
    fn indexer_block_healthy_is_ok() {
        let block = indexer_block(
            Some(r#"{"schema":1,"updated_at":990,"running":true}"#.to_owned()),
            1_000,
        );
        assert_eq!(block.severity, "ok");
        assert_eq!(block.message, "Indexer healthy");
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
    fn parse_cpu_times_sums_total_and_idle() {
        let s = "cpu  100 0 50 800 40 0 10 0 0 0\ncpu0 1 2 3 4 5\n";
        let got = parse_cpu_times(s).expect("cpu");
        assert_eq!(got.total, 1000);
        assert_eq!(got.idle, 840);
    }

    #[test]
    fn parse_cpu_times_rejects_non_cpu() {
        let s = "intr 123 456\ncpu 1 2 3 4 5\n";
        assert!(parse_cpu_times(s).is_none());
    }

    #[test]
    fn parse_disk_io_reads_mmcblk0() {
        let s = " 179       0 mmcblk0 199979 13155 29888336 3420661 160002 10834 98764001 100392075 0 4135060 103840049 0 0 0 0 4730 27312\n";
        let got = parse_disk_io(s, "mmcblk0").expect("mmcblk0");
        assert_eq!(got.read_bytes, 29_888_336_u64 * 512);
        assert_eq!(got.write_bytes, 98_764_001_u64 * 512);
    }

    #[test]
    fn parse_disk_io_none_when_absent() {
        let s = "   8       0 sda 1 2 3 4 5 6 7 8 0 0 0 0\n";
        assert!(parse_disk_io(s, "mmcblk0").is_none());
    }

    #[test]
    fn metrics_exposes_cpu_and_sd_io_when_present() {
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
        proc.insert(
            "stat".to_owned(),
            "cpu  100 0 50 800 40 0 10 0 0 0\ncpu0 1 2 3 4 5\n".to_owned(),
        );
        proc.insert(
            "diskstats".to_owned(),
            " 179       0 mmcblk0 199979 13155 29888336 3420661 160002 10834 98764001 100392075 0 4135060 103840049 0 0 0 0 4730 27312\n".to_owned(),
        );
        let probe = FakeProbe {
            proc,
            cpu_temp: Some(47239),
            ..FakeProbe::default()
        };
        let m = system_metrics(&probe, Some(42));
        let cpu = m.cpu_times.expect("cpu_times");
        assert_eq!(cpu.total, 1000);
        assert_eq!(cpu.idle, 840);
        let io = m.sd_io.expect("sd_io");
        assert_eq!(io.read_bytes, 29_888_336_u64 * 512);
        assert_eq!(io.write_bytes, 98_764_001_u64 * 512);
    }

    #[test]
    fn cpu_temp_absent_when_no_sensor() {
        let m = system_metrics(&FakeProbe::default(), None);
        assert!(m.cpu_temp_c.is_none());
    }

    #[test]
    fn clean_host_string_trims_nul_and_whitespace() {
        assert_eq!(
            clean_host_string("cybertruck\n").as_deref(),
            Some("cybertruck")
        );
        assert_eq!(
            clean_host_string("Raspberry Pi Zero 2 W Rev 1.0\0").as_deref(),
            Some("Raspberry Pi Zero 2 W Rev 1.0")
        );
        assert_eq!(clean_host_string("  \0 \n"), None);
        assert_eq!(clean_host_string(""), None);
    }

    #[test]
    fn metrics_populates_host_facts_when_available() {
        let probe = FakeProbe {
            hostname_file: Some("cybertruck\n".to_owned()),
            platform_file: Some("Raspberry Pi Zero 2 W Rev 1.0\0".to_owned()),
            ipv4: Some("192.168.1.42".to_owned()),
            ..FakeProbe::default()
        };
        let m = system_metrics(&probe, None);
        assert_eq!(m.hostname.as_deref(), Some("cybertruck"));
        assert_eq!(m.platform.as_deref(), Some("Raspberry Pi Zero 2 W Rev 1.0"));
        assert_eq!(m.ip_address.as_deref(), Some("192.168.1.42"));
    }

    #[test]
    fn metrics_host_facts_none_when_unavailable() {
        let m = system_metrics(&FakeProbe::default(), None);
        assert!(m.hostname.is_none());
        assert!(m.ip_address.is_none());
        assert!(m.platform.is_none());
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
        let stats = FakeStatsClient {
            outcome: Some(VolumeStatsOutcome::Unavailable),
        };
        let s = storage(&probe, &paths(), &stats, None);
        assert_eq!(s.filesystems.len(), 1);
        assert_eq!(s.volumes.len(), 2);
        assert!(s.governor.is_none());
    }

    #[test]
    fn storage_reads_governor_when_present_and_valid() {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        let probe = FakeProbe {
            governor_file: Some(format!(
                "{{\"schema\":1,\"updated_at\":{now},\"mode\":\"armed\",\"drain_only\":true,\"free_bytes\":53687091200,\"total_bytes\":504658657280,\"target_free_frac\":0.08,\"target_exit_frac\":0.1,\"recency_floor_secs\":3600,\"last_stop\":\"already_healthy\",\"last_bytes_freed\":0,\"last_items\":0}}"
            )),
            ..FakeProbe::default()
        };
        let stats = FakeStatsClient {
            outcome: Some(VolumeStatsOutcome::Unavailable),
        };
        let out = storage(&probe, &paths(), &stats, None);
        let governor = out.governor.expect("governor");
        assert_eq!(governor["mode"], "armed");
        assert_eq!(governor["free_bytes"], 53_687_091_200_u64);
    }

    #[test]
    fn storage_governor_none_when_absent() {
        let probe = FakeProbe::default();
        let stats = FakeStatsClient {
            outcome: Some(VolumeStatsOutcome::Unavailable),
        };
        let out = storage(&probe, &paths(), &stats, None);
        assert!(out.governor.is_none());
    }

    #[test]
    fn storage_governor_none_when_unparseable() {
        let probe = FakeProbe {
            governor_file: Some("{not json".to_owned()),
            ..FakeProbe::default()
        };
        let stats = FakeStatsClient {
            outcome: Some(VolumeStatsOutcome::Unavailable),
        };
        let out = storage(&probe, &paths(), &stats, None);
        assert!(out.governor.is_none());
    }

    #[test]
    fn storage_governor_none_when_wrong_schema() {
        let probe = FakeProbe {
            governor_file: Some(
                "{\"schema\":3,\"updated_at\":1700000000,\"mode\":\"armed\",\"drain_only\":true,\"free_bytes\":1,\"total_bytes\":2,\"target_free_frac\":0.08,\"target_exit_frac\":0.1,\"recency_floor_secs\":3600,\"last_stop\":\"already_healthy\",\"last_bytes_freed\":0,\"last_items\":0}".to_owned(),
            ),
            ..FakeProbe::default()
        };
        let stats = FakeStatsClient {
            outcome: Some(VolumeStatsOutcome::Unavailable),
        };
        let out = storage(&probe, &paths(), &stats, None);
        assert!(out.governor.is_none());
    }

    #[test]
    fn storage_reads_schema2_governor_when_present_and_valid() {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        let probe = FakeProbe {
            governor_file: Some(format!(
                "{{\"schema\":2,\"updated_at\":{now},\"uploads_allowed\":false,\"seq\":9,\"interval_secs\":20,\"publisher_instance\":\"inst\",\"mode\":\"armed\",\"drain_only\":true,\"free_bytes\":53687091200,\"total_bytes\":504658657280,\"target_free_frac\":0.08,\"target_exit_frac\":0.1,\"recency_floor_secs\":3600,\"last_stop\":\"already_healthy\",\"last_bytes_freed\":0,\"last_items\":0}}"
            )),
            ..FakeProbe::default()
        };
        let stats = FakeStatsClient {
            outcome: Some(VolumeStatsOutcome::Unavailable),
        };
        let out = storage(&probe, &paths(), &stats, None);
        let governor = out.governor.expect("governor");
        assert_eq!(governor["schema"], 2);
        assert_eq!(governor["uploads_allowed"], false);
        assert_eq!(governor["seq"], 9);
        assert_eq!(governor["interval_secs"], 20);
        assert_eq!(governor["publisher_instance"], "inst");
    }

    #[test]
    fn storage_governor_none_when_required_field_missing() {
        let probe = FakeProbe {
            governor_file: Some(
                "{\"schema\":1,\"updated_at\":1700000000,\"drain_only\":true,\"free_bytes\":1,\"total_bytes\":2,\"target_free_frac\":0.08,\"target_exit_frac\":0.1,\"recency_floor_secs\":3600,\"last_stop\":\"already_healthy\",\"last_bytes_freed\":0,\"last_items\":0}".to_owned(),
            ),
            ..FakeProbe::default()
        };
        let stats = FakeStatsClient {
            outcome: Some(VolumeStatsOutcome::Unavailable),
        };
        let out = storage(&probe, &paths(), &stats, None);
        assert!(out.governor.is_none());
    }

    #[test]
    fn validate_governor_accepts_valid_payload() {
        let now = 1_700_000_000;
        let dto = valid_governor_dto(now);
        assert!(validate_governor(dto, now).is_some());
    }

    #[test]
    fn validate_governor_accepts_schema2_and_schema1_without_new_fields() {
        let now = 1_700_000_000;
        let schema2: GovernorDto = serde_json::from_str(
            "{\"schema\":2,\"updated_at\":1700000000,\"uploads_allowed\":false,\"seq\":5,\"interval_secs\":20,\"publisher_instance\":\"inst\",\"mode\":\"armed\",\"drain_only\":true,\"free_bytes\":1,\"total_bytes\":2,\"target_free_frac\":0.08,\"target_exit_frac\":0.1,\"recency_floor_secs\":3600,\"last_stop\":\"already_healthy\",\"last_bytes_freed\":0,\"last_items\":0}",
        )
        .expect("schema2 parse");
        assert!(validate_governor(schema2, now).is_some());

        let schema1_without_new_fields: GovernorDto = serde_json::from_str(
            "{\"schema\":1,\"updated_at\":1700000000,\"mode\":\"armed\",\"drain_only\":true,\"free_bytes\":1,\"total_bytes\":2,\"target_free_frac\":0.08,\"target_exit_frac\":0.1,\"recency_floor_secs\":3600,\"last_stop\":\"already_healthy\",\"last_bytes_freed\":0,\"last_items\":0}",
        )
        .expect("schema1 parse");
        assert!(validate_governor(schema1_without_new_fields, now).is_some());
    }

    #[test]
    fn validate_governor_rejects_stale_payload() {
        let now = 1_700_000_000;
        let dto = valid_governor_dto(now - 4000);
        assert!(validate_governor(dto, now).is_none());
    }

    #[test]
    fn validate_governor_rejects_implausibly_future_payload() {
        let now = 1_700_000_000;
        let dto = valid_governor_dto(now + 400);
        assert!(validate_governor(dto, now).is_none());
    }

    #[test]
    fn validate_governor_rejects_bogus_mode() {
        let now = 1_700_000_000;
        let mut dto = valid_governor_dto(now);
        dto.mode = "bogus".to_owned();
        assert!(validate_governor(dto, now).is_none());
    }

    #[test]
    fn validate_governor_rejects_empty_mode() {
        let now = 1_700_000_000;
        let mut dto = valid_governor_dto(now);
        dto.mode = String::new();
        assert!(validate_governor(dto, now).is_none());
    }

    #[test]
    fn validate_governor_rejects_free_bytes_above_total() {
        let now = 1_700_000_000;
        let mut dto = valid_governor_dto(now);
        dto.free_bytes = dto.total_bytes + 1;
        assert!(validate_governor(dto, now).is_none());
    }

    #[test]
    fn validate_governor_rejects_zero_total_bytes() {
        let now = 1_700_000_000;
        let mut dto = valid_governor_dto(now);
        dto.total_bytes = 0;
        dto.free_bytes = 0;
        assert!(validate_governor(dto, now).is_none());
    }

    #[test]
    fn validate_governor_rejects_zero_target_exit_fraction() {
        let now = 1_700_000_000;
        let mut dto = valid_governor_dto(now);
        dto.target_exit_frac = 0.0;
        assert!(validate_governor(dto, now).is_none());
    }

    #[test]
    fn validate_governor_rejects_target_exit_fraction_over_one() {
        let now = 1_700_000_000;
        let mut dto = valid_governor_dto(now);
        dto.target_exit_frac = 1.5;
        assert!(validate_governor(dto, now).is_none());
    }

    #[test]
    fn validate_governor_rejects_negative_recency_floor() {
        let now = 1_700_000_000;
        let mut dto = valid_governor_dto(now);
        dto.recency_floor_secs = -1;
        assert!(validate_governor(dto, now).is_none());
    }

    #[test]
    fn validate_governor_rejects_empty_last_stop() {
        let now = 1_700_000_000;
        let mut dto = valid_governor_dto(now);
        dto.last_stop = String::new();
        assert!(validate_governor(dto, now).is_none());
    }

    #[test]
    fn storage_reports_dashcam_bitmap_and_media_statvfs() {
        let probe = FakeProbe {
            stat: Some(FsStat {
                free_bytes: 400,
                total_bytes: 1_000,
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
        let stats = FakeStatsClient {
            outcome: Some(VolumeStatsOutcome::Stats(VolumeStats {
                cluster_count: 10,
                bytes_per_cluster: 512,
                total_bytes: 5_120,
                used_bytes: 2_048,
                free_bytes: 3_072,
                used_clusters: 4,
                free_clusters: 6,
                stable: true,
            })),
        };
        let s = storage(&probe, &paths(), &stats, None);
        assert_eq!(s.volumes.len(), 2);
        assert_eq!(s.volumes[0].label, "TESLACAM");
        assert_eq!(s.volumes[0].source, "bitmap");
        assert_eq!(s.volumes[0].free_bytes, Some(3_072));
        assert!(s.volumes[0].stable);
        assert_eq!(s.volumes[1].label, "MEDIA");
        assert_eq!(s.volumes[1].source, "statvfs");
        assert_eq!(s.volumes[1].used_bytes, Some(600));
        assert!(s.volumes[1].stable);
    }

    #[test]
    fn storage_leaves_volume_bytes_unknown_when_sources_unavailable() {
        let probe = FakeProbe::default();
        let stats = FakeStatsClient {
            outcome: Some(VolumeStatsOutcome::Unavailable),
        };
        let s = storage(&probe, &paths(), &stats, None);
        assert_eq!(s.volumes.len(), 2);
        assert!(s.volumes.iter().all(|v| v.total_bytes.is_none()));
        assert!(!s.volumes[0].stable);
        assert!(!s.volumes[1].stable);
    }

    #[test]
    fn storage_passes_through_quarantined_summary() {
        let probe = FakeProbe::default();
        let stats = FakeStatsClient {
            outcome: Some(VolumeStatsOutcome::Unavailable),
        };
        let s = storage(
            &probe,
            &paths(),
            &stats,
            Some(QuarantinedDto {
                count: 3,
                bytes: 4096,
            }),
        );
        let q = s.quarantined.expect("quarantined present");
        assert_eq!(q.count, 3);
        assert_eq!(q.bytes, 4096);
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
