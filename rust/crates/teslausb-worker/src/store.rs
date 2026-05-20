//! SQLite-backed waypoint / clip index store.
//!
//! Layer-3 adapter (per the charter layering rule). Wraps
//! `rusqlite::Connection` behind a typed-error API. Schema
//! and rationale: [ADR-0010].
//!
//! [ADR-0010]: ../../../../docs/adr/0010-rusqlite-for-indexer-store.md
//!
//! ## Concurrency model
//!
//! * Single writer (the indexer) + multiple readers (cleanup,
//!   future web). WAL mode is enabled at open so readers do
//!   not block on writer transactions.
//! * The DB API is blocking (rusqlite is synchronous). Callers
//!   running inside a tokio runtime MUST wrap each call in
//!   `tokio::task::spawn_blocking`. This is enforced by
//!   review, not by the type system, because hiding the
//!   blocking call inside an `async fn` is exactly the
//!   anti-pattern the charter rejects.
//!
//! ## Migration discipline
//!
//! Every schema mutation lives inside `MIGRATIONS`, which is
//! an ordered list of `(version, sql)` pairs. [`Store::open`]
//! reads the current `schema_version` from the `meta` table,
//! then applies every migration with `version > current` in
//! one transaction. Downgrades are refused.

// File-level: "SQLite", "WAL", "FK", "UPSERT" are domain
// terms that read more naturally in prose than as code.
// Matches the carve-out used in the SEI files.
#![allow(clippy::doc_markdown)]

use std::path::{Path, PathBuf};
use std::time::SystemTime;

use rusqlite::{Connection, OpenFlags, OptionalExtension, Transaction, params};
use thiserror::Error;

use crate::sei::ClipWalk;

/// Schema-version key in the `meta` table.
const META_KEY_SCHEMA_VERSION: &str = "schema_version";

/// Ordered DB migrations. Each is one DDL transaction that
/// brings the schema from version `index` to version
/// `index + 1`. NEVER mutate an existing entry — append a
/// new one and bump [`CURRENT_SCHEMA_VERSION`].
const MIGRATIONS: &[&str] = &[
    // v0 → v1: initial schema.
    "\
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path TEXT NOT NULL UNIQUE,
    bucket TEXT NOT NULL,
    clip_started_utc INTEGER,
    indexed_at_utc INTEGER NOT NULL,
    waypoint_count INTEGER NOT NULL DEFAULT 0,
    gps_waypoint_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX clips_by_bucket_started ON clips(bucket, clip_started_utc);
CREATE TABLE waypoints (
    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    frame_index INTEGER NOT NULL,
    timestamp_ms REAL NOT NULL,
    latitude_deg REAL NOT NULL,
    longitude_deg REAL NOT NULL,
    speed_mps REAL NOT NULL,
    heading_deg REAL NOT NULL,
    PRIMARY KEY (clip_id, frame_index)
);
CREATE INDEX waypoints_by_clip ON waypoints(clip_id);
",
];

/// Current schema version. Bump when appending to the
/// `MIGRATIONS` constant.
pub const CURRENT_SCHEMA_VERSION: u32 = 1;

/// Which Tesla bucket a clip lives in. Stored as TEXT in the
/// `bucket` column. The enum prevents the cleanup worker from
/// ever passing a misspelled bucket name to a delete query.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Bucket {
    /// `RecentClips` — the rolling 1-hour dashcam buffer Tesla
    /// overwrites in place. The cleanup worker may delete
    /// no-GPS clips here once they age out.
    Recent,
    /// `SavedClips` — clips the driver explicitly saved (horn
    /// tap). Never deleted by the cleanup worker.
    Saved,
    /// `SentryClips` — clips Sentry mode triggered. Never
    /// deleted by the cleanup worker.
    Sentry,
}

impl Bucket {
    /// Stable DB string representation. NEVER change these
    /// without writing a migration.
    #[must_use]
    pub const fn as_db_str(self) -> &'static str {
        match self {
            Self::Recent => "recent",
            Self::Saved => "saved",
            Self::Sentry => "sentry",
        }
    }

    /// Parse the DB-stored string back to a [`Bucket`]. Used
    /// by [`Store`] queries that return rows.
    fn from_db_str(s: &str) -> Result<Self> {
        match s {
            "recent" => Ok(Self::Recent),
            "saved" => Ok(Self::Saved),
            "sentry" => Ok(Self::Sentry),
            other => Err(StoreError::UnknownBucket(other.to_string())),
        }
    }
}

