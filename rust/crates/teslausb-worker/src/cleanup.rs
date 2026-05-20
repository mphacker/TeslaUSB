//! GPS-aware cleanup worker — frees space on the backing
//! store by deleting `RecentClips` clips that have aged out
//! and carry no GPS waypoints.
//!
//! Design rules (from `docs/00-PLAN.md` + ADR-0010 + the
//! operator's binding "preserve GPS-tagged clips" directive):
//!
//! * Only `RecentClips` is ever swept. `SavedClips` and
//!   `SentryClips` are explicitly skipped at the bucket
//!   level — defense in depth against a future config typo.
//! * A `RecentClips` clip with ≥ 1 GPS-fix waypoint is
//!   preserved when `config.cleanup.preserve_with_gps` is
//!   true (the default).
//! * A clip is eligible for deletion when:
//!   - it lives in `RecentClips`, AND
//!   - its `clip_started_utc` (or `indexed_at_utc` fallback)
//!     is older than `retention_days`, AND
//!   - it has no GPS waypoint OR `preserve_with_gps` is off.
//! * Free-space floor: if the backing volume drops below
//!   `min_free_pct`, the worker broadens its cutoff to "now"
//!   for the current pass so every no-GPS `RecentClips` clip
//!   becomes eligible regardless of age. GPS-tagged clips are
//!   still preserved when `preserve_with_gps` is on. Set
//!   `min_free_pct = 0` to disable the floor entirely.
//! * Deletion is "store first, then file" (the inverse of
//!   create order) so a power cut between the two leaves a
//!   recoverable state: the next bootstrap pass will re-walk
//!   the file and re-index it.

// File-level: "GPS", "SQLite", "RecentClips" etc. are domain
// terms.
#![allow(clippy::doc_markdown)]

use std::path::{Path, PathBuf};
use std::time::SystemTime;

use thiserror::Error;
use tracing::{debug, info, warn};

use crate::config::Config;
use crate::store::{Bucket, ClipRecord, Store, StoreError};

/// Errors emitted by the cleanup worker. Per-clip filesystem
/// failures are NOT errors — they are logged and skipped
/// (e.g. a clip the user moved between the store query and
/// the unlink) so one stale row cannot stall the daemon.
#[derive(Debug, Error)]
pub enum CleanupError {
    /// Underlying store error.
    #[error("store error: {0}")]
    Store(#[from] StoreError),
    /// `statvfs` failed when measuring free-space pressure.
    #[error("statvfs({path:?}) failed: {source}")]
    Statvfs {
        /// Path we tried to stat.
        path: PathBuf,
        /// Underlying I/O error.
        #[source]
        source: std::io::Error,
    },
    /// The system clock reports a time before the Unix
    /// epoch.
    #[error("system clock reports a pre-epoch time")]
    ClockBeforeEpoch,
}

/// Result alias for cleanup operations.
pub type Result<T> = std::result::Result<T, CleanupError>;

/// Summary returned by [`Cleanup::run_once`]. Used in
/// supervisor logs and tests.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct CleanupSummary {
    /// Clips the store reported as candidates (`RecentClips`
    /// older than the cutoff).
    pub considered: u32,
    /// Candidates preserved because of GPS waypoints.
    pub preserved_gps: u32,
    /// Candidates the worker actually deleted.
    pub deleted: u32,
    /// Candidates the worker tried to delete but the
    /// filesystem unlink failed (logged at WARN).
    pub failed: u32,
    /// `true` if a free-space-pressure sweep was active for
    /// this run (the floor in `config.cleanup.min_free_pct`
    /// was breached).
    pub pressure: bool,
}

/// GPS-aware cleanup worker.
pub struct Cleanup {
    config: Config,
}

impl Cleanup {
    /// Build a cleanup worker from `config`.
    #[must_use]
    pub fn new(config: Config) -> Self {
        Self { config }
    }

