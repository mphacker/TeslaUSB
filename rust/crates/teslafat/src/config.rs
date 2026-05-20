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

/// Top-level config struct deserialised from the TOML file.
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

    /// NBD listen-socket configuration (Phase 1.6+).
    #[serde(default)]
    pub nbd: NbdConfig,
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

impl Config {
    /// Load and validate a TOML config file from `path`.
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
        ensure!(
            (1..=HANDSHAKE_TIMEOUT_MAX_S).contains(&self.nbd.handshake_timeout_seconds),
            "nbd.handshake_timeout_seconds must be in [1, {HANDSHAKE_TIMEOUT_MAX_S}] (got {})",
            self.nbd.handshake_timeout_seconds,
        );
        ensure!(
            !self.nbd.socket_path.as_os_str().is_empty(),
            "nbd.socket_path must not be empty",
        );
        Ok(())
    }
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
            nbd: NbdConfig::default(),
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
        assert_eq!(cfg.nbd.socket_path, PathBuf::from(DEFAULT_SOCKET_PATH));
        assert_eq!(
            cfg.nbd.handshake_timeout_seconds,
            DEFAULT_HANDSHAKE_TIMEOUT_S
        );
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

[nbd]
socket_path = \"/run/teslausb/teslafat-0.sock\"
handshake_timeout_seconds = 45
";
        let cfg: Config = toml::from_str(raw).unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg.backing_root, PathBuf::from("/srv/cam"));
        assert_eq!(cfg.volume_size_gb, 256);
        assert_eq!(cfg.volume_label, "DASHCAM");
        assert_eq!(cfg.cluster_size, Some(32_768));
        assert_eq!(cfg.fs_type, FsType::Exfat);
        assert_eq!(cfg.retention.recentclips_hide_after_seconds, 7200);
        assert_eq!(
            cfg.nbd.socket_path,
            PathBuf::from("/run/teslausb/teslafat-0.sock")
        );
        assert_eq!(cfg.nbd.handshake_timeout_seconds, 45);
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

    // ---- NbdConfig --------------------------------------------------

    #[test]
    fn rejects_zero_handshake_timeout() {
        let mut cfg = sample_config();
        cfg.nbd.handshake_timeout_seconds = 0;
        let err = cfg.validate().unwrap_err();
        assert!(
            err.to_string().contains("handshake_timeout_seconds"),
            "got: {err}"
        );
    }

    #[test]
    fn rejects_oversize_handshake_timeout() {
        let mut cfg = sample_config();
        cfg.nbd.handshake_timeout_seconds = HANDSHAKE_TIMEOUT_MAX_S + 1;
        let err = cfg.validate().unwrap_err();
        assert!(
            err.to_string().contains("handshake_timeout_seconds"),
            "got: {err}"
        );
    }

    #[test]
    fn accepts_min_and_max_handshake_timeout() {
        let mut cfg = sample_config();
        cfg.nbd.handshake_timeout_seconds = 1;
        cfg.validate().unwrap();
        cfg.nbd.handshake_timeout_seconds = HANDSHAKE_TIMEOUT_MAX_S;
        cfg.validate().unwrap();
    }

    #[test]
    fn rejects_empty_socket_path() {
        let mut cfg = sample_config();
        cfg.nbd.socket_path = PathBuf::new();
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("socket_path"), "got: {err}");
    }

    #[test]
    fn rejects_unknown_nbd_field() {
        let raw = "\
backing_root = \"/var/teslacam\"
volume_size_gb = 64

[nbd]
bogus_subfield = true
";
        let err = toml::from_str::<Config>(raw).unwrap_err();
        assert!(err.to_string().contains("bogus_subfield"), "got: {err}");
    }

    #[test]
    fn handshake_timeout_returns_seconds_as_duration() {
        let mut cfg = sample_config();
        cfg.nbd.handshake_timeout_seconds = 45;
        assert_eq!(cfg.nbd.handshake_timeout(), Duration::from_secs(45));
    }
}