/// Row returned by [`Store::list_clips_in_bucket_older_than`]
/// and [`Store::clip_by_path`].
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

/// SQLite-backed indexer store.
pub struct Store {
    conn: Connection,
}

impl std::fmt::Debug for Store {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // `rusqlite::Connection` is intentionally not `Debug`
        // because the inner handle has no meaningful repr.
        // For our purposes a placeholder is enough — the only
        // caller is `unwrap_err()` in tests.
        f.debug_struct("Store").finish_non_exhaustive()
    }
}

impl Store {
    /// Open (or create) a store at `path`. Applies pending
    /// schema migrations. Use [`Store::open_in_memory`] in
    /// tests.
    ///
    /// # Errors
    ///
    /// Returns `Err` if the parent directory cannot be
    /// created, the file cannot be opened, the WAL pragma
    /// cannot be set, or a migration fails.
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).map_err(|e| StoreError::Io {
                    path: parent.to_path_buf(),
                    source: e,
                })?;
            }
        }
        let conn = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
        )?;
        let mut store = Self { conn };
        store.configure_pragmas(false)?;
        store.run_migrations()?;
        Ok(store)
    }

    /// Open a fresh in-memory store. Used by tests.
    ///
    /// # Errors
    ///
    /// Returns `Err` if SQLite cannot initialise the in-memory
    /// connection or a migration fails.
    pub fn open_in_memory() -> Result<Self> {
        let conn = Connection::open_in_memory()?;
        let mut store = Self { conn };
        // WAL is meaningless on an in-memory DB; skip it to
        // avoid the noisy "cannot change into wal mode from
        // within a transaction" diagnostic that some SQLite
        // builds emit.
        store.configure_pragmas(true)?;
        store.run_migrations()?;
        Ok(store)
    }

    fn configure_pragmas(&self, in_memory: bool) -> Result<()> {
        // Foreign keys must be re-enabled on every connection;
        // SQLite's default is OFF for backwards compat. Without
        // this the `ON DELETE CASCADE` on `waypoints` does not
        // fire when we delete a `clips` row.
        self.conn.execute_batch("PRAGMA foreign_keys = ON;")?;
        if !in_memory {
            // WAL gives the cleanup worker / web layer
            // concurrent-read access while the indexer writes.
            // `journal_mode = wal` returns a row; use `query`
            // not `execute`.
            self.conn.pragma_update(None, "journal_mode", "WAL")?;
            // `synchronous = NORMAL` is the WAL-mode-safe
            // setting recommended by the SQLite docs: durable
            // across power loss, ~2× faster than FULL.
            self.conn.pragma_update(None, "synchronous", "NORMAL")?;
        }
        Ok(())
    }

    fn run_migrations(&mut self) -> Result<()> {
        let current = self.read_schema_version()?;
        if current > CURRENT_SCHEMA_VERSION {
            return Err(StoreError::SchemaTooNew {
                found: current,
                expected: CURRENT_SCHEMA_VERSION,
            });
        }
        if current == CURRENT_SCHEMA_VERSION {
            return Ok(());
        }
        let tx = self.conn.transaction()?;
        for (idx, sql) in MIGRATIONS.iter().enumerate() {
            // `idx` is 0-based; the migration at `idx` brings
            // the schema from version `idx` to version `idx + 1`.
            let target = u32::try_from(idx + 1).unwrap_or(u32::MAX);
            if target <= current {
                continue;
            }
            tx.execute_batch(sql)?;
        }
        // Stamp the new version. UPSERT so we cover both the
        // "DB is empty" and "DB exists but we're migrating
        // forward" paths.
        tx.execute(
            "INSERT INTO meta (key, value) VALUES (?1, ?2)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![META_KEY_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION.to_string()],
        )?;
        tx.commit()?;
        Ok(())
    }

    fn read_schema_version(&self) -> Result<u32> {
        // First call may happen against a brand-new DB that
        // doesn't yet have a `meta` table. Detect via the
        // `sqlite_master` catalog instead of catching the
        // resulting error, which would mask real failures.
        let has_meta: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM sqlite_master
             WHERE type = 'table' AND name = 'meta'",
            [],
            |row| row.get(0),
        )?;
        if has_meta == 0 {
            return Ok(0);
        }
        let stored: Option<String> = self
            .conn
            .query_row(
                "SELECT value FROM meta WHERE key = ?1",
                params![META_KEY_SCHEMA_VERSION],
                |row| row.get(0),
            )
            .optional()?;
        match stored {
            None => Ok(0),
            Some(s) => s.parse::<u32>().map_err(|_| StoreError::SchemaCorrupt(s)),
        }
    }

    /// Returns the schema version currently stamped on the DB.
    /// Used by tests and by the operator-facing `--check` path.
    ///
    /// # Errors
    ///
    /// Returns `Err` on a SQLite error or a corrupt version
    /// string.
    pub fn schema_version(&self) -> Result<u32> {
        self.read_schema_version()
    }

    /// Insert a clip and all its waypoints in one transaction.
    /// If a row already exists for `relative_path`, its
    /// waypoints are deleted and replaced — re-indexing a
    /// clip is idempotent.
    ///
    /// # Errors
    ///
    /// Returns `Err` on any SQLite error, on a timestamp that
    /// predates the Unix epoch, or if the transaction cannot
    /// be committed.
    pub fn record_clip(
        &mut self,
        bucket: Bucket,
        relative_path: &Path,
        walk: &ClipWalk,
    ) -> Result<i64> {
        let indexed_at = system_time_to_unix_seconds(SystemTime::now())?;
        let clip_started = match walk.clip_started_utc {
            None => None,
            Some(t) => Some(system_time_to_unix_seconds(t)?),
        };
        let rel = path_to_db_str(relative_path);
        let waypoint_count = u32::try_from(walk.waypoints.len()).unwrap_or(u32::MAX);
        let gps_count = u32::try_from(
            walk.waypoints
                .iter()
                .filter(|w| w.message.has_gps_fix())
                .count(),
        )
        .unwrap_or(u32::MAX);

        let tx = self.conn.transaction()?;
        let clip_id = upsert_clip_row(
            &tx,
            &rel,
            bucket,
            clip_started,
            indexed_at,
            waypoint_count,
            gps_count,
        )?;
        // Wipe and replace waypoints — re-indexing the same
        // clip after a sample-rate change should not leave
        // stale rows.
        tx.execute("DELETE FROM waypoints WHERE clip_id = ?1", params![clip_id])?;
        {
            let mut stmt = tx.prepare(
                "INSERT INTO waypoints (
                    clip_id, frame_index, timestamp_ms,
                    latitude_deg, longitude_deg, speed_mps, heading_deg
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            )?;
            for w in &walk.waypoints {
                stmt.execute(params![
                    clip_id,
                    w.frame_index,
                    w.timestamp_ms,
                    w.message.latitude_deg,
                    w.message.longitude_deg,
                    f64::from(w.message.vehicle_speed_mps),
                    w.message.heading_deg,
                ])?;
            }
        }
        tx.commit()?;
        Ok(clip_id)
    }

    /// Fetch a clip's record by its `relative_path`. Returns
    /// `None` if the clip is not indexed.
    ///
    /// # Errors
    ///
    /// Returns `Err` on a SQLite error or an unknown bucket
    /// string in the row.
    pub fn clip_by_path(&self, relative_path: &Path) -> Result<Option<ClipRecord>> {
        let rel = path_to_db_str(relative_path);
        let row = self
            .conn
            .query_row(
                "SELECT id, relative_path, bucket, clip_started_utc,
                        indexed_at_utc, waypoint_count, gps_waypoint_count
                 FROM clips WHERE relative_path = ?1",
                params![rel],
                clip_record_from_row,
            )
            .optional()?;
        row.transpose()
    }

    /// Returns whether the indexer has seen this `relative_path`
    /// at all. Cheaper than [`Store::clip_by_path`] when only
    /// presence matters (the indexer's startup bootstrap uses
    /// this to skip already-indexed files).
    ///
    /// # Errors
    ///
    /// Returns `Err` on a SQLite error.
    pub fn knows_clip(&self, relative_path: &Path) -> Result<bool> {
        let rel = path_to_db_str(relative_path);
        let n: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM clips WHERE relative_path = ?1",
            params![rel],
            |r| r.get(0),
        )?;
        Ok(n > 0)
    }

    /// Returns `Some(true)` if the clip is indexed and has at
    /// least one GPS-fix waypoint; `Some(false)` if indexed
    /// but no GPS; `None` if not indexed.
    ///
    /// # Errors
    ///
    /// Returns `Err` on a SQLite error.
    pub fn clip_has_gps(&self, relative_path: &Path) -> Result<Option<bool>> {
        let rel = path_to_db_str(relative_path);
        let row: Option<i64> = self
            .conn
            .query_row(
                "SELECT gps_waypoint_count FROM clips WHERE relative_path = ?1",
                params![rel],
                |r| r.get(0),
            )
            .optional()?;
        Ok(row.map(|n| n > 0))
    }

    /// List clips in `bucket` whose `clip_started_utc` is
    /// strictly less than `cutoff_unix_s`. Clips with a NULL
    /// `clip_started_utc` (missing `mvhd`) fall back to
    /// `indexed_at_utc` for the comparison — otherwise a
    /// corrupted clip would be immortal.
    ///
    /// Returned in ascending start-time order (NULLs first,
    /// then oldest first) so the cleanup worker can stop
    /// scanning as soon as it has freed enough space.
    ///
    /// # Errors
    ///
    /// Returns `Err` on a SQLite error or an unknown bucket
    /// string in a row.
    pub fn list_clips_in_bucket_older_than(
        &self,
        bucket: Bucket,
        cutoff_unix_s: i64,
    ) -> Result<Vec<ClipRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, relative_path, bucket, clip_started_utc,
                    indexed_at_utc, waypoint_count, gps_waypoint_count
             FROM clips
             WHERE bucket = ?1
               AND COALESCE(clip_started_utc, indexed_at_utc) < ?2
             ORDER BY COALESCE(clip_started_utc, indexed_at_utc) ASC",
        )?;
        let rows = stmt.query_map(
            params![bucket.as_db_str(), cutoff_unix_s],
            clip_record_from_row,
        )?;
        rows.collect::<std::result::Result<Vec<_>, _>>()?
            .into_iter()
            .collect()
    }

    /// Delete a clip (and, via `ON DELETE CASCADE`, all its
    /// waypoints) from the index. No-op if the clip is not
    /// indexed; returns whether a row was actually deleted.
    ///
    /// # Errors
    ///
    /// Returns `Err` on a SQLite error.
    pub fn delete_clip_by_path(&self, relative_path: &Path) -> Result<bool> {
        let rel = path_to_db_str(relative_path);
        let n = self
            .conn
            .execute("DELETE FROM clips WHERE relative_path = ?1", params![rel])?;
        Ok(n > 0)
    }

    /// Total number of indexed clips. Used by tests and by
    /// the operator-facing `--stats` path.
    ///
    /// # Errors
    ///
    /// Returns `Err` on a SQLite error.
    pub fn clip_count(&self) -> Result<u64> {
        let n: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM clips", [], |r| r.get(0))?;
        u64::try_from(n).map_err(|_| StoreError::SchemaCorrupt(format!("negative clip count: {n}")))
    }

    /// Total number of indexed waypoints across all clips.
    ///
    /// # Errors
    ///
    /// Returns `Err` on a SQLite error.
    pub fn waypoint_count(&self) -> Result<u64> {
        let n: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM waypoints", [], |r| r.get(0))?;
        u64::try_from(n)
            .map_err(|_| StoreError::SchemaCorrupt(format!("negative waypoint count: {n}")))
    }
}

