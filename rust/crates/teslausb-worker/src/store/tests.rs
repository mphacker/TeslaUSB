//! Integration tests for the store layer. Kept in their own
//! file so each production module stays under the charter's
//! 500-line ceiling.

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

use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rusqlite::{Connection, params};
use teslausb_core::sei::tesla::SeiMessage;

use super::bucket::Bucket;
use super::schema::{CURRENT_SCHEMA_VERSION, META_KEY_SCHEMA_VERSION};
use super::store_impl::Store;
use super::types::StoreError;
use crate::sei::{ClipWalk, Waypoint};

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
    // No clip_started_utc -> falls back to now-ish indexed_at;
    // we use a huge cutoff far in the future to verify the row
    // IS returned.
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
    // `PRAGMA foreign_keys = ON` were forgotten, this would
    // fail.
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
    // Reopen; migration must be a no-op and data must still
    // be there.
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
    // Bring it up to current, then poke a higher version into
    // the meta table and try to reopen.
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
