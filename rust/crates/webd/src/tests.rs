//! Handler/unit tests over a seeded temporary catalog.
//!
//! The fixture is built with `indexd`'s authoritative migration ladder
//! (`indexd::db::apply_migrations`) so the tests bind to the as-built schema,
//! then seeded with a handful of rows. The connection used to build the fixture
//! is a plain (non-WAL) rollback-journal connection, so the resulting file is
//! trivially openable read-only afterwards. Requests are driven through the real
//! `axum` router via `tower`'s `oneshot`.
#![allow(
    clippy::unwrap_used,
    clippy::panic,
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::similar_names
)]

use axum::Router;
use axum::body::Body;
use axum::http::{Method, Request, StatusCode};
use http_body_util::BodyExt;
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use tempfile::TempDir;
use tower::ServiceExt;

use crate::gadget::{GadgetClient, TransportError};
use crate::{Catalog, MediaConfig, build_router, router_with_gadget};

/// A live fixture: a seeded catalog + its router. `_dir` keeps the temp files
/// alive for the duration of the test (handlers open the DB per request).
struct Fixture {
    _dir: TempDir,
    app: Router,
}

/// Build a seeded catalog and an app router over it.
fn fixture() -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    seed(&db_path);

    // A separate static dir with a placeholder index.html, so SPA-host tests
    // don't depend on the process working directory.
    let static_dir = dir.path().join("static");
    std::fs::create_dir_all(&static_dir).unwrap();
    std::fs::write(
        static_dir.join("index.html"),
        "<!doctype html><title>TeslaUSB</title><main>shell</main>",
    )
    .unwrap();

    let catalog = Catalog::open(&db_path).unwrap();
    let archive_dir = dir.path().join("archive");
    let cache_dir = dir.path().join("cache");
    std::fs::create_dir_all(&archive_dir).unwrap();
    std::fs::create_dir_all(&cache_dir).unwrap();
    let media = MediaConfig::new(archive_dir, cache_dir);
    // Read-only tests never issue a DELETE, so they never connect; point the
    // gadgetd socket at a path that does not exist.
    let gadget_sock = dir.path().join("gadgetd.sock");
    let app = build_router(catalog, static_dir, media, gadget_sock);
    Fixture { _dir: dir, app }
}

/// Seed the catalog with two clips, two trips, three events, and two prefs.
fn seed(path: &std::path::Path) {
    let mut conn = Connection::open(path).unwrap();
    conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
    indexd::db::apply_migrations(&mut conn).unwrap();

    conn.execute_batch(
        "INSERT INTO clips (id, canonical_key, started_at, ended_at, partition, folder_class, is_sentry, duration_s, availability, created_at, updated_at) VALUES
            (1, 'clip-1', 1000, 1200, 'p1', 'SavedClips', 0, 60.0, 'present', 0, 0),
            (2, 'clip-2', 2000, 2100, 'p1', 'SentryClips', 1, 30.0, 'present', 0, 0);
         INSERT INTO angles (id, clip_id, camera, file_ref, view_kind, offset_ms, duration_s, size_bytes) VALUES
            (1, 1, 'front', 'p1/clip-1/front.mp4', 'ro_usb', 0, 60.0, 1111),
            (2, 1, 'back',  'p1/clip-1/back.mp4',  'ro_usb', 0, 60.0, 2222),
            (3, 2, 'front', 'p1/clip-2/front.mp4', 'ro_usb', 0, 30.0, 3333);
         INSERT INTO prefs (key, value) VALUES
            ('speed_unit', 'mph'),
            ('map_provider', 'osm');",
    )
    .unwrap();

    // Trip 1: full bbox + a real indexd-encoded polyline blob.
    let blob = indexd::derive::encode_polyline(&[vec![(40.0, -75.1), (40.2, -74.9)]]);
    conn.execute(
        "INSERT INTO trips (id, day, started_at, ended_at, bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon, distance_m, point_count, polyline, created_at, updated_at)
         VALUES (1, '2024-01-01', 1000, 1200, 40.0, -75.1, 40.2, -74.9, 1234.5, 2, ?1, 0, 0)",
        params![blob],
    )
    .unwrap();

    // Trip 2: no bbox, no polyline, no distance.
    conn.execute_batch(
        "INSERT INTO trips (id, day, started_at, ended_at, distance_m, point_count, created_at, updated_at)
         VALUES (2, '2024-01-02', 5000, 5100, NULL, 0, 0, 0);
         INSERT INTO trip_points (trip_id, seq, t, lat, lon, speed, heading) VALUES
            (1, 0, 1000, 40.0, -75.1, 10.0, 90.0),
            (1, 1, 1100, 40.2, -74.9, 12.0, 95.0);
         INSERT INTO events (id, trip_id, clip_id, type, severity, t, lat, lon, front_frame_offset, front_frame_index, description, created_at) VALUES
            (1, 1, 1, 'harsh_braking', 2, 1050, 40.1, -75.0, 1500, 45, 'Harsh braking', 0),
            (2, 1, 1, 'sharp_turn',    2, 1080, 40.15, -74.95, 1800, 54, 'Sharp turn', 0),
            (3, NULL, 2, 'sentry',     1, 2000, NULL, NULL, NULL, NULL, 'Sentry event', 0);",
    )
    .unwrap();
}

/// Issue a GET and return `(status, parsed-json)`.
async fn get_json(app: &Router, uri: &str) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(Request::builder().uri(uri).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value)
}

/// Issue a GET and return `(status, content-type, body-as-string)`.
async fn get_raw(app: &Router, uri: &str) -> (StatusCode, String, String) {
    let resp = app
        .clone()
        .oneshot(Request::builder().uri(uri).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let content_type = resp
        .headers()
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default()
        .to_owned();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        content_type,
        String::from_utf8_lossy(&bytes).into_owned(),
    )
}

#[tokio::test]
async fn days_rolls_up_trip_and_event_counts() {
    let fx = fixture();
    let (status, body) = get_json(&fx.app, "/api/days").await;
    assert_eq!(status, StatusCode::OK);
    let days = body.as_array().unwrap();
    assert_eq!(days.len(), 2);
    // Ordered DESC by day, so 2024-01-02 first.
    let first = &days[0];
    assert_eq!(first["day"], "2024-01-02");
    assert_eq!(first["trip_count"], 1);
    assert_eq!(first["event_count"], 0);
    let jan1 = days.iter().find(|d| d["day"] == "2024-01-01").unwrap();
    assert_eq!(jan1["trip_count"], 1);
    assert_eq!(jan1["event_count"], 2);
    assert_eq!(jan1["distance_m"], 1234.5);
}

#[tokio::test]
async fn trips_list_decodes_polyline_and_bbox() {
    let fx = fixture();
    let (status, body) = get_json(&fx.app, "/api/trips").await;
    assert_eq!(status, StatusCode::OK);
    let trips = body.as_array().unwrap();
    assert_eq!(trips.len(), 2);

    let trip1 = trips.iter().find(|t| t["id"] == 1).unwrap();
    // bbox present, polyline decoded to one segment of two [lat,lon] points.
    assert_eq!(trip1["bbox"]["min_lat"], 40.0);
    assert_eq!(trip1["point_count"], 2);
    let segments = trip1["polyline"].as_array().unwrap();
    assert_eq!(segments.len(), 1);
    assert_eq!(segments[0].as_array().unwrap().len(), 2);
    assert_eq!(segments[0][0][0], 40.0);
    assert_eq!(segments[0][0][1], -75.1);

    let trip2 = trips.iter().find(|t| t["id"] == 2).unwrap();
    assert!(trip2["bbox"].is_null());
    assert!(trip2["distance_m"].is_null());
    assert_eq!(trip2["polyline"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn trips_filter_by_day() {
    let fx = fixture();
    let (status, body) = get_json(&fx.app, "/api/trips?day=2024-01-02").await;
    assert_eq!(status, StatusCode::OK);
    let trips = body.as_array().unwrap();
    assert_eq!(trips.len(), 1);
    assert_eq!(trips[0]["id"], 2);
}

#[tokio::test]
async fn trip_detail_includes_points_and_404s() {
    let fx = fixture();
    let (status, body) = get_json(&fx.app, "/api/trips/1").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["id"], 1);
    let points = body["points"].as_array().unwrap();
    assert_eq!(points.len(), 2);
    assert_eq!(points[0]["t"], 1000);
    assert_eq!(points[0]["speed"], 10.0);

    let (status, body) = get_json(&fx.app, "/api/trips/999").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["error"]["code"], "not_found");
}

#[tokio::test]
async fn events_cursor_paginate() {
    let fx = fixture();
    let (status, body) = get_json(&fx.app, "/api/events?limit=2").await;
    assert_eq!(status, StatusCode::OK);
    let items = body["items"].as_array().unwrap();
    assert_eq!(items.len(), 2);
    assert_eq!(body["limit"], 2);
    assert_eq!(body["next_cursor"], 2); // full page -> cursor = last id
    // Event field mapping.
    assert_eq!(items[0]["type"], "harsh_braking");
    assert_eq!(items[0]["front_frame_offset_ms"], 1500);
    assert_eq!(items[0]["front_frame_index"], 45);

    // Next page: one remaining event, cursor exhausted.
    let (status, body) = get_json(&fx.app, "/api/events?after=2&limit=2").await;
    assert_eq!(status, StatusCode::OK);
    let items = body["items"].as_array().unwrap();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["id"], 3);
    assert_eq!(items[0]["type"], "sentry");
    assert!(items[0]["trip_id"].is_null());
    assert!(body["next_cursor"].is_null());
}