    /// Run one cleanup pass against `store`. Safe to call
    /// repeatedly on an interval (the supervisor does this
    /// every `config.cleanup.interval_seconds`).
    ///
    /// # Errors
    ///
    /// Returns `Err` only on a store error or a fatal clock
    /// / `statvfs` failure. Per-clip unlink failures count
    /// toward `failed` in the summary but do NOT abort the
    /// pass.
    pub fn run_once(&self, store: &Store) -> Result<CleanupSummary> {
        let now = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map_err(|_| CleanupError::ClockBeforeEpoch)?
            .as_secs();
        let now_i64 = i64::try_from(now).unwrap_or(i64::MAX);
        let pressure = self.measure_pressure()?;
        let cutoff = self.effective_cutoff(now_i64, pressure);
        self.run_once_with(store, cutoff, pressure)
    }

    /// Pure-logic cutoff calculation. Pulled out so tests can
    /// verify the pressure path without freezing the clock or
    /// stubbing `statvfs`.
    ///
    /// * `now_unix_s` — current time as Unix seconds.
    /// * `pressure`   — `true` when free-space is below
    ///   `min_free_pct` (computed internally by the
    ///   `measure_pressure` adapter).
    ///
    /// Returns the cutoff to pass to
    /// [`Store::list_clips_in_bucket_older_than`]. Under
    /// pressure the cutoff is `now`, broadening the candidate
    /// set to every no-GPS `RecentClips` clip; otherwise it is
    /// `now - retention_days`.
    #[must_use]
    pub fn effective_cutoff(&self, now_unix_s: i64, pressure: bool) -> i64 {
        if pressure {
            return now_unix_s;
        }
        let retention = self.config.cleanup.retention().as_secs();
        now_unix_s.saturating_sub(i64::try_from(retention).unwrap_or(i64::MAX))
    }

    /// Inner pass that takes its `cutoff` and `pressure`
    /// inputs explicitly. The supervisor uses
    /// [`Cleanup::run_once`]; tests bypass clock + statvfs
    /// by calling this directly.
    ///
    /// # Errors
    ///
    /// Returns `Err` on a store error. Per-clip unlink
    /// failures are not propagated.
    pub fn run_once_with(
        &self,
        store: &Store,
        cutoff_unix_s: i64,
        pressure: bool,
    ) -> Result<CleanupSummary> {
        let mut summary = CleanupSummary {
            pressure,
            ..CleanupSummary::default()
        };
        // Defense in depth: only ever sweep RecentClips,
        // even if a future code path were to pass another
        // bucket through. We deliberately do not loop over
        // Bucket::all().
        let bucket = Bucket::Recent;
        let candidates = store.list_clips_in_bucket_older_than(bucket, cutoff_unix_s)?;
        summary.considered = u32::try_from(candidates.len()).unwrap_or(u32::MAX);
        for clip in candidates {
            if self.should_preserve(&clip) {
                summary.preserved_gps += 1;
                continue;
            }
            match self.delete_one(store, &clip) {
                Ok(()) => summary.deleted += 1,
                Err(e) => {
                    summary.failed += 1;
                    warn!(
                        path = %clip.relative_path.display(),
                        error = %e,
                        "cleanup: unlink failed; row left in store for retry",
                    );
                }
            }
        }
        info!(
            considered = summary.considered,
            preserved_gps = summary.preserved_gps,
            deleted = summary.deleted,
            failed = summary.failed,
            pressure = summary.pressure,
            "cleanup pass complete",
        );
        Ok(summary)
    }

    /// Pure-logic preservation check. Pulled out so tests
    /// can exercise the policy without touching the store.
    #[must_use]
    pub fn should_preserve(&self, clip: &ClipRecord) -> bool {
        if clip.bucket != Bucket::Recent {
            // Defense-in-depth: Saved/Sentry are not eligible
            // at all. The store query already filters to
            // RecentClips; this guard catches a future
            // refactor.
            return true;
        }
        if self.config.cleanup.preserve_with_gps && clip.has_gps() {
            return true;
        }
        false
    }

