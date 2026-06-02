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
        // Explicit-offset event.json rows are already true UTC and must
        // never be anchor-corrected; encode that as a NULL local-naive so
        // relink leaves them untouched. No-offset rows keep their raw
        // wall-clock so the offset can be re-derived idempotently.
        let (timestamp_utc, local_naive) = if event.metadata.timestamp_has_offset {
            (event.metadata.timestamp_utc, None)
        } else {
            let raw = event.metadata.timestamp_local_naive;
            let corrected = match dir_tz_offset(&tx, &event.event_dir_relative_path)? {
                Some(offset) => raw - offset,
                None => event.metadata.timestamp_utc,
            };
            (corrected, Some(raw))
        };
        let primary_clip_id =
            best_clip_id_for_event(&tx, &event.event_dir_relative_path, timestamp_utc)?;
        let event_json = path_to_db_str(&event.event_json_relative_path);
        let event_dir = path_to_db_str(&event.event_dir_relative_path);
        tx.execute(
            "INSERT INTO clip_events (
                event_json_relative_path, event_dir_relative_path, bucket,
                primary_clip_id, timestamp_utc, timestamp_local_naive, est_lat, est_lon,
                reason, city, camera, indexed_at_utc
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
             ON CONFLICT(event_json_relative_path) DO UPDATE SET
                event_dir_relative_path = excluded.event_dir_relative_path,
                bucket = excluded.bucket,
                primary_clip_id = excluded.primary_clip_id,
                timestamp_utc = excluded.timestamp_utc,
                timestamp_local_naive = excluded.timestamp_local_naive,
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
                timestamp_utc,
                local_naive,
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
        "SELECT event_json_relative_path, timestamp_local_naive, timestamp_utc
         FROM clip_events
         WHERE event_dir_relative_path = ?1",
    )?;
    let rows = stmt.query_map(params![event_dir_db], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, Option<i64>>(1)?,
            r.get::<_, i64>(2)?,
        ))
    })?;
    let events = rows.collect::<std::result::Result<Vec<_>, _>>()?;
    drop(stmt);
    // Re-derive the directory's local↔UTC offset from the immutable clip
    // filenames + true SEI starts, then recompute every anchorable event's
    // UTC and re-select its primary clip against that corrected instant.
    let offset = dir_tz_offset(tx, event_dir)?;
    for (event_json, local_naive, current_utc) in events {
        // A NULL local-naive marks an explicit-offset (already-true-UTC)
        // row; leave it untouched. Otherwise correct only when an anchor
        // offset is available, never folding a prior correction back in.
        let timestamp_utc = match (local_naive, offset) {
            (Some(raw), Some(off)) => raw - off,
            _ => current_utc,
        };
        let primary_clip_id = best_clip_id_for_event(tx, event_dir, timestamp_utc)?;
        tx.execute(
            "UPDATE clip_events
             SET primary_clip_id = ?1, timestamp_utc = ?2
             WHERE event_json_relative_path = ?3",
            params![primary_clip_id, timestamp_utc, event_json],
        )?;
    }
    Ok(())
}

/// Consensus local↔UTC offset (seconds east of UTC, rounded to 15 min)
/// for an event directory, derived from its front clips' immutable
/// filename wall-clock stamps versus their true SEI `clip_started_utc`.
///
/// Taking the most common offset across all anchored front clips in the
/// directory tolerates a single clip with a bad/missing SEI start.
/// Returns `None` when no front clip in the directory carries a usable
/// (in-range) anchor, in which case the caller must not correct.
fn dir_tz_offset(tx: &rusqlite::Transaction<'_>, event_dir: &Path) -> Result<Option<i64>> {
    let pattern = like_pattern_for_event_dir(event_dir);
    let mut stmt = tx.prepare(
        "SELECT relative_path, clip_started_utc
         FROM clips
         WHERE relative_path LIKE ?1 ESCAPE '\\'
           AND relative_path LIKE '%-front.mp4'
           AND clip_started_utc IS NOT NULL",
    )?;
    let rows = stmt.query_map(params![pattern], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))
    })?;
    let anchors = rows.collect::<std::result::Result<Vec<_>, _>>()?;
    drop(stmt);
    let mut counts: Vec<(i64, u32)> = Vec::new();
    for (relative_path, clip_started_utc) in anchors {
        let Some(file_name) = Path::new(&relative_path)
            .file_name()
            .and_then(|name| name.to_str())
        else {
            continue;
        };
        let Some(file_local_naive) = crate::clip_event::parse_clip_name_local_naive(file_name)
        else {
            continue;
        };
        let Some(offset) =
            crate::clip_event::rounded_tz_offset(file_local_naive, clip_started_utc)
        else {
            continue;
        };
        match counts.iter_mut().find(|(value, _)| *value == offset) {
            Some(entry) => entry.1 += 1,
            None => counts.push((offset, 1)),
        }
    }
    // Most frequent offset wins; ties break toward the numerically
    // smaller offset for determinism.
    Ok(counts
        .into_iter()
        .max_by(|a, b| a.1.cmp(&b.1).then_with(|| b.0.cmp(&a.0)))
        .map(|(offset, _)| offset))
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
         WHERE relative_path LIKE ?1 ESCAPE '\\'
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
    // Tesla event directory names embed `_` (e.g. `2026-06-01_20-11-00`),
    // which is a single-character wildcard in SQL `LIKE`. Escape the
    // literal prefix (paired with `ESCAPE '\'` in the query) so it can
    // never match a sibling clip in a differently named directory; the
    // trailing `%` stays an intentional wildcard.
    let mut pattern = escape_like_literal(&prefix);
    pattern.push('%');
    pattern
}

fn escape_like_literal(value: &str) -> String {
    const LIKE_ESCAPE: char = '\\';

    let mut escaped = String::with_capacity(value.len());
    for ch in value.chars() {
        if matches!(ch, LIKE_ESCAPE | '%' | '_') {
            escaped.push(LIKE_ESCAPE);
        }
        escaped.push(ch);
    }
    escaped
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::{escape_like_literal, like_pattern_for_event_dir};

    #[test]
    fn escapes_like_wildcards_in_dir_prefix() {
        // Underscores in the Tesla dir name must be escaped so the
        // prefix matches one directory, not any single-char variant.
        let pattern = like_pattern_for_event_dir(Path::new("SavedClips/2026-06-01_20-11-00"));
        assert!(pattern.ends_with("20-11-00/%"));
        assert!(pattern.contains("2026-06-01\\_20-11-00"));
    }

    #[test]
    fn escapes_percent_and_backslash() {
        assert_eq!(escape_like_literal("a%b_c\\d"), "a\\%b\\_c\\\\d");
    }
}