fn upsert_clip_row(
    tx: &Transaction<'_>,
    relative_path: &str,
    bucket: Bucket,
    clip_started_utc: Option<i64>,
    indexed_at_utc: i64,
    waypoint_count: u32,
    gps_waypoint_count: u32,
) -> Result<i64> {
    // UPSERT keyed on `relative_path` so re-indexing replaces
    // the existing row without disturbing its `id` (and
    // therefore without orphaning unrelated FKs we may add
    // later). `RETURNING id` is available from SQLite 3.35
    // (Apr 2021); the bundled SQLite is well past that.
    let id: i64 = tx.query_row(
        "INSERT INTO clips (
            relative_path, bucket, clip_started_utc,
            indexed_at_utc, waypoint_count, gps_waypoint_count
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6)
         ON CONFLICT(relative_path) DO UPDATE SET
            bucket = excluded.bucket,
            clip_started_utc = excluded.clip_started_utc,
            indexed_at_utc = excluded.indexed_at_utc,
            waypoint_count = excluded.waypoint_count,
            gps_waypoint_count = excluded.gps_waypoint_count
         RETURNING id",
        params![
            relative_path,
            bucket.as_db_str(),
            clip_started_utc,
            indexed_at_utc,
            waypoint_count,
            gps_waypoint_count,
        ],
        |r| r.get(0),
    )?;
    Ok(id)
}