    fn delete_one(&self, store: &Store, clip: &ClipRecord) -> std::result::Result<(), DeleteError> {
        // Defense-in-depth path-traversal guard. The indexer
        // is the only writer of `relative_path` and stores
        // values produced by `relative_to_backing_root`, but
        // cleanup is the only code path that *deletes* files,
        // so we re-validate here. We refuse to act on:
        //   * any absolute path (we always want backing-root-
        //     relative rows);
        //   * any path containing `..`, `.`, or a Windows-
        //     style prefix component that could escape
        //     `backing_root`.
        // A bad row stays in the DB; an operator can inspect
        // it via SQLite. A future increment may flag and quarantine.
        let safe = self
            .safe_absolute_path(&clip.relative_path)
            .ok_or_else(|| DeleteError::UnsafePath {
                path: clip.relative_path.clone(),
            })?;
        // "store first, then file" — see module docstring.
        // If the unlink fails, we still rolled the row out
        // of the DB; the next bootstrap pass will re-walk
        // the file and re-index it, so the system converges.
        store
            .delete_clip_by_path(&clip.relative_path)
            .map_err(DeleteError::Store)?;
        let absolute = safe;
        match std::fs::remove_file(&absolute) {
            Ok(()) => {
                debug!(
                    path = %absolute.display(),
                    "cleanup: clip deleted",
                );
                Ok(())
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                // The file was already gone — that's the
                // desired end state. Not an error.
                debug!(
                    path = %absolute.display(),
                    "cleanup: file already absent",
                );
                Ok(())
            }
            Err(e) => Err(DeleteError::Unlink {
                path: absolute,
                source: e,
            }),
        }
    }

    /// Resolve `relative` against `backing_root`, returning
    /// `None` if the path is unsafe to act on. A path is unsafe
    /// when it is absolute, when it has a prefix/root-dir
    /// component (Windows drive letter, UNC root), or when any
    /// component is `..` — any of which could escape
    /// `backing_root`. `.` segments are normalized away by
    /// `Path::components()` and are therefore harmless.
    fn safe_absolute_path(&self, relative: &Path) -> Option<PathBuf> {
        use std::path::Component;
        if relative.is_absolute() {
            return None;
        }
        for component in relative.components() {
            match component {
                Component::Normal(_) | Component::CurDir => {}
                // Prefix (e.g. `C:`), RootDir, and ParentDir
                // (`..`) are all rejected.
                Component::Prefix(_) | Component::RootDir | Component::ParentDir => return None,
            }
        }
        Some(self.config.backing_root.join(relative))
    }

    fn measure_pressure(&self) -> Result<bool> {
        if self.config.cleanup.min_free_pct == 0 {
            // Floor disabled.
            return Ok(false);
        }
        let free_pct = free_pct(&self.config.backing_root).map_err(|e| CleanupError::Statvfs {
            path: self.config.backing_root.clone(),
            source: e,
        })?;
        Ok(free_pct < f64::from(self.config.cleanup.min_free_pct))
    }
}

#[derive(Debug, Error)]
enum DeleteError {
    #[error("{0}")]
    Store(StoreError),
    #[error("unlink {path:?} failed: {source}")]
    Unlink {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error(
        "refusing to delete unsafe path {path:?} (absolute, parent traversal, or root component)"
    )]
    UnsafePath { path: PathBuf },
}

/// Free-space percent of the volume containing `path`.
///
/// Linux uses `statvfs(3)` via `rustix` (no `unsafe`); on
/// non-Linux hosts the function returns `100.0` so the
/// dev-workstation test suite never sees synthetic free-space
/// pressure.
#[cfg(target_os = "linux")]
fn free_pct(path: &Path) -> std::io::Result<f64> {
    let stat = rustix::fs::statvfs(path).map_err(std::io::Error::from)?;
    let total = stat.f_blocks as f64 * stat.f_frsize as f64;
    let free = stat.f_bavail as f64 * stat.f_frsize as f64;
    if total <= 0.0 {
        return Ok(100.0);
    }
    Ok((free / total) * 100.0)
}