#[tokio::test]
async fn events_filter_by_trip_and_reject_bad_params() {
    let fx = fixture();
    let (status, body) = get_json(&fx.app, "/api/events?trip=1").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["items"].as_array().unwrap().len(), 2);

    let (status, body) = get_json(&fx.app, "/api/events?limit=0").await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_limit");

    let (status, body) = get_json(&fx.app, "/api/events?after=-1").await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_after");
}

#[tokio::test]
async fn clips_list_with_angles_and_pagination() {
    let fx = fixture();
    let (status, body) = get_json(&fx.app, "/api/clips").await;
    assert_eq!(status, StatusCode::OK);
    let items = body["items"].as_array().unwrap();
    assert_eq!(items.len(), 2);

    let clip1 = &items[0];
    assert_eq!(clip1["id"], 1);
    assert_eq!(clip1["is_sentry"], false);
    let angles = clip1["angles"].as_array().unwrap();
    assert_eq!(angles.len(), 2);
    // Ordered by camera ASC: back, front.
    assert_eq!(angles[0]["camera"], "back");
    assert_eq!(angles[1]["camera"], "front");
    // view_kind passed through opaquely; indexd emits 'ro_usb' for live
    // car-volume clips (D1; indexd commit 6bd5ced) and 'archive' for Pi-side.
    assert_eq!(angles[0]["view_kind"], "ro_usb");

    let clip2 = &items[1];
    assert_eq!(clip2["is_sentry"], true);

    // Pagination: one per page -> cursor = first id.
    let (_, body) = get_json(&fx.app, "/api/clips?limit=1").await;
    assert_eq!(body["items"].as_array().unwrap().len(), 1);
    assert_eq!(body["next_cursor"], 1);

    // folder_class filter.
    let (_, body) = get_json(&fx.app, "/api/clips?folder_class=SentryClips").await;
    let items = body["items"].as_array().unwrap();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["id"], 2);
}

#[tokio::test]
async fn clip_detail_and_404() {
    let fx = fixture();
    let (status, body) = get_json(&fx.app, "/api/clips/2").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["id"], 2);
    assert_eq!(body["is_sentry"], true);
    assert_eq!(body["angles"].as_array().unwrap().len(), 1);

    let (status, _) = get_json(&fx.app, "/api/clips/999").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn analytics_aggregates() {
    let fx = fixture();
    let (status, body) = get_json(&fx.app, "/api/analytics").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["total_trips"], 2);
    assert_eq!(body["total_events"], 3);
    assert_eq!(body["total_distance_m"], 1234.5);
    assert_eq!(body["events_by_type"].as_array().unwrap().len(), 3);
    assert_eq!(body["trips_by_day"].as_array().unwrap().len(), 2);

    // Drive time: trip1 (1200-1000=200) + trip2 (5100-5000=100) = 300s.
    assert_eq!(body["total_drive_time_s"], 300);
    // Warnings/critical: two sev-2 events, one sev-1 → 2.
    assert_eq!(body["warning_event_count"], 2);
    // Speeds from trip_points (10.0, 12.0 m/s).
    assert_eq!(body["avg_speed_mps"], 11.0);
    assert_eq!(body["max_speed_mps"], 12.0);

    // Severity breakdown ascending: {1:1, 2:2}.
    let by_sev = body["events_by_severity"].as_array().unwrap();
    assert_eq!(by_sev.len(), 2);
    assert_eq!(by_sev[0]["severity"], 1);
    assert_eq!(by_sev[0]["count"], 1);
    assert_eq!(by_sev[1]["severity"], 2);
    assert_eq!(by_sev[1]["count"], 2);

    // Footage: 2 clips, 3 angle files, 1111+2222+3333 = 6666 bytes.
    let vs = &body["video_stats"];
    assert_eq!(vs["total_clips"], 2);
    assert_eq!(vs["total_files"], 3);
    assert_eq!(vs["total_bytes"], 6666);
    let by_class = vs["by_folder_class"].as_array().unwrap();
    assert_eq!(by_class.len(), 2);
    // Ordered by class name ASC: SavedClips, SentryClips.
    assert_eq!(by_class[0]["folder_class"], "SavedClips");
    assert_eq!(by_class[0]["clip_count"], 1);
    assert_eq!(by_class[0]["file_count"], 2);
    assert_eq!(by_class[0]["size_bytes"], 3333);
    assert_eq!(by_class[1]["folder_class"], "SentryClips");
    assert_eq!(by_class[1]["clip_count"], 1);
    assert_eq!(by_class[1]["file_count"], 1);
    assert_eq!(by_class[1]["size_bytes"], 3333);
}

#[tokio::test]
async fn settings_returns_raw_prefs() {
    let fx = fixture();
    let (status, body) = get_json(&fx.app, "/api/settings").await;
    assert_eq!(status, StatusCode::OK);
    let rows = body.as_array().unwrap();
    assert_eq!(rows.len(), 2);
    // Ordered by key ASC.
    assert_eq!(rows[0]["key"], "map_provider");
    assert_eq!(rows[0]["value"], "osm");
    assert_eq!(rows[1]["key"], "speed_unit");
}

#[tokio::test]
async fn spa_host_serves_index_and_falls_back() {
    let fx = fixture();
    let (status, content_type, body) = get_raw(&fx.app, "/").await;
    assert_eq!(status, StatusCode::OK);
    assert!(content_type.contains("text/html"));
    assert!(body.contains("TeslaUSB"));

    // SPA-fallback: an unknown non-API path serves index.html.
    let (status, _, body) = get_raw(&fx.app, "/trips/some/client/route").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.contains("shell"));
}

#[tokio::test]
async fn unknown_api_path_is_json_404_not_spa() {
    let fx = fixture();
    let (status, content_type, body) = get_raw(&fx.app, "/api/does-not-exist").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(content_type.contains("application/json"));
    assert!(body.contains("not_found"));
}

#[test]
fn catalog_connection_rejects_writes() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("catalog.db");
    seed(&path);
    let catalog = Catalog::open(&path).unwrap();
    let conn = catalog.connect().unwrap();
    let err = conn
        .execute("INSERT INTO prefs (key, value) VALUES ('x', 'y')", [])
        .unwrap_err();
    assert!(
        matches!(err, rusqlite::Error::SqliteFailure(_, _)),
        "expected a read-only write rejection, got {err:?}"
    );
}

#[test]
fn catalog_rejects_newer_schema() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("catalog.db");
    seed(&path);
    // Forge a future schema version.
    {
        let conn = Connection::open(&path).unwrap();
        conn.execute(
            "INSERT INTO schema_version (version, applied_at, note) VALUES (999, 0, 'future')",
            [],
        )
        .unwrap();
    }
    let err = Catalog::open(&path).unwrap_err();
    assert!(matches!(err, crate::CatalogError::SchemaTooNew { .. }));
}

// ─── Task 5.1b: archive streaming + export ───────────────────────────────

/// A live media fixture: a seeded catalog wired to a real on-disk archive root,
/// plus a router. `_dir` keeps the temp tree (catalog + archive + cache + a
/// secret file outside the jail) alive for the test.
struct MediaFixture {
    _dir: TempDir,
    app: Router,
}

/// Deterministic byte pattern so range slices can be asserted exactly.
fn pattern(len: usize) -> Vec<u8> {
    (0..len).map(|i| u8::try_from(i % 256).unwrap()).collect()
}

