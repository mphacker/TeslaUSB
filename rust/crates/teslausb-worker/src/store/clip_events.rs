//! `clip_events` table write/query helpers.
//!
//! Kept separate from `store_impl` so the core Store module stays
//! under the charter's god-module ceiling.

use std::path::Path;
use std::time::SystemTime;

use rusqlite::{OptionalExtension, params};

use super::helpers::{path_to_db_str, system_time_to_unix_seconds};
use super::store_impl::Store;
use super::types::{ClipEventRecord, Result, StoreError};

impl Store {
    /// Insert or update one Tesla `event.json` row.
    ///
    /// The row is raw event metadata and is intentionally not
    /// part of the trip materializer's derived-table rebuild.
    ///
    /// # Errors
    ///
    /// Returns `Err` on SQLite failure or timestamp conversion
    /// failure for the index time.
    pub fn record_clip_event(&mut self, event: &ClipEventRecord) -> Result<()> {
        let indexed_at = system_time_to_unix_seconds(SystemTime::now())?;
        let tx = self.conn.transaction()?;
        let primary_clip_id = best_clip_id_for_event(
            &tx,
            &event.event_dir_relative_path,
            event.metadata.timestamp_utc,
        )?;
        let event_json = path_to_db_str(&event.event_json_relative_path);
        let event_dir = path_to_db_str(&event.event_dir_relative_path);
        tx.execute(
            "INSERT INTO clip_events (
                event_json_relative_path, event_dir_relative_path, bucket,
                primary_clip_id, timestamp_utc, est_lat, est_lon,
                reason, city, camera, indexed_at_utc
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)
             ON CONFLICT(event_json_relative_path) DO UPDATE SET
                event_dir_relative_path = excluded.event_dir_relative_path,
                bucket = excluded.bucket,
                primary_clip_id = excluded.primary_clip_id,
                timestamp_utc = excluded.timestamp_utc,
                est_lat = excluded.est_lat,
                est_lon = excluded.est_lon,
                reason = excluded.reason,
                city = excluded.city,
                camera = excluded.camera,
                indexed_at_utc = excluded.indexed_at_utc",
            params![
                event_json,
                event_dir,
                event.bucket.as_db_str(),
                primary_clip_id,
                event.metadata.timestamp_utc,
                event.metadata.est_lat,
                event.metadata.est_lon,
                event.metadata.reason.as_deref(),
                event.metadata.city.as_deref(),
                event.metadata.camera.as_deref(),
                indexed_at,
            ],
        )?;
        tx.commit()?;
        Ok(())
    }

    /// Total number of indexed Tesla `event.json` rows.
    ///
    /// # Errors
    ///
    /// Returns `Err` on a SQLite error.
    pub fn clip_event_count(&self) -> Result<u64> {
        let n: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM clip_events", [], |r| r.get(0))?;
        u64::try_from(n)
            .map_err(|_| StoreError::SchemaCorrupt(format!("negative clip_events count: {n}")))
    }
}

pub(super) fn link_clip_events_for_clip(
    tx: &rusqlite::Transaction<'_>,
    relative_path: &Path,
) -> Result<()> {
    let Some(event_dir) = relative_path.parent() else {
        return Ok(());
    };
    relink_clip_events_in_dir(tx, event_dir)
}

fn relink_clip_events_in_dir(tx: &rusqlite::Transaction<'_>, event_dir: &Path) -> Result<()> {
    let event_dir_db = path_to_db_str(event_dir);
    let mut stmt = tx.prepare(
        "SELECT event_json_relative_path, timestamp_utc
         FROM clip_events
         WHERE event_dir_relative_path = ?1",
    )?;
    let rows = stmt.query_map(params![event_dir_db], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))
    })?;
    let events = rows.collect::<std::result::Result<Vec<_>, _>>()?;
    drop(stmt);
    for (event_json, timestamp_utc) in events {
        let primary_clip_id = best_clip_id_for_event(tx, event_dir, timestamp_utc)?;
        tx.execute(
            "UPDATE clip_events
             SET primary_clip_id = ?1
             WHERE event_json_relative_path = ?2",
            params![primary_clip_id, event_json],
        )?;
    }
    Ok(())
}

fn best_clip_id_for_event(
    tx: &rusqlite::Transaction<'_>,
    event_dir: &Path,
    timestamp_utc: i64,
) -> Result<Option<i64>> {
    let pattern = like_pattern_for_event_dir(event_dir);
    tx.query_row(
        "SELECT id
         FROM clips
         WHERE relative_path LIKE ?1
           AND relative_path NOT LIKE '%-back.mp4'
           AND relative_path NOT LIKE '%-left_repeater.mp4'
           AND relative_path NOT LIKE '%-right_repeater.mp4'
           AND relative_path NOT LIKE '%-left_pillar.mp4'
           AND relative_path NOT LIKE '%-right_pillar.mp4'
         ORDER BY
           CASE
             WHEN clip_started_utc IS NULL THEN 2
             WHEN clip_started_utc <= ?2 THEN 0
             ELSE 1
           END,
           ABS(COALESCE(clip_started_utc, indexed_at_utc) - ?2),
           id
         LIMIT 1",
        params![pattern, timestamp_utc],
        |r| r.get(0),
    )
    .optional()
    .map_err(StoreError::from)
}

fn like_pattern_for_event_dir(event_dir: &Path) -> String {
    const UNIX_SEPARATOR: char = '/';

    let mut prefix = path_to_db_str(event_dir);
    if !prefix.is_empty() {
        let separator = if prefix.contains(UNIX_SEPARATOR) {
            UNIX_SEPARATOR
        } else {
            std::path::MAIN_SEPARATOR
        };
        prefix.push(separator);
    }
    prefix.push('%');
    prefix
}
