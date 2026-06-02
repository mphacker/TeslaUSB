//! TOML config file loader for the `teslafat` daemon.
//!
//! Schema is enforced at parse time via `#[serde(deny_unknown_fields)]`
//! and re-validated after parse by an internal `validate` helper for
//! the semantic constraints `serde` alone cannot express (FAT32
//! label length, volume-size range, power-of-two cluster size).
//! Both paths surface their failure as `anyhow::Error` so the
//! binary boundary can attach the config path with `with_context`.
//!
//! The `setup.sh` installer (Phase 6.4) writes
//! `/etc/teslausb/teslafat.toml` against this schema. Adding,
//! renaming, or removing a field is a schema break; bump the doc
//! header here and update `setup.sh` in the same change set.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use serde::Deserialize;

const VOLUME_SIZE_GB_MIN: u32 = 4;
const VOLUME_SIZE_GB_MAX: u32 = 2048;

/// Filesystem flavour to synthesize on the NBD export.
///
/// Defaults to [`FsType::Fat32`] (Tesla's classic compatibility
/// target). [`FsType::Exfat`] is supported for very large
/// volumes where the FAT32 32 KiB cluster size becomes wasteful;
/// firmware support for exFAT on the dashcam volume varies by
/// vehicle model and Tesla firmware version, so the default is
/// conservative.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum FsType {
    /// Synthesize a FAT32 volume (default).
    Fat32,
    /// Synthesize an `exFAT` volume.
    Exfat,
}

impl Default for FsType {
    fn default() -> Self {
        Self::Fat32
    }
}
const CLUSTER_SIZE_MIN: u32 = 512;
const CLUSTER_SIZE_MAX: u32 = 131_072;
const VOLUME_LABEL_MAX: usize = 11;
const DEFAULT_LABEL: &str = "TESLACAM";
const DEFAULT_HIDE_AFTER_S: u64 = 3600;
const DEFAULT_SOCKET_PATH: &str = "/run/teslausb/teslafat.sock";
const DEFAULT_HANDSHAKE_TIMEOUT_S: u64 = 30;
const HANDSHAKE_TIMEOUT_MAX_S: u64 = 600;
/// Default MBR disk signature (offset 440) when the config omits one.
/// Any non-zero 32-bit value works; this is an arbitrary fixed
/// constant so a freshly-installed disk has a stable signature.
const DEFAULT_DISK_SIGNATURE: u32 = 0x5445_5355; // "TESU" little-endian-ish
/// Maximum number of primary MBR partitions (the classic limit).
const PARTITION_MAX: usize = 4;

/// Per-partition volume config: everything `teslafat` needs to
/// synthesise one FAT/`exFAT` filesystem view over a backing tree.
///
/// One [`DiskConfig`] owns an ordered list of these (one per MBR
/// partition); [`crate::backend::SynthBackend::open`] consumes a
/// single `Config` to build the view for that partition. Disk-level
/// concerns (the NBD listen socket, the MBR disk signature) live on
/// [`DiskConfig`], not here — a partition does not own a socket.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// Root of the real Linux directory tree that backs the
    /// synthesised FAT/exFAT volume. Tesla writes land here as
    /// native files; the indexer / cloud-sync subsystems read from
    /// here directly without going through the NBD path.
    pub backing_root: PathBuf,

    /// Total size, in GiB, of the synthesised volume reported to
    /// Tesla. Must be in `[VOLUME_SIZE_GB_MIN, VOLUME_SIZE_GB_MAX]`.
    /// Larger gives Tesla more headroom for sentry events but does
    /// not extend `RecentClips` retention (time-based).
    pub volume_size_gb: u32,

    /// Volume label shown in Tesla's UI / file managers. FAT32
    /// limits this to 11 ASCII characters; longer is rejected.
    #[serde(default = "default_label")]
    pub volume_label: String,

    /// Cluster size override, in bytes. `None` auto-computes to
    /// match `mkfs.vfat` defaults. When set, must be a power of two
    /// in `[CLUSTER_SIZE_MIN, CLUSTER_SIZE_MAX]`.
    #[serde(default)]
    pub cluster_size: Option<u32>,

    /// Filesystem flavour to synthesize on the NBD export.
    /// Defaults to [`FsType::Fat32`] (Tesla's compatibility
    /// target). See [`FsType`] for the available options.
    #[serde(default)]
    pub fs_type: FsType,

    /// Retention policy applied to the synthesised view (Phase 4).
    #[serde(default)]
    pub retention: RetentionConfig,

    /// Directory under which the write path stores pending data
    /// chunks that arrived before their owning file's directory
    /// entry. Each cluster's chunks live in one append-only file
    /// at `<spill_dir>/<cluster:08x>.bin`. The directory is
    /// truncated on daemon start (in-memory index naming the
    /// files is gone after a crash, so the stale files are
    /// unreachable).
    ///
    /// `None` falls back to the legacy in-memory spill (16 MiB
    /// cap), which is unsuitable for production — Tesla writes
    /// pre-dir-entry bursts of multiple GB per clip that cannot
    /// fit in any in-memory cap on a 464 MB Pi. See ADR-0021.
    ///
    /// Default `None` for backwards compatibility with existing
    /// test configs; setup.sh writes an explicit path in
    /// production.
    #[serde(default)]
    pub spill_dir: Option<PathBuf>,

    /// Whether this partition's synth view is rebuilt and live-swapped
    /// when the daemon receives `SIGHUP` (the chime/`LightShow`
    /// activation path; see `scripts/tesla_gadget_rebind.sh`).
    ///
    /// Defaults to `true`. Set `false` for the continuously-written
    /// `TeslaCam` partition: live-swapping the layout of a volume the
    /// car is actively recording into is out of scope for chime
    /// activation, and re-walking its large clip tree on every chime
    /// change would needlessly delay the media swap whose
    /// `RELOAD_LIVE_MARKER` the rebind script waits on. Keeping
    /// exactly one reloadable partition (the media volume) preserves
    /// the single-marker contract from the legacy two-LUN design.
    #[serde(default = "default_reload_on_sighup")]
    pub reload_on_sighup: bool,
}