/// Build the media fixture: a 100-byte `front` + small `back` archive angle on
/// clip 10, a `ro_usb` angle (must 404), a `..`-traversal and an absolute-path
/// reinjection angle (both must 404), and a ~1 MiB clip for the streamed proof.
fn media_fixture() -> MediaFixture {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    let archive = dir.path().join("archive");
    let cache = dir.path().join("cache");
    std::fs::create_dir_all(archive.join("p1/clip-10")).unwrap();
    std::fs::create_dir_all(archive.join("p1/clip-13")).unwrap();
    std::fs::create_dir_all(archive.join("p1/clip-17")).unwrap();
    std::fs::create_dir_all(archive.join("p1/clip-18")).unwrap();
    std::fs::create_dir_all(&cache).unwrap();

    // Real archive files.
    std::fs::write(archive.join("p1/clip-10/front.mp4"), pattern(100)).unwrap();
    std::fs::write(archive.join("p1/clip-10/back.mp4"), pattern(40)).unwrap();
    // The ro_usb angle's file exists too, proving its 404 is about view_kind.
    std::fs::write(archive.join("p1/clip-10/left.mp4"), pattern(10)).unwrap();
    let big = 1024 * 1024 + 7; // > 4 * 256 KiB chunks, not a chunk multiple.
    std::fs::write(archive.join("p1/clip-13/front.mp4"), pattern(big)).unwrap();
    // Hostile camera name maps to a real file (zip-entry sanitization test).
    std::fs::write(archive.join("p1/clip-17/cam.mp4"), pattern(20)).unwrap();
    // A 0-byte archive file (empty-file range handling).
    std::fs::write(archive.join("p1/clip-18/empty.mp4"), pattern(0)).unwrap();
    // clip 16's archive file is intentionally NOT created (missing-on-disk).

    // A secret file OUTSIDE the archive root, targeted by the escape angles.
    let secret = dir.path().join("secret.mp4");
    std::fs::write(&secret, b"top secret").unwrap();
    let secret_abs = std::fs::canonicalize(&secret).unwrap();

    // A symlink INSIDE the archive root pointing OUTSIDE it: proves the
    // canonicalize + starts_with branch (not just syntactic `..` rejection).
    #[cfg(unix)]
    {
        std::fs::create_dir_all(archive.join("p1/clip-19")).unwrap();
        std::os::unix::fs::symlink(&secret, archive.join("p1/clip-19/evil.mp4")).unwrap();
    }

    let static_dir = dir.path().join("static");
    std::fs::create_dir_all(&static_dir).unwrap();
    std::fs::write(static_dir.join("index.html"), "<!doctype html>shell").unwrap();

    seed_media(&db_path, big, secret_abs.to_str().unwrap());

    let catalog = Catalog::open(&db_path).unwrap();
    let media = MediaConfig::new(archive, cache);
    let gadget_sock = dir.path().join("gadgetd.sock");
    let app = build_router(catalog, static_dir, media, gadget_sock);
    MediaFixture { _dir: dir, app }
}

fn seed_media(path: &std::path::Path, big: usize, secret_abs: &str) {
    let mut conn = Connection::open(path).unwrap();
    conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
    indexd::db::apply_migrations(&mut conn).unwrap();

    // All clips first (FK parents), then all angles.
    conn.execute_batch(
        "INSERT INTO clips (id, canonical_key, started_at, ended_at, partition, folder_class, is_sentry, duration_s, availability, created_at, updated_at) VALUES
            (10, 'clip-10', 1000, 1200, 'p1', 'SavedClips', 0, 60.0, 'present', 0, 0),
            (11, 'clip-11', 1000, 1200, 'p1', 'SavedClips', 0, 60.0, 'present', 0, 0),
            (12, 'clip-12', 1000, 1200, 'p1', 'SavedClips', 0, 60.0, 'present', 0, 0),
            (13, 'clip-13', 1000, 1200, 'p1', 'SavedClips', 0, 60.0, 'present', 0, 0),
            (15, 'clip-15', 1000, 1200, 'p1', 'SavedClips', 0, 60.0, 'present', 0, 0),
            (16, 'clip-16', 1000, 1200, 'p1', 'SavedClips', 0, 60.0, 'present', 0, 0),
            (17, 'clip-17', 1000, 1200, 'p1', 'SavedClips', 0, 60.0, 'present', 0, 0),
            (18, 'clip-18', 1000, 1200, 'p1', 'SavedClips', 0, 60.0, 'present', 0, 0),
            (19, 'clip-19', 1000, 1200, 'p1', 'SavedClips', 0, 60.0, 'present', 0, 0);
         INSERT INTO angles (id, clip_id, camera, file_ref, view_kind, offset_ms, duration_s, size_bytes) VALUES
            (10, 10, 'front', 'p1/clip-10/front.mp4', 'archive', 0, 60.0, 100),
            (11, 10, 'back',  'p1/clip-10/back.mp4',  'archive', 0, 60.0, 40),
            (12, 10, 'left_repeater', 'p1/clip-10/left.mp4', 'ro_usb', 0, 60.0, 10),
            (13, 11, 'front', '../secret.mp4', 'archive', 0, 60.0, 10),
            (15, 13, 'front', 'p1/clip-13/front.mp4', 'archive', 0, 60.0, 0),
            (16, 15, 'left_repeater', 'p1/clip-10/left.mp4', 'ro_usb', 0, 60.0, 10),
            (17, 16, 'front', 'p1/clip-16/missing.mp4', 'archive', 0, 60.0, 10),
            (19, 18, 'front', 'p1/clip-18/empty.mp4', 'archive', 0, 0.0, 999),
            (20, 19, 'front', 'p1/clip-19/evil.mp4', 'archive', 0, 60.0, 10);",
    )
    .unwrap();
    // Absolute-path reinjection angle (path is platform-specific).
    conn.execute(
        "INSERT INTO angles (id, clip_id, camera, file_ref, view_kind, offset_ms, duration_s, size_bytes)
         VALUES (14, 12, 'front', ?1, 'archive', 0, 60.0, 10)",
        params![secret_abs],
    )
    .unwrap();
    // Hostile camera name on a real archive file (zip-entry sanitization).
    conn.execute(
        "INSERT INTO angles (id, clip_id, camera, file_ref, view_kind, offset_ms, duration_s, size_bytes)
         VALUES (18, 17, ?1, 'p1/clip-17/cam.mp4', 'archive', 0, 60.0, 20)",
        params!["../bad\"\r\nname"],
    )
    .unwrap();
    // size_bytes on clips 13/18 is deliberately stale; the handler must stat the
    // real file instead of trusting the column.
    let _ = big;
}

/// Drive a request and return `(status, headers, body-bytes)`.
async fn request(
    app: &Router,
    method: Method,
    uri: &str,
    range: Option<&str>,
) -> (StatusCode, axum::http::HeaderMap, Vec<u8>) {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(range) = range {
        builder = builder.header("range", range);
    }
    let resp = app
        .clone()
        .oneshot(builder.body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let headers = resp.headers().clone();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap()
        .to_vec();
    (status, headers, bytes)
}

fn header<'a>(headers: &'a axum::http::HeaderMap, name: &str) -> Option<&'a str> {
    headers.get(name).and_then(|v| v.to_str().ok())
}

