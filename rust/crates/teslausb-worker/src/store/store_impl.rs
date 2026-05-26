//! The [`Store`] type — a `rusqlite::Connection` wrapped in a
//! typed-error API. All public query methods live here.

use std::path::Path;
use std::time::SystemTime;

use rusqlite::{Connection, OpenFlags, OptionalExtension, params};

use crate::sei::ClipWalk;

use super::bucket::Bucket;
use super::helpers::{
    clip_record_from_row, path_to_db_str, system_time_to_unix_seconds, upsert_clip_row,
};
use super::schema::{CURRENT_SCHEMA_VERSION, META_KEY_SCHEMA_VERSION, MIGRATIONS};
use super::types::{ClipRecord, Result, StoreError};

/// SQLite-backed indexer store.
pub struct Store {
    // `pub(super)` so the sibling `tests` submodule can probe
    // raw pragmas (WAL mode, FK enabled, raw waypoint rows).
    // Outside the store layer this field stays private.
    pub(super) conn: Connection,
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
    /// Sets the `trips_dirty` meta flag at the end of the
    /// transaction so the supervisor's next materialise tick
    /// rebuilds the derived `trips` / `detected_events` rows.
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
                    latitude_deg, longitude_deg, speed_mps, heading_deg,
                    acceleration_x, acceleration_y, acceleration_z,
                    gear, steering_angle,
                    brake_applied, blinker_on_left, blinker_on_right,
                    autopilot_state
                 ) VALUES (
                    ?1, ?2, ?3,
                    ?4, ?5, ?6, ?7,
                    ?8, ?9, ?10,
                    ?11, ?12,
                    ?13, ?14, ?15,
                    ?16
                 )",
            )?;
            for w in &walk.waypoints {
                let m = &w.message;
                stmt.execute(params![
                    clip_id,
                    w.frame_index,
                    w.timestamp_ms,
                    m.latitude_deg,
                    m.longitude_deg,
                    f64::from(m.vehicle_speed_mps),
                    m.heading_deg,
                    m.linear_acceleration_mps2_x,
                    m.linear_acceleration_mps2_y,
                    m.linear_acceleration_mps2_z,
                    m.gear_state.as_db_str(),
                    f64::from(m.steering_wheel_angle),
                    i64::from(m.brake_applied),
                    i64::from(m.blinker_on_left),
                    i64::from(m.blinker_on_right),
                    m.autopilot_state.as_db_str(),
                ])?;
            }
        }
        tx.commit()?;
        // Out-of-transaction UPSERT into meta is fine: it's a
        // best-effort hint to the supervisor; missing it would
        // only delay the next rebuild by one tick.
        crate::materializer::mark_trips_dirty(&self.conn)?;
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

    /// List ALL clips in the index (every bucket), ordered by
    /// `id` ascending for a stable iteration. Used by
    /// [`crate::cleanup::Cleanup::gc_orphans`] to find rows
    /// whose backing files have been removed out-of-band (e.g.
    /// by an exFAT recovery, a manual `rm`, or a power-cut
    /// truncation) and drop them so the index stops reporting
    /// phantom clips.
    ///
    /// On very large indexes this allocates one row per clip
    /// (a few dozen bytes each). The worker is the only caller
    /// and runs on the 5-minute cleanup tick, so the peak is
    /// bounded by the disk's clip capacity — well under 100k
    /// even on a year-long deployment.
    ///
    /// # Errors
    ///
    /// Returns `Err` on a SQLite error or an unknown bucket
    /// string in a row.
    pub fn list_all_clips(&self) -> Result<Vec<ClipRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, relative_path, bucket, clip_started_utc,
                    indexed_at_utc, waypoint_count, gps_waypoint_count
             FROM clips
             ORDER BY id ASC",
        )?;
        let rows = stmt.query_map([], clip_record_from_row)?;
        rows.collect::<std::result::Result<Vec<_>, _>>()?
            .into_iter()
            .collect()
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

    /// Mark the `trips`/`detected_events` tables as dirty so
    /// the next [`Store::rebuild_trips_if_dirty`] call performs
    /// a rebuild. Used by the supervisor when the mapping
    /// overrides file changes on disk.
    ///
    /// # Errors
    ///
    /// Returns `Err` on any SQLite error while writing the
    /// `meta` row.
    pub fn mark_trips_dirty(&self) -> Result<()> {
        crate::materializer::mark_trips_dirty(&self.conn)?;
        Ok(())
    }

    /// Run the trip+event materialiser if the `trips_dirty`
    /// flag is set. No-op when the flag is clear, so callers
    /// can drive this from a periodic tick without paying for
    /// repeated rebuilds.
    ///
    /// Returns `Some(stats)` when a rebuild ran, `None`
    /// otherwise.
    ///
    /// # Errors
    ///
    /// Returns `Err` on any SQLite error during the rebuild.
    pub fn rebuild_trips_if_dirty(
        &mut self,
        overrides: &crate::mapping_overrides::MappingOverrides,
    ) -> Result<Option<crate::materializer::RebuildStats>> {
        if !crate::materializer::trips_dirty(&self.conn)? {
            return Ok(None);
        }
        let stats = crate::materializer::Materializer::from_overrides(overrides)
            .rebuild_all(&mut self.conn)?;
        Ok(Some(stats))
    }

    /// Force a trip+event rebuild regardless of the dirty
    /// flag. Used by the supervisor's startup backfill so the
    /// first deploy after schema-v3 migration always lands
    /// derived rows before the web layer reads them.
    ///
    /// # Errors
    ///
    /// Returns `Err` on any SQLite error during the rebuild.
    pub fn rebuild_trips_now(
        &mut self,
        overrides: &crate::mapping_overrides::MappingOverrides,
    ) -> Result<crate::materializer::RebuildStats> {
        let stats = crate::materializer::Materializer::from_overrides(overrides)
            .rebuild_all(&mut self.conn)?;
        Ok(stats)
    }
}