/// NBD daemon listen-socket configuration.
///
/// The daemon binds one Unix socket and accepts NBD newstyle
/// connections from a kernel `nbd-client` (which in turn backs the
/// `g_mass_storage` USB gadget). The path is per-instance because
/// the systemd unit is templated (`teslafat@0.service` for LUN 0,
/// `teslafat@1.service` for LUN 1) and each instance ships its own
/// config file.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NbdConfig {
    /// Filesystem path to bind the listen socket at. Defaults to
    /// `DEFAULT_SOCKET_PATH` (`/run/teslausb/teslafat.sock`). The
    /// parent directory must exist and be writable by the daemon
    /// user — `setup.sh` (Phase 6.4) creates `/run/teslausb` via a
    /// systemd-tmpfiles entry.
    #[serde(default = "default_socket_path")]
    pub socket_path: PathBuf,

    /// Maximum seconds a client may spend in the newstyle handshake
    /// before the daemon drops the connection. Defaults to
    /// `DEFAULT_HANDSHAKE_TIMEOUT_S` (30 s). Capped at
    /// `HANDSHAKE_TIMEOUT_MAX_S` (10 min) to prevent a config typo
    /// from disabling the protection. The daemon's transmission
    /// loop has no per-request timeout — that's the kernel
    /// nbd-client's responsibility (`/sys/block/nbdN/queue/io_timeout`).
    #[serde(default = "default_handshake_timeout_s")]
    pub handshake_timeout_seconds: u64,
}

impl Default for NbdConfig {
    fn default() -> Self {
        Self {
            socket_path: PathBuf::from(DEFAULT_SOCKET_PATH),
            handshake_timeout_seconds: DEFAULT_HANDSHAKE_TIMEOUT_S,
        }
    }
}

impl NbdConfig {
    /// Convert the handshake-timeout seconds value into a
    /// `std::time::Duration` ready for `tokio::time::timeout`.
    #[must_use]
    pub fn handshake_timeout(&self) -> Duration {
        Duration::from_secs(self.handshake_timeout_seconds)
    }
}

/// Hide-from-view policy. The synthesiser omits aged `RecentClips`
/// entries from the directory listing without touching the backing
/// files; cleanup (real deletion) is a separate web-UI policy.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RetentionConfig {
    /// Hide `RecentClips` files older than this many seconds from
    /// Tesla's directory view. Defaults to `DEFAULT_HIDE_AFTER_S`
    /// (one hour) which matches Tesla's own rotation window.
    /// A value of `0` is interpreted by [`crate::backend::SynthBackend::open`]
    /// as "retention disabled" — no file is ever hidden by the
    /// mtime filter. This matches operator intuition (`0 = off`)
    /// rather than the [`crate::retention::Policy`] internal
    /// semantics where `Duration::ZERO` is the strictest possible
    /// threshold.
    #[serde(default = "default_hide_after_s")]
    pub recentclips_hide_after_seconds: u64,
}