#[tokio::test]
async fn stream_full_body_sets_accept_ranges_and_length() {
    let fx = media_fixture();
    let (status, headers, body) = request(&fx.app, Method::GET, "/api/clips/10/stream", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(header(&headers, "accept-ranges"), Some("bytes"));
    assert_eq!(header(&headers, "content-type"), Some("video/mp4"));
    assert_eq!(header(&headers, "content-length"), Some("100"));
    assert!(headers.get("content-range").is_none());
    assert_eq!(body, pattern(100));
}

#[tokio::test]
async fn stream_defaults_to_front_camera() {
    let fx = media_fixture();
    // No ?camera= → front; same bytes as the explicit front angle.
    let (status, _, body) = request(&fx.app, Method::GET, "/api/clips/10/stream", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body.len(), 100);
}

#[tokio::test]
async fn stream_open_ended_range_is_206() {
    let fx = media_fixture();
    let (status, headers, body) = request(
        &fx.app,
        Method::GET,
        "/api/clips/10/stream",
        Some("bytes=0-"),
    )
    .await;
    assert_eq!(status, StatusCode::PARTIAL_CONTENT);
    assert_eq!(header(&headers, "content-range"), Some("bytes 0-99/100"));
    assert_eq!(header(&headers, "content-length"), Some("100"));
    assert_eq!(header(&headers, "accept-ranges"), Some("bytes"));
    assert_eq!(body, pattern(100));
}

#[tokio::test]
async fn stream_mid_range_returns_exact_slice() {
    let fx = media_fixture();
    let (status, headers, body) = request(
        &fx.app,
        Method::GET,
        "/api/clips/10/stream?camera=front",
        Some("bytes=10-19"),
    )
    .await;
    assert_eq!(status, StatusCode::PARTIAL_CONTENT);
    assert_eq!(header(&headers, "content-range"), Some("bytes 10-19/100"));
    assert_eq!(header(&headers, "content-length"), Some("10"));
    assert_eq!(body, pattern(100)[10..20]);
}

#[tokio::test]
async fn stream_suffix_and_open_start_ranges() {
    let fx = media_fixture();
    let (status, headers, body) = request(
        &fx.app,
        Method::GET,
        "/api/clips/10/stream",
        Some("bytes=-10"),
    )
    .await;
    assert_eq!(status, StatusCode::PARTIAL_CONTENT);
    assert_eq!(header(&headers, "content-range"), Some("bytes 90-99/100"));
    assert_eq!(body, pattern(100)[90..100]);

    let (status, headers, body) = request(
        &fx.app,
        Method::GET,
        "/api/clips/10/stream",
        Some("bytes=50-"),
    )
    .await;
    assert_eq!(status, StatusCode::PARTIAL_CONTENT);
    assert_eq!(header(&headers, "content-range"), Some("bytes 50-99/100"));
    assert_eq!(body, pattern(100)[50..100]);
}

#[tokio::test]
async fn stream_unsatisfiable_range_is_416() {
    let fx = media_fixture();
    let (status, headers, _) = request(
        &fx.app,
        Method::GET,
        "/api/clips/10/stream",
        Some("bytes=200-300"),
    )
    .await;
    assert_eq!(status, StatusCode::RANGE_NOT_SATISFIABLE);
    assert_eq!(header(&headers, "content-range"), Some("bytes */100"));
}

#[tokio::test]
async fn stream_head_has_headers_no_body() {
    let fx = media_fixture();
    let (status, headers, body) =
        request(&fx.app, Method::HEAD, "/api/clips/10/stream", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(header(&headers, "content-length"), Some("100"));
    assert!(body.is_empty());

    let (status, headers, body) = request(
        &fx.app,
        Method::HEAD,
        "/api/clips/10/stream",
        Some("bytes=10-19"),
    )
    .await;
    assert_eq!(status, StatusCode::PARTIAL_CONTENT);
    assert_eq!(header(&headers, "content-range"), Some("bytes 10-19/100"));
    assert!(body.is_empty());
}

#[tokio::test]
async fn stream_404s_for_missing_clip_camera_and_ro_usb() {
    let fx = media_fixture();
    // Missing clip.
    let (status, _, _) = request(&fx.app, Method::GET, "/api/clips/999/stream", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    // Missing camera on an existing clip.
    let (status, _, _) = request(
        &fx.app,
        Method::GET,
        "/api/clips/10/stream?camera=nonexistent",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    // ro_usb angle is never streamable from the archive endpoint.
    let (status, _, _) = request(
        &fx.app,
        Method::GET,
        "/api/clips/10/stream?camera=left_repeater",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn stream_rejects_path_traversal_and_absolute_reinjection() {
    let fx = media_fixture();
    // file_ref = '../secret.mp4' (clip 11) → escapes jail → 404.
    let (status, _, body) = request(&fx.app, Method::GET, "/api/clips/11/stream", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(
        !body.windows(3).any(|w| w == b"top"),
        "must not leak secret"
    );
    // file_ref = absolute path to the secret (clip 12) → 404.
    let (status, _, _) = request(&fx.app, Method::GET, "/api/clips/12/stream", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn stream_large_file_is_chunked_not_buffered() {
    let fx = media_fixture();
    let big = 1024 * 1024 + 7;
    let resp = fx
        .app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/clips/13/stream")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(
        header(resp.headers(), "content-length"),
        Some(big.to_string().as_str())
    );

    // Pull the body frame-by-frame: a buffered body would arrive as one frame.
    // A streamed body arrives as many bounded (<=256 KiB) chunks.
    let mut body = resp.into_body();
    let mut frames = 0usize;
    let mut total = 0usize;
    let chunk_cap = 256 * 1024;
    while let Some(frame) = body.frame().await {
        let frame = frame.unwrap();
        if let Some(data) = frame.data_ref() {
            frames += 1;
            total += data.len();
            assert!(
                data.len() <= chunk_cap,
                "frame {} exceeds chunk cap",
                data.len()
            );
        }
    }
    assert_eq!(total, big);
    assert!(frames > 1, "expected multiple frames, got {frames}");
}

#[tokio::test]
async fn export_streams_zip_of_archive_angles() {
    let fx = media_fixture();
    let (status, headers, body) = request(&fx.app, Method::GET, "/api/clips/10/export", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(header(&headers, "content-type"), Some("application/zip"));
    assert_eq!(
        header(&headers, "content-disposition"),
        Some("attachment; filename=\"clip-10.zip\"")
    );

    let mut zip = zip::ZipArchive::new(std::io::Cursor::new(body)).unwrap();
    // Only the two archive angles (front, back) — not the ro_usb left_repeater.
    let mut names: Vec<String> = (0..zip.len())
        .map(|i| zip.by_index(i).unwrap().name().to_owned())
        .collect();
    names.sort();
    assert_eq!(names, vec!["back.mp4".to_owned(), "front.mp4".to_owned()]);

    let mut buf = Vec::new();
    std::io::Read::read_to_end(&mut zip.by_name("front.mp4").unwrap(), &mut buf).unwrap();
    assert_eq!(buf, pattern(100));
}

#[tokio::test]
async fn export_head_does_not_build_body() {
    let fx = media_fixture();
    let (status, headers, body) =
        request(&fx.app, Method::HEAD, "/api/clips/10/export", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(header(&headers, "content-type"), Some("application/zip"));
    assert_eq!(
        header(&headers, "content-disposition"),
        Some("attachment; filename=\"clip-10.zip\"")
    );
    assert!(body.is_empty());
}

#[tokio::test]
async fn export_404s_for_clip_without_archive_angles() {
    let fx = media_fixture();
    let (status, _, _) = request(&fx.app, Method::GET, "/api/clips/999/export", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn download_single_angle_sets_attachment() {
    let fx = media_fixture();
    let (status, headers, body) = request(
        &fx.app,
        Method::GET,
        "/api/clips/10/angles/front/download",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(header(&headers, "content-type"), Some("video/mp4"));
    assert_eq!(
        header(&headers, "content-disposition"),
        Some("attachment; filename=\"clip-10-front.mp4\"")
    );
    assert_eq!(body, pattern(100));

    // ro_usb angle is not downloadable from the archive endpoint.
    let (status, _, _) = request(
        &fx.app,
        Method::GET,
        "/api/clips/10/angles/left_repeater/download",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

// ─── 5.1b second-pass coverage (rubber-duck-driven edge/security cases) ───

#[tokio::test]
async fn stream_404s_when_archive_file_is_missing_on_disk() {
    let fx = media_fixture();
    // clip 16's angle row exists but its file was never written → Resolved::Missing.
    let (status, _, _) = request(&fx.app, Method::GET, "/api/clips/16/stream", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn export_and_head_404_when_only_archive_file_is_missing() {
    let fx = media_fixture();
    // clip 16 has exactly one archive angle and its file is missing → no
    // resolvable entries → both GET and HEAD must 404 (the HEAD/GET-consistency
    // guarantee: HEAD must not 200 where GET would 404).
    let (status, _, _) = request(&fx.app, Method::GET, "/api/clips/16/export", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let (status, _, _) = request(&fx.app, Method::HEAD, "/api/clips/16/export", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn export_404s_for_ro_usb_only_clip() {
    let fx = media_fixture();
    // clip 15 has a single ro_usb angle → no archive angles → 404.
    let (status, _, _) = request(&fx.app, Method::GET, "/api/clips/15/export", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn export_sanitizes_hostile_camera_in_zip_entry_name() {
    let fx = media_fixture();
    // clip 17's angle camera is `../bad"\r\nname` mapped to a real file.
    let (status, _, body) = request(&fx.app, Method::GET, "/api/clips/17/export", None).await;
    assert_eq!(status, StatusCode::OK);
    let mut zip = zip::ZipArchive::new(std::io::Cursor::new(body)).unwrap();
    assert_eq!(zip.len(), 1);
    let name = zip.by_index(0).unwrap().name().to_owned();
    assert!(
        !name.contains('/')
            && !name.contains('\\')
            && !name.contains('"')
            && !name.contains('\r')
            && !name.contains('\n')
            && !name.contains(".."),
        "sanitized zip entry leaked separators/control chars: {name:?}"
    );
    assert!(
        std::path::Path::new(&name)
            .extension()
            .is_some_and(|ext| ext.eq_ignore_ascii_case("mp4")),
        "entry should keep an .mp4 suffix: {name:?}"
    );
}

#[tokio::test]
async fn export_includes_present_archive_entries_only() {
    let fx = media_fixture();
    // clip 10 has two present archive angles plus one ro_usb angle that is
    // skipped by view_kind → exactly two entries, nothing spurious.
    let (status, _, body) = request(&fx.app, Method::GET, "/api/clips/10/export", None).await;
    assert_eq!(status, StatusCode::OK);
    let zip = zip::ZipArchive::new(std::io::Cursor::new(body)).unwrap();
    assert_eq!(zip.len(), 2);
}

#[tokio::test]
async fn stream_empty_file_full_and_range() {
    let fx = media_fixture();
    // 0-byte archive file: no-range GET → 200 with Content-Length 0, empty body.
    let (status, headers, body) = request(&fx.app, Method::GET, "/api/clips/18/stream", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(header(&headers, "content-length"), Some("0"));
    assert_eq!(header(&headers, "accept-ranges"), Some("bytes"));
    assert!(body.is_empty());

    // Any range against a 0-byte file is unsatisfiable → 416 `bytes */0`.
    let (status, headers, _) = request(
        &fx.app,
        Method::GET,
        "/api/clips/18/stream",
        Some("bytes=0-0"),
    )
    .await;
    assert_eq!(status, StatusCode::RANGE_NOT_SATISFIABLE);
    assert_eq!(header(&headers, "content-range"), Some("bytes */0"));
}

#[tokio::test]
async fn stream_single_byte_range_is_one_byte() {
    let fx = media_fixture();
    let (status, headers, body) = request(
        &fx.app,
        Method::GET,
        "/api/clips/10/stream",
        Some("bytes=0-0"),
    )
    .await;
    assert_eq!(status, StatusCode::PARTIAL_CONTENT);
    assert_eq!(header(&headers, "content-range"), Some("bytes 0-0/100"));
    assert_eq!(header(&headers, "content-length"), Some("1"));
    assert_eq!(body, pattern(100)[0..1]);
}

#[tokio::test]
async fn stream_end_past_eof_clamps_to_last_byte() {
    let fx = media_fixture();
    // end (500) past EOF clamps to total-1 (99) → 206, not 416.
    let (status, headers, body) = request(
        &fx.app,
        Method::GET,
        "/api/clips/10/stream",
        Some("bytes=90-500"),
    )
    .await;
    assert_eq!(status, StatusCode::PARTIAL_CONTENT);
    assert_eq!(header(&headers, "content-range"), Some("bytes 90-99/100"));
    assert_eq!(header(&headers, "content-length"), Some("10"));
    assert_eq!(body, pattern(100)[90..100]);
}

#[tokio::test]
async fn stream_multiple_range_headers_is_416() {
    let fx = media_fixture();
    // Two Range headers (a multi-range / ambiguous request) → unsatisfiable.
    let resp = fx
        .app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/clips/10/stream")
                .header("range", "bytes=0-10")
                .header("range", "bytes=20-30")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::RANGE_NOT_SATISFIABLE);
    assert_eq!(header(resp.headers(), "content-range"), Some("bytes */100"));
}

#[cfg(unix)]
#[tokio::test]
async fn stream_rejects_symlink_escape_via_canonicalize() {
    let fx = media_fixture();
    // clip 19's file_ref is a symlink INSIDE the root pointing OUTSIDE it.
    // Syntactic `..` checks would miss this; only canonicalize + starts_with
    // catches it → 404, no secret bytes leaked.
    let (status, _, body) = request(&fx.app, Method::GET, "/api/clips/19/stream", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(
        !body.windows(3).any(|w| w == b"top"),
        "symlink escape must not leak secret bytes"
    );
    // And the same angle must not be downloadable or exportable.
    let (status, _, _) = request(
        &fx.app,
        Method::GET,
        "/api/clips/19/angles/front/download",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let (status, _, _) = request(&fx.app, Method::GET, "/api/clips/19/export", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

// ---------------------------------------------------------------------------
// Car-delete handoff route (`DELETE /api/clips/:id?target=car`,
// `GET /api/handoff/:id`). The gadgetd socket is mocked: these tests cover the
// route's validation, gadgetd request shape, and outcome→HTTP mapping without a
// real daemon. The live destructive delete is an operator-gated hardware test.
// ---------------------------------------------------------------------------

/// What the mock gadgetd returns for a call.
enum Reply {
    /// A canned JSON response value.
    Json(Value),
    /// A transport "unavailable" error (socket down/absent).
    Unavailable,
}

/// A mock [`GadgetClient`] that records the last request and returns a canned
/// reply, so the route's request-building and response-mapping are exercised
/// without a real Unix socket.
struct MockGadget {
    reply: Reply,
    last: Arc<Mutex<Option<Value>>>,
}

impl GadgetClient for MockGadget {
    fn call(&self, request: Value) -> Result<Value, TransportError> {
        *self.last.lock().unwrap() = Some(request);
        match &self.reply {
            Reply::Json(v) => Ok(v.clone()),
            Reply::Unavailable => Err(TransportError::Unavailable("socket down".to_owned())),
        }
    }
}

/// A delete fixture: a catalog seeded with car-deletable clips plus a router
/// wired to a mock gadgetd. `last` captures the request sent to gadgetd.
struct DeleteFixture {
    _dir: TempDir,
    app: Router,
    last: Arc<Mutex<Option<Value>>>,
}

const EVENT: &str = "2026-06-01_20-10-04";
const EVENT2: &str = "2026-06-01_20-11-04";

fn delete_fixture(reply: Reply) -> DeleteFixture {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    seed_car_clips(&db_path);

    let static_dir = dir.path().join("static");
    std::fs::create_dir_all(&static_dir).unwrap();
    std::fs::write(static_dir.join("index.html"), "<!doctype html>shell").unwrap();

    let catalog = Catalog::open(&db_path).unwrap();
    let media = MediaConfig::new(dir.path().join("archive"), dir.path().join("cache"));
    let last = Arc::new(Mutex::new(None));
    let gadget: Arc<dyn GadgetClient> = Arc::new(MockGadget {
        reply,
        last: Arc::clone(&last),
    });
    let app = router_with_gadget(catalog, static_dir, media, gadget);
    DeleteFixture {
        _dir: dir,
        app,
        last,
    }
}

/// Seed clips with the real `slot0` / `TeslaCam/...` encoding the planner needs.
fn seed_car_clips(path: &std::path::Path) {
    let mut conn = Connection::open(path).unwrap();
    conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
    indexd::db::apply_migrations(&mut conn).unwrap();

    // clip 10: a well-formed car-deletable SavedClips minute with two ro_usb
    //   angles whose file_refs match the canonical_key-derived paths.
    // clip 11: same shape but on slot1 (media) → planner refuses (not car).
    // clip 12: ro_usb angle whose file_ref escapes the clip → planner refuses.
    let key = format!("0:TeslaCam/SavedClips/{EVENT}/{EVENT}");
    let key1 = format!("1:TeslaCam/SavedClips/{EVENT}/{EVENT}");
    let key12 = format!("0:TeslaCam/SavedClips/{EVENT2}/{EVENT2}");
    conn.execute_batch(&format!(
        "INSERT INTO clips (id, canonical_key, started_at, ended_at, partition, folder_class, is_sentry, duration_s, availability, created_at, updated_at) VALUES
            (10, '{key}', 1000, 1060, 'slot0', 'SavedClips', 0, 60.0, 'present', 0, 0),
            (11, '{key1}', 1000, 1060, 'slot1', 'SavedClips', 0, 60.0, 'present', 0, 0),
            (12, '{key12}', 1000, 1060, 'slot0', 'SavedClips', 0, 60.0, 'present', 0, 0);
         INSERT INTO angles (id, clip_id, camera, file_ref, view_kind, offset_ms, duration_s, size_bytes) VALUES
            (1, 10, 'back',  'TeslaCam/SavedClips/{EVENT}/{EVENT}-back.mp4',  'ro_usb', 0, 60.0, 1),
            (2, 10, 'front', 'TeslaCam/SavedClips/{EVENT}/{EVENT}-front.mp4', 'ro_usb', 0, 60.0, 2),
            (3, 11, 'front', 'TeslaCam/SavedClips/{EVENT}/{EVENT}-front.mp4', 'ro_usb', 0, 60.0, 2),
            (4, 12, 'front', 'TeslaCam/SavedClips/{EVENT2}/2026-06-01_20-09-04-front.mp4', 'ro_usb', 0, 60.0, 2);"
    ))
    .unwrap();
}

/// Issue a DELETE and return `(status, parsed-json)`.
async fn delete_json(app: &Router, uri: &str) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::DELETE)
                .uri(uri)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value)
}

#[tokio::test]
async fn delete_car_clip_happy_path_sends_one_handoff() {
    let fx = delete_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "result": "done" }),
    ));
    let (status, body) = delete_json(&fx.app, "/api/clips/10?target=car").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["state"], "done");
    assert_eq!(body["handoff_id"], "h-1");

    // gadgetd saw ONE request: partition 1, op delete_paths, both derived files.
    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "request_mutation");
    assert_eq!(req["partition"], 1);
    assert_eq!(req["mutation"]["op"], "delete_paths");
    let paths = req["mutation"]["rel_paths"].as_array().unwrap();
    assert_eq!(paths.len(), 2);
    assert_eq!(
        paths[0],
        format!("TeslaCam/SavedClips/{EVENT}/{EVENT}-back.mp4")
    );
    assert_eq!(
        paths[1],
        format!("TeslaCam/SavedClips/{EVENT}/{EVENT}-front.mp4")
    );
}

#[tokio::test]
async fn delete_requires_explicit_target() {
    let fx = delete_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "result": "done" }),
    ));
    let (status, body) = delete_json(&fx.app, "/api/clips/10").await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "target_required");
    // No destructive default: gadgetd must NOT have been contacted.
    assert!(fx.last.lock().unwrap().is_none());
}

#[tokio::test]
async fn delete_archive_target_is_not_implemented() {
    let fx = delete_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "result": "done" }),
    ));
    let (status, body) = delete_json(&fx.app, "/api/clips/10?target=archive").await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED);
    assert_eq!(body["error"]["code"], "not_implemented");
    assert!(fx.last.lock().unwrap().is_none());
}

#[tokio::test]
async fn delete_unknown_clip_is_404() {
    let fx = delete_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "result": "done" }),
    ));
    let (status, _) = delete_json(&fx.app, "/api/clips/999?target=car").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(fx.last.lock().unwrap().is_none());
}

#[tokio::test]
async fn delete_non_car_partition_is_refused_before_handoff() {
    let fx = delete_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "result": "done" }),
    ));
    let (status, body) = delete_json(&fx.app, "/api/clips/11?target=car").await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "not_car_deletable");
    assert!(fx.last.lock().unwrap().is_none());
}