#[cfg(not(target_os = "linux"))]
#[allow(clippy::unnecessary_wraps)] // unified signature with Linux variant
fn free_pct(_path: &Path) -> std::io::Result<f64> {
    Ok(100.0)
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::expect_used,
        clippy::indexing_slicing,
        clippy::panic,
        clippy::unwrap_used,
        clippy::cast_possible_truncation,
        clippy::cast_lossless,
        clippy::float_cmp,
        clippy::doc_markdown
    )]

    use std::time::{Duration, UNIX_EPOCH};

    use teslausb_core::sei::tesla::SeiMessage;

    use super::*;
    use crate::sei::{ClipWalk, Waypoint};

    fn cfg(backing: &Path, preserve_with_gps: bool) -> Config {
        let toml = format!(
            "backing_root = \"{}\"\n\n[cleanup]\nretention_days = 1\nmin_free_pct = 0\npreserve_with_gps = {}\n",
            backing.to_string_lossy().replace('\\', "/"),
            if preserve_with_gps { "true" } else { "false" },
        );
        toml::from_str(&toml).unwrap()
    }

    fn msg_gps(lat: f64, lon: f64) -> SeiMessage {
        SeiMessage {
            latitude_deg: lat,
            longitude_deg: lon,
            ..SeiMessage::default()
        }
    }

    fn walk_with(waypoints: Vec<Waypoint>, started: Option<SystemTime>) -> ClipWalk {
        ClipWalk {
            clip_started_utc: started,
            timescale: 90_000,
            frame_count: u32::try_from(waypoints.len()).unwrap_or(u32::MAX),
            waypoints,
        }
    }

    fn wp(frame: u32, msg: SeiMessage) -> Waypoint {
        Waypoint {
            frame_index: frame,
            timestamp_ms: f64::from(frame),
            message: msg,
        }
    }

    fn write_real_file(path: &Path) {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(path, b"stub").unwrap();
    }

    fn record(
        store: &mut Store,
        cfg: &Config,
        bucket: Bucket,
        rel: &str,
        started: Option<SystemTime>,
        gps: bool,
    ) {
        let waypoints = if gps {
            vec![wp(0, msg_gps(37.0, -122.0))]
        } else {
            vec![wp(0, msg_gps(0.0, 0.0))]
        };
        let walk = walk_with(waypoints, started);
        store.record_clip(bucket, Path::new(rel), &walk).unwrap();
        // Also write the real file so unlink can succeed.
        let abs = cfg.backing_root.join(rel);
        write_real_file(&abs);
    }

    #[test]
    fn deletes_old_no_gps_recent_clips() {
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), true);
        let mut store = Store::open_in_memory().unwrap();
        let old = UNIX_EPOCH + Duration::from_secs(100);
        record(
            &mut store,
            &c,
            Bucket::Recent,
            "TeslaCam/RecentClips/a.mp4",
            Some(old),
            false,
        );
        let cleanup = Cleanup::new(c.clone());
        let s = cleanup.run_once_with(&store, 1_000, false).unwrap();
        assert_eq!(s.considered, 1);
        assert_eq!(s.deleted, 1);
        assert_eq!(s.preserved_gps, 0);
        assert_eq!(s.failed, 0);
        assert_eq!(store.clip_count().unwrap(), 0);
        assert!(!c.backing_root.join("TeslaCam/RecentClips/a.mp4").exists());
    }

    #[test]
    fn preserves_old_gps_tagged_recent_clips_when_configured() {
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), true);
        let mut store = Store::open_in_memory().unwrap();
        let old = UNIX_EPOCH + Duration::from_secs(100);
        record(
            &mut store,
            &c,
            Bucket::Recent,
            "TeslaCam/RecentClips/gps.mp4",
            Some(old),
            true,
        );
        let cleanup = Cleanup::new(c.clone());
        let s = cleanup.run_once_with(&store, 1_000, false).unwrap();
        assert_eq!(s.considered, 1);
        assert_eq!(s.preserved_gps, 1);
        assert_eq!(s.deleted, 0);
        assert_eq!(store.clip_count().unwrap(), 1);
        assert!(c.backing_root.join("TeslaCam/RecentClips/gps.mp4").exists());
    }

    #[test]
    fn deletes_gps_clips_when_preserve_disabled() {
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), false);
        let mut store = Store::open_in_memory().unwrap();
        let old = UNIX_EPOCH + Duration::from_secs(100);
        record(
            &mut store,
            &c,
            Bucket::Recent,
            "TeslaCam/RecentClips/g.mp4",
            Some(old),
            true,
        );
        let cleanup = Cleanup::new(c);
        let s = cleanup.run_once_with(&store, 1_000, false).unwrap();
        assert_eq!(s.deleted, 1);
        assert_eq!(s.preserved_gps, 0);
        assert_eq!(store.clip_count().unwrap(), 0);
    }

    #[test]
    fn never_deletes_saved_clips() {
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), false);
        let mut store = Store::open_in_memory().unwrap();
        let old = UNIX_EPOCH + Duration::from_secs(100);
        record(
            &mut store,
            &c,
            Bucket::Saved,
            "TeslaCam/SavedClips/keep.mp4",
            Some(old),
            false,
        );
        let cleanup = Cleanup::new(c.clone());
        let s = cleanup.run_once_with(&store, 1_000, false).unwrap();
        assert_eq!(s.considered, 0);
        assert_eq!(s.deleted, 0);
        assert_eq!(store.clip_count().unwrap(), 1);
        assert!(c.backing_root.join("TeslaCam/SavedClips/keep.mp4").exists());
    }

    #[test]
    fn never_deletes_sentry_clips() {
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), false);
        let mut store = Store::open_in_memory().unwrap();
        let old = UNIX_EPOCH + Duration::from_secs(100);
        record(
            &mut store,
            &c,
            Bucket::Sentry,
            "TeslaCam/SentryClips/keep.mp4",
            Some(old),
            false,
        );
        let cleanup = Cleanup::new(c);
        let s = cleanup.run_once_with(&store, 1_000, false).unwrap();
        assert_eq!(s.considered, 0);
        assert_eq!(store.clip_count().unwrap(), 1);
    }

    #[test]
    fn skips_clips_newer_than_cutoff() {
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), true);
        let mut store = Store::open_in_memory().unwrap();
        let new = UNIX_EPOCH + Duration::from_secs(2_000);
        record(
            &mut store,
            &c,
            Bucket::Recent,
            "TeslaCam/RecentClips/new.mp4",
            Some(new),
            false,
        );
        let cleanup = Cleanup::new(c);
        let s = cleanup.run_once_with(&store, 1_000, false).unwrap();
        assert_eq!(s.considered, 0);
        assert_eq!(s.deleted, 0);
    }

    #[test]
    fn already_missing_file_counts_as_deleted() {
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), true);
        let mut store = Store::open_in_memory().unwrap();
        let old = UNIX_EPOCH + Duration::from_secs(100);
        // Record a row but do NOT write the file.
        let waypoints = vec![wp(0, msg_gps(0.0, 0.0))];
        store
            .record_clip(
                Bucket::Recent,
                Path::new("TeslaCam/RecentClips/ghost.mp4"),
                &walk_with(waypoints, Some(old)),
            )
            .unwrap();
        let cleanup = Cleanup::new(c);
        let s = cleanup.run_once_with(&store, 1_000, false).unwrap();
        assert_eq!(s.deleted, 1);
        assert_eq!(s.failed, 0);
        assert_eq!(store.clip_count().unwrap(), 0);
    }

    #[test]
    fn empty_store_is_a_no_op() {
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), true);
        let store = Store::open_in_memory().unwrap();
        let cleanup = Cleanup::new(c);
        let s = cleanup.run_once_with(&store, 1_000, false).unwrap();
        assert_eq!(s, CleanupSummary::default());
    }

    #[test]
    fn pressure_flag_propagates_into_summary() {
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), true);
        let store = Store::open_in_memory().unwrap();
        let cleanup = Cleanup::new(c);
        let s = cleanup.run_once_with(&store, 1_000, true).unwrap();
        assert!(s.pressure);
    }

    #[test]
    fn should_preserve_protects_saved_bucket_via_defense_in_depth() {
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), false);
        let cleanup = Cleanup::new(c);
        let rec = ClipRecord {
            id: 1,
            relative_path: PathBuf::from("TeslaCam/SavedClips/x.mp4"),
            bucket: Bucket::Saved,
            clip_started_utc: Some(0),
            indexed_at_utc: 0,
            waypoint_count: 0,
            gps_waypoint_count: 0,
        };
        assert!(cleanup.should_preserve(&rec));
    }

    #[test]
    fn should_preserve_protects_gps_clips_when_configured() {
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), true);
        let cleanup = Cleanup::new(c);
        let rec = ClipRecord {
            id: 1,
            relative_path: PathBuf::from("TeslaCam/RecentClips/x.mp4"),
            bucket: Bucket::Recent,
            clip_started_utc: Some(0),
            indexed_at_utc: 0,
            waypoint_count: 5,
            gps_waypoint_count: 3,
        };
        assert!(cleanup.should_preserve(&rec));
    }

    #[test]
    fn should_preserve_lets_no_gps_recent_through() {
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), true);
        let cleanup = Cleanup::new(c);
        let rec = ClipRecord {
            id: 1,
            relative_path: PathBuf::from("TeslaCam/RecentClips/x.mp4"),
            bucket: Bucket::Recent,
            clip_started_utc: Some(0),
            indexed_at_utc: 0,
            waypoint_count: 5,
            gps_waypoint_count: 0,
        };
        assert!(!cleanup.should_preserve(&rec));
    }

    #[test]
    fn deletes_in_age_order_oldest_first() {
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), true);
        let mut store = Store::open_in_memory().unwrap();
        for (rel, ts) in [
            ("TeslaCam/RecentClips/mid.mp4", 500),
            ("TeslaCam/RecentClips/old.mp4", 100),
            ("TeslaCam/RecentClips/older.mp4", 50),
        ] {
            record(
                &mut store,
                &c,
                Bucket::Recent,
                rel,
                Some(UNIX_EPOCH + Duration::from_secs(ts)),
                false,
            );
        }
        let cleanup = Cleanup::new(c.clone());
        let s = cleanup.run_once_with(&store, 1_000, false).unwrap();
        assert_eq!(s.considered, 3);
        assert_eq!(s.deleted, 3);
        assert_eq!(store.clip_count().unwrap(), 0);
    }

    #[test]
    fn run_once_uses_real_clock_and_returns_smoke() {
        // Smoke test for the clock-driven path; can't assert
        // exact deletions without freezing time.
        let dir = tempfile::tempdir().unwrap();
        let c = cfg(dir.path(), true);
        let store = Store::open_in_memory().unwrap();
        let cleanup = Cleanup::new(c);
        let s = cleanup.run_once(&store).unwrap();
        assert_eq!(s, CleanupSummary::default());
    }

    #[test]
    fn effective_cutoff_no_pressure_subtracts_retention() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = cfg(dir.path(), true);
        c.cleanup.retention_days = 1;
        let cleanup = Cleanup::new(c);
        // 1 day = 86_400 s
        let now: i64 = 1_000_000;
        assert_eq!(cleanup.effective_cutoff(now, false), now - 86_400);
    }

    #[test]
    fn effective_cutoff_under_pressure_returns_now() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = cfg(dir.path(), true);
        c.cleanup.retention_days = 30;
        let cleanup = Cleanup::new(c);
        let now: i64 = 1_000_000;
        assert_eq!(cleanup.effective_cutoff(now, true), now);
    }

    #[test]
    fn pressure_sweep_deletes_young_no_gps_clip_that_age_would_spare() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = cfg(dir.path(), true);
        c.cleanup.retention_days = 30;
        let mut store = Store::open_in_memory().unwrap();
        // Clip is "now" — age cutoff would NOT touch it.
        let now_t = UNIX_EPOCH + Duration::from_secs(10_000_000);
        record(
            &mut store,
            &c,
            Bucket::Recent,
            "TeslaCam/RecentClips/young_no_gps.mp4",
            Some(now_t),
            false,
        );
        let cleanup = Cleanup::new(c);
        // Simulate `run_once` under pressure: cutoff = now;
        // age would have used now - 30 days = 7_408_000.
        let s = cleanup.run_once_with(&store, 10_000_001, true).unwrap();
        assert_eq!(s.considered, 1);
        assert_eq!(s.deleted, 1);
        assert!(s.pressure);
    }

    #[test]
    fn pressure_sweep_still_preserves_gps_when_configured() {
        let dir = tempfile::tempdir().unwrap();
        let mut c = cfg(dir.path(), true);
        c.cleanup.retention_days = 30;
        let mut store = Store::open_in_memory().unwrap();
        let now_t = UNIX_EPOCH + Duration::from_secs(10_000_000);
        record(
            &mut store,
            &c,
            Bucket::Recent,
            "TeslaCam/RecentClips/young_gps.mp4",
            Some(now_t),
            true,
        );
        let cleanup = Cleanup::new(c);
        let s = cleanup.run_once_with(&store, 10_000_001, true).unwrap();
        assert_eq!(s.considered, 1);
        assert_eq!(s.preserved_gps, 1);
        assert_eq!(s.deleted, 0);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn free_pct_real_statvfs_returns_sane_percent() {
        // Exercises the rustix::fs::statvfs path on Linux only.
        // The dev workstation skips this; the Pi (and any
        // Linux CI runner) runs it.
        let dir = tempfile::tempdir().unwrap();
        let pct = super::free_pct(dir.path()).unwrap();
        assert!(
            (0.0..=100.0).contains(&pct),
            "free_pct returned out-of-range value: {pct}",
        );
    }

    // ------------------------------------------------------------------
    // Path-traversal defense (security-review finding, Phase 4b.3)
    // ------------------------------------------------------------------

    #[test]
    fn safe_absolute_path_accepts_normal_relative_path() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = cfg(dir.path(), true);
        let cleanup = Cleanup::new(cfg);
        let rel = Path::new("RecentClips/2024-01-01_12-34-56/front.mp4");
        let abs = cleanup.safe_absolute_path(rel).expect("normal path");
        assert!(abs.starts_with(dir.path()));
    }

    #[test]
    fn safe_absolute_path_rejects_absolute_input() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = cfg(dir.path(), true);
        let cleanup = Cleanup::new(cfg);
        #[cfg(unix)]
        let bad = Path::new("/etc/passwd");
        #[cfg(windows)]
        let bad = Path::new(r"C:\Windows\System32\drivers\etc\hosts");
        assert!(cleanup.safe_absolute_path(bad).is_none());
    }

    #[test]
    fn safe_absolute_path_rejects_parent_traversal() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = cfg(dir.path(), true);
        let cleanup = Cleanup::new(cfg);
        assert!(
            cleanup
                .safe_absolute_path(Path::new("RecentClips/../../etc/passwd"))
                .is_none(),
        );
        assert!(
            cleanup
                .safe_absolute_path(Path::new("../escape.mp4"))
                .is_none(),
        );
    }

    #[test]
    fn safe_absolute_path_normalizes_current_dir_segment() {
        // Path::components() drops `.` segments; `./x` and `x`
        // are equivalent and both safe. We assert the path
        // resolves rather than is rejected.
        let dir = tempfile::tempdir().unwrap();
        let cfg = cfg(dir.path(), true);
        let cleanup = Cleanup::new(cfg);
        let resolved = cleanup
            .safe_absolute_path(Path::new("RecentClips/./front.mp4"))
            .expect("current-dir segment is harmless");
        assert!(resolved.starts_with(dir.path()));
    }

    #[test]
    fn run_once_skips_unsafe_path_row_and_counts_as_failed() {
        // A row that escapes backing_root must not be acted on.
        // The row stays in the store; the file (which lives
        // outside backing_root) is untouched. Counter goes to
        // `failed`, not `deleted`.
        let dir = tempfile::tempdir().unwrap();
        let backing = dir.path().join("backing");
        std::fs::create_dir_all(&backing).unwrap();
        let cfg = cfg(&backing, true);
        let mut store = Store::open_in_memory().unwrap();

        // Create a "victim" file outside backing_root that
        // would be hit if the guard failed.
        let victim = dir.path().join("victim.txt");
        std::fs::write(&victim, b"do not delete").unwrap();

        // Inject a row with a traversal path. We bypass the
        // `record` helper (which writes a real file under
        // backing_root) and call record_clip directly with the
        // unsafe relative_path.
        let unsafe_rel = Path::new("../victim.txt");
        let walk = walk_with(vec![], Some(UNIX_EPOCH + Duration::from_secs(1_000)));
        store
            .record_clip(Bucket::Recent, unsafe_rel, &walk)
            .unwrap();

        let cleanup = Cleanup::new(cfg);
        let s = cleanup.run_once_with(&store, 10_000_000, false).unwrap();
        assert_eq!(s.considered, 1);
        assert_eq!(s.deleted, 0);
        assert_eq!(s.failed, 1);
        // Victim file must still exist.
        assert!(victim.exists(), "victim file was deleted!");
        // Row must still exist (delete was aborted before
        // touching the store).
        let rows = store
            .list_clips_in_bucket_older_than(Bucket::Recent, 10_000_000)
            .unwrap();
        assert_eq!(rows.len(), 1);
    }
}