impl Default for RetentionConfig {
    fn default() -> Self {
        Self {
            recentclips_hide_after_seconds: DEFAULT_HIDE_AFTER_S,
        }
    }
}

fn default_label() -> String {
    DEFAULT_LABEL.to_string()
}

const fn default_hide_after_s() -> u64 {
    DEFAULT_HIDE_AFTER_S
}

fn default_socket_path() -> PathBuf {
    PathBuf::from(DEFAULT_SOCKET_PATH)
}

const fn default_handshake_timeout_s() -> u64 {
    DEFAULT_HANDSHAKE_TIMEOUT_S
}

const fn default_reload_on_sighup() -> bool {
    true
}

impl Config {
    /// Load and validate a single-partition TOML fragment from
    /// `path`. Disk-level configs use [`DiskConfig::load`]; this
    /// remains the per-volume loader used by tests and tooling that
    /// validate one partition's settings in isolation.
    ///
    /// # Errors
    ///
    /// Returns `Err` if the file cannot be read, the bytes are not
    /// valid UTF-8 TOML, an unknown field is present, or post-parse
    /// validation rejects a value (out-of-range volume size,
    /// non-power-of-two cluster size, oversize volume label).
    pub fn load(path: &Path) -> Result<Self> {
        let raw =
            std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        let cfg: Self =
            toml::from_str(&raw).with_context(|| format!("parsing {}", path.display()))?;
        cfg.validate()?;
        Ok(cfg)
    }

    fn validate(&self) -> Result<()> {
        ensure!(
            (VOLUME_SIZE_GB_MIN..=VOLUME_SIZE_GB_MAX).contains(&self.volume_size_gb),
            "volume_size_gb must be in [{VOLUME_SIZE_GB_MIN}, {VOLUME_SIZE_GB_MAX}] (got {})",
            self.volume_size_gb,
        );
        if let Some(c) = self.cluster_size {
            ensure!(
                c.is_power_of_two() && (CLUSTER_SIZE_MIN..=CLUSTER_SIZE_MAX).contains(&c),
                "cluster_size must be a power of two in [{CLUSTER_SIZE_MIN}, {CLUSTER_SIZE_MAX}] (got {c})",
            );
        }
        ensure!(
            self.volume_label.len() <= VOLUME_LABEL_MAX,
            "volume_label must be <= {VOLUME_LABEL_MAX} chars (FAT32 limit): {:?}",
            self.volume_label,
        );
        Ok(())
    }
}

/// Top-level disk config: one USB mass-storage LUN backed by a
/// synthesised MBR disk that exposes an ordered set of partitions.
///
/// This is the format `setup.sh` writes to
/// `/etc/teslausb/teslafat.toml` (ADR-0023). The daemon binds one
/// NBD socket ([`Self::nbd`]) for the whole disk, synthesises an MBR
/// at LBA 0 stamped with [`Self::disk_signature`], and routes the
/// LBA ranges of each entry in [`Self::partition`] to its own
/// [`Config`]-driven view.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DiskConfig {
    /// NBD listen-socket configuration for the single disk LUN.
    #[serde(default)]
    pub nbd: NbdConfig,

    /// MBR disk signature written at byte offset 440 of sector 0.
    /// Defaults to [`DEFAULT_DISK_SIGNATURE`]. Must be non-zero so
    /// the host treats the disk as initialised.
    #[serde(default = "default_disk_signature")]
    pub disk_signature: u32,

    /// Ordered partition list (1..=4). Entry 0 is the first MBR
    /// primary partition (the `TeslaCam` dashcam volume), entry 1 the
    /// second (the media volume carrying `LockChime.wav`,
    /// `LightShow/`, `Boombox/`), and so on.
    pub partition: Vec<Config>,
}

impl DiskConfig {
    /// Load and validate a disk-level TOML config file from `path`.
    ///
    /// # Errors
    ///
    /// Returns `Err` if the file cannot be read, is not valid TOML,
    /// contains an unknown field, declares zero or more than
    /// [`PARTITION_MAX`] partitions, has an out-of-range NBD
    /// handshake timeout or empty socket path, or any partition
    /// fails its own [`Config`] validation.
    pub fn load(path: &Path) -> Result<Self> {
        let raw =
            std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        let cfg: Self =
            toml::from_str(&raw).with_context(|| format!("parsing {}", path.display()))?;
        cfg.validate()?;
        Ok(cfg)
    }