#[tokio::test]
async fn delete_with_escaping_file_ref_is_refused_before_handoff() {
    let fx = delete_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "result": "done" }),
    ));
    let (status, body) = delete_json(&fx.app, "/api/clips/12?target=car").await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "invalid_clip");
    assert!(fx.last.lock().unwrap().is_none());
}

#[tokio::test]
async fn delete_busy_handoff_is_409() {
    let fx = delete_fixture(Reply::Json(json!({ "refused": "handoff_active" })));
    let (status, body) = delete_json(&fx.app, "/api/clips/10?target=car").await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["code"], "handoff_busy");
}

#[tokio::test]
async fn delete_save_active_is_409() {
    // Car mid-save: a transient, retryable refusal (contract §2.3 → 409),
    // NOT a 422 validation refusal.
    let fx = delete_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "refused": "save_active" }),
    ));
    let (status, body) = delete_json(&fx.app, "/api/clips/10?target=car").await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["code"], "handoff_busy");
}

#[tokio::test]
async fn delete_gadgetd_unavailable_is_503() {
    let fx = delete_fixture(Reply::Unavailable);
    let (status, body) = delete_json(&fx.app, "/api/clips/10?target=car").await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"]["code"], "gadgetd_unavailable");
}

#[tokio::test]
async fn delete_critical_fault_is_500() {
    let fx = delete_fixture(Reply::Json(
        json!({ "handoff_id": "h-9", "result": "critical_fault", "detail": "stuck mount" }),
    ));
    let (status, body) = delete_json(&fx.app, "/api/clips/10?target=car").await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
    assert_eq!(body["error"]["code"], "critical_fault");
}