fn clip_record_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Result<ClipRecord>> {
    let id: i64 = row.get(0)?;
    let relative_path: String = row.get(1)?;
    let bucket_str: String = row.get(2)?;
    let clip_started_utc: Option<i64> = row.get(3)?;
    let indexed_at_utc: i64 = row.get(4)?;
    let waypoint_count: i64 = row.get(5)?;
    let gps_waypoint_count: i64 = row.get(6)?;
    // Build the result outside the rusqlite mapper so that
    // our `StoreError::UnknownBucket` doesn't have to be
    // shoehorned through `rusqlite::Error::FromSqlConversionFailure`.
    Ok((|| -> Result<ClipRecord> {
        Ok(ClipRecord {
            id,
            relative_path: PathBuf::from(relative_path),
            bucket: Bucket::from_db_str(&bucket_str)?,
            clip_started_utc,
            indexed_at_utc,
            waypoint_count: u32::try_from(waypoint_count).unwrap_or(u32::MAX),
            gps_waypoint_count: u32::try_from(gps_waypoint_count).unwrap_or(u32::MAX),
        })
    })())
}

fn path_to_db_str(p: &Path) -> String {
    // Backing paths on the Pi are always UTF-8 (created by us
    // from UTF-8 config + UTF-8 Tesla filenames). On the off
    // chance the OS hands us non-UTF-8 bytes we use the lossy
    // conversion so the DB row is at least roundtrippable as
    // a key — we never reinterpret it back as a real path,
    // we only compare strings.
    p.to_string_lossy().into_owned()
}

