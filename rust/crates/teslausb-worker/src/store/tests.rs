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
use super::types::{ClipEventRecord, StoreError};
use crate::clip_event::ClipEventMetadata;
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

#[test]
fn extended_telemetry_columns_round_trip() {
    use teslausb_core::sei::tesla::{AutopilotState, Gear};
    let mut store = Store::open_in_memory().unwrap();
    let msg = SeiMessage {
        latitude_deg: 1.0,
        longitude_deg: 2.0,
        vehicle_speed_mps: 30.0,
        heading_deg: 90.0,
        linear_acceleration_mps2_x: 0.1,
        linear_acceleration_mps2_y: -4.5,
        linear_acceleration_mps2_z: 9.8,
        gear_state: Gear::Drive,
        steering_wheel_angle: 0.5,
        brake_applied: true,
        blinker_on_left: false,
        blinker_on_right: true,
        autopilot_state: AutopilotState::Autosteer,
        ..SeiMessage::default()
    };
    store
        .record_clip(
            Bucket::Recent,
            Path::new("RecentClips/ext.mp4"),
            &walk(None, vec![wp(0, 0.0, msg)]),
        )
        .unwrap();
    #[allow(clippy::items_after_statements)]
    type Row = (f64, f64, f64, String, f64, i64, i64, i64, String);
    let row: Row = store
        .conn
        .query_row(
            "SELECT acceleration_x, acceleration_y, acceleration_z,
                    gear, steering_angle,
                    brake_applied, blinker_on_left, blinker_on_right,
                    autopilot_state
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
                    r.get(6)?,
                    r.get(7)?,
                    r.get(8)?,
                ))
            },
        )
        .unwrap();
    assert_eq!(row.0, 0.1);
    assert_eq!(row.1, -4.5);
    assert_eq!(row.2, 9.8);
    assert_eq!(row.3, "DRIVE");
    assert!((row.4 - 0.5).abs() < 1e-6);
    assert_eq!(row.5, 1);
    assert_eq!(row.6, 0);
    assert_eq!(row.7, 1);
    assert_eq!(row.8, "AUTOSTEER");
}

#[test]
fn multiple_waypoints_per_frame_index_are_persisted() {
    // Regression: Tesla emits consecutive SEI NAL units between
    // two slices, so the walker yields waypoints with the same
    // frame_index. Before v2 the composite PK (clip_id,
    // frame_index) caused the second INSERT to fail and the
    // whole clip's waypoints were rolled back. Now both rows
    // must persist (the synthetic `id` PK disambiguates).
    let mut store = Store::open_in_memory().unwrap();
    let msg1 = SeiMessage {
        latitude_deg: 1.0,
        longitude_deg: 2.0,
        ..SeiMessage::default()
    };
    let msg2 = SeiMessage {
        latitude_deg: 1.0,
        longitude_deg: 2.000_01,
        ..SeiMessage::default()
    };
    store
        .record_clip(
            Bucket::Recent,
            Path::new("RecentClips/dup.mp4"),
            &walk(None, vec![wp(7, 100.0, msg1), wp(7, 100.0, msg2)]),
        )
        .unwrap();
    let n: i64 = store
        .conn
        .query_row(
            "SELECT COUNT(*) FROM waypoints WHERE frame_index = 7",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(n, 2);
}

#[test]
fn migration_from_v1_preserves_existing_waypoints() {
    // Stand up a v1-shaped DB (composite PK on waypoints,
    // 7 columns) with real rows, then replay the v1->v2
    // migration SQL and assert: existing rows survive
    // verbatim, the new columns exist and default to NULL,
    // and `frame_index` collisions are now allowed.
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
         CREATE TABLE clips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            relative_path TEXT NOT NULL UNIQUE,
            bucket TEXT NOT NULL,
            clip_started_utc INTEGER,
            indexed_at_utc INTEGER NOT NULL,
            waypoint_count INTEGER NOT NULL DEFAULT 0,
            gps_waypoint_count INTEGER NOT NULL DEFAULT 0
         );
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
         INSERT INTO clips (relative_path, bucket, indexed_at_utc)
            VALUES ('a.mp4', 'recent', 100);
         INSERT INTO waypoints (clip_id, frame_index, timestamp_ms,
                                latitude_deg, longitude_deg, speed_mps, heading_deg)
            VALUES (1, 5, 33.3, 1.0, 2.0, 12.0, 90.0);",
    )
    .unwrap();
    conn.execute_batch(super::schema::MIGRATIONS[1]).unwrap();
    let (clip_id, frame, lat): (i64, i64, f64) = conn
        .query_row(
            "SELECT clip_id, frame_index, latitude_deg FROM waypoints",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(clip_id, 1);
    assert_eq!(frame, 5);
    assert_eq!(lat, 1.0);
    let acc_x: Option<f64> = conn
        .query_row("SELECT acceleration_x FROM waypoints", [], |r| r.get(0))
        .unwrap();
    assert!(acc_x.is_none());
    // Two rows with the same (clip_id, frame_index) are now legal.
    conn.execute(
        "INSERT INTO waypoints (clip_id, frame_index, timestamp_ms,
            latitude_deg, longitude_deg, speed_mps, heading_deg)
         VALUES (1, 5, 33.3, 1.0, 2.0, 12.0, 90.0)",
        params![],
    )
    .unwrap();
    let n: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM waypoints WHERE frame_index = 5",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(n, 2);
}

