//! TOML config loader for the `teslausb-worker` daemon.
//!
//! Mirrors the schema discipline of `teslafat::config`:
//! `#[serde(deny_unknown_fields)]` everywhere, semantic
//! validation in a separate `validate()` pass, and `anyhow`
//! at the binary boundary so failures surface with the file
//! path attached.
//!
//! Phase 4b.5 will install this at `/etc/teslausb/teslausb-worker.toml`.

// File-level: "SQLite", "TOML", "inotify", "CLOSE_WRITE" are
// domain terms; backticking each one in doc comments adds
// noise without value. Matches the SEI files' carve-out.
#![allow(clippy::doc_markdown)]

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use serde::Deserialize;

/// Default polling interval for the cleanup worker (5 min).
const DEFAULT_CLEANUP_INTERVAL_S: u64 = 300;
/// Default retention window for `RecentClips` (no-GPS clips):
/// 24 hours.
const DEFAULT_RETENTION_DAYS: u32 = 1;
/// Cap on retention days. Two years is well past anything the
/// SD card can hold; we use it as a sanity gate against typos
/// like `retention_days = 365000`.
const RETENTION_DAYS_MAX: u32 = 730;
/// Default minimum free-space percent below which the cleanup
/// worker treats any eligible no-GPS clip as deletable
/// regardless of `retention_days`. Matches v1's `min_free_pct`.
const DEFAULT_MIN_FREE_PCT: u8 = 10;
/// Default sample rate for the SEI walker (decode every Nth
/// frame). 30 ≈ 1 waypoint / second on Tesla footage, plenty
/// for route mapping.
const DEFAULT_SEI_SAMPLE_RATE: u32 = 30;
/// Default debounce window for inotify CLOSE_WRITE events: a
/// Tesla clip may emit several writes before the final close
/// and we want to index once, not five times.
const DEFAULT_INDEX_DEBOUNCE_MS: u64 = 1500;

/// Top-level worker config.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// Backing root of the synthesised volume. Same value as
    /// `teslafat`'s `backing_root` — the worker reads through
    /// the real backing tree without going through the NBD path.
    pub backing_root: PathBuf,

    /// Path to the SQLite database file the indexer writes and
    /// cleanup / web read. Defaults to
    /// `/var/lib/teslausb/index.sqlite3`. Parent directory must
    /// exist and be writable by the worker user.
    #[serde(default = "default_db_path")]
    pub db_path: PathBuf,

    /// Indexer configuration.
    #[serde(default)]
    pub indexer: IndexerConfig,

    /// Cleanup-worker configuration.
    #[serde(default)]
    pub cleanup: CleanupConfig,
}

/// Indexer subsystem config.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct IndexerConfig {
    /// How aggressively to sample SEI frames. `1` decodes every
    /// SEI; `30` decodes ~1 / second on Tesla footage. Higher
    /// values shrink the DB and the per-clip indexing time.
    /// `0` is treated as `1` by the SEI walker.
    #[serde(default = "default_sei_sample_rate")]
    pub sei_sample_rate: u32,

    /// Debounce window in milliseconds between successive
    /// inotify CLOSE_WRITE events on the same clip. Tesla writes
    /// the file then often closes/reopens for a final fsync;
    /// without debouncing we'd index twice.
    #[serde(default = "default_index_debounce_ms")]
    pub debounce_ms: u64,
}

impl Default for IndexerConfig {
    fn default() -> Self {
        Self {
            sei_sample_rate: DEFAULT_SEI_SAMPLE_RATE,
            debounce_ms: DEFAULT_INDEX_DEBOUNCE_MS,
        }
    }
}

impl IndexerConfig {
    /// Debounce window as a [`Duration`].
    #[must_use]
    pub fn debounce(&self) -> Duration {
        Duration::from_millis(self.debounce_ms)
    }
}

/// Cleanup-worker config.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CleanupConfig {
    /// Seconds between successive cleanup sweeps. Defaults to
    /// 5 minutes — matches v1 cadence.
    #[serde(default = "default_cleanup_interval_s")]
    pub interval_seconds: u64,

    /// Days a no-GPS `RecentClips` clip is preserved before it
    /// becomes eligible for deletion. Capped at two years
    /// (`RETENTION_DAYS_MAX`) to defend against typos like
    /// `retention_days = 365000`.
    #[serde(default = "default_retention_days")]
    pub retention_days: u32,

    /// Free-space floor (percent of `backing_root` volume).
    /// When free space drops below this, the cleanup worker
    /// treats any eligible no-GPS clip as deletable regardless
    /// of `retention_days`. Range `[0, 100]`. `0` disables the
    /// emergency floor (relying purely on `retention_days`).
    #[serde(default = "default_min_free_pct")]
    pub min_free_pct: u8,

    /// If true, a `RecentClips` clip with ≥ 1 GPS-fix waypoint
    /// is never deleted — only no-GPS clips age out. This is
    /// the v1 default and the binding operator preference
    /// (route data is precious; idle / parked clips are noise).
    #[serde(default = "default_true")]
    pub preserve_with_gps: bool,
}

