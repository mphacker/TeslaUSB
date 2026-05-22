//! Loader for the unified `/etc/teslausb/teslausb.toml` config
//! introduced in AC.1.
//!
//! Mirrors the Python schema in
//! `web/teslausb_web/services/storage_config.py`. Owned here in
//! the Rust worker because the cleanup loop (AC.5) consumes the
//! `[cleanup]` section on every sweep and the resize helper
//! (AC.3) consumes the `[storage]` section on every apply.
//!
//! Why a second config file (not a section in `worker.toml`)?
//! The same bytes must be read by BOTH the worker AND the Flask
//! UI, and the UI writes back through `storage_config.py`'s
//! `save()`. Sharing one file keeps the two consumers' views in
//! sync without an IPC round-trip on every page load.
//!
//! Backwards-compat: callers fall back to
//! [`StorageConfig::default()`] when the file is absent. The
//! legacy `/etc/teslausb/teslafat-{0,1}.toml` files remain the
//! source of truth for `teslafat` (different schema, different
//! consumer). The AC.3 resize helper regenerates teslafat-*.toml
//! from teslausb.toml so they never diverge in practice.

// "TeslaCam", "exFAT", "FAT32", "SEI", "GPS", "TOML" are domain
// terms; backticking every mention adds noise without value.
// Matches the SEI / cleanup carve-out.
#![allow(clippy::doc_markdown)]

use std::path::Path;

use anyhow::{Context, Result, ensure};
use serde::Deserialize;

/// Lower bound for `os_reserve_gb` — anything below this risks
/// starving the rootfs of free space during routine operation.
/// Matches `OS_RESERVE_MIN_GB` in `storage_config.py`.
pub const OS_RESERVE_MIN_GB: u32 = 8;
/// Default for `os_reserve_gb`. Matches `OS_RESERVE_DEFAULT_GB`
/// in `storage_config.py`.
pub const OS_RESERVE_DEFAULT_GB: u32 = 20;
/// Lower bound for any per-LUN size, in GB.
pub const LUN_MIN_GB: u32 = 4;
/// Upper bound for any per-LUN size, in GB. Matches the
/// teslafat backend's accepted range.
pub const LUN_MAX_GB: u32 = 2048;
/// Upper bound for `target_free_pct`. `0` is the auto-tune
/// sentinel (cleanup loop computes 2× bytes-per-recording-minute
/// from the indexer median).
pub const TARGET_FREE_PCT_MAX: u8 = 50;
/// Upper bound for `sentry_max_age_days`. `0` = unlimited
/// (Sentry only auto-deleted as a last resort).
pub const SENTRY_MAX_AGE_DAYS_MAX: u32 = 3650;

const fn default_os_reserve_gb() -> u32 {
    OS_RESERVE_DEFAULT_GB
}

const fn default_teslacam_gb() -> u32 {
    64
}

const fn default_media_gb() -> u32 {
    32
}

const fn default_zero_u8() -> u8 {
    0
}

const fn default_zero_u32() -> u32 {
    0
}

const fn default_true() -> bool {
    true
}

/// LUN sizing + OS-reserve guard. Mirrors the `[storage]`
/// section of teslausb.toml.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StorageSection {
    /// Hard reserve for OS + journals + worker scratch.
    #[serde(default = "default_os_reserve_gb")]
    pub os_reserve_gb: u32,
    /// Size LUN 0 reports to Tesla (TeslaCam, exFAT).
    #[serde(default = "default_teslacam_gb")]
    pub teslacam_gb: u32,
    /// Size LUN 1 reports to Tesla (media, FAT32).
    #[serde(default = "default_media_gb")]
    pub media_gb: u32,
}

impl Default for StorageSection {
    fn default() -> Self {
        Self {
            os_reserve_gb: OS_RESERVE_DEFAULT_GB,
            teslacam_gb: default_teslacam_gb(),
            media_gb: default_media_gb(),
        }
    }
}

/// Auto-cleanup knobs consumed by `cleanup.rs` (AC.5). Mirrors
/// the `[cleanup]` section of teslausb.toml.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CleanupSection {
    /// Percent of TeslaCam capacity to keep free. `0` means
    /// auto-tune: 2× the median 6-camera-1-minute recording
    /// size, expressed as a percent of LUN capacity.
    #[serde(default = "default_zero_u8")]
    pub target_free_pct: u8,
    /// Sentry events older than this become eligible for
    /// auto-deletion via Tier C. `0` = unlimited (Sentry only
    /// deleted when Tier A+B exhausted and free still below
    /// target).
    #[serde(default = "default_zero_u32")]
    pub sentry_max_age_days: u32,
    /// When true, `RecentClips` with GPS or SEI tesla-data are
    /// classified Tier B (preserved over plain clips).
    #[serde(default = "default_true")]
    pub preserve_with_gps: bool,
}

impl Default for CleanupSection {
    fn default() -> Self {
        Self {
            target_free_pct: 0,
            sentry_max_age_days: 0,
            preserve_with_gps: true,
        }
    }
}

/// Full snapshot of `/etc/teslausb/teslausb.toml`.
#[derive(Debug, Clone, Default, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StorageConfig {
    /// LUN sizing section.
    #[serde(default)]
    pub storage: StorageSection,
    /// Cleanup section.
    #[serde(default)]
    pub cleanup: CleanupSection,
}