    fn validate(&self) -> Result<()> {
        ensure!(
            !self.partition.is_empty(),
            "at least one [[partition]] is required",
        );
        ensure!(
            self.partition.len() <= PARTITION_MAX,
            "at most {PARTITION_MAX} partitions are supported (got {})",
            self.partition.len(),
        );
        ensure!(self.disk_signature != 0, "disk_signature must be non-zero",);
        ensure!(
            (1..=HANDSHAKE_TIMEOUT_MAX_S).contains(&self.nbd.handshake_timeout_seconds),
            "nbd.handshake_timeout_seconds must be in [1, {HANDSHAKE_TIMEOUT_MAX_S}] (got {})",
            self.nbd.handshake_timeout_seconds,
        );
        ensure!(
            !self.nbd.socket_path.as_os_str().is_empty(),
            "nbd.socket_path must not be empty",
        );
        for (i, part) in self.partition.iter().enumerate() {
            part.validate().with_context(|| {
                format!(
                    "partition[{i}] (backing_root {})",
                    part.backing_root.display()
                )
            })?;
        }
        Ok(())
    }
}

const fn default_disk_signature() -> u32 {
    DEFAULT_DISK_SIGNATURE
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINIMAL_TOML: &str = "\
backing_root = \"/var/teslacam\"
volume_size_gb = 64
";

    fn sample_config() -> Config {
        Config {
            backing_root: PathBuf::from("/var/teslacam"),
            volume_size_gb: 64,
            volume_label: DEFAULT_LABEL.to_string(),
            cluster_size: None,
            fs_type: FsType::default(),
            retention: RetentionConfig::default(),
            spill_dir: None,
            reload_on_sighup: true,
        }
    }

    fn sample_disk_config() -> DiskConfig {
        DiskConfig {
            nbd: NbdConfig::default(),
            disk_signature: DEFAULT_DISK_SIGNATURE,
            partition: vec![sample_config()],
        }
    }

    #[test]
    fn parses_minimal_toml_with_defaults() {
        let cfg: Config = toml::from_str(MINIMAL_TOML).unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg.backing_root, PathBuf::from("/var/teslacam"));
        assert_eq!(cfg.volume_size_gb, 64);
        assert_eq!(cfg.volume_label, DEFAULT_LABEL);
        assert_eq!(cfg.cluster_size, None);
        assert_eq!(cfg.fs_type, FsType::Fat32);
        assert_eq!(
            cfg.retention.recentclips_hide_after_seconds,
            DEFAULT_HIDE_AFTER_S
        );
        // Absent `reload_on_sighup` defaults to true so an existing
        // single-partition config keeps live-reloading on SIGHUP.
        assert!(cfg.reload_on_sighup);
    }

    #[test]
    fn reload_on_sighup_parses_explicit_false() {
        let raw = "\
backing_root = \"/srv/cam\"
volume_size_gb = 256
reload_on_sighup = false
";
        let cfg: Config = toml::from_str(raw).unwrap();
        cfg.validate().unwrap();
        assert!(!cfg.reload_on_sighup);
    }

    #[test]
    fn parses_full_toml() {
        let raw = "\
backing_root = \"/srv/cam\"
volume_size_gb = 256
volume_label = \"DASHCAM\"
cluster_size = 32768
fs_type = \"exfat\"

[retention]
recentclips_hide_after_seconds = 7200
";
        let cfg: Config = toml::from_str(raw).unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg.backing_root, PathBuf::from("/srv/cam"));
        assert_eq!(cfg.volume_size_gb, 256);
        assert_eq!(cfg.volume_label, "DASHCAM");
        assert_eq!(cfg.cluster_size, Some(32_768));
        assert_eq!(cfg.fs_type, FsType::Exfat);
        assert_eq!(cfg.retention.recentclips_hide_after_seconds, 7200);
    }

    #[test]
    fn parses_fat32_fs_type_explicitly() {
        let raw = "\
backing_root = \"/var/teslacam\"
volume_size_gb = 64
fs_type = \"fat32\"
";
        let cfg: Config = toml::from_str(raw).unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg.fs_type, FsType::Fat32);
    }

    #[test]
    fn rejects_unknown_fs_type() {
        let raw = "\
backing_root = \"/var/teslacam\"
volume_size_gb = 64
fs_type = \"ntfs\"
";
        let err = toml::from_str::<Config>(raw).unwrap_err();
        assert!(err.to_string().contains("fs_type"), "got: {err}");
    }

    #[test]
    fn rejects_unknown_top_level_field() {
        let raw = "\
backing_root = \"/var/teslacam\"
volume_size_gb = 64
not_a_field = \"boom\"
";
        let err = toml::from_str::<Config>(raw).unwrap_err();
        assert!(err.to_string().contains("not_a_field"), "got: {err}");
    }

    #[test]
    fn rejects_unknown_retention_field() {
        let raw = "\
backing_root = \"/var/teslacam\"
volume_size_gb = 64

[retention]
recentclips_hide_after_seconds = 60
bogus_subfield = true
";
        let err = toml::from_str::<Config>(raw).unwrap_err();
        assert!(err.to_string().contains("bogus_subfield"), "got: {err}");
    }

    #[test]
    fn rejects_oversize_label() {
        let mut cfg = sample_config();
        cfg.volume_label = "TOOLONGLABEL_AAA".to_string();
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("volume_label"), "got: {err}");
    }

    #[test]
    fn accepts_eleven_char_label() {
        let mut cfg = sample_config();
        cfg.volume_label = "ABCDEFGHIJK".to_string();
        cfg.validate().unwrap();
    }

    #[test]
    fn rejects_undersize_volume() {
        let mut cfg = sample_config();
        cfg.volume_size_gb = 2;
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("volume_size_gb"), "got: {err}");
    }

    #[test]
    fn rejects_oversize_volume() {
        let mut cfg = sample_config();
        cfg.volume_size_gb = 4096;
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("volume_size_gb"), "got: {err}");
    }

    #[test]
    fn rejects_non_power_of_two_cluster() {
        let mut cfg = sample_config();
        cfg.cluster_size = Some(3000);
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("cluster_size"), "got: {err}");
    }

    #[test]
    fn rejects_oversize_cluster() {
        let mut cfg = sample_config();
        cfg.cluster_size = Some(262_144);
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("cluster_size"), "got: {err}");
    }

    #[test]
    fn rejects_undersize_cluster() {
        let mut cfg = sample_config();
        cfg.cluster_size = Some(256);
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("cluster_size"), "got: {err}");
    }

    #[test]
    fn load_reads_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("teslafat.toml");
        std::fs::write(&path, MINIMAL_TOML).unwrap();
        let cfg = Config::load(&path).unwrap();
        assert_eq!(cfg.volume_size_gb, 64);
    }

    #[test]
    fn load_missing_file_errors_with_path() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nope.toml");
        let err = Config::load(&path).unwrap_err();
        assert!(err.to_string().contains("reading"), "got: {err}");
        let chain: Vec<String> = err.chain().map(ToString::to_string).collect();
        assert!(
            chain.iter().any(|s| s.contains("nope.toml")),
            "path missing from error chain: {chain:?}"
        );
    }

    #[test]
    fn load_invalid_toml_errors_with_path() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("bad.toml");
        std::fs::write(&path, "this = is not [ valid").unwrap();
        let err = Config::load(&path).unwrap_err();
        let chain: Vec<String> = err.chain().map(ToString::to_string).collect();
        assert!(
            chain.iter().any(|s| s.contains("bad.toml")),
            "path missing from error chain: {chain:?}"
        );
    }

    // ---- DiskConfig + NbdConfig -------------------------------------

    const MINIMAL_DISK_TOML: &str = "\
[[partition]]
backing_root = \"/srv/cam\"
volume_size_gb = 256
fs_type = \"exfat\"