impl Default for CleanupConfig {
    fn default() -> Self {
        Self {
            interval_seconds: DEFAULT_CLEANUP_INTERVAL_S,
            retention_days: DEFAULT_RETENTION_DAYS,
            min_free_pct: DEFAULT_MIN_FREE_PCT,
            preserve_with_gps: true,
        }
    }
}

impl CleanupConfig {
    /// Sweep interval as a [`Duration`].
    #[must_use]
    pub fn interval(&self) -> Duration {
        Duration::from_secs(self.interval_seconds)
    }

    /// Retention window as a [`Duration`].
    #[must_use]
    pub fn retention(&self) -> Duration {
        Duration::from_secs(u64::from(self.retention_days) * 24 * 60 * 60)
    }
}

fn default_db_path() -> PathBuf {
    PathBuf::from("/var/lib/teslausb/index.sqlite3")
}

const fn default_sei_sample_rate() -> u32 {
    DEFAULT_SEI_SAMPLE_RATE
}

const fn default_index_debounce_ms() -> u64 {
    DEFAULT_INDEX_DEBOUNCE_MS
}

const fn default_cleanup_interval_s() -> u64 {
    DEFAULT_CLEANUP_INTERVAL_S
}

const fn default_retention_days() -> u32 {
    DEFAULT_RETENTION_DAYS
}

const fn default_min_free_pct() -> u8 {
    DEFAULT_MIN_FREE_PCT
}

const fn default_true() -> bool {
    true
}

impl Config {
    /// Load and validate a TOML config file from `path`.
    ///
    /// # Errors
    ///
    /// Returns `Err` if the file cannot be read, the bytes are
    /// not valid UTF-8 TOML, an unknown field is present, or
    /// semantic validation rejects a value.
    pub fn load(path: &Path) -> Result<Self> {
        let raw =
            std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        let cfg: Self =
            toml::from_str(&raw).with_context(|| format!("parsing {}", path.display()))?;
        cfg.validate()?;
        Ok(cfg)
    }

    /// Absolute path to the on-disk directory backing
    /// `bucket`. Matches the standard Tesla layout
    /// (`<backing_root>/TeslaCam/<BucketDir>/`). The three
    /// bucket dirs are always direct children of `TeslaCam/`;
    /// the worker never invents alternate locations.
    #[must_use]
    pub fn bucket_root(&self, bucket: crate::store::Bucket) -> PathBuf {
        self.backing_root
            .join("TeslaCam")
            .join(bucket.tesla_dir_name())
    }

    fn validate(&self) -> Result<()> {
        ensure!(
            !self.backing_root.as_os_str().is_empty(),
            "backing_root must not be empty",
        );
        ensure!(
            !self.db_path.as_os_str().is_empty(),
            "db_path must not be empty",
        );
        ensure!(
            self.cleanup.retention_days <= RETENTION_DAYS_MAX,
            "cleanup.retention_days must be <= {RETENTION_DAYS_MAX} (got {})",
            self.cleanup.retention_days,
        );
        ensure!(
            self.cleanup.min_free_pct <= 100,
            "cleanup.min_free_pct must be in [0, 100] (got {})",
            self.cleanup.min_free_pct,
        );
        ensure!(
            self.cleanup.interval_seconds > 0,
            "cleanup.interval_seconds must be > 0",
        );
        ensure!(
            self.indexer.debounce_ms > 0,
            "indexer.debounce_ms must be > 0",
        );
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINIMAL_TOML: &str = "\
backing_root = \"/srv/teslausb\"
";

    #[test]
    fn parses_minimal_with_defaults() {
        let cfg: Config = toml::from_str(MINIMAL_TOML).unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg.backing_root, PathBuf::from("/srv/teslausb"));
        assert_eq!(cfg.db_path, default_db_path());
        assert_eq!(cfg.indexer.sei_sample_rate, DEFAULT_SEI_SAMPLE_RATE);
        assert_eq!(cfg.indexer.debounce_ms, DEFAULT_INDEX_DEBOUNCE_MS);
        assert_eq!(cfg.cleanup.interval_seconds, DEFAULT_CLEANUP_INTERVAL_S);
        assert_eq!(cfg.cleanup.retention_days, DEFAULT_RETENTION_DAYS);
        assert_eq!(cfg.cleanup.min_free_pct, DEFAULT_MIN_FREE_PCT);
        assert!(cfg.cleanup.preserve_with_gps);
    }

