//! Row-mapping and serialization helpers. Crate-private to
//! the store layer.

use std::path::{Path, PathBuf};
use std::time::SystemTime;

use rusqlite::{Transaction, params};

use super::bucket::Bucket;
use super::types::{ClipRecord, Result, StoreError};

/// UPSERT a `clips` row keyed on `relative_path`. Returns the
/// row's `id`. `RETURNING id` requires SQLite >= 3.35 (Apr 2021);
/// the bundled SQLite is well past that.
pub(super) fn upsert_clip_row(
    tx: &Transaction<'_>,
    relative_path: &str,
    bucket: Bucket,
    clip_started_utc: Option<i64>,
    indexed_at_utc: i64,
    waypoint_count: u32,
    gps_waypoint_count: u32,
) -> Result<i64> {
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

/// rusqlite row-mapper for [`ClipRecord`]. The outer
/// `rusqlite::Result` carries SQLite errors; the inner
/// `Result<ClipRecord>` carries our `StoreError::UnknownBucket`
/// without having to shoehorn it through
/// `rusqlite::Error::FromSqlConversionFailure`.
pub(super) fn clip_record_from_row(
    row: &rusqlite::Row<'_>,
) -> rusqlite::Result<Result<ClipRecord>> {
    let id: i64 = row.get(0)?;
    let relative_path: String = row.get(1)?;
    let bucket_str: String = row.get(2)?;
    let clip_started_utc: Option<i64> = row.get(3)?;
    let indexed_at_utc: i64 = row.get(4)?;
    let waypoint_count: i64 = row.get(5)?;
    let gps_waypoint_count: i64 = row.get(6)?;
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

/// Backing paths on the Pi are always UTF-8 (created by us
/// from UTF-8 config + UTF-8 Tesla filenames). On the off
/// chance the OS hands us non-UTF-8 bytes we use the lossy
/// conversion so the DB row is at least roundtrippable as a
/// key — we never reinterpret it back as a real path, only
/// compare strings.
pub(super) fn path_to_db_str(p: &Path) -> String {
    p.to_string_lossy().into_owned()
}

/// Convert a `SystemTime` to Unix seconds. Returns
/// `TimestampUnderflow` for pre-epoch times (Tesla footage
/// never produces them) and `SchemaCorrupt` if the seconds
/// overflow `i64` (will not happen in any realistic future).
pub(super) fn system_time_to_unix_seconds(t: SystemTime) -> Result<i64> {
    t.duration_since(SystemTime::UNIX_EPOCH)
        .map_err(|_| StoreError::TimestampUnderflow(t))
        .and_then(|d| {
            i64::try_from(d.as_secs()).map_err(|_| {
                StoreError::SchemaCorrupt(format!("timestamp {t:?} overflows i64 seconds"))
            })
        })
}
