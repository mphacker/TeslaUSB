//! Mapping-overrides reader — mtime-cached, zero-cost on idle ticks.
//!
//! The user-editable mapping settings (`trip_gap_minutes`,
//! `speed_limit_mph`) live in a tiny JSON file written by the
//! web app at [`crate::config::Config::mapping_overrides_path`].
//! The materializer reads them on every rebuild — but a
//! rebuild can fire every supervisor tick (5 min) for the
//! lifetime of the daemon. So the load path is built around
//! three properties:
//!
//! * **One `metadata()` syscall per `load()`** — no read, no
//!   JSON parse when the file hasn't changed. `std::fs::metadata`
//!   is sub-microsecond on the Pi Zero 2 W.
//! * **In-memory snapshot is ~16 bytes** (a `Copy` struct);
//!   the speed-limit threshold is pre-converted to m/s at load
//!   time so the per-waypoint loop in `derive_events` never
//!   multiplies.
//! * **Missing file ⇒ defaults**, same as the Python service.
//!
//! Mirrors `web/teslausb_web/services/mapping_settings_service.py`
//! so the two implementations stay in lock-step without an IPC
//! round-trip.
//!
//! Speed-limit semantics: `speed_limit_mph = 0` ⇒
//! [`MappingOverrides::speed_limit_enabled`] is `false` and
//! `derive_events` skips the speed-limit branch entirely.

use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::SystemTime;

use serde::Deserialize;
use tracing::warn;

/// Schema version of the on-disk JSON. Must match the Python
/// side (`mapping_settings_service._SCHEMA_VERSION`).
pub const SCHEMA_VERSION: u32 = 1;

/// Default trip-grouping gap (minutes). Matches v1 / Python.
pub const DEFAULT_TRIP_GAP_MINUTES: i64 = 5;

/// Default speed-limit threshold (mph). `0` ⇒ disabled.
pub const DEFAULT_SPEED_LIMIT_MPH: i64 = 0;

/// Conversion factor (mph → m/s).
pub const MPH_TO_MPS: f64 = 0.44704;

/// Materialized snapshot of the on-disk overrides.
///
/// `Copy` because it's two scalars — cheaper than a refcount
/// for the hot path.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MappingOverrides {
    /// Trip-grouping gap, in seconds.
    pub trip_gap_seconds: i64,
    /// Speed-limit threshold in m/s. `0.0` ⇒ no speed-limit
    /// events emitted, anywhere.
    pub speed_limit_mps: f64,
}

impl MappingOverrides {
    /// Builtin defaults (matches a missing JSON file).
    #[must_use]
    pub const fn defaults() -> Self {
        Self {
            trip_gap_seconds: DEFAULT_TRIP_GAP_MINUTES * 60,
            // Cannot multiply `i64 * f64` in `const`. Inline.
            speed_limit_mps: 0.0,
        }
    }

    /// `true` when speed-limit events should be emitted.
    #[must_use]
    pub fn speed_limit_enabled(&self) -> bool {
        self.speed_limit_mps > 0.0
    }
}

impl Default for MappingOverrides {
    fn default() -> Self {
        Self::defaults()
    }
}

#[derive(Debug, Deserialize)]
struct RawOverrides {
    #[serde(default = "default_schema_version")]
    schema_version: u32,
    #[serde(default = "default_trip_gap_minutes")]
    trip_gap_minutes: i64,
    #[serde(default = "default_speed_limit_mph")]
    speed_limit_mph: i64,
}

const fn default_schema_version() -> u32 {
    SCHEMA_VERSION
}

const fn default_trip_gap_minutes() -> i64 {
    DEFAULT_TRIP_GAP_MINUTES
}

const fn default_speed_limit_mph() -> i64 {
    DEFAULT_SPEED_LIMIT_MPH
}

/// Cached-snapshot reader. Use one per worker process — clones
/// share the same backing cache.
#[derive(Debug)]
pub struct MappingOverridesReader {
    path: PathBuf,
    // `(mtime, snapshot)`. `None` ⇒ never loaded; `Some(None, ..)`
    // would conflict with "file is known missing", so we encode
    // "file missing" as `Some((UNIX_EPOCH, defaults))` — the
    // syscall returns `Err(NotFound)` in that case, and a
    // subsequent file create will have a strictly-later mtime,
    // so the cache invalidates correctly.
    cache: Mutex<Option<(SystemTime, MappingOverrides)>>,
}