    #[test]
    fn parses_full_toml() {
        let raw = "\
backing_root = \"/srv/teslausb\"
db_path = \"/var/lib/teslausb/i.db\"

[indexer]
sei_sample_rate = 1
debounce_ms = 500

[cleanup]
interval_seconds = 60
retention_days = 14
min_free_pct = 25
preserve_with_gps = false
";
        let cfg: Config = toml::from_str(raw).unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg.db_path, PathBuf::from("/var/lib/teslausb/i.db"));
        assert_eq!(cfg.indexer.sei_sample_rate, 1);
        assert_eq!(cfg.indexer.debounce_ms, 500);
        assert_eq!(cfg.cleanup.interval_seconds, 60);
        assert_eq!(cfg.cleanup.retention_days, 14);
        assert_eq!(cfg.cleanup.min_free_pct, 25);
        assert!(!cfg.cleanup.preserve_with_gps);
    }

    #[test]
    fn rejects_unknown_top_level_field() {
        let raw = "\
backing_root = \"/srv/teslausb\"
bogus = 1
";
        let err = toml::from_str::<Config>(raw).unwrap_err();
        assert!(err.to_string().contains("bogus"), "got: {err}");
    }

    #[test]
    fn rejects_unknown_indexer_field() {
        let raw = "\
backing_root = \"/srv/teslausb\"

[indexer]
bogus = 1
";
        let err = toml::from_str::<Config>(raw).unwrap_err();
        assert!(err.to_string().contains("bogus"), "got: {err}");
    }

    #[test]
    fn rejects_unknown_cleanup_field() {
        let raw = "\
backing_root = \"/srv/teslausb\"

[cleanup]
bogus = 1
";
        let err = toml::from_str::<Config>(raw).unwrap_err();
        assert!(err.to_string().contains("bogus"), "got: {err}");
    }

    fn sample() -> Config {
        toml::from_str(MINIMAL_TOML).unwrap()
    }

    #[test]
    fn rejects_empty_backing_root() {
        let mut cfg = sample();
        cfg.backing_root = PathBuf::new();
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("backing_root"), "got: {err}");
    }

    #[test]
    fn rejects_empty_db_path() {
        let mut cfg = sample();
        cfg.db_path = PathBuf::new();
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("db_path"), "got: {err}");
    }

    #[test]
    fn rejects_oversize_retention_days() {
        let mut cfg = sample();
        cfg.cleanup.retention_days = RETENTION_DAYS_MAX + 1;
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("retention_days"), "got: {err}");
    }

    #[test]
    fn rejects_min_free_pct_over_100() {
        let mut cfg = sample();
        cfg.cleanup.min_free_pct = 101;
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("min_free_pct"), "got: {err}");
    }

    #[test]
    fn accepts_min_free_pct_zero_to_disable_floor() {
        let mut cfg = sample();
        cfg.cleanup.min_free_pct = 0;
        cfg.validate().unwrap();
    }

    #[test]
    fn rejects_zero_cleanup_interval() {
        let mut cfg = sample();
        cfg.cleanup.interval_seconds = 0;
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("interval_seconds"), "got: {err}");
    }

    #[test]
    fn rejects_zero_debounce_ms() {
        let mut cfg = sample();
        cfg.indexer.debounce_ms = 0;
        let err = cfg.validate().unwrap_err();
        assert!(err.to_string().contains("debounce_ms"), "got: {err}");
    }

    #[test]
    fn cleanup_interval_returns_duration() {
        let mut cfg = sample();
        cfg.cleanup.interval_seconds = 60;
        assert_eq!(cfg.cleanup.interval(), Duration::from_secs(60));
    }

    #[test]
    fn cleanup_retention_returns_duration_in_days() {
        let mut cfg = sample();
        cfg.cleanup.retention_days = 3;
        assert_eq!(
            cfg.cleanup.retention(),
            Duration::from_secs(3 * 24 * 60 * 60)
        );
    }

    #[test]
    fn indexer_debounce_returns_duration() {
        let mut cfg = sample();
        cfg.indexer.debounce_ms = 250;
        assert_eq!(cfg.indexer.debounce(), Duration::from_millis(250));
    }

    #[test]
    fn load_reads_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("worker.toml");
        std::fs::write(&path, MINIMAL_TOML).unwrap();
        let cfg = Config::load(&path).unwrap();
        assert_eq!(cfg.backing_root, PathBuf::from("/srv/teslausb"));
    }

    #[test]
    fn load_missing_file_errors_with_path() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nope.toml");
        let err = Config::load(&path).unwrap_err();
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