#[test]
fn migration_v3_creates_trips_events_and_clip_trip_map() {
    let store = Store::open_in_memory().unwrap();
    assert_eq!(store.schema_version().unwrap(), CURRENT_SCHEMA_VERSION);
    for table in ["trips", "detected_events", "clip_trip_map"] {
        let n: i64 = store
            .conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master
                 WHERE type = 'table' AND name = ?1",
                params![table],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 1, "expected table {table} after v3 migration");
    }
    for index in [
        "trips_by_start_utc",
        "events_by_trip",
        "events_by_type_ts",
        "clip_trip_map_by_trip",
    ] {
        let n: i64 = store
            .conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master
                 WHERE type = 'index' AND name = ?1",
                params![index],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 1, "expected index {index} after v3 migration");
    }
    store
        .conn
        .execute_batch(
            "INSERT INTO clips (relative_path, bucket, indexed_at_utc)
                VALUES ('a.mp4', 'recent', 100);
             INSERT INTO trips (start_utc, end_utc, start_clip_id, end_clip_id)
                VALUES (100, 200, 1, 1);
             INSERT INTO clip_trip_map (clip_id, trip_id) VALUES (1, 1);
             INSERT INTO detected_events
                (trip_id, clip_id, event_type, severity, timestamp_utc)
                VALUES (1, 1, 'harsh-brake', 'warning', 150);",
        )
        .unwrap();
    store
        .conn
        .execute("DELETE FROM clips WHERE id = 1", [])
        .unwrap();
    for tbl in ["clip_trip_map", "trips", "detected_events"] {
        let n: i64 = store
            .conn
            .query_row(&format!("SELECT COUNT(*) FROM {tbl}"), [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 0, "clip delete should cascade {tbl}");
    }
}

#[test]
fn migration_v4_adds_description_and_frame_index_columns() {
    let store = Store::open_in_memory().unwrap();
    assert_eq!(store.schema_version().unwrap(), CURRENT_SCHEMA_VERSION);
    // Both columns must exist and be writable.
    store
        .conn
        .execute_batch(
            "INSERT INTO clips (relative_path, bucket, indexed_at_utc)
                VALUES ('a.mp4', 'recent', 100);
             INSERT INTO trips (start_utc, end_utc, start_clip_id, end_clip_id)
                VALUES (100, 200, 1, 1);
             INSERT INTO detected_events
                (trip_id, clip_id, event_type, severity, timestamp_utc,
                 description, frame_index)
                VALUES (1, 1, 'harsh_braking', 'warning', 150,
                        'Harsh braking detected (-4.50 m/s^2)', 42);",
        )
        .unwrap();
    let (desc, fi): (String, i64) = store
        .conn
        .query_row(
            "SELECT description, frame_index FROM detected_events",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(desc, "Harsh braking detected (-4.50 m/s^2)");
    assert_eq!(fi, 42);
}

fn clip_event_record(path: &str, event_dir: &str, timestamp_utc: i64) -> ClipEventRecord {
    ClipEventRecord {
        event_json_relative_path: Path::new(path).to_path_buf(),
        event_dir_relative_path: Path::new(event_dir).to_path_buf(),
        bucket: Bucket::Saved,
        metadata: ClipEventMetadata {
            timestamp_utc,
            est_lat: Some(42.5414),
            est_lon: Some(-83.1234),
            reason: Some("user_interaction_honk".to_string()),
            city: Some("Detroit".to_string()),
            camera: Some("front".to_string()),
        },
    }
}

#[test]
fn migration_v5_creates_clip_events_table() {
    let store = Store::open_in_memory().unwrap();
    assert_eq!(store.schema_version().unwrap(), CURRENT_SCHEMA_VERSION);
    let table = "clip_events";
    let n: i64 = store
        .conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master
             WHERE type = 'table' AND name = ?1",
            params![table],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(n, 1, "expected table {table} after v5 migration");
    for index in [
        "clip_events_by_timestamp",
        "clip_events_by_dir",
        "clip_events_by_primary_clip",
    ] {
        let n: i64 = store
            .conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master
                 WHERE type = 'index' AND name = ?1",
                params![index],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 1, "expected index {index} after v5 migration");
    }
}