#[tokio::test]
async fn handoff_status_normalizes_in_flight_phase() {
    let fx = delete_fixture(Reply::Json(
        json!({ "handoff_id": "h-3", "phase": "applying", "result": null }),
    ));
    let (status, body) = get_json(&fx.app, "/api/handoff/h-3").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["state"], "applying");
    assert_eq!(body["handoff_id"], "h-3");
}

#[tokio::test]
async fn handoff_status_unknown_id_is_404() {
    let fx = delete_fixture(Reply::Json(json!({ "error": "unknown handoff_id: h-x" })));
    let (status, _) = get_json(&fx.app, "/api/handoff/h-x").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn gadget_status_maps_live_state() {
    let fx = delete_fixture(Reply::Json(json!({
        "present": true,
        "bound": true,
        "bound_udc": "fe980000.usb",
        "udc_state": "configured",
        "lun_file": "/data/teslausb/cam.img",
        "media_lun_file": "/data/teslausb/media.img",
        "handoff_active": false,
        "last_result": "done",
        "last_handoff_id": "h-9",
    })));
    let (status, body) = get_json(&fx.app, "/api/gadget/status").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["present"], true);
    assert_eq!(body["bound"], true);
    assert_eq!(body["udc_state"], "configured");
    assert_eq!(body["media_lun_file"], "/data/teslausb/media.img");
    assert_eq!(body["handoff_active"], false);
    assert_eq!(body["last_handoff_id"], "h-9");
    // The route must issue exactly the read-only gadget_status command.
    let sent = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(sent["cmd"], "gadget_status");
}

#[tokio::test]
async fn gadget_status_degrades_partial_reply() {
    // A reply with only `present` must not 500; missing fields read as null/false.
    let fx = delete_fixture(Reply::Json(json!({ "present": false })));
    let (status, body) = get_json(&fx.app, "/api/gadget/status").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["present"], false);
    assert_eq!(body["bound"], false);
    assert_eq!(body["udc_state"], Value::Null);
    assert_eq!(body["handoff_active"], false);
}

#[tokio::test]
async fn gadget_status_gadgetd_down_is_503() {
    let fx = delete_fixture(Reply::Unavailable);
    let (status, _) = get_json(&fx.app, "/api/gadget/status").await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn gadget_status_error_frame_is_502() {
    let fx = delete_fixture(Reply::Json(json!({ "error": "internal" })));
    let (status, _) = get_json(&fx.app, "/api/gadget/status").await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
}

#[tokio::test]
async fn jobs_stream_is_an_event_stream() {
    let fx = delete_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "result": "done" }),
    ));
    let resp = fx
        .app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/jobs")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let ct = resp
        .headers()
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default();
    assert!(
        ct.starts_with("text/event-stream"),
        "unexpected content-type: {ct}"
    );
}

#[tokio::test]
async fn failed_delete_is_recorded_in_failed_jobs_snapshot() {
    // A failed car-delete (gadgetd `result: failed`) returns 502 and is retained
    // in the failed-jobs ring served by GET /api/jobs/failed. The two requests
    // share one router → one AppState → one JobHub.
    let fx = delete_fixture(Reply::Json(
        json!({ "handoff_id": "h-9", "result": "failed", "detail": "io error" }),
    ));
    let (status, _) = delete_json(&fx.app, "/api/clips/10?target=car").await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);

    let (status, body) = get_json(&fx.app, "/api/jobs/failed").await;
    assert_eq!(status, StatusCode::OK);
    let jobs = body["jobs"].as_array().unwrap();
    assert_eq!(jobs.len(), 1);
    assert_eq!(jobs[0]["kind"], "clip_delete");
    assert_eq!(jobs[0]["state"], "failed");
    assert_eq!(jobs[0]["detail"], "io error");
    assert_eq!(jobs[0]["handoff_id"], "h-9");
}

#[tokio::test]
async fn successful_delete_is_not_in_failed_jobs_snapshot() {
    let fx = delete_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "result": "done" }),
    ));
    let (status, _) = delete_json(&fx.app, "/api/clips/10?target=car").await;
    assert_eq!(status, StatusCode::OK);

    let (status, body) = get_json(&fx.app, "/api/jobs/failed").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body["jobs"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn busy_delete_is_not_recorded_as_failed() {
    // A transient 409 refusal is terminal for the job but is NOT a failure the
    // operator must triage, so it stays out of the failed-jobs ring.
    let fx = delete_fixture(Reply::Json(json!({ "refused": "handoff_active" })));
    let (status, _) = delete_json(&fx.app, "/api/clips/10?target=car").await;
    assert_eq!(status, StatusCode::CONFLICT);

    let (status, body) = get_json(&fx.app, "/api/jobs/failed").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body["jobs"].as_array().unwrap().is_empty());
}

// ---------------------------------------------------------------------------
// Lock-chime media install/remove (`POST /api/chimes`, `DELETE
// /api/chimes/:id`). The gadgetd socket is mocked; these cover validation,
// staging + cleanup, the gadgetd request shape (partition 2 / install_file /
// delete_paths), outcome→HTTP mapping, and job lifecycle. The destructive p2
// write itself is an operator-gated hardware test.
// ---------------------------------------------------------------------------

/// Mock gadgetd for the chime path: records the request AND whether the staged
/// `source_path` existed and was non-empty at the instant of the call (proving
/// staging happens before the handoff and the file is present for the read).
struct ChimeMock {
    reply: Reply,
    last: Arc<Mutex<Option<Value>>>,
    source_existed: Arc<Mutex<Option<bool>>>,
}

impl GadgetClient for ChimeMock {
    fn call(&self, request: Value) -> Result<Value, TransportError> {
        if let Some(src) = request["mutation"]["source_path"].as_str() {
            let ok = std::fs::metadata(src).map(|m| m.len() > 0).unwrap_or(false);
            *self.source_existed.lock().unwrap() = Some(ok);
        }
        *self.last.lock().unwrap() = Some(request);
        match &self.reply {
            Reply::Json(v) => Ok(v.clone()),
            Reply::Unavailable => Err(TransportError::Unavailable("socket down".to_owned())),
        }
    }
}

/// A chime fixture: an empty catalog + a router wired to [`ChimeMock`], with the
/// staging dir exposed so tests can assert it is empty after each handoff.
struct ChimeFixture {
    _dir: TempDir,
    app: Router,
    last: Arc<Mutex<Option<Value>>>,
    source_existed: Arc<Mutex<Option<bool>>>,
    staging: std::path::PathBuf,
}

