//! Row record + error types for the store layer.

use std::path::PathBuf;
use std::time::SystemTime;

use thiserror::Error;

use super::bucket::Bucket;

/// Row returned by [`super::Store::list_clips_in_bucket_older_than`]
/// and [`super::Store::clip_by_path`].
#[derive(Debug, Clone, PartialEq)]
pub struct ClipRecord {
    /// Primary key.
    pub id: i64,
    /// Path relative to the configured `backing_root`.
    pub relative_path: PathBuf,
    /// Bucket the clip lives in.
    pub bucket: Bucket,
    /// Recording start time (unix seconds), `None` if `mvhd`
    /// was missing.
    pub clip_started_utc: Option<i64>,
    /// When the indexer last wrote this row (unix seconds).
    pub indexed_at_utc: i64,
    /// Total waypoints stored for this clip.
    pub waypoint_count: u32,
    /// Subset of `waypoint_count` whose lat/lon were a real
    /// GPS fix.
    pub gps_waypoint_count: u32,
}

impl ClipRecord {
    /// Convenience: a clip is "GPS-tagged" if it has at least
    /// one waypoint that recorded a non-zero fix. Mirrors
    /// [`crate::sei::Waypoint`]'s GPS check.
    #[must_use]
    pub const fn has_gps(&self) -> bool {
        self.gps_waypoint_count > 0
    }
}

/// Errors emitted by the store layer.
#[derive(Debug, Error)]
pub enum StoreError {
    /// Underlying SQLite error.
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),
    /// The DB on disk was created by a newer version of the
    /// worker than is currently running. Refusing to open
    /// rather than risk silently losing data.
    #[error("schema version {found} is newer than this binary supports ({expected})")]
    SchemaTooNew {
        /// Version stamped in the DB.
        found: u32,
        /// Version this binary supports.
        expected: u32,
    },
    /// The DB stamped a `schema_version` that we cannot parse
    /// as a `u32`. Indicates a corrupted DB or a manual edit.
    #[error("schema version {0:?} is not a valid u32")]
    SchemaCorrupt(String),
    /// A row's `bucket` column held a value not in the
    /// [`Bucket`] enum.
    #[error("unknown bucket name in DB: {0:?}")]
    UnknownBucket(String),
    /// I/O error setting up the DB file (e.g. creating the
    /// parent directory).
    #[error("i/o error preparing {path:?}: {source}")]
    Io {
        /// Path we tried to operate on.
        path: PathBuf,
        /// Underlying I/O error.
        #[source]
        source: std::io::Error,
    },
    /// A timestamp could not be converted from `SystemTime`
    /// to unix seconds. Only happens for times before the
    /// Unix epoch, which Tesla footage never produces.
    #[error("timestamp {0:?} is before the Unix epoch")]
    TimestampUnderflow(SystemTime),
}

/// Result alias for store operations.
pub type Result<T> = std::result::Result<T, StoreError>;