[[partition]]
backing_root = \"/srv/media\"
volume_size_gb = 32
fs_type = \"exfat\"
";

    #[test]
    fn parses_two_partition_disk_toml() {
        let cfg: DiskConfig = toml::from_str(MINIMAL_DISK_TOML).unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg.partition.len(), 2);
        assert_eq!(cfg.partition[0].backing_root, PathBuf::from("/srv/cam"));
        assert_eq!(cfg.partition[0].volume_size_gb, 256);
        assert_eq!(cfg.partition[1].backing_root, PathBuf::from("/srv/media"));
        assert_eq!(cfg.partition[1].fs_type, FsType::Exfat);
        // Defaults applied at the disk level.
        assert_eq!(cfg.disk_signature, DEFAULT_DISK_SIGNATURE);
        assert_eq!(cfg.nbd.socket_path, PathBuf::from(DEFAULT_SOCKET_PATH));
    }

    #[test]
    fn parses_disk_toml_with_nbd_and_signature() {
        let raw = "\
disk_signature = 0xDEADBEEF

[nbd]
socket_path = \"/run/teslausb/teslafat.sock\"
handshake_timeout_seconds = 45

[[partition]]
backing_root = \"/srv/cam\"
volume_size_gb = 256
fs_type = \"exfat\"
";
        let cfg: DiskConfig = toml::from_str(raw).unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg.disk_signature, 0xDEAD_BEEF);
        assert_eq!(cfg.nbd.handshake_timeout_seconds, 45);
        assert_eq!(cfg.partition.len(), 1);
    }

    #[test]
    fn rejects_disk_with_no_partitions() {
        let mut cfg = sample_disk_config();
        cfg.partition.clear();
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("partition"), "got: {err}");
    }

    #[test]
    fn toml_requires_a_partition_array() {
        let raw = "disk_signature = 1\n";
        let err = toml::from_str::<DiskConfig>(raw).unwrap_err();
        assert!(err.to_string().contains("partition"), "got: {err}");
    }

    #[test]
    fn rejects_disk_with_too_many_partitions() {
        let mut cfg = sample_disk_config();
        cfg.partition = std::iter::repeat_with(sample_config)
            .take(PARTITION_MAX + 1)
            .collect();
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("partitions"), "got: {err}");
    }

    #[test]
    fn rejects_zero_disk_signature() {
        let mut cfg = sample_disk_config();
        cfg.disk_signature = 0;
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("disk_signature"), "got: {err}");
    }

    #[test]
    fn disk_validate_propagates_partition_error() {
        let mut cfg = sample_disk_config();
        cfg.partition[0].volume_size_gb = 2;
        let err = cfg.validate().unwrap_err();
        let chain: Vec<String> = err.chain().map(ToString::to_string).collect();
        assert!(
            chain.iter().any(|s| s.contains("volume_size_gb")),
            "underlying partition error missing: {chain:?}"
        );
        assert!(
            chain.iter().any(|s| s.contains("partition[0]")),
            "partition index context missing: {chain:?}"
        );
    }

    #[test]
    fn disk_load_reads_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("teslafat.toml");
        std::fs::write(&path, MINIMAL_DISK_TOML).unwrap();
        let cfg = DiskConfig::load(&path).unwrap();
        assert_eq!(cfg.partition.len(), 2);
    }

    #[test]
    fn rejects_zero_handshake_timeout() {
        let mut cfg = sample_disk_config();
        cfg.nbd.handshake_timeout_seconds = 0;
        let err = cfg.validate().unwrap_err();
        assert!(
            err.to_string().contains("handshake_timeout_seconds"),
            "got: {err}"
        );
    }

    #[test]
    fn rejects_oversize_handshake_timeout() {
        let mut cfg = sample_disk_config();
        cfg.nbd.handshake_timeout_seconds = HANDSHAKE_TIMEOUT_MAX_S + 1;
        let err = cfg.validate().unwrap_err();
        assert!(
            err.to_string().contains("handshake_timeout_seconds"),
            "got: {err}"
        );
    }

    #[test]
    fn accepts_min_and_max_handshake_timeout() {
        let mut cfg = sample_disk_config();
        cfg.nbd.handshake_timeout_seconds = 1;
        cfg.validate().unwrap();
        cfg.nbd.handshake_timeout_seconds = HANDSHAKE_TIMEOUT_MAX_S;
        cfg.validate().unwrap();
    }

    #[test]
    fn rejects_empty_socket_path() {
        let mut cfg = sample_disk_config();
        cfg.nbd.socket_path = PathBuf::new();
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("socket_path"), "got: {err}");
    }

    #[test]
    fn rejects_unknown_nbd_field() {
        let raw = "\
[[partition]]
backing_root = \"/var/teslacam\"
volume_size_gb = 64

[nbd]
bogus_subfield = true
";
        let err = toml::from_str::<DiskConfig>(raw).unwrap_err();
        assert!(err.to_string().contains("bogus_subfield"), "got: {err}");
    }

    #[test]
    fn handshake_timeout_returns_seconds_as_duration() {
        let nbd = NbdConfig {
            socket_path: PathBuf::from(DEFAULT_SOCKET_PATH),
            handshake_timeout_seconds: 45,
        };
        assert_eq!(nbd.handshake_timeout(), Duration::from_secs(45));
    }
}