impl StorageConfig {
    /// Load and validate a config from `path`. Returns
    /// [`StorageConfig::default()`] if the file is absent
    /// (back-compat: a freshly-installed device without AC.1's
    /// setup step should still boot).
    ///
    /// # Errors
    ///
    /// Returns `Err` if the file exists but cannot be read,
    /// the bytes are not valid TOML, an unknown field is
    /// present, or semantic validation rejects a value.
    pub fn load(path: &Path) -> Result<Self> {
        if !path.exists() {
            return Ok(Self::default());
        }
        let raw =
            std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        let cfg: Self =
            toml::from_str(&raw).with_context(|| format!("parsing {}", path.display()))?;
        cfg.validate()?;
        Ok(cfg)
    }

    /// Semantic validation. Bounds-checks every field but does
    /// NOT enforce the cross-field `teslacam + media <=
    /// sd_total - os_reserve` constraint — that requires the
    /// SD-card capacity and is the resize helper's job.
    ///
    /// # Errors
    ///
    /// Returns `Err` with a `storage.<field>` or `cleanup.<field>`
    /// prefix identifying the offending value.
    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.storage.os_reserve_gb >= OS_RESERVE_MIN_GB,
            "storage.os_reserve_gb must be >= {OS_RESERVE_MIN_GB} (got {})",
            self.storage.os_reserve_gb,
        );
        check_lun_size("storage.teslacam_gb", self.storage.teslacam_gb)?;
        check_lun_size("storage.media_gb", self.storage.media_gb)?;
        ensure!(
            self.cleanup.target_free_pct <= TARGET_FREE_PCT_MAX,
            "cleanup.target_free_pct must be in [0, {TARGET_FREE_PCT_MAX}] (got {})",
            self.cleanup.target_free_pct,
        );
        ensure!(
            self.cleanup.sentry_max_age_days <= SENTRY_MAX_AGE_DAYS_MAX,
            "cleanup.sentry_max_age_days must be in [0, {SENTRY_MAX_AGE_DAYS_MAX}] (got {})",
            self.cleanup.sentry_max_age_days,
        );
        Ok(())
    }
}

fn check_lun_size(name: &str, value: u32) -> Result<()> {
    ensure!(
        (LUN_MIN_GB..=LUN_MAX_GB).contains(&value),
        "{name} must be in [{LUN_MIN_GB}, {LUN_MAX_GB}] (got {value})",
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_round_trip() {
        let cfg: StorageConfig = toml::from_str("").unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg, StorageConfig::default());
    }

    #[test]
    fn parses_full_file() {
        let raw = "\
[storage]
os_reserve_gb = 32
teslacam_gb = 128
media_gb = 64

[cleanup]
target_free_pct = 12
sentry_max_age_days = 90
preserve_with_gps = false
";
        let cfg: StorageConfig = toml::from_str(raw).unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg.storage.os_reserve_gb, 32);
        assert_eq!(cfg.storage.teslacam_gb, 128);
        assert_eq!(cfg.storage.media_gb, 64);
        assert_eq!(cfg.cleanup.target_free_pct, 12);
        assert_eq!(cfg.cleanup.sentry_max_age_days, 90);
        assert!(!cfg.cleanup.preserve_with_gps);
    }

    #[test]
    fn load_returns_defaults_when_file_absent() {
        let tmp = tempfile::tempdir().unwrap();
        let cfg = StorageConfig::load(&tmp.path().join("missing.toml")).unwrap();
        assert_eq!(cfg, StorageConfig::default());
    }

    #[test]
    fn load_parses_file_when_present() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("teslausb.toml");
        std::fs::write(
            &path,
            "[storage]\nteslacam_gb = 100\n[cleanup]\nsentry_max_age_days = 60\n",
        )
        .unwrap();
        let cfg = StorageConfig::load(&path).unwrap();
        assert_eq!(cfg.storage.teslacam_gb, 100);
        assert_eq!(cfg.cleanup.sentry_max_age_days, 60);
    }

    #[test]
    fn rejects_low_os_reserve() {
        let raw = "[storage]\nos_reserve_gb = 4\n";
        let cfg: StorageConfig = toml::from_str(raw).unwrap();
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("os_reserve_gb"), "got: {err}",);
    }

    #[test]
    fn rejects_lun_below_min() {
        let raw = "[storage]\nteslacam_gb = 1\n";
        let cfg: StorageConfig = toml::from_str(raw).unwrap();
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("teslacam_gb"), "got: {err}");
    }

    #[test]
    fn rejects_lun_above_max() {
        let raw = "[storage]\nmedia_gb = 9999\n";
        let cfg: StorageConfig = toml::from_str(raw).unwrap();
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("media_gb"), "got: {err}");
    }

    #[test]
    fn rejects_target_free_pct_above_max() {
        let raw = "[cleanup]\ntarget_free_pct = 80\n";
        let cfg: StorageConfig = toml::from_str(raw).unwrap();
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("target_free_pct"), "got: {err}");
    }

    #[test]
    fn rejects_unknown_storage_field() {
        let raw = "[storage]\nbogus = 1\n";
        let err = toml::from_str::<StorageConfig>(raw).unwrap_err();
        assert!(err.to_string().contains("bogus"), "got: {err}");
    }

    #[test]
    fn rejects_unknown_cleanup_field() {
        let raw = "[cleanup]\nbogus = 1\n";
        let err = toml::from_str::<StorageConfig>(raw).unwrap_err();
        assert!(err.to_string().contains("bogus"), "got: {err}");
    }
}