impl MappingOverridesReader {
    /// Build a new reader for `path`. Does NOT read the file —
    /// the first `load()` does that.
    #[must_use]
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            cache: Mutex::new(None),
        }
    }

    /// Path the reader watches. Useful for logging.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Return the live snapshot. At most one `metadata()` call
    /// per invocation; the file is read + parsed only when the
    /// mtime changed since the previous successful load.
    pub fn load(&self) -> MappingOverrides {
        let current_mtime = self.current_mtime();
        let mut guard = self.cache.lock().expect("mapping-overrides cache poisoned");
        if let Some((cached_mtime, snapshot)) = *guard {
            if cached_mtime == current_mtime {
                return snapshot;
            }
        }
        let snapshot = self.read_and_parse(current_mtime);
        *guard = Some((current_mtime, snapshot));
        snapshot
    }

    /// Has the on-disk file been touched since the last
    /// successful `load()`? Cheap — one `metadata()` call, no
    /// parse. Used by the supervisor to decide whether to
    /// mark the trips table dirty.
    pub fn mtime_changed_since_last_load(&self) -> bool {
        let current_mtime = self.current_mtime();
        let guard = self.cache.lock().expect("mapping-overrides cache poisoned");
        match *guard {
            None => true,
            Some((cached_mtime, _)) => cached_mtime != current_mtime,
        }
    }

    fn current_mtime(&self) -> SystemTime {
        match std::fs::metadata(&self.path) {
            Ok(meta) => meta.modified().unwrap_or(SystemTime::UNIX_EPOCH),
            Err(_) => SystemTime::UNIX_EPOCH,
        }
    }

    fn read_and_parse(&self, mtime: SystemTime) -> MappingOverrides {
        if mtime == SystemTime::UNIX_EPOCH {
            // File missing or unreadable — defaults.
            return MappingOverrides::defaults();
        }
        let raw = match std::fs::read_to_string(&self.path) {
            Ok(s) => s,
            Err(e) => {
                warn!(
                    error = %e,
                    path = %self.path.display(),
                    "mapping-overrides read failed; using defaults",
                );
                return MappingOverrides::defaults();
            }
        };
        let parsed: RawOverrides = match serde_json::from_str(&raw) {
            Ok(v) => v,
            Err(e) => {
                warn!(
                    error = %e,
                    path = %self.path.display(),
                    "mapping-overrides JSON parse failed; using defaults",
                );
                return MappingOverrides::defaults();
            }
        };
        if parsed.schema_version != SCHEMA_VERSION {
            warn!(
                schema_version = parsed.schema_version,
                expected = SCHEMA_VERSION,
                path = %self.path.display(),
                "mapping-overrides schema version unsupported; using defaults",
            );
            return MappingOverrides::defaults();
        }
        let trip_gap_minutes = parsed.trip_gap_minutes.clamp(1, 60);
        let speed_limit_mph = parsed.speed_limit_mph.clamp(0, 200);
        #[allow(clippy::cast_precision_loss)]
        MappingOverrides {
            trip_gap_seconds: trip_gap_minutes * 60,
            speed_limit_mps: speed_limit_mph as f64 * MPH_TO_MPS,
        }
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, SystemTime};

    use tempfile::tempdir;

    use super::*;

    #[test]
    fn missing_file_returns_defaults() {
        let dir = tempdir().unwrap();
        let reader = MappingOverridesReader::new(dir.path().join("nope.json"));
        let snap = reader.load();
        assert_eq!(snap, MappingOverrides::defaults());
        assert!(!snap.speed_limit_enabled());
        assert_eq!(snap.trip_gap_seconds, 300);
    }

    #[test]
    fn parses_valid_file() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("ov.json");
        std::fs::write(
            &path,
            br#"{"schema_version":1,"trip_gap_minutes":7,"speed_limit_mph":65}"#,
        )
        .unwrap();
        let reader = MappingOverridesReader::new(path);
        let snap = reader.load();
        assert_eq!(snap.trip_gap_seconds, 7 * 60);
        assert!(snap.speed_limit_enabled());
        assert!((snap.speed_limit_mps - 65.0 * MPH_TO_MPS).abs() < 1e-9);
    }

    #[test]
    fn cache_returns_same_snapshot_until_mtime_changes() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("ov.json");
        std::fs::write(
            &path,
            br#"{"schema_version":1,"trip_gap_minutes":5,"speed_limit_mph":0}"#,
        )
        .unwrap();
        let reader = MappingOverridesReader::new(path.clone());
        let s1 = reader.load();
        let s2 = reader.load();
        assert_eq!(s1, s2);
        assert!(!reader.mtime_changed_since_last_load());

        // Bump mtime + rewrite.
        let new_mtime = SystemTime::now() + Duration::from_secs(60);
        std::fs::write(
            &path,
            br#"{"schema_version":1,"trip_gap_minutes":9,"speed_limit_mph":75}"#,
        )
        .unwrap();
        filetime::set_file_mtime(&path, filetime::FileTime::from_system_time(new_mtime))
            .unwrap();

        assert!(reader.mtime_changed_since_last_load());
        let s3 = reader.load();
        assert_eq!(s3.trip_gap_seconds, 9 * 60);
        assert!(s3.speed_limit_enabled());
    }

    #[test]
    fn malformed_json_falls_back_to_defaults() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("ov.json");
        std::fs::write(&path, b"{not json").unwrap();
        let reader = MappingOverridesReader::new(path);
        assert_eq!(reader.load(), MappingOverrides::defaults());
    }

    #[test]
    fn schema_version_mismatch_falls_back_to_defaults() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("ov.json");
        std::fs::write(
            &path,
            br#"{"schema_version":99,"trip_gap_minutes":7,"speed_limit_mph":65}"#,
        )
        .unwrap();
        let reader = MappingOverridesReader::new(path);
        assert_eq!(reader.load(), MappingOverrides::defaults());
    }
}