fn system_time_to_unix_seconds(t: SystemTime) -> Result<i64> {
    t.duration_since(SystemTime::UNIX_EPOCH)
        .map_err(|_| StoreError::TimestampUnderflow(t))
        .and_then(|d| {
            i64::try_from(d.as_secs()).map_err(|_| {
                StoreError::SchemaCorrupt(format!("timestamp {t:?} overflows i64 seconds"))
            })
        })
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
    use crate::sei::Waypoint;

    fn msg_with_gps(lat: f64, lon: f64) -> SeiMessage {
        SeiMessage {
            latitude_deg: lat,
            longitude_deg: lon,
            vehicle_speed_mps: 12.5,
            heading_deg: 90.0,
            ..SeiMessage::default()
        }
    }

    fn walk(started_utc: Option<SystemTime>, waypoints: Vec<Waypoint>) -> ClipWalk {
        ClipWalk {
            clip_started_utc: started_utc,
            timescale: 90_000,
            frame_count: u32::try_from(waypoints.len()).unwrap_or(u32::MAX),
            waypoints,
        }
    }

    fn wp(frame: u32, ms: f64, msg: SeiMessage) -> Waypoint {
        Waypoint {
            frame_index: frame,
            timestamp_ms: ms,
            message: msg,
        }
    }

    #[test]
    fn open_in_memory_starts_at_current_version() {
        let store = Store::open_in_memory().unwrap();
        assert_eq!(store.schema_version().unwrap(), CURRENT_SCHEMA_VERSION);
    }

    #[test]
    fn fresh_store_is_empty() {
        let store = Store::open_in_memory().unwrap();
        assert_eq!(store.clip_count().unwrap(), 0);
        assert_eq!(store.waypoint_count().unwrap(), 0);
    }

    #[test]
    fn bucket_round_trip_through_db_str() {
        for b in [Bucket::Recent, Bucket::Saved, Bucket::Sentry] {
            assert_eq!(Bucket::from_db_str(b.as_db_str()).unwrap(), b);
        }
    }

    #[test]
    fn unknown_bucket_str_errors() {
        let err = Bucket::from_db_str("nope").unwrap_err();
        assert!(matches!(err, StoreError::UnknownBucket(s) if s == "nope"));
    }

    #[test]
    fn record_clip_stores_clip_and_waypoints() {
        let mut store = Store::open_in_memory().unwrap();
        let started = UNIX_EPOCH + Duration::from_secs(1_700_000_000);
        let w = walk(
            Some(started),
            vec![
                wp(0, 0.0, msg_with_gps(0.0, 0.0)),
                wp(30, 1_000.0, msg_with_gps(37.0, -122.0)),
                wp(60, 2_000.0, msg_with_gps(37.001, -122.001)),
            ],
        );
        let id = store
            .record_clip(Bucket::Recent, Path::new("RecentClips/a.mp4"), &w)
            .unwrap();
        assert!(id > 0);
        assert_eq!(store.clip_count().unwrap(), 1);
        assert_eq!(store.waypoint_count().unwrap(), 3);

        let rec = store
            .clip_by_path(Path::new("RecentClips/a.mp4"))
            .unwrap()
            .unwrap();
        assert_eq!(rec.bucket, Bucket::Recent);
        assert_eq!(rec.waypoint_count, 3);
        assert_eq!(rec.gps_waypoint_count, 2);
        assert!(rec.has_gps());
        assert_eq!(rec.clip_started_utc, Some(1_700_000_000));
    }

    #[test]
    fn record_clip_replaces_waypoints_on_reindex() {
        let mut store = Store::open_in_memory().unwrap();
        let path = Path::new("RecentClips/x.mp4");
        let w_first = walk(
            None,
            (0..5)
                .map(|i| wp(i * 30, f64::from(i), msg_with_gps(0.0, 0.0)))
                .collect(),
        );
        let id1 = store.record_clip(Bucket::Recent, path, &w_first).unwrap();
        assert_eq!(store.waypoint_count().unwrap(), 5);

        let w_second = walk(None, vec![wp(0, 0.0, msg_with_gps(1.0, 1.0))]);
        let id2 = store.record_clip(Bucket::Recent, path, &w_second).unwrap();
        assert_eq!(id1, id2, "id must be stable on re-index");
        assert_eq!(store.clip_count().unwrap(), 1);
        assert_eq!(store.waypoint_count().unwrap(), 1);
        let rec = store.clip_by_path(path).unwrap().unwrap();
        assert_eq!(rec.gps_waypoint_count, 1);
    }

    #[test]
    fn knows_clip_reflects_presence() {
        let mut store = Store::open_in_memory().unwrap();
        let p = Path::new("RecentClips/k.mp4");
        assert!(!store.knows_clip(p).unwrap());
        store
            .record_clip(Bucket::Recent, p, &walk(None, vec![]))
            .unwrap();
        assert!(store.knows_clip(p).unwrap());
    }

    #[test]
    fn clip_has_gps_three_states() {
        let mut store = Store::open_in_memory().unwrap();
        let p_missing = Path::new("RecentClips/missing.mp4");
        let p_no_gps = Path::new("RecentClips/nogps.mp4");
        let p_with_gps = Path::new("RecentClips/gps.mp4");

        assert_eq!(store.clip_has_gps(p_missing).unwrap(), None);

        store
            .record_clip(
                Bucket::Recent,
                p_no_gps,
                &walk(None, vec![wp(0, 0.0, msg_with_gps(0.0, 0.0))]),
            )
            .unwrap();
        assert_eq!(store.clip_has_gps(p_no_gps).unwrap(), Some(false));

        store
            .record_clip(
                Bucket::Recent,
                p_with_gps,
                &walk(None, vec![wp(0, 0.0, msg_with_gps(10.0, 10.0))]),
            )
            .unwrap();
        assert_eq!(store.clip_has_gps(p_with_gps).unwrap(), Some(true));
    }

    #[test]
    fn list_older_than_filters_by_bucket_and_time() {
        let mut store = Store::open_in_memory().unwrap();
        let old = UNIX_EPOCH + Duration::from_secs(100);
        let mid = UNIX_EPOCH + Duration::from_secs(500);
        let new = UNIX_EPOCH + Duration::from_secs(1_000);
        store
            .record_clip(
                Bucket::Recent,
                Path::new("RecentClips/old.mp4"),
                &walk(Some(old), vec![]),
            )
            .unwrap();
        store
            .record_clip(
                Bucket::Recent,
                Path::new("RecentClips/mid.mp4"),
                &walk(Some(mid), vec![]),
            )
            .unwrap();
        store
            .record_clip(
                Bucket::Recent,
                Path::new("RecentClips/new.mp4"),
                &walk(Some(new), vec![]),
            )
            .unwrap();
        store
            .record_clip(
                Bucket::Saved,
                Path::new("SavedClips/old.mp4"),
                &walk(Some(old), vec![]),
            )
            .unwrap();

        let cutoff = 700;
        let recent_old = store
            .list_clips_in_bucket_older_than(Bucket::Recent, cutoff)
            .unwrap();
        let paths: Vec<_> = recent_old
            .iter()
            .map(|r| r.relative_path.to_string_lossy().into_owned())
            .collect();
        assert_eq!(paths, vec!["RecentClips/old.mp4", "RecentClips/mid.mp4"]);

        let sentry_old = store
            .list_clips_in_bucket_older_than(Bucket::Sentry, cutoff)
            .unwrap();
        assert!(sentry_old.is_empty());

        let saved_old = store
            .list_clips_in_bucket_older_than(Bucket::Saved, cutoff)
            .unwrap();
        assert_eq!(saved_old.len(), 1);
        assert_eq!(saved_old[0].bucket, Bucket::Saved);
    }

    #[test]
    fn list_older_than_falls_back_to_indexed_at_when_started_is_null() {
        let mut store = Store::open_in_memory().unwrap();
        // No clip_started_utc -> falls back to now-ish
        // indexed_at; we use a huge cutoff far in the future
        // to verify the row IS returned.
        store
            .record_clip(
                Bucket::Recent,
                Path::new("RecentClips/null.mp4"),
                &walk(None, vec![]),
            )
            .unwrap();
        let far_future = i64::MAX / 2;
        let rows = store
            .list_clips_in_bucket_older_than(Bucket::Recent, far_future)
            .unwrap();
        assert_eq!(rows.len(), 1);
    }

    #[test]
    fn delete_clip_by_path_removes_clip_and_waypoints() {
        let mut store = Store::open_in_memory().unwrap();
        let p = Path::new("RecentClips/d.mp4");
        store
            .record_clip(
                Bucket::Recent,
                p,
                &walk(
                    None,
                    vec![
                        wp(0, 0.0, msg_with_gps(1.0, 1.0)),
                        wp(30, 1.0, msg_with_gps(2.0, 2.0)),
                    ],
                ),
            )
            .unwrap();
        assert_eq!(store.waypoint_count().unwrap(), 2);

        let removed = store.delete_clip_by_path(p).unwrap();
        assert!(removed);
        assert_eq!(store.clip_count().unwrap(), 0);
        // FK cascade must have wiped the waypoints. If
        // `PRAGMA foreign_keys = ON` were forgotten, this
        // would fail.
        assert_eq!(store.waypoint_count().unwrap(), 0);
    }

    #[test]
    fn delete_missing_clip_returns_false() {
        let store = Store::open_in_memory().unwrap();
        assert!(!store.delete_clip_by_path(Path::new("nope.mp4")).unwrap());
    }

    #[test]
    fn migration_is_idempotent_on_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("idx.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .record_clip(
                    Bucket::Recent,
                    Path::new("RecentClips/a.mp4"),
                    &walk(None, vec![wp(0, 0.0, msg_with_gps(1.0, 1.0))]),
                )
                .unwrap();
        }
        // Reopen; migration must be a no-op and data must
        // still be there.
        let store = Store::open(&path).unwrap();
        assert_eq!(store.schema_version().unwrap(), CURRENT_SCHEMA_VERSION);
        assert_eq!(store.clip_count().unwrap(), 1);
        assert_eq!(store.waypoint_count().unwrap(), 1);
    }

    #[test]
    fn open_creates_missing_parent_directory() {
        let dir = tempfile::tempdir().unwrap();
        let nested = dir.path().join("a").join("b").join("c").join("idx.db");
        let store = Store::open(&nested).unwrap();
        drop(store);
        assert!(nested.exists());
    }

    #[test]
    fn schema_too_new_is_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("future.sqlite3");
        // Bring it up to current, then poke a higher version
        // into the meta table and try to reopen.
        {
            let _store = Store::open(&path).unwrap();
        }
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute(
                "UPDATE meta SET value = ?1 WHERE key = ?2",
                params!["999", META_KEY_SCHEMA_VERSION],
            )
            .unwrap();
        }
        let err = Store::open(&path).unwrap_err();
        assert!(matches!(
            err,
            StoreError::SchemaTooNew {
                found: 999,
                expected: CURRENT_SCHEMA_VERSION
            }
        ));
    }

    #[test]
    fn corrupt_schema_version_is_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("corrupt.sqlite3");
        {
            let _store = Store::open(&path).unwrap();
        }
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute(
                "UPDATE meta SET value = ?1 WHERE key = ?2",
                params!["not-a-number", META_KEY_SCHEMA_VERSION],
            )
            .unwrap();
        }
        let err = Store::open(&path).unwrap_err();
        assert!(matches!(err, StoreError::SchemaCorrupt(s) if s == "not-a-number"));
    }

    #[test]
    fn wal_pragma_is_set_on_file_backed_db() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("wal.sqlite3");
        let store = Store::open(&path).unwrap();
        let mode: String = store
            .conn
            .query_row("PRAGMA journal_mode", [], |r| r.get(0))
            .unwrap();
        assert_eq!(mode.to_lowercase(), "wal");
    }

    #[test]
    fn foreign_keys_pragma_is_enabled() {
        let store = Store::open_in_memory().unwrap();
        let enabled: i64 = store
            .conn
            .query_row("PRAGMA foreign_keys", [], |r| r.get(0))
            .unwrap();
        assert_eq!(enabled, 1);
    }

    #[test]
    fn system_time_before_epoch_is_rejected() {
        let mut store = Store::open_in_memory().unwrap();
        let pre_epoch = UNIX_EPOCH.checked_sub(Duration::from_secs(1)).unwrap();
        let err = store
            .record_clip(
                Bucket::Recent,
                Path::new("RecentClips/pre.mp4"),
                &walk(Some(pre_epoch), vec![]),
            )
            .unwrap_err();
        assert!(matches!(err, StoreError::TimestampUnderflow(_)));
    }

    #[test]
    fn record_clip_with_zero_waypoints_persists_clip_row() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .record_clip(
                Bucket::Sentry,
                Path::new("SentryClips/empty.mp4"),
                &walk(None, vec![]),
            )
            .unwrap();
        let rec = store
            .clip_by_path(Path::new("SentryClips/empty.mp4"))
            .unwrap()
            .unwrap();
        assert_eq!(rec.bucket, Bucket::Sentry);
        assert_eq!(rec.waypoint_count, 0);
        assert_eq!(rec.gps_waypoint_count, 0);
        assert!(!rec.has_gps());
    }

    #[test]
    fn record_clip_counts_only_real_gps_fixes() {
        let mut store = Store::open_in_memory().unwrap();
        let waypoints = vec![
            wp(0, 0.0, msg_with_gps(0.0, 0.0)),
            wp(30, 1.0, msg_with_gps(0.0, 0.0)),
            wp(60, 2.0, msg_with_gps(37.0, -122.0)),
        ];
        store
            .record_clip(
                Bucket::Recent,
                Path::new("RecentClips/mix.mp4"),
                &walk(None, waypoints),
            )
            .unwrap();
        let rec = store
            .clip_by_path(Path::new("RecentClips/mix.mp4"))
            .unwrap()
            .unwrap();
        assert_eq!(rec.waypoint_count, 3);
        assert_eq!(rec.gps_waypoint_count, 1);
    }

    #[test]
    fn waypoints_persist_with_correct_fields() {
        let mut store = Store::open_in_memory().unwrap();
        let msg = SeiMessage {
            latitude_deg: 37.5,
            longitude_deg: -122.25,
            vehicle_speed_mps: 27.0,
            heading_deg: 180.5,
            ..SeiMessage::default()
        };
        store
            .record_clip(
                Bucket::Recent,
                Path::new("RecentClips/q.mp4"),
                &walk(None, vec![wp(42, 1_234.5, msg)]),
            )
            .unwrap();
        let (frame, ts, lat, lon, speed, hdg): (i64, f64, f64, f64, f64, f64) = store
            .conn
            .query_row(
                "SELECT frame_index, timestamp_ms, latitude_deg,
                        longitude_deg, speed_mps, heading_deg
                 FROM waypoints",
                [],
                |r| {
                    Ok((
                        r.get(0)?,
                        r.get(1)?,
                        r.get(2)?,
                        r.get(3)?,
                        r.get(4)?,
                        r.get(5)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(frame, 42);
        assert_eq!(ts, 1_234.5);
        assert_eq!(lat, 37.5);
        assert_eq!(lon, -122.25);
        assert_eq!(speed, 27.0);
        assert_eq!(hdg, 180.5);
    }
}