fn chime_fixture(reply: Reply) -> ChimeFixture {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    seed_car_clips(&db_path);

    let static_dir = dir.path().join("static");
    std::fs::create_dir_all(&static_dir).unwrap();
    std::fs::write(static_dir.join("index.html"), "<!doctype html>shell").unwrap();

    let catalog = Catalog::open(&db_path).unwrap();
    let cache_dir = dir.path().join("cache");
    let media = MediaConfig::new(dir.path().join("archive"), cache_dir.clone());
    let last = Arc::new(Mutex::new(None));
    let source_existed = Arc::new(Mutex::new(None));
    let gadget: Arc<dyn GadgetClient> = Arc::new(ChimeMock {
        reply,
        last: Arc::clone(&last),
        source_existed: Arc::clone(&source_existed),
    });
    let app = router_with_gadget(catalog, static_dir, media, gadget);
    ChimeFixture {
        _dir: dir,
        app,
        last,
        source_existed,
        staging: cache_dir.join("media-staging"),
    }
}

/// A minimal valid 16-bit PCM mono 44.1 kHz WAV with `data_len` audio bytes.
fn sample_wav(data_len: usize) -> Vec<u8> {
    let channels = 1u16;
    let sample_rate = 44_100u32;
    let bits = 16u16;
    let block_align = channels * (bits / 8);
    let byte_rate = sample_rate * u32::from(block_align);
    let mut v = Vec::new();
    v.extend_from_slice(b"RIFF");
    v.extend_from_slice(&u32::try_from(36 + data_len).unwrap().to_le_bytes());
    v.extend_from_slice(b"WAVE");
    v.extend_from_slice(b"fmt ");
    v.extend_from_slice(&16u32.to_le_bytes());
    v.extend_from_slice(&1u16.to_le_bytes());
    v.extend_from_slice(&channels.to_le_bytes());
    v.extend_from_slice(&sample_rate.to_le_bytes());
    v.extend_from_slice(&byte_rate.to_le_bytes());
    v.extend_from_slice(&block_align.to_le_bytes());
    v.extend_from_slice(&bits.to_le_bytes());
    v.extend_from_slice(b"data");
    v.extend_from_slice(&u32::try_from(data_len).unwrap().to_le_bytes());
    v.extend(std::iter::repeat_n(0u8, data_len));
    v
}

const BOUNDARY: &str = "X-TESLAUSB-BOUNDARY";

/// Build a `multipart/form-data` body from `(field-name, content)` parts.
fn multipart_body(parts: &[(&str, &[u8])]) -> Vec<u8> {
    let mut body = Vec::new();
    for (name, content) in parts {
        body.extend_from_slice(format!("--{BOUNDARY}\r\n").as_bytes());
        body.extend_from_slice(
            format!("Content-Disposition: form-data; name=\"{name}\"; filename=\"chime.wav\"\r\n")
                .as_bytes(),
        );
        body.extend_from_slice(b"Content-Type: audio/wav\r\n\r\n");
        body.extend_from_slice(content);
        body.extend_from_slice(b"\r\n");
    }
    body.extend_from_slice(format!("--{BOUNDARY}--\r\n").as_bytes());
    body
}

/// POST a multipart body and return `(status, parsed-json)`.
async fn post_chime(app: &Router, body: Vec<u8>) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::POST)
                .uri("/api/chimes")
                .header(
                    axum::http::header::CONTENT_TYPE,
                    format!("multipart/form-data; boundary={BOUNDARY}"),
                )
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value)
}

/// True if the staging dir is absent or contains no entries.
fn staging_is_empty(dir: &std::path::Path) -> bool {
    match std::fs::read_dir(dir) {
        Ok(mut entries) => entries.next().is_none(),
        Err(_) => true,
    }
}

#[tokio::test]
async fn install_chime_happy_path_sends_install_file_and_cleans_up() {
    let fx = chime_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "result": "done" }),
    ));
    let (status, body) = post_chime(&fx.app, multipart_body(&[("file", &sample_wav(64))])).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["state"], "done");
    assert_eq!(body["handoff_id"], "h-1");

    // gadgetd saw ONE install_file mutation on the MEDIA partition at the fixed
    // root path, with a staged source that existed and was non-empty.
    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "request_mutation");
    assert_eq!(req["partition"], 2);
    assert_eq!(req["mutation"]["op"], "install_file");
    assert_eq!(req["mutation"]["rel_path"], "LockChime.wav");
    assert!(req["mutation"]["source_path"].is_string());
    assert_eq!(*fx.source_existed.lock().unwrap(), Some(true));

    // Staged file is unlinked after the handoff returns.
    assert!(staging_is_empty(&fx.staging), "staging dir must be empty");

    // A successful install is not a failure.
    let (_, failed) = get_json(&fx.app, "/api/jobs/failed").await;
    assert!(failed["jobs"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn install_chime_transient_refusal_is_409_not_failed() {
    let fx = chime_fixture(Reply::Json(json!({ "refused": "handoff_active" })));
    let (status, body) = post_chime(&fx.app, multipart_body(&[("file", &sample_wav(64))])).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["code"], "handoff_busy");
    assert!(staging_is_empty(&fx.staging), "staging dir must be empty");

    let (_, failed) = get_json(&fx.app, "/api/jobs/failed").await;
    assert!(failed["jobs"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn install_chime_failed_is_502_and_recorded_and_cleaned_up() {
    let fx = chime_fixture(Reply::Json(
        json!({ "handoff_id": "h-9", "result": "failed", "detail": "io error" }),
    ));
    let (status, _) = post_chime(&fx.app, multipart_body(&[("file", &sample_wav(64))])).await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert!(staging_is_empty(&fx.staging), "staging dir must be empty");

    let (_, body) = get_json(&fx.app, "/api/jobs/failed").await;
    let jobs = body["jobs"].as_array().unwrap();
    assert_eq!(jobs.len(), 1);
    assert_eq!(jobs[0]["kind"], "chime_install");
    assert_eq!(jobs[0]["state"], "failed");
    assert_eq!(jobs[0]["detail"], "io error");
}

#[tokio::test]
async fn install_chime_oversize_is_422_before_handoff() {
    let fx = chime_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "result": "done" }),
    ));
    // 1 MiB + 1 byte: over the logical cap but under the 2 MiB body limit, so the
    // incremental size guard trips with 422 before any staging/handoff.
    let oversize = vec![0u8; 1024 * 1024 + 1];
    let (status, body) = post_chime(&fx.app, multipart_body(&[("file", &oversize)])).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "chime_too_large");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
    assert!(staging_is_empty(&fx.staging), "no staged file");
}

#[tokio::test]
async fn install_chime_non_wav_is_422_before_handoff() {
    let fx = chime_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "result": "done" }),
    ));
    let (status, body) = post_chime(&fx.app, multipart_body(&[("file", b"not a wav file")])).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "invalid_wav");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
    assert!(staging_is_empty(&fx.staging), "no staged file");
}

#[tokio::test]
async fn install_chime_missing_file_field_is_400() {
    let fx = chime_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "result": "done" }),
    ));
    let (status, body) = post_chime(&fx.app, multipart_body(&[("other", &sample_wav(64))])).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "upload_required");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn install_chime_duplicate_file_field_is_400() {
    let fx = chime_fixture(Reply::Json(
        json!({ "handoff_id": "h-1", "result": "done" }),
    ));
    let body = multipart_body(&[("file", &sample_wav(64)), ("file", &sample_wav(64))]);
    let (status, parsed) = post_chime(&fx.app, body).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(parsed["error"]["code"], "duplicate_field");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn remove_chime_happy_path_sends_delete_paths() {
    let fx = chime_fixture(Reply::Json(
        json!({ "handoff_id": "h-2", "result": "done" }),
    ));
    let (status, body) = delete_json(&fx.app, "/api/chimes/LockChime").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["state"], "done");
    assert_eq!(body["handoff_id"], "h-2");

    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["partition"], 2);
    assert_eq!(req["mutation"]["op"], "delete_paths");
    let paths = req["mutation"]["rel_paths"].as_array().unwrap();
    assert_eq!(paths.len(), 1);
    assert_eq!(paths[0], "LockChime.wav");
}

#[tokio::test]
async fn remove_chime_unknown_id_is_404() {
    let fx = chime_fixture(Reply::Json(
        json!({ "handoff_id": "h-2", "result": "done" }),
    ));
    let (status, _) = delete_json(&fx.app, "/api/chimes/Bogus").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

/// Build a read-only app router over the catalog at `db_path` (no gadgetd
/// contact). Shared by the `GET /api/chimes` tests.
fn ro_app(dir: &TempDir, db_path: &std::path::Path) -> Router {
    let static_dir = dir.path().join("static");
    std::fs::create_dir_all(&static_dir).unwrap();
    std::fs::write(static_dir.join("index.html"), "<!doctype html>shell").unwrap();
    let catalog = Catalog::open(db_path).unwrap();
    let archive_dir = dir.path().join("archive");
    let cache_dir = dir.path().join("cache");
    std::fs::create_dir_all(&archive_dir).unwrap();
    std::fs::create_dir_all(&cache_dir).unwrap();
    let media = MediaConfig::new(archive_dir, cache_dir);
    let gadget_sock = dir.path().join("gadgetd.sock");
    build_router(catalog, static_dir, media, gadget_sock)
}

#[tokio::test]
async fn get_chimes_reports_installed_chime() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    {
        let mut conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        indexd::db::apply_migrations(&mut conn).unwrap();
        conn.execute(
            "INSERT INTO media_entries (partition, rel_path, name, size_bytes, modified, updated_at)
             VALUES ('slot1', 'LockChime.wav', 'LockChime.wav', 219770, '2026-06-01T20:10:04', 0)",
            [],
        )
        .unwrap();
    }
    let app = ro_app(&dir, &db_path);
    let (status, body) = get_json(&app, "/api/chimes").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["installed"]["name"], "LockChime.wav");
    assert_eq!(body["installed"]["rel_path"], "LockChime.wav");
    assert_eq!(body["installed"]["size_bytes"], 219_770);
    assert_eq!(body["installed"]["modified"], "2026-06-01T20:10:04");
}