#[test]
fn migration_from_v4_to_v5_preserves_existing_rows() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("v4.sqlite3");
    {
        let conn = Connection::open(&db).unwrap();
        conn.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        for sql in &super::schema::MIGRATIONS[..4] {
            conn.execute_batch(sql).unwrap();
        }
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?1, '4')",
            params![META_KEY_SCHEMA_VERSION],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO clips (relative_path, bucket, indexed_at_utc)
             VALUES ('SavedClips/event/a-front.mp4', 'saved', 100)",
            [],
        )
        .unwrap();
    }
    let store = Store::open(&db).unwrap();
    assert_eq!(store.schema_version().unwrap(), CURRENT_SCHEMA_VERSION);
    assert_eq!(store.clip_count().unwrap(), 1);
    assert_eq!(store.clip_event_count().unwrap(), 0);
}

#[test]
fn record_clip_event_persists_event_json_metadata() {
    let mut store = Store::open_in_memory().unwrap();
    store
        .record_clip_event(&clip_event_record(
            "SavedClips/2026-06-01_20-11-00/event.json",
            "SavedClips/2026-06-01_20-11-00",
            1_780_345_835,
        ))
        .unwrap();
    let row: (String, i64, f64, f64, String, String, String) = store
        .conn
        .query_row(
            "SELECT bucket, timestamp_utc, est_lat, est_lon, reason, city, camera
             FROM clip_events",
            [],
            |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                    r.get(6)?,
                ))
            },
        )
        .unwrap();
    assert_eq!(row.0, "saved");
    assert_eq!(row.1, 1_780_345_835);
    assert_eq!(row.2, 42.5414);
    assert_eq!(row.3, -83.1234);
    assert_eq!(row.4, "user_interaction_honk");
    assert_eq!(row.5, "Detroit");
    assert_eq!(row.6, "front");
}

#[test]
fn record_clip_event_links_to_existing_clip_in_event_dir() {
    let mut store = Store::open_in_memory().unwrap();
    let started = UNIX_EPOCH + Duration::from_secs(1_780_345_800);
    let clip_id = store
        .record_clip(
            Bucket::Saved,
            Path::new("SavedClips/2026-06-01_20-11-00/2026-06-01_20-10-00-front.mp4"),
            &walk(Some(started), vec![]),
        )
        .unwrap();
    store
        .record_clip_event(&clip_event_record(
            "SavedClips/2026-06-01_20-11-00/event.json",
            "SavedClips/2026-06-01_20-11-00",
            1_780_345_835,
        ))
        .unwrap();
    let linked: i64 = store
        .conn
        .query_row("SELECT primary_clip_id FROM clip_events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(linked, clip_id);
}

#[test]
fn record_clip_relinks_preexisting_clip_event() {
    let mut store = Store::open_in_memory().unwrap();
    store
        .record_clip_event(&clip_event_record(
            "SavedClips/2026-06-01_20-11-00/event.json",
            "SavedClips/2026-06-01_20-11-00",
            1_780_345_835,
        ))
        .unwrap();
    let started = UNIX_EPOCH + Duration::from_secs(1_780_345_800);
    let clip_id = store
        .record_clip(
            Bucket::Saved,
            Path::new("SavedClips/2026-06-01_20-11-00/2026-06-01_20-10-00-front.mp4"),
            &walk(Some(started), vec![]),
        )
        .unwrap();
    let linked: i64 = store
        .conn
        .query_row("SELECT primary_clip_id FROM clip_events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(linked, clip_id);
}

#[test]
fn clip_events_survive_materializer_rebuild_all() {
    let mut store = Store::open_in_memory().unwrap();
    store
        .record_clip_event(&clip_event_record(
            "SentryClips/2026-06-01_20-11-00/event.json",
            "SentryClips/2026-06-01_20-11-00",
            1_780_345_835,
        ))
        .unwrap();
    store
        .rebuild_trips_now(&crate::mapping_overrides::MappingOverrides::default())
        .unwrap();
    assert_eq!(store.clip_event_count().unwrap(), 1);
}
