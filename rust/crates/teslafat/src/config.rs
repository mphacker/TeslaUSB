//! TOML config file loader for the `teslafat` daemon.
//!
//! Schema is enforced at parse time via `#[serde(deny_unknown_fields)]`
//! and re-validated after parse via [`Config::validate`] for the
//! semantic constraints `serde` alone cannot express (FAT32 label
//! length, volume-size range, power-of-two cluster size). Both paths
//! surface their failure as `anyhow::Error` so the binary boundary
//! can attach the config path with `with_context`.
//!
//! The `setup.sh` installer (Phase 6.4) writes
//! `/etc/teslausb/teslafat.toml` against this schema. Adding,
//! renaming, or removing a field is a schema break; bump the doc
//! header here and update `setup.sh` in the same change set.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, ensure};
use serde::Deserialize;

const VOLUME_SIZE_GB_MIN: u32 = 4;
const VOLUME_SIZE_GB_MAX: u32 = 2048;
const CLUSTER_SIZE_MIN: u32 = 512;
const CLUSTER_SIZE_MAX: u32 = 131_072;
const VOLUME_LABEL_MAX: usize = 11;
const DEFAULT_LABEL: &str = "TESLACAM";
const DEFAULT_HIDE_AFTER_S: u64 = 3600;

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

    /// Retention policy applied to the synthesised view (Phase 4).
    #[serde(default)]
    pub retention: RetentionConfig,
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
            retention: RetentionConfig::default(),
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
        assert_eq!(
            cfg.retention.recentclips_hide_after_seconds,
            DEFAULT_HIDE_AFTER_S
        );
    }

    #[test]
    fn parses_full_toml() {
        let raw = "\
backing_root = \"/srv/cam\"
volume_size_gb = 256
volume_label = \"DASHCAM\"
cluster_size = 32768

[retention]
recentclips_hide_after_seconds = 7200
";
        let cfg: Config = toml::from_str(raw).unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg.backing_root, PathBuf::from("/srv/cam"));
        assert_eq!(cfg.volume_size_gb, 256);
        assert_eq!(cfg.volume_label, "DASHCAM");
        assert_eq!(cfg.cluster_size, Some(32_768));
        assert_eq!(cfg.retention.recentclips_hide_after_seconds, 7200);
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
}