#[tokio::test]
async fn get_chimes_reports_null_when_none_installed() {
    // The standard fixture is a v2 catalog with NO media_entries rows.
    let fx = fixture();
    let (status, body) = get_json(&fx.app, "/api/chimes").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body["installed"].is_null());
}

#[tokio::test]
async fn get_chimes_ignores_chime_on_wrong_partition() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    {
        let mut conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        indexd::db::apply_migrations(&mut conn).unwrap();
        // A row with the right name but on the dashcam partition must not be
        // reported as the installed lock chime.
        conn.execute(
            "INSERT INTO media_entries (partition, rel_path, name, size_bytes, modified, updated_at)
             VALUES ('slot0', 'LockChime.wav', 'LockChime.wav', 100, NULL, 0)",
            [],
        )
        .unwrap();
    }
    let app = ro_app(&dir, &db_path);
    let (status, body) = get_json(&app, "/api/chimes").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body["installed"].is_null());
}

#[tokio::test]
async fn get_chimes_degrades_to_null_when_media_table_absent() {
    // A pre-v2 indexd catalog (schema_version=1, no media_entries table)
    // opened by a v2-aware webd must answer `{installed: null}`, not 500.
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    {
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL, note TEXT);
             INSERT INTO schema_version (version, applied_at, note) VALUES (1, 0, 'v1');",
        )
        .unwrap();
    }
    let app = ro_app(&dir, &db_path);
    let (status, body) = get_json(&app, "/api/chimes").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body["installed"].is_null());
}

// ── Toybox category GET tests ───────────────────────────────────────────────
//
// Each category shares the same probe+LIKE pattern; we test:
//   1. Items returned when rows exist.
//   2. Empty `{items:[]}` when no rows exist (not 500).
//   3. Empty `{items:[]}` when the table itself is absent (pre-v2 catalog).
//   4. Category isolation (e.g. Wraps rows don't appear in LightShows).

/// Seed `media_entries` into an open connection with an already-migrated schema.
fn seed_media_rows(conn: &Connection, rows: &[(&str, &str, &str, i64)]) {
    for (partition, rel_path, name, size_bytes) in rows {
        conn.execute(
            "INSERT INTO media_entries (partition, rel_path, name, size_bytes, modified, updated_at)
             VALUES (?1, ?2, ?3, ?4, NULL, 0)",
            params![partition, rel_path, name, size_bytes],
        )
        .unwrap();
    }
}

/// Helper: open a pre-v2 DB (`schema_version=1`, no `media_entries` table).
fn open_v1_catalog(dir: &TempDir) -> (std::path::PathBuf, Router) {
    let db_path = dir.path().join("v1_catalog.db");
    {
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL, note TEXT);
             INSERT INTO schema_version (version, applied_at, note) VALUES (1, 0, 'v1');",
        )
        .unwrap();
    }
    let app = ro_app(dir, &db_path);
    (db_path, app)
}

#[tokio::test]
async fn get_boombox_returns_items() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    {
        let mut conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        indexd::db::apply_migrations(&mut conn).unwrap();
        seed_media_rows(
            &conn,
            &[
                ("slot1", "Boombox/horn.wav", "horn.wav", 32768),
                ("slot1", "Boombox/quack.mp3", "quack.mp3", 65536),
            ],
        );
    }
    let app = ro_app(&dir, &db_path);
    let (status, body) = get_json(&app, "/api/boombox").await;
    assert_eq!(status, StatusCode::OK);
    let items = body["items"].as_array().unwrap();
    assert_eq!(items.len(), 2);
    assert!(items.iter().any(|i| i["name"] == "horn.wav"));
    assert!(items.iter().any(|i| i["rel_path"] == "Boombox/quack.mp3"));
}

#[tokio::test]
async fn get_boombox_empty_when_no_rows() {
    let fx = fixture(); // standard fixture has no media_entries rows
    let (status, body) = get_json(&fx.app, "/api/boombox").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["items"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn get_boombox_degrades_on_v1_catalog() {
    let dir = tempfile::tempdir().unwrap();
    let (_, app) = open_v1_catalog(&dir);
    let (status, body) = get_json(&app, "/api/boombox").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["items"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn get_music_returns_items() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    {
        let mut conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        indexd::db::apply_migrations(&mut conn).unwrap();
        seed_media_rows(
            &conn,
            &[
                ("slot1", "Music/song.mp3", "song.mp3", 1_048_576),
                (
                    "slot1",
                    "Music/Artist/Album/track.flac",
                    "track.flac",
                    4_194_304,
                ),
            ],
        );
    }
    let app = ro_app(&dir, &db_path);
    let (status, body) = get_json(&app, "/api/music").await;
    assert_eq!(status, StatusCode::OK);
    let items = body["items"].as_array().unwrap();
    assert_eq!(items.len(), 2);
}

#[tokio::test]
async fn get_music_degrades_on_v1_catalog() {
    let dir = tempfile::tempdir().unwrap();
    let (_, app) = open_v1_catalog(&dir);
    let (status, body) = get_json(&app, "/api/music").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["items"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn get_lightshows_returns_lightshow_files_only() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    {
        let mut conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        indexd::db::apply_migrations(&mut conn).unwrap();
        seed_media_rows(
            &conn,
            &[
                ("slot1", "LightShow/show.fseq", "show.fseq", 2_097_152),
                // Wraps row — must NOT appear in /api/lightshows.
                ("slot1", "LightShow/wraps/mywrap.png", "mywrap.png", 524_288),
            ],
        );
    }
    let app = ro_app(&dir, &db_path);
    let (status, body) = get_json(&app, "/api/lightshows").await;
    assert_eq!(status, StatusCode::OK);
    let items = body["items"].as_array().unwrap();
    assert_eq!(
        items.len(),
        1,
        "wraps row must be excluded from /api/lightshows"
    );
    assert_eq!(items[0]["name"], "show.fseq");
}

#[tokio::test]
async fn get_lightshows_degrades_on_v1_catalog() {
    let dir = tempfile::tempdir().unwrap();
    let (_, app) = open_v1_catalog(&dir);
    let (status, body) = get_json(&app, "/api/lightshows").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["items"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn get_plates_returns_items() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    {
        let mut conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        indexd::db::apply_migrations(&mut conn).unwrap();
        seed_media_rows(
            &conn,
            &[("slot1", "LicensePlate/myplate.png", "myplate.png", 102_400)],
        );
    }
    let app = ro_app(&dir, &db_path);
    let (status, body) = get_json(&app, "/api/plates").await;
    assert_eq!(status, StatusCode::OK);
    let items = body["items"].as_array().unwrap();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["name"], "myplate.png");
    assert_eq!(items[0]["size_bytes"], 102_400);
}

#[tokio::test]
async fn get_plates_degrades_on_v1_catalog() {
    let dir = tempfile::tempdir().unwrap();
    let (_, app) = open_v1_catalog(&dir);
    let (status, body) = get_json(&app, "/api/plates").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["items"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn get_wraps_returns_wraps_only() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    {
        let mut conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        indexd::db::apply_migrations(&mut conn).unwrap();
        seed_media_rows(
            &conn,
            &[
                // LightShow row — must NOT appear in /api/wraps.
                ("slot1", "LightShow/show.fseq", "show.fseq", 2_097_152),
                ("slot1", "LightShow/wraps/mywrap.png", "mywrap.png", 524_288),
            ],
        );
    }
    let app = ro_app(&dir, &db_path);
    let (status, body) = get_json(&app, "/api/wraps").await;
    assert_eq!(status, StatusCode::OK);
    let items = body["items"].as_array().unwrap();
    assert_eq!(
        items.len(),
        1,
        "lightshow row must be excluded from /api/wraps"
    );
    assert_eq!(items[0]["name"], "mywrap.png");
}

#[tokio::test]
async fn get_wraps_degrades_on_v1_catalog() {
    let dir = tempfile::tempdir().unwrap();
    let (_, app) = open_v1_catalog(&dir);
    let (status, body) = get_json(&app, "/api/wraps").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["items"].as_array().unwrap().len(), 0);
}
