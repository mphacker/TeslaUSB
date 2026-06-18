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
use crate::scheduler::SchedulerClient;
use crate::{Catalog, MediaConfig, build_router, router_with_clients, router_with_gadget};

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

fn media_content_app(files: &[(&str, &[u8])], create_root: bool) -> (TempDir, Router) {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    let mut conn = Connection::open(&db_path).unwrap();
    conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
    indexd::db::apply_migrations(&mut conn).unwrap();

    std::fs::create_dir_all(dir.path().join("archive")).unwrap();
    std::fs::create_dir_all(dir.path().join("cache")).unwrap();

    let static_dir = dir.path().join("static");
    std::fs::create_dir_all(&static_dir).unwrap();
    std::fs::write(static_dir.join("index.html"), "<!doctype html>shell").unwrap();

    let media_ro = dir.path().join("media-ro");
    if create_root {
        std::fs::create_dir_all(&media_ro).unwrap();
        for (rel, bytes) in files {
            let path = media_ro.join(rel);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, bytes).unwrap();
        }
    }

    let catalog = Catalog::open(&db_path).unwrap();
    let media = MediaConfig::new(dir.path().join("archive"), dir.path().join("cache"))
        .with_media_ro_root(media_ro);
    let gadget_sock = dir.path().join("gadgetd.sock");
    let app = build_router(catalog, static_dir, media, gadget_sock);
    (dir, app)
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
async fn media_content_streams_full_file() {
    let (dir, app) = media_content_app(&[("Music/song.mp3", &pattern(16))], true);
    let (status, headers, body) = request(
        &app,
        Method::GET,
        "/api/media/content?path=Music/song.mp3",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(header(&headers, "content-type"), Some("audio/mpeg"));
    assert_eq!(header(&headers, "content-length"), Some("16"));
    assert_eq!(header(&headers, "accept-ranges"), Some("bytes"));
    assert_eq!(header(&headers, "x-content-type-options"), Some("nosniff"));
    assert_eq!(body.as_slice(), pattern(16).as_slice());
    let _ = dir;
}

#[tokio::test]
async fn media_content_range_returns_206() {
    let (dir, app) = media_content_app(&[("Music/song.mp3", &pattern(16))], true);
    let (status, headers, body) = request(
        &app,
        Method::GET,
        "/api/media/content?path=Music/song.mp3",
        Some("bytes=0-3"),
    )
    .await;
    assert_eq!(status, StatusCode::PARTIAL_CONTENT);
    assert_eq!(header(&headers, "content-range"), Some("bytes 0-3/16"));
    assert_eq!(header(&headers, "content-length"), Some("4"));
    assert_eq!(body, pattern(16)[0..4]);
    let _ = dir;
}

#[tokio::test]
async fn media_content_head_has_empty_body() {
    let (dir, app) = media_content_app(&[("Music/song.mp3", &pattern(16))], true);
    let (status, headers, body) = request(
        &app,
        Method::HEAD,
        "/api/media/content?path=Music/song.mp3",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(header(&headers, "content-length"), Some("16"));
    assert!(body.is_empty());
    let _ = dir;
}

#[tokio::test]
async fn media_content_traversal_is_404() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    let mut conn = Connection::open(&db_path).unwrap();
    conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
    indexd::db::apply_migrations(&mut conn).unwrap();

    let static_dir = dir.path().join("static");
    std::fs::create_dir_all(&static_dir).unwrap();
    std::fs::write(static_dir.join("index.html"), "<!doctype html>shell").unwrap();

    let media_ro = dir.path().join("media-ro");
    std::fs::create_dir_all(&media_ro).unwrap();
    std::fs::write(media_ro.join("LockChime.wav"), b"abc").unwrap();

    let secret = dir.path().join("secret.txt");
    std::fs::write(&secret, b"secret").unwrap();

    let catalog = Catalog::open(&db_path).unwrap();
    let media = MediaConfig::new(dir.path().join("archive"), dir.path().join("cache"))
        .with_media_ro_root(media_ro);
    let gadget_sock = dir.path().join("gadgetd.sock");
    let app = build_router(catalog, static_dir, media, gadget_sock);

    let (status, _, _) = request(
        &app,
        Method::GET,
        "/api/media/content?path=../secret.txt",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let (status, _, _) = request(
        &app,
        Method::GET,
        "/api/media/content?path=/etc/passwd",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn media_content_missing_file_is_404() {
    let (dir, app) = media_content_app(&[("Music/song.mp3", &pattern(16))], true);
    let (status, _, _) = request(
        &app,
        Method::GET,
        "/api/media/content?path=Music/missing.mp3",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let _ = dir;
}

#[tokio::test]
async fn media_content_unsatisfiable_range_is_416() {
    let (dir, app) = media_content_app(&[("Music/song.mp3", &pattern(16))], true);
    let (status, headers, _) = request(
        &app,
        Method::GET,
        "/api/media/content?path=Music/song.mp3",
        Some("bytes=100-200"),
    )
    .await;
    assert_eq!(status, StatusCode::RANGE_NOT_SATISFIABLE);
    assert_eq!(header(&headers, "content-range"), Some("bytes */16"));
    assert_eq!(header(&headers, "x-content-type-options"), Some("nosniff"));
    let _ = dir;
}

#[tokio::test]
async fn media_content_directory_is_404() {
    // Seeding `Music/song.mp3` creates the `Music/` directory; requesting the
    // directory itself must be rejected as a non-regular file, not streamed.
    let (dir, app) = media_content_app(&[("Music/song.mp3", &pattern(16))], true);
    let (status, _, _) = request(&app, Method::GET, "/api/media/content?path=Music", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let _ = dir;
}

#[tokio::test]
async fn media_content_absent_mount_is_503() {
    let (dir, app) = media_content_app(&[("Music/song.mp3", &pattern(16))], false);
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/media/content?path=Music/song.mp3")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(header(resp.headers(), "retry-after"), Some("2"));
    assert_eq!(
        header(resp.headers(), "content-type"),
        Some("application/json")
    );
    let body = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(
        body.as_ref(),
        br#"{"error":{"code":"media_unavailable","message":"media not mounted"}}"#
    );
    let _ = dir;
}

#[test]
fn content_type_for_maps_extensions() {
    assert_eq!(
        super::media::content_type_for(std::path::Path::new("song.wav")),
        "audio/wav"
    );
    assert_eq!(
        super::media::content_type_for(std::path::Path::new("song.mp3")),
        "audio/mpeg"
    );
    assert_eq!(
        super::media::content_type_for(std::path::Path::new("song.flac")),
        "audio/flac"
    );
    assert_eq!(
        super::media::content_type_for(std::path::Path::new("song.aac")),
        "audio/aac"
    );
    assert_eq!(
        super::media::content_type_for(std::path::Path::new("song.m4a")),
        "audio/mp4"
    );
    assert_eq!(
        super::media::content_type_for(std::path::Path::new("song.png")),
        "image/png"
    );
    assert_eq!(
        super::media::content_type_for(std::path::Path::new("song.jpg")),
        "image/jpeg"
    );
    assert_eq!(
        super::media::content_type_for(std::path::Path::new("song.jpeg")),
        "image/jpeg"
    );
    assert_eq!(
        super::media::content_type_for(std::path::Path::new("song.fseq")),
        "application/octet-stream"
    );
    assert_eq!(
        super::media::content_type_for(std::path::Path::new("song.unknown")),
        "application/octet-stream"
    );
    assert_eq!(
        super::media::content_type_for(std::path::Path::new("song")),
        "application/octet-stream"
    );
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
    multipart_body_with_filename("chime.wav", parts)
}

fn multipart_body_with_filename(filename: &str, parts: &[(&str, &[u8])]) -> Vec<u8> {
    let mut body = Vec::new();
    for (name, content) in parts {
        body.extend_from_slice(format!("--{BOUNDARY}\r\n").as_bytes());
        body.extend_from_slice(
            format!("Content-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n")
                .as_bytes(),
        );
        body.extend_from_slice(b"Content-Type: audio/wav\r\n\r\n");
        body.extend_from_slice(content);
        body.extend_from_slice(b"\r\n");
    }
    body.extend_from_slice(format!("--{BOUNDARY}--\r\n").as_bytes());
    body
}

fn multipart_body_with_content_type(
    filename: &str,
    content_type: &str,
    parts: &[(&str, &[u8])],
) -> Vec<u8> {
    let mut body = Vec::new();
    for (name, content) in parts {
        body.extend_from_slice(format!("--{BOUNDARY}\r\n").as_bytes());
        body.extend_from_slice(
            format!("Content-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n")
                .as_bytes(),
        );
        body.extend_from_slice(format!("Content-Type: {content_type}\r\n\r\n").as_bytes());
        body.extend_from_slice(content);
        body.extend_from_slice(b"\r\n");
    }
    body.extend_from_slice(format!("--{BOUNDARY}--\r\n").as_bytes());
    body
}

/// POST a multipart body to `/api/boombox` and return `(status, parsed-json)`.
async fn post_boombox(app: &Router, body: Vec<u8>) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::POST)
                .uri("/api/boombox")
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

async fn post_wraps(app: &Router, body: Vec<u8>) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::POST)
                .uri("/api/wraps")
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

fn valid_wrap_png(w: u32, h: u32) -> Vec<u8> {
    let mut data = b"\x89PNG\r\n\x1a\n".to_vec();
    data.extend_from_slice(&13u32.to_be_bytes());
    data.extend_from_slice(b"IHDR");
    data.extend_from_slice(&w.to_be_bytes());
    data.extend_from_slice(&h.to_be_bytes());
    data.extend_from_slice(&[8, 6, 0, 0, 0]);
    data
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
async fn install_chime_happy_path_enqueues_install_file_and_retains_blob() {
    let fx = chime_fixture(Reply::Json(json!({ "job_id": "m-1", "state": "queued" })));
    let (status, body) = post_chime(&fx.app, multipart_body(&[("file", &sample_wav(64))])).await;
    // Frictionless write path: accepted into gadgetd's durable queue (202),
    // never a transient-busy error, even with the car connected.
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");
    assert_eq!(body["job_id"], "m-1");

    // gadgetd saw ONE enqueue_mutation install_file on the MEDIA partition at
    // the fixed root path, with a staged source that existed and was non-empty.
    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "enqueue_mutation");
    assert_eq!(req["partition"], 2);
    assert_eq!(req["mutation"]["op"], "install_file");
    assert_eq!(req["mutation"]["rel_path"], "LockChime.wav");
    assert!(req["mutation"]["source_path"].is_string());
    assert!(req["blob_path"].is_string());
    assert_eq!(*fx.source_existed.lock().unwrap(), Some(true));

    // The staged blob is RETAINED after a successful enqueue: gadgetd owns it
    // and reclaims it after apply, so webd must not unlink it.
    assert!(
        !staging_is_empty(&fx.staging),
        "staged blob must be retained for gadgetd to apply"
    );

    // A queued install is not a failure.
    let (_, failed) = get_json(&fx.app, "/api/jobs/failed").await;
    assert!(failed["jobs"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn install_chime_rejected_is_422_and_cleans_up() {
    // gadgetd rejecting the mutation up-front (e.g. a full queue) is a 422; the
    // staged blob is unlinked by webd because gadgetd never took ownership.
    let fx = chime_fixture(Reply::Json(json!({ "error": "queue full" })));
    let (status, body) = post_chime(&fx.app, multipart_body(&[("file", &sample_wav(64))])).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "refused");
    assert!(staging_is_empty(&fx.staging), "staging dir must be empty");

    // A rejected mutation is not retained in the failed ring.
    let (_, failed) = get_json(&fx.app, "/api/jobs/failed").await;
    assert!(failed["jobs"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn install_chime_bad_reply_is_502_and_recorded_and_cleaned_up() {
    // An unparseable gadgetd enqueue reply (no job_id/state:"queued") is a 502,
    // recorded as a failed job, and the staged blob is cleaned up.
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
async fn remove_chime_happy_path_enqueues_delete_paths() {
    let fx = chime_fixture(Reply::Json(json!({ "job_id": "m-2", "state": "queued" })));
    let (status, body) = delete_json(&fx.app, "/api/chimes/LockChime").await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");
    assert_eq!(body["job_id"], "m-2");

    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "enqueue_mutation");
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

/// POST a JSON body and return `(status, parsed-json)`.
async fn post_json(app: &Router, uri: &str, body: Value) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::POST)
                .uri(uri)
                .header(axum::http::header::CONTENT_TYPE, "application/json")
                .body(Body::from(serde_json::to_vec(&body).unwrap()))
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
async fn bulk_delete_boombox_enqueues_one_mutation_with_all_paths() {
    let fx = delete_fixture(Reply::Json(
        json!({ "job_id": "m-bulk", "state": "queued" }),
    ));
    let (status, body) = post_json(
        &fx.app,
        "/api/boombox/bulk-delete",
        json!({ "names": ["horn.wav", "airhorn.mp3"] }),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");
    assert_eq!(body["job_id"], "m-bulk");

    // gadgetd saw exactly ONE enqueue carrying BOTH derived paths (one
    // eject/remount for the batch, not one per file).
    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "enqueue_mutation");
    assert_eq!(req["partition"], 2);
    assert_eq!(req["mutation"]["op"], "delete_paths");
    let paths = req["mutation"]["rel_paths"].as_array().unwrap();
    assert_eq!(paths.len(), 2);
    assert_eq!(paths[0], "Boombox/horn.wav");
    assert_eq!(paths[1], "Boombox/airhorn.mp3");
}

#[tokio::test]
async fn bulk_delete_wraps_rebuilds_wraps_subdir() {
    let fx = delete_fixture(Reply::Json(json!({ "job_id": "m-w", "state": "queued" })));
    let (status, _) = post_json(
        &fx.app,
        "/api/wraps/bulk-delete",
        json!({ "names": ["cyber.png"] }),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    let req = fx.last.lock().unwrap().clone().unwrap();
    let paths = req["mutation"]["rel_paths"].as_array().unwrap();
    assert_eq!(paths[0], "Wraps/cyber.png");
}

#[tokio::test]
async fn bulk_delete_dedupes_repeated_names() {
    let fx = delete_fixture(Reply::Json(json!({ "job_id": "m-d", "state": "queued" })));
    let (status, _) = post_json(
        &fx.app,
        "/api/music/bulk-delete",
        json!({ "names": ["a.mp3", "a.mp3", "b.flac"] }),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    let req = fx.last.lock().unwrap().clone().unwrap();
    let paths = req["mutation"]["rel_paths"].as_array().unwrap();
    assert_eq!(paths.len(), 2, "duplicate name collapsed to one path");
    assert_eq!(paths[0], "Music/a.mp3");
    assert_eq!(paths[1], "Music/b.flac");
}

#[tokio::test]
async fn bulk_delete_empty_batch_is_400_before_handoff() {
    let fx = delete_fixture(Reply::Json(json!({ "state": "queued" })));
    let (status, body) =
        post_json(&fx.app, "/api/plates/bulk-delete", json!({ "names": [] })).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "empty_batch");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn bulk_delete_traversal_name_is_refused_before_handoff() {
    let fx = delete_fixture(Reply::Json(json!({ "state": "queued" })));
    let (status, body) = post_json(
        &fx.app,
        "/api/lightshows/bulk-delete",
        json!({ "names": ["ok.fseq", ".."] }),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "invalid_filename");
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

struct BoomboxFixture {
    _dir: TempDir,
    app: Router,
    last: Arc<Mutex<Option<Value>>>,
}

fn boombox_fixture(reply: Reply, rows: &[(&str, &str, &str, i64)]) -> BoomboxFixture {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    {
        let mut conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        indexd::db::apply_migrations(&mut conn).unwrap();
        seed_media_rows(&conn, rows);
    }

    let static_dir = dir.path().join("static");
    std::fs::create_dir_all(&static_dir).unwrap();
    std::fs::write(static_dir.join("index.html"), "<!doctype html>shell").unwrap();

    let catalog = Catalog::open(&db_path).unwrap();
    let archive_dir = dir.path().join("archive");
    let cache_dir = dir.path().join("cache");
    std::fs::create_dir_all(&archive_dir).unwrap();
    std::fs::create_dir_all(&cache_dir).unwrap();
    let media = MediaConfig::new(archive_dir, cache_dir);
    let last = Arc::new(Mutex::new(None));
    let gadget: Arc<dyn GadgetClient> = Arc::new(MockGadget {
        reply,
        last: Arc::clone(&last),
    });
    let app = router_with_gadget(catalog, static_dir, media, gadget);

    BoomboxFixture {
        _dir: dir,
        app,
        last,
    }
}

struct WrapsFixture {
    _dir: TempDir,
    app: Router,
    last: Arc<Mutex<Option<Value>>>,
}

fn wraps_fixture(reply: Reply, rows: &[(&str, &str, &str, i64)]) -> WrapsFixture {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    {
        let mut conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        indexd::db::apply_migrations(&mut conn).unwrap();
        seed_media_rows(&conn, rows);
    }

    let static_dir = dir.path().join("static");
    std::fs::create_dir_all(&static_dir).unwrap();
    std::fs::write(static_dir.join("index.html"), "<!doctype html>shell").unwrap();

    let catalog = Catalog::open(&db_path).unwrap();
    let archive_dir = dir.path().join("archive");
    let cache_dir = dir.path().join("cache");
    std::fs::create_dir_all(&archive_dir).unwrap();
    std::fs::create_dir_all(&cache_dir).unwrap();
    let media = MediaConfig::new(archive_dir, cache_dir);
    let last = Arc::new(Mutex::new(None));
    let gadget: Arc<dyn GadgetClient> = Arc::new(MockGadget {
        reply,
        last: Arc::clone(&last),
    });
    let app = router_with_gadget(catalog, static_dir, media, gadget);

    WrapsFixture {
        _dir: dir,
        app,
        last,
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
async fn boombox_upload_rejected_when_full() {
    let fx = boombox_fixture(
        Reply::Json(json!({ "state": "queued", "job_id": "m-1" })),
        &[
            ("slot1", "Boombox/a.wav", "a.wav", 1),
            ("slot1", "Boombox/b.wav", "b.wav", 1),
            ("slot1", "Boombox/c.mp3", "c.mp3", 1),
            ("slot1", "Boombox/d.mp3", "d.mp3", 1),
            ("slot1", "Boombox/e.mp3", "e.mp3", 1),
        ],
    );
    let (status, body) = post_boombox(
        &fx.app,
        multipart_body_with_filename(
            "f.mp3",
            &[("file", b"ID3\x03\x00\x00\x00\x00\x00\x00fakeaudio")],
        ),
    )
    .await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "boombox_full");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn boombox_replace_allowed_when_full() {
    let fx = boombox_fixture(
        Reply::Json(json!({ "state": "queued", "job_id": "m-1" })),
        &[
            ("slot1", "Boombox/a.wav", "a.wav", 1),
            ("slot1", "Boombox/b.wav", "b.wav", 1),
            ("slot1", "Boombox/c.mp3", "c.mp3", 1),
            ("slot1", "Boombox/d.mp3", "d.mp3", 1),
            ("slot1", "Boombox/e.mp3", "e.mp3", 1),
        ],
    );
    let (status, body) = post_boombox(
        &fx.app,
        multipart_body_with_filename(
            "c.mp3",
            &[("file", b"ID3\x03\x00\x00\x00\x00\x00\x00replace")],
        ),
    )
    .await;

    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");
    assert!(fx.last.lock().unwrap().is_some(), "gadgetd contacted");
}

/// A differently-cased name is a DISTINCT file on the case-sensitive p2 store,
/// so uploading `c.mp3` while `C.MP3` already occupies one of the five slots is
/// a sixth file, not a replace, and must be rejected before any gadgetd handoff.
#[tokio::test]
async fn boombox_case_variant_rejected_when_full() {
    let fx = boombox_fixture(
        Reply::Json(json!({ "state": "queued", "job_id": "m-1" })),
        &[
            ("slot1", "Boombox/a.wav", "a.wav", 1),
            ("slot1", "Boombox/b.wav", "b.wav", 1),
            ("slot1", "Boombox/C.MP3", "C.MP3", 1),
            ("slot1", "Boombox/d.mp3", "d.mp3", 1),
            ("slot1", "Boombox/e.mp3", "e.mp3", 1),
        ],
    );
    let (status, body) = post_boombox(
        &fx.app,
        multipart_body_with_filename(
            "c.mp3",
            &[("file", b"ID3\x03\x00\x00\x00\x00\x00\x00distinct")],
        ),
    )
    .await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "boombox_full");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn boombox_nested_same_name_is_not_a_replace_at_capacity() {
    // A nested `Boombox/old/e.mp3` shares the bare name `e.mp3` with the
    // incoming root-level upload but is a DISTINCT destination path. At capacity
    // it must NOT be treated as a replace (which would bypass the cap).
    let fx = boombox_fixture(
        Reply::Json(json!({ "state": "queued", "job_id": "m-1" })),
        &[
            ("slot1", "Boombox/a.wav", "a.wav", 1),
            ("slot1", "Boombox/b.wav", "b.wav", 1),
            ("slot1", "Boombox/c.mp3", "c.mp3", 1),
            ("slot1", "Boombox/d.mp3", "d.mp3", 1),
            ("slot1", "Boombox/old/e.mp3", "e.mp3", 1),
        ],
    );
    let (status, body) = post_boombox(
        &fx.app,
        multipart_body_with_filename(
            "e.mp3",
            &[("file", b"ID3\x03\x00\x00\x00\x00\x00\x00audio")],
        ),
    )
    .await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "boombox_full");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn boombox_upload_under_cap_reaches_gadgetd() {
    let fx = boombox_fixture(
        Reply::Json(json!({ "state": "queued", "job_id": "m-1" })),
        &[
            ("slot1", "Boombox/a.wav", "a.wav", 1),
            ("slot1", "Boombox/b.wav", "b.wav", 1),
            ("slot1", "Boombox/c.mp3", "c.mp3", 1),
            ("slot1", "Boombox/d.mp3", "d.mp3", 1),
        ],
    );
    let (status, body) = post_boombox(
        &fx.app,
        multipart_body_with_filename(
            "e.mp3",
            &[("file", b"ID3\x03\x00\x00\x00\x00\x00\x00audio")],
        ),
    )
    .await;

    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");
    assert!(fx.last.lock().unwrap().is_some(), "gadgetd contacted");
}

#[tokio::test]
async fn boombox_upload_rejected_when_too_large() {
    let fx = boombox_fixture(
        Reply::Json(json!({ "state": "queued", "job_id": "m-1" })),
        &[],
    );
    let oversize = vec![0_u8; 8 * 1024 * 1024 + 1];
    let (status, body) = post_boombox(
        &fx.app,
        multipart_body_with_filename("big.mp3", &[("file", &oversize)]),
    )
    .await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "file_too_large");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn wraps_upload_rejected_when_full() {
    let fx = wraps_fixture(
        Reply::Json(json!({ "state": "queued", "job_id": "m-1" })),
        &[
            ("slot1", "Wraps/w01.png", "w01.png", 1),
            ("slot1", "Wraps/w02.png", "w02.png", 1),
            ("slot1", "Wraps/w03.png", "w03.png", 1),
            ("slot1", "Wraps/w04.png", "w04.png", 1),
            ("slot1", "Wraps/w05.png", "w05.png", 1),
            ("slot1", "Wraps/w06.png", "w06.png", 1),
            ("slot1", "Wraps/w07.png", "w07.png", 1),
            ("slot1", "Wraps/w08.png", "w08.png", 1),
            ("slot1", "Wraps/w09.png", "w09.png", 1),
            ("slot1", "Wraps/w10.png", "w10.png", 1),
        ],
    );
    let body = multipart_body_with_content_type(
        "w11.png",
        "image/png",
        &[("file", &valid_wrap_png(512, 512))],
    );

    let (status, body) = post_wraps(&fx.app, body).await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "wraps_full");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn wraps_replace_allowed_when_full() {
    let fx = wraps_fixture(
        Reply::Json(json!({ "state": "queued", "job_id": "m-1" })),
        &[
            ("slot1", "Wraps/w01.png", "w01.png", 1),
            ("slot1", "Wraps/w02.png", "w02.png", 1),
            ("slot1", "Wraps/w03.png", "w03.png", 1),
            ("slot1", "Wraps/w04.png", "w04.png", 1),
            ("slot1", "Wraps/w05.png", "w05.png", 1),
            ("slot1", "Wraps/w06.png", "w06.png", 1),
            ("slot1", "Wraps/w07.png", "w07.png", 1),
            ("slot1", "Wraps/w08.png", "w08.png", 1),
            ("slot1", "Wraps/w09.png", "w09.png", 1),
            ("slot1", "Wraps/w10.png", "w10.png", 1),
        ],
    );
    let body = multipart_body_with_content_type(
        "w05.png",
        "image/png",
        &[("file", &valid_wrap_png(512, 512))],
    );

    let (status, body) = post_wraps(&fx.app, body).await;

    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");
    assert!(fx.last.lock().unwrap().is_some(), "gadgetd contacted");
}

#[tokio::test]
async fn wraps_under_cap_reaches_gadgetd() {
    let fx = wraps_fixture(
        Reply::Json(json!({ "state": "queued", "job_id": "m-1" })),
        &[
            ("slot1", "Wraps/w01.png", "w01.png", 1),
            ("slot1", "Wraps/w02.png", "w02.png", 1),
            ("slot1", "Wraps/w03.png", "w03.png", 1),
            ("slot1", "Wraps/w04.png", "w04.png", 1),
            ("slot1", "Wraps/w05.png", "w05.png", 1),
            ("slot1", "Wraps/w06.png", "w06.png", 1),
            ("slot1", "Wraps/w07.png", "w07.png", 1),
            ("slot1", "Wraps/w08.png", "w08.png", 1),
            ("slot1", "Wraps/w09.png", "w09.png", 1),
        ],
    );
    let body = multipart_body_with_content_type(
        "w10.png",
        "image/png",
        &[("file", &valid_wrap_png(512, 512))],
    );

    let (status, body) = post_wraps(&fx.app, body).await;

    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");
    assert!(fx.last.lock().unwrap().is_some(), "gadgetd contacted");
}

#[tokio::test]
async fn wraps_nested_same_name_is_not_a_replace_at_capacity() {
    // A nested `Wraps/old/w10.png` shares the bare name `w10.png` with the
    // incoming root-level upload but is a DISTINCT destination path. At capacity
    // it must NOT be treated as a replace (which would bypass the cap).
    let fx = wraps_fixture(
        Reply::Json(json!({ "state": "queued", "job_id": "m-1" })),
        &[
            ("slot1", "Wraps/w01.png", "w01.png", 1),
            ("slot1", "Wraps/w02.png", "w02.png", 1),
            ("slot1", "Wraps/w03.png", "w03.png", 1),
            ("slot1", "Wraps/w04.png", "w04.png", 1),
            ("slot1", "Wraps/w05.png", "w05.png", 1),
            ("slot1", "Wraps/w06.png", "w06.png", 1),
            ("slot1", "Wraps/w07.png", "w07.png", 1),
            ("slot1", "Wraps/w08.png", "w08.png", 1),
            ("slot1", "Wraps/w09.png", "w09.png", 1),
            ("slot1", "Wraps/old/w10.png", "w10.png", 1),
        ],
    );
    let body = multipart_body_with_content_type(
        "w10.png",
        "image/png",
        &[("file", &valid_wrap_png(512, 512))],
    );

    let (status, body) = post_wraps(&fx.app, body).await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "wraps_full");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn wraps_upload_rejected_bad_name() {
    let fx = wraps_fixture(
        Reply::Json(json!({ "state": "queued", "job_id": "m-1" })),
        &[],
    );
    let body = multipart_body_with_content_type(
        "bad!name.png",
        "image/png",
        &[("file", &valid_wrap_png(512, 512))],
    );

    let (status, body) = post_wraps(&fx.app, body).await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "invalid_filename");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
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
                // Wraps row (root-level Wraps/) — must NOT appear in /api/lightshows.
                ("slot1", "Wraps/mywrap.png", "mywrap.png", 524_288),
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
                ("slot1", "Wraps/mywrap.png", "mywrap.png", 524_288),
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

// ---------------------------------------------------------------------------
// Chime-scheduler proxy (`/api/chime-scheduler/*`) — webd is a pure forwarder
// to schedulerd. These tests inject a MockScheduler via `router_with_clients`
// and assert the request shape webd forwards + the response/error mapping.
// ---------------------------------------------------------------------------

/// A canned schedulerd reply for the mock.
enum SchedReply {
    /// Return this JSON value (a success body or an `{error:{..}}` envelope).
    Json(Value),
    /// Simulate the socket being unreachable.
    Unavailable,
    /// Simulate an unreadable/garbled reply.
    Protocol,
}

/// A mock [`SchedulerClient`] that records the last forwarded request and, when
/// a `staged_path` is present, whether that file existed and was non-empty at
/// the instant of the call (proving webd stages the upload before the handoff).
struct MockScheduler {
    reply: SchedReply,
    last: Arc<Mutex<Option<Value>>>,
    staged_existed: Arc<Mutex<Option<bool>>>,
}

impl SchedulerClient for MockScheduler {
    fn call(&self, request: Value) -> Result<Value, TransportError> {
        if let Some(src) = request["staged_path"].as_str() {
            let ok = std::fs::metadata(src).map(|m| m.len() > 0).unwrap_or(false);
            *self.staged_existed.lock().unwrap() = Some(ok);
        }
        *self.last.lock().unwrap() = Some(request);
        match &self.reply {
            SchedReply::Json(v) => Ok(v.clone()),
            SchedReply::Unavailable => Err(TransportError::Unavailable("socket down".to_owned())),
            SchedReply::Protocol => Err(TransportError::Protocol("garbled".to_owned())),
        }
    }
}

/// A scheduler fixture: an empty catalog + a router wired to [`MockScheduler`].
/// `last` captures the request forwarded to schedulerd.
struct SchedFixture {
    _dir: TempDir,
    app: Router,
    last: Arc<Mutex<Option<Value>>>,
}

fn sched_fixture(reply: SchedReply) -> SchedFixture {
    sched_fixture_with_rows(reply, &[])
}

fn sched_fixture_with_rows(reply: SchedReply, rows: &[(&str, &str, &str, i64)]) -> SchedFixture {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    seed(&db_path);
    if !rows.is_empty() {
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        seed_media_rows(&conn, rows);
    }

    let static_dir = dir.path().join("static");
    std::fs::create_dir_all(&static_dir).unwrap();
    std::fs::write(static_dir.join("index.html"), "<!doctype html>shell").unwrap();

    let library_dir = dir.path().join("chimes");
    std::fs::create_dir_all(&library_dir).unwrap();

    let catalog = Catalog::open(&db_path).unwrap();
    let media = MediaConfig::new(dir.path().join("archive"), dir.path().join("cache"));
    // The gadget client is unused on the scheduler routes; point it at a dead
    // socket so an accidental call fails loudly rather than silently passing.
    let gadget_sock = dir.path().join("gadgetd.sock");
    let gadget = crate::default_gadget_client(gadget_sock);

    let last = Arc::new(Mutex::new(None));
    let staged_existed = Arc::new(Mutex::new(None));
    let scheduler: Arc<dyn SchedulerClient> = Arc::new(MockScheduler {
        reply,
        last: Arc::clone(&last),
        staged_existed: Arc::clone(&staged_existed),
    });
    let app = router_with_clients(catalog, static_dir, media, gadget, scheduler, library_dir);
    SchedFixture {
        _dir: dir,
        app,
        last,
    }
}

/// Issue a PUT with a JSON body and return `(status, parsed-json)`.
async fn put_json(app: &Router, uri: &str, body: Value) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::PUT)
                .uri(uri)
                .header(axum::http::header::CONTENT_TYPE, "application/json")
                .body(Body::from(serde_json::to_vec(&body).unwrap()))
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
async fn chime_scheduler_snapshot_rewrites_library_from_media_catalog() {
    let snap = json!({
        "schedules": [{ "id": "s-1", "name": "Holidays" }],
        "groups": [],
        "randomMode": { "enabled": false },
        "library": [{ "filename": "stale.wav", "bytes": 999 }],
    });
    let fx = sched_fixture_with_rows(
        SchedReply::Json(snap.clone()),
        &[
            ("slot1", "Chimes/Horn.wav", "Horn.wav", 64),
            ("slot1", "Chimes/sub/Nested.wav", "Nested.wav", 32),
            ("slot1", "Chimes/notes.txt", "notes.txt", 16),
        ],
    );
    let (status, body) = get_json(&fx.app, "/api/chime-scheduler").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["schedules"], snap["schedules"]);
    assert_eq!(body["groups"], snap["groups"]);
    assert_eq!(body["randomMode"], snap["randomMode"]);
    assert_eq!(
        body["library"],
        json!([{ "filename": "Horn.wav", "bytes": 64 }])
    );
    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "snapshot");
}

#[tokio::test]
async fn chime_scheduler_add_schedule_is_201_and_forwards_input() {
    let fx = sched_fixture(SchedReply::Json(json!({ "id": "s-9", "name": "Weekday" })));
    let input = json!({ "name": "Weekday", "kind": "weekly", "days": [1, 2, 3] });
    let (status, body) = post_json(&fx.app, "/api/chime-scheduler/schedules", input.clone()).await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(body["id"], "s-9");
    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "add_schedule");
    assert_eq!(req["input"], input);
}

#[tokio::test]
async fn chime_scheduler_validation_error_maps_to_422_with_relayed_code() {
    // schedulerd rejects bad input with a runtime code; webd relays code+message
    // and maps an unknown code to 422 (the conservative validation default).
    let fx = sched_fixture(SchedReply::Json(json!({
        "error": { "code": "bad_weekday", "message": "weekday out of range" }
    })));
    let (status, body) = post_json(
        &fx.app,
        "/api/chime-scheduler/schedules",
        json!({ "name": "x", "days": [9] }),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "bad_weekday");
    assert_eq!(body["error"]["message"], "weekday out of range");
}

#[tokio::test]
async fn chime_scheduler_not_found_maps_to_404() {
    let fx = sched_fixture(SchedReply::Json(json!({
        "error": { "code": "not_found", "message": "no such schedule" }
    })));
    let (status, body) = delete_json(&fx.app, "/api/chime-scheduler/schedules/nope").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["error"]["code"], "not_found");
    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "delete_schedule");
    assert_eq!(req["id"], "nope");
}

#[tokio::test]
async fn chime_scheduler_update_schedule_forwards_id_and_input() {
    let fx = sched_fixture(SchedReply::Json(json!({ "id": "s-1", "name": "Renamed" })));
    let input = json!({ "name": "Renamed" });
    let (status, _) = put_json(&fx.app, "/api/chime-scheduler/schedules/s-1", input.clone()).await;
    assert_eq!(status, StatusCode::OK);
    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "update_schedule");
    assert_eq!(req["id"], "s-1");
    assert_eq!(req["input"], input);
}

#[tokio::test]
async fn chime_scheduler_set_random_mode_forwards_mode() {
    let fx = sched_fixture(SchedReply::Json(
        json!({ "enabled": true, "groupId": "g-1" }),
    ));
    let mode = json!({ "enabled": true, "groupId": "g-1" });
    let (status, _) = put_json(&fx.app, "/api/chime-scheduler/random-mode", mode.clone()).await;
    assert_eq!(status, StatusCode::OK);
    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "set_random_mode");
    assert_eq!(req["mode"], mode);
}

#[tokio::test]
async fn chime_scheduler_group_crud_forwards_commands() {
    let fx = sched_fixture(SchedReply::Json(json!({ "id": "g-1" })));
    let (status, _) = post_json(
        &fx.app,
        "/api/chime-scheduler/groups",
        json!({ "name": "Festive", "members": ["a.wav"] }),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(fx.last.lock().unwrap().clone().unwrap()["cmd"], "add_group");

    let (status, _) = delete_json(&fx.app, "/api/chime-scheduler/groups/g-1").await;
    assert_eq!(status, StatusCode::OK);
    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "delete_group");
    assert_eq!(req["id"], "g-1");
}

#[tokio::test]
async fn chime_scheduler_library_list_uses_media_catalog() {
    let fx = sched_fixture_with_rows(
        SchedReply::Json(json!({ "items": [{ "filename": "ignored.wav" }] })),
        &[
            ("slot1", "Chimes/Horn.wav", "Horn.wav", 64),
            ("slot1", "Chimes/sub/Nested.wav", "Nested.wav", 32),
            ("slot1", "Chimes/notes.txt", "notes.txt", 16),
        ],
    );
    let (status, body) = get_json(&fx.app, "/api/chime-scheduler/library").await;
    assert_eq!(status, StatusCode::OK);
    let items = body["items"].as_array().unwrap();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["name"], "Horn.wav");
    assert_eq!(items[0]["rel_path"], "Chimes/Horn.wav");
    assert_eq!(items[0]["size_bytes"], 64);
    assert!(fx.last.lock().unwrap().is_none(), "schedulerd not contacted");
}

/// POST a multipart body to the library upload route and return `(status, json)`.
async fn post_library(app: &Router, body: Vec<u8>) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::POST)
                .uri("/api/chime-scheduler/library")
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

#[tokio::test]
async fn chime_scheduler_library_upload_queues_media_install() {
    let fx = library_fixture(Reply::Json(json!({ "state": "queued", "job_id": "m-1" })));
    let (status, body) = post_library(&fx.app, multipart_body(&[("file", &sample_wav(64))])).await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");
    assert_eq!(body["job_id"], "m-1");

    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "enqueue_mutation");
    assert_eq!(req["partition"], 2);
    assert_eq!(req["mutation"]["op"], "install_file");
    assert_eq!(req["mutation"]["rel_path"], "Chimes/chime.wav");
    assert!(req["mutation"]["source_path"].is_string());
    assert!(req["blob_path"].is_string());
}

#[tokio::test]
async fn chime_scheduler_library_upload_non_wav_is_422_before_forward() {
    let fx = sched_fixture(SchedReply::Json(json!({ "filename": "x" })));
    let (status, body) = post_library(&fx.app, multipart_body(&[("file", b"not a wav")])).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "invalid_wav");
    assert!(
        fx.last.lock().unwrap().is_none(),
        "schedulerd not contacted"
    );
}

#[tokio::test]
async fn chime_scheduler_library_delete_queues_media_remove() {
    let fx = library_fixture(Reply::Json(json!({ "state": "queued", "job_id": "m-1" })));
    let (status, body) = delete_json(&fx.app, "/api/chime-scheduler/library/Horn.wav").await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");
    assert_eq!(body["job_id"], "m-1");

    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "enqueue_mutation");
    assert_eq!(req["partition"], 2);
    assert_eq!(req["mutation"]["op"], "delete_paths");
    assert_eq!(req["mutation"]["rel_paths"][0], "Chimes/Horn.wav");
}

#[tokio::test]
async fn chime_scheduler_library_bulk_delete_enqueues_one_mutation() {
    let fx = library_fixture(Reply::Json(json!({ "state": "queued", "job_id": "m-bulk" })));
    let (status, body) = post_json(
        &fx.app,
        "/api/chime-scheduler/library/bulk-delete",
        json!({ "names": ["Horn.wav", "Airhorn.wav"] }),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");
    assert_eq!(body["job_id"], "m-bulk");

    // gadgetd saw exactly ONE enqueue carrying BOTH derived `Chimes/` paths
    // (one eject/remount for the batch, not one per file).
    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "enqueue_mutation");
    assert_eq!(req["partition"], 2);
    assert_eq!(req["mutation"]["op"], "delete_paths");
    let paths = req["mutation"]["rel_paths"].as_array().unwrap();
    assert_eq!(paths.len(), 2);
    assert_eq!(paths[0], "Chimes/Horn.wav");
    assert_eq!(paths[1], "Chimes/Airhorn.wav");
}

#[tokio::test]
async fn chime_scheduler_library_bulk_delete_rejects_traversal_before_handoff() {
    let fx = library_fixture(Reply::Json(json!({ "state": "queued", "job_id": "m-1" })));
    let (status, body) = post_json(
        &fx.app,
        "/api/chime-scheduler/library/bulk-delete",
        json!({ "names": ["ok.wav", ".."] }),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "invalid_filename");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn chime_scheduler_library_delete_rejects_invalid_name_before_handoff() {
    let fx = library_fixture(Reply::Json(json!({ "state": "queued", "job_id": "m-1" })));
    let (status, body) = delete_json(&fx.app, "/api/chime-scheduler/library/../bad.wav").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["error"]["code"], "not_found");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn chime_scheduler_unavailable_maps_to_503() {
    let fx = sched_fixture(SchedReply::Unavailable);
    let (status, body) = get_json(&fx.app, "/api/chime-scheduler").await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"]["code"], "scheduler_unavailable");
}

#[tokio::test]
async fn chime_scheduler_protocol_error_maps_to_502() {
    let fx = sched_fixture(SchedReply::Protocol);
    let (status, body) = get_json(&fx.app, "/api/chime-scheduler").await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert_eq!(body["error"]["code"], "scheduler_protocol");
}

// ---------------------------------------------------------------------------
// File-backed chime-library routes (`crate::chime_library`): serve audio,
// download, and activate (Set Active). schedulerd owns the library dir; these
// routes only READ it. The gadgetd handoff for activate is mocked.
// ---------------------------------------------------------------------------

/// GET a path and return `(status, content-type, content-disposition, body)`.
async fn get_chime_bytes(
    app: &Router,
    uri: &str,
) -> (StatusCode, Option<String>, Option<String>, Vec<u8>) {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::GET)
                .uri(uri)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let header = |name: axum::http::HeaderName| {
        resp.headers()
            .get(&name)
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned)
    };
    let content_type = header(axum::http::header::CONTENT_TYPE);
    let disposition = header(axum::http::header::CONTENT_DISPOSITION);
    let body = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap()
        .to_vec();
    (status, content_type, disposition, body)
}

/// POST with an empty body and return `(status, parsed-json)`.
async fn post_empty(app: &Router, uri: &str) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::POST)
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

/// A library fixture wired to a mock gadgetd (for activate) plus a real,
/// writable library dir so tests can seed library files.
struct LibraryFixture {
    _dir: TempDir,
    app: Router,
    last: Arc<Mutex<Option<Value>>>,
    library_dir: std::path::PathBuf,
}

fn library_fixture(gadget_reply: Reply) -> LibraryFixture {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    seed(&db_path);

    let static_dir = dir.path().join("static");
    std::fs::create_dir_all(&static_dir).unwrap();
    std::fs::write(static_dir.join("index.html"), "<!doctype html>shell").unwrap();

    let media_ro = dir.path().join("media-ro");
    let library_dir = media_ro.join("Chimes");
    std::fs::create_dir_all(&library_dir).unwrap();

    let catalog = Catalog::open(&db_path).unwrap();
    let media = MediaConfig::new(dir.path().join("archive"), dir.path().join("cache"))
        .with_media_ro_root(media_ro);

    let last = Arc::new(Mutex::new(None));
    let gadget: Arc<dyn GadgetClient> = Arc::new(MockGadget {
        reply: gadget_reply,
        last: Arc::clone(&last),
    });
    // schedulerd is unused by the file-backed routes; a dead socket would do,
    // but a mock keeps an accidental call loud.
    let scheduler: Arc<dyn SchedulerClient> = Arc::new(MockScheduler {
        reply: SchedReply::Unavailable,
        last: Arc::new(Mutex::new(None)),
        staged_existed: Arc::new(Mutex::new(None)),
    });
    let app = router_with_clients(
        catalog,
        static_dir,
        media,
        gadget,
        scheduler,
        library_dir.clone(),
    );
    LibraryFixture {
        _dir: dir,
        app,
        last,
        library_dir,
    }
}

#[tokio::test]
async fn library_audio_serves_wav_bytes_inline() {
    let fx = library_fixture(Reply::Json(json!({})));
    let wav = sample_wav(64);
    std::fs::write(fx.library_dir.join("Horn.wav"), &wav).unwrap();

    let (status, ct, disp, body) =
        get_chime_bytes(&fx.app, "/api/chimes/library/Horn.wav/audio").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(ct.as_deref(), Some("audio/wav"));
    assert!(disp.is_none(), "inline audio must not be an attachment");
    assert_eq!(body, wav);
}

#[tokio::test]
async fn library_download_sets_attachment_disposition() {
    let fx = library_fixture(Reply::Json(json!({})));
    let wav = sample_wav(64);
    std::fs::write(fx.library_dir.join("Horn.wav"), &wav).unwrap();

    let (status, ct, disp, body) =
        get_chime_bytes(&fx.app, "/api/chimes/library/Horn.wav/download").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(ct.as_deref(), Some("audio/wav"));
    assert_eq!(disp.as_deref(), Some("attachment; filename=\"Horn.wav\""));
    assert_eq!(body, wav);
}

#[tokio::test]
async fn library_audio_missing_file_is_404() {
    let fx = library_fixture(Reply::Json(json!({})));
    let (status, ..) = get_chime_bytes(&fx.app, "/api/chimes/library/Gone.wav/audio").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn library_audio_rejects_traversal() {
    let fx = library_fixture(Reply::Json(json!({})));
    // Seed a file OUTSIDE the library dir (in its parent) that a traversal
    // would target.
    let outside = fx.library_dir.parent().unwrap().join("secret.wav");
    std::fs::write(&outside, b"x").unwrap();
    // `%2e%2e%2f` decodes to `../`; the single-segment filename guard rejects it.
    let (status, ..) = get_raw(&fx.app, "/api/chimes/library/%2e%2e%2fsecret.wav/audio").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn library_audio_oversize_file_is_404() {
    let fx = library_fixture(Reply::Json(json!({})));
    // A safe-named file just over the 1 MiB cap must be refused as a flat 404
    // (uniform with the jail; never a 413 that would leak that it exists), and
    // the capped read must never buffer the whole oversized file.
    let big = vec![0u8; (1024 * 1024) + 1];
    std::fs::write(fx.library_dir.join("Big.wav"), &big).unwrap();
    let (status, ..) = get_chime_bytes(&fx.app, "/api/chimes/library/Big.wav/audio").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn library_activate_happy_path_enqueues_lockchime_install() {
    let fx = library_fixture(Reply::Json(json!({ "job_id": "m-7", "state": "queued" })));
    std::fs::write(fx.library_dir.join("Horn.wav"), sample_wav(64)).unwrap();

    let (status, body) = post_empty(&fx.app, "/api/chimes/library/Horn.wav/activate").await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");

    // Routed through the SAME frictionless install primitive: MEDIA partition,
    // fixed LockChime.wav destination.
    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "enqueue_mutation");
    assert_eq!(req["partition"], 2);
    assert_eq!(req["mutation"]["op"], "install_file");
    assert_eq!(req["mutation"]["rel_path"], "LockChime.wav");
}

#[tokio::test]
async fn library_activate_missing_file_is_404() {
    let fx = library_fixture(Reply::Json(json!({ "state": "queued" })));
    let (status, _) = post_empty(&fx.app, "/api/chimes/library/Gone.wav/activate").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn library_activate_invalid_wav_is_422_before_handoff() {
    let fx = library_fixture(Reply::Json(json!({ "state": "queued" })));
    std::fs::write(fx.library_dir.join("Bad.wav"), b"not a real wav").unwrap();
    let (status, body) = post_empty(&fx.app, "/api/chimes/library/Bad.wav/activate").await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "invalid_wav");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

// ---------------------------------------------------------------------------
// Music folder/move/delete endpoints
// (`POST /api/music/folder`, `POST /api/music/folder-delete`,
//  `POST /api/music/move`, `POST /api/music/delete`,
//  extended `POST /api/music` with optional `path` field).
//
// The gadgetd socket is mocked via two mock variants:
//   * `MockGadget` (already defined above) — records the LAST call; used where
//     only one gadgetd round-trip is expected.
//   * `AllCallsMock` — records ALL calls; used for move (two round-trips).
// ---------------------------------------------------------------------------

/// A mock [`GadgetClient`] that records EVERY call in order, returning the same
/// canned reply for each. Used by the move test, which issues two enqueue calls.
struct AllCallsMock {
    reply: Reply,
    calls: Arc<Mutex<Vec<Value>>>,
}

impl GadgetClient for AllCallsMock {
    fn call(&self, request: Value) -> Result<Value, TransportError> {
        self.calls.lock().unwrap().push(request);
        match &self.reply {
            Reply::Json(v) => Ok(v.clone()),
            Reply::Unavailable => Err(TransportError::Unavailable("socket down".to_owned())),
        }
    }
}

/// Music fixture that seeds real files on a media-ro temp dir and wires the
/// router to an `AllCallsMock` so every gadgetd call is captured.
struct MusicFixture {
    _dir: TempDir,
    app: Router,
    calls: Arc<Mutex<Vec<Value>>>,
    /// Absolute path of the media-ro root; tests may seed additional files/dirs.
    media_ro: std::path::PathBuf,
}

/// Build a music fixture with an optional set of pre-seeded media-ro files.
///
/// `files` is a slice of `(relative-path, bytes)` pairs seeded under the
/// media-ro root (e.g. `("Music/A/x.mp3", b"fake")`).
fn music_fixture_with_media(files: &[(&str, &[u8])]) -> MusicFixture {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("catalog.db");
    {
        let mut conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        indexd::db::apply_migrations(&mut conn).unwrap();
    }

    let static_dir = dir.path().join("static");
    std::fs::create_dir_all(&static_dir).unwrap();
    std::fs::write(static_dir.join("index.html"), "<!doctype html>shell").unwrap();

    let media_ro = dir.path().join("media-ro");
    std::fs::create_dir_all(&media_ro).unwrap();
    for (rel, bytes) in files {
        let path = media_ro.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, bytes).unwrap();
    }

    let catalog = Catalog::open(&db_path).unwrap();
    let media = MediaConfig::new(dir.path().join("archive"), dir.path().join("cache"))
        .with_media_ro_root(media_ro.clone());

    let calls = Arc::new(Mutex::new(Vec::new()));
    let gadget: Arc<dyn GadgetClient> = Arc::new(AllCallsMock {
        reply: Reply::Json(json!({ "job_id": "m-1", "state": "queued" })),
        calls: Arc::clone(&calls),
    });
    let app = router_with_gadget(catalog, static_dir, media, gadget);
    MusicFixture {
        _dir: dir,
        app,
        calls,
        media_ro,
    }
}
/// POST a multipart body to `POST /api/music` and return `(status, json)`.
async fn post_music(app: &Router, body: Vec<u8>) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method(Method::POST)
                .uri("/api/music")
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

/// Build a multipart/form-data body containing a text `path` field followed by
/// a binary `file` field (audio/mpeg). Suitable for testing the extended
/// `POST /api/music` handler.
fn music_multipart_with_path(mp3_filename: &str, mp3_bytes: &[u8], path: &str) -> Vec<u8> {
    let mut body = Vec::new();
    // Text "path" field — no filename in Content-Disposition.
    body.extend_from_slice(format!("--{BOUNDARY}\r\n").as_bytes());
    body.extend_from_slice(
        format!("Content-Disposition: form-data; name=\"path\"\r\n\r\n").as_bytes(),
    );
    body.extend_from_slice(path.as_bytes());
    body.extend_from_slice(b"\r\n");
    // Binary "file" field.
    body.extend_from_slice(format!("--{BOUNDARY}\r\n").as_bytes());
    body.extend_from_slice(
        format!(
            "Content-Disposition: form-data; name=\"file\"; filename=\"{mp3_filename}\"\r\n"
        )
        .as_bytes(),
    );
    body.extend_from_slice(b"Content-Type: audio/mpeg\r\n\r\n");
    body.extend_from_slice(mp3_bytes);
    body.extend_from_slice(b"\r\n");
    body.extend_from_slice(format!("--{BOUNDARY}--\r\n").as_bytes());
    body
}

// ── folder create ──────────────────────────────────────────────────────────

#[tokio::test]
async fn create_folder_enqueues_keep_file_and_returns_202() {
    let fx = delete_fixture(Reply::Json(json!({ "job_id": "m-1", "state": "queued" })));
    let (status, body) =
        post_json(&fx.app, "/api/music/folder", json!({ "path": "NewBand" })).await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");

    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "enqueue_mutation");
    assert_eq!(req["partition"], 2);
    assert_eq!(req["mutation"]["op"], "install_file");
    assert_eq!(req["mutation"]["rel_path"], "Music/NewBand/.teslausb-keep");
}

#[tokio::test]
async fn create_folder_nested_path_enqueues_nested_keep() {
    let fx = delete_fixture(Reply::Json(json!({ "job_id": "m-2", "state": "queued" })));
    let (status, _) = post_json(
        &fx.app,
        "/api/music/folder",
        json!({ "path": "Artist/Album" }),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);

    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["mutation"]["rel_path"], "Music/Artist/Album/.teslausb-keep");
}

#[tokio::test]
async fn create_folder_traversal_path_is_400() {
    let fx = delete_fixture(Reply::Json(json!({ "state": "queued" })));
    let (status, body) =
        post_json(&fx.app, "/api/music/folder", json!({ "path": ".." })).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_path");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

// ── folder delete ──────────────────────────────────────────────────────────

#[tokio::test]
async fn folder_delete_enqueues_remove_and_returns_202() {
    // Seed three files under Music/NewBand on the media-ro filesystem.
    let fx = music_fixture_with_media(&[
        ("Music/NewBand/a.mp3", b"x"),
        ("Music/NewBand/b.mp3", b"y"),
        ("Music/NewBand/.teslausb-keep", b"k"),
    ]);
    let (status, body) =
        post_json(&fx.app, "/api/music/folder-delete", json!({ "path": "NewBand" })).await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");

    // All delete calls must be delete_paths enqueues on partition 2; the final
    // call must be the remove_empty_dir prune of the now-empty folder.
    let calls = fx.calls.lock().unwrap().clone();
    assert!(calls.len() >= 2, "expected file deletes + a dir prune");

    let (prune, deletes) = calls.split_last().unwrap();
    for c in deletes {
        assert_eq!(c["cmd"], "enqueue_mutation");
        assert_eq!(c["partition"], 2);
        assert_eq!(c["mutation"]["op"], "delete_paths");
    }
    assert_eq!(prune["cmd"], "enqueue_mutation");
    assert_eq!(prune["partition"], 2);
    assert_eq!(prune["mutation"]["op"], "remove_empty_dir");
    assert_eq!(prune["mutation"]["rel_path"], "Music/NewBand");

    // The union of all rel_paths across the delete calls must equal the files.
    let mut all_paths: Vec<String> = deletes
        .iter()
        .flat_map(|c| {
            c["mutation"]["rel_paths"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_owned())
        })
        .collect();
    all_paths.sort();
    all_paths.dedup();
    assert_eq!(
        all_paths,
        vec![
            "Music/NewBand/.teslausb-keep".to_owned(),
            "Music/NewBand/a.mp3".to_owned(),
            "Music/NewBand/b.mp3".to_owned(),
        ],
        "union of delete_paths must equal the seeded child files"
    );
}

#[tokio::test]
async fn folder_delete_empty_folder_on_disk_repairs_orphan_and_returns_202() {
    // Folder directory exists on disk but contains zero files. Rather than 404,
    // the prune is still enqueued to REPAIR the already-orphaned empty directory
    // (a folder whose files were deleted before the prune existed).
    let fx = music_fixture_with_media(&[]);
    std::fs::create_dir_all(fx.media_ro.join("Music").join("EmptyBand")).unwrap();

    let (status, body) =
        post_json(&fx.app, "/api/music/folder-delete", json!({ "path": "EmptyBand" })).await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");

    // Exactly one call: the remove_empty_dir prune (no files to delete).
    let calls = fx.calls.lock().unwrap().clone();
    assert_eq!(calls.len(), 1, "only the dir prune is enqueued");
    assert_eq!(calls[0]["mutation"]["op"], "remove_empty_dir");
    assert_eq!(calls[0]["mutation"]["rel_path"], "Music/EmptyBand");
}

#[tokio::test]
async fn folder_delete_traversal_path_is_400() {
    let fx = delete_fixture(Reply::Json(json!({ "state": "queued" })));
    let (status, body) =
        post_json(&fx.app, "/api/music/folder-delete", json!({ "path": ".." })).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_path");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[cfg(unix)]
#[tokio::test]
async fn folder_delete_symlinked_folder_is_404_and_not_followed() {
    // Defence-in-depth: a folder that is a symlink (diverging canonical vs lexical
    // path) must be refused — never followed into another folder's files.
    let fx = music_fixture_with_media(&[("Music/Keep/song.mp3", b"x")]);
    let music = fx.media_ro.join("Music");
    std::os::unix::fs::symlink(music.join("Keep"), music.join("Link")).unwrap();

    let (status, _) =
        post_json(&fx.app, "/api/music/folder-delete", json!({ "path": "Link" })).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(
        fx.calls.lock().unwrap().is_empty(),
        "a symlinked folder must not enqueue any delete"
    );
}

// ── move ───────────────────────────────────────────────────────────────────

#[tokio::test]
async fn move_music_enqueues_install_only_and_returns_202() {
    // move_music is copy-only: it enqueues the destination install and nothing
    // else. The SPA deletes the source after the destination lands in the catalog.
    let fx = music_fixture_with_media(&[("Music/A/x.mp3", b"fake mp3 data")]);
    let (status, body) = post_json(
        &fx.app,
        "/api/music/move",
        json!({ "from": "A/x.mp3", "to": "B/x.mp3" }),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["state"], "queued");

    let calls = fx.calls.lock().unwrap().clone();
    assert_eq!(calls.len(), 1, "expected exactly one gadgetd call (install only; no delete)");

    // The single call must be install_file at the destination.
    assert_eq!(calls[0]["cmd"], "enqueue_mutation");
    assert_eq!(calls[0]["partition"], 2);
    assert_eq!(calls[0]["mutation"]["op"], "install_file");
    assert_eq!(calls[0]["mutation"]["rel_path"], "Music/B/x.mp3");

    // No delete_paths call — the SPA owns the source removal after convergence.
    assert!(
        calls.iter().all(|c| c["mutation"]["op"] != "delete_paths"),
        "move must not enqueue any delete_paths"
    );
}

#[tokio::test]
async fn move_missing_source_is_404() {
    let fx = music_fixture_with_media(&[]);
    let (status, _) = post_json(
        &fx.app,
        "/api/music/move",
        json!({ "from": "A/x.mp3", "to": "B/x.mp3" }),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(fx.calls.lock().unwrap().is_empty(), "gadgetd not contacted");
}

#[tokio::test]
async fn move_to_existing_dest_is_409() {
    let fx = music_fixture_with_media(&[
        ("Music/A/x.mp3", b"source data"),
        ("Music/B/x.mp3", b"existing data"),
    ]);
    let (status, body) = post_json(
        &fx.app,
        "/api/music/move",
        json!({ "from": "A/x.mp3", "to": "B/x.mp3" }),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["code"], "already_exists");
    assert!(fx.calls.lock().unwrap().is_empty(), "gadgetd not contacted");
}

#[tokio::test]
async fn move_same_from_to_is_400() {
    let fx = music_fixture_with_media(&[("Music/A/x.mp3", b"data")]);
    let (status, body) = post_json(
        &fx.app,
        "/api/music/move",
        json!({ "from": "A/x.mp3", "to": "A/x.mp3" }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_move");
    assert!(fx.calls.lock().unwrap().is_empty(), "gadgetd not contacted");
}

#[tokio::test]
async fn move_with_traversal_in_from_is_400() {
    let fx = music_fixture_with_media(&[]);
    let (status, body) = post_json(
        &fx.app,
        "/api/music/move",
        json!({ "from": "../secret.mp3", "to": "B/x.mp3" }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_path");
    assert!(fx.calls.lock().unwrap().is_empty(), "gadgetd not contacted");
}

#[tokio::test]
async fn move_with_traversal_in_to_is_400() {
    let fx = music_fixture_with_media(&[("Music/A/x.mp3", b"data")]);
    let (status, body) = post_json(
        &fx.app,
        "/api/music/move",
        json!({ "from": "A/x.mp3", "to": "../B/x.mp3" }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_path");
    assert!(fx.calls.lock().unwrap().is_empty(), "gadgetd not contacted");
}

// ── install with path field ────────────────────────────────────────────────

#[tokio::test]
async fn install_music_with_path_builds_subdir_rel_path() {
    let fx = delete_fixture(Reply::Json(json!({ "job_id": "m-10", "state": "queued" })));
    let body = music_multipart_with_path("track.mp3", b"ID3\x00fake", "Daft Punk");
    let (status, _) = post_music(&fx.app, body).await;
    assert_eq!(status, StatusCode::ACCEPTED);

    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["mutation"]["op"], "install_file");
    assert_eq!(req["mutation"]["rel_path"], "Music/Daft Punk/track.mp3");
}

#[tokio::test]
async fn install_music_with_nested_path_builds_nested_rel_path() {
    let fx = delete_fixture(Reply::Json(json!({ "job_id": "m-11", "state": "queued" })));
    let body = music_multipart_with_path("track.mp3", b"ID3\x00fake", "Artist/Album");
    let (status, _) = post_music(&fx.app, body).await;
    assert_eq!(status, StatusCode::ACCEPTED);

    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["mutation"]["rel_path"], "Music/Artist/Album/track.mp3");
}

#[tokio::test]
async fn install_music_with_traversal_path_is_400_before_handoff() {
    let fx = delete_fixture(Reply::Json(json!({ "state": "queued" })));
    let body = music_multipart_with_path("track.mp3", b"ID3\x00fake", "..");
    let (status, resp) = post_music(&fx.app, body).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(resp["error"]["code"], "invalid_path");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn install_music_without_path_is_top_level() {
    // No `path` field → existing behaviour: rel_path = "Music/<name>".
    let fx = delete_fixture(Reply::Json(json!({ "job_id": "m-12", "state": "queued" })));
    let body = multipart_body_with_filename("song.mp3", &[("file", b"ID3\x00fake")]);
    let (status, _) = post_music(&fx.app, body).await;
    assert_eq!(status, StatusCode::ACCEPTED);

    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["mutation"]["rel_path"], "Music/song.mp3");
}

// ── nested delete ──────────────────────────────────────────────────────────

#[tokio::test]
async fn delete_music_paths_maps_nested_paths_to_music_prefix() {
    let fx = delete_fixture(Reply::Json(json!({ "job_id": "m-d", "state": "queued" })));
    let (status, _) = post_json(
        &fx.app,
        "/api/music/delete",
        json!({ "paths": ["A/x.mp3", "y.mp3"] }),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);

    let req = fx.last.lock().unwrap().clone().unwrap();
    assert_eq!(req["cmd"], "enqueue_mutation");
    assert_eq!(req["partition"], 2);
    assert_eq!(req["mutation"]["op"], "delete_paths");
    let paths = req["mutation"]["rel_paths"].as_array().unwrap();
    assert_eq!(paths.len(), 2);
    assert_eq!(paths[0], "Music/A/x.mp3");
    assert_eq!(paths[1], "Music/y.mp3");
}

#[tokio::test]
async fn delete_music_paths_traversal_component_is_400_before_handoff() {
    let fx = delete_fixture(Reply::Json(json!({ "state": "queued" })));
    let (status, body) = post_json(
        &fx.app,
        "/api/music/delete",
        json!({ "paths": ["ok.mp3", "../evil.mp3"] }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_path");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn delete_music_paths_over_cap_is_422_before_handoff() {
    let fx = delete_fixture(Reply::Json(json!({ "state": "queued" })));
    let many: Vec<String> = (0..=crate::media_upload::MAX_BULK_DELETE)
        .map(|i| format!("{i}.mp3"))
        .collect();
    let (status, body) = post_json(&fx.app, "/api/music/delete", json!({ "paths": many })).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "batch_too_large");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

// ── run_remove_many chunking (>16 paths) ──────────────────────────────────

#[tokio::test]
async fn folder_delete_with_more_than_16_files_produces_chunked_enqueues() {
    // Seed 20 files under Music/BigBand — more than DELETE_CHUNK=16.
    let files: Vec<(String, &[u8])> = (0..20)
        .map(|i| (format!("Music/BigBand/track{i:02}.mp3"), b"x".as_slice()))
        .collect();
    let file_refs: Vec<(&str, &[u8])> = files.iter().map(|(s, b)| (s.as_str(), *b)).collect();
    let fx = music_fixture_with_media(&file_refs);

    let (status, _) =
        post_json(&fx.app, "/api/music/folder-delete", json!({ "path": "BigBand" })).await;
    assert_eq!(status, StatusCode::ACCEPTED);

    let calls = fx.calls.lock().unwrap().clone();
    // The final call is the remove_empty_dir prune; the rest are delete chunks.
    let (prune, deletes) = calls.split_last().unwrap();
    assert_eq!(prune["mutation"]["op"], "remove_empty_dir");
    assert_eq!(prune["mutation"]["rel_path"], "Music/BigBand");

    // Must have produced ≥2 delete_paths calls (20 files / 16 per chunk = 2 chunks).
    assert!(
        deletes.len() >= 2,
        "expected at least 2 chunked enqueues, got {}",
        deletes.len()
    );

    // Each individual delete call must have ≤16 paths.
    for (i, c) in deletes.iter().enumerate() {
        assert_eq!(c["mutation"]["op"], "delete_paths");
        let paths = c["mutation"]["rel_paths"].as_array().unwrap();
        assert!(
            paths.len() <= 16,
            "chunk {i} has {} paths (must be ≤16)",
            paths.len()
        );
    }

    // The union of all paths must equal all 20 seeded files.
    let mut all_paths: Vec<String> = deletes
        .iter()
        .flat_map(|c| {
            c["mutation"]["rel_paths"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_owned())
        })
        .collect();
    all_paths.sort();
    all_paths.dedup();
    assert_eq!(all_paths.len(), 20, "union of all chunks must cover all 20 files");
}

// ── validate_music_subpath extended rejections ────────────────────────────

#[tokio::test]
async fn validate_music_subpath_rejects_backslash_component() {
    let fx = delete_fixture(Reply::Json(json!({ "state": "queued" })));
    let (status, body) = post_json(
        &fx.app,
        "/api/music/folder",
        json!({ "path": r"Band\Album" }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_path");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn validate_music_subpath_rejects_control_char_component() {
    let fx = delete_fixture(Reply::Json(json!({ "state": "queued" })));
    // A path component containing a tab (0x09, an ASCII control char).
    let (status, body) = post_json(
        &fx.app,
        "/api/music/folder",
        json!({ "path": "Band\tAlbum" }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_path");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

// ── delete_music_paths double-prefix guard ────────────────────────────────

#[tokio::test]
async fn delete_music_paths_rejects_music_prefix_in_path() {
    let fx = delete_fixture(Reply::Json(json!({ "state": "queued" })));
    // Caller incorrectly includes the "Music/" prefix — must be rejected 400.
    let (status, body) = post_json(
        &fx.app,
        "/api/music/delete",
        json!({ "paths": ["Music/A/x.mp3"] }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_path");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}

#[tokio::test]
async fn delete_music_paths_rejects_music_prefix_case_insensitive() {
    let fx = delete_fixture(Reply::Json(json!({ "state": "queued" })));
    let (status, body) = post_json(
        &fx.app,
        "/api/music/delete",
        json!({ "paths": ["MUSIC/A/x.mp3"] }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_path");
    assert!(fx.last.lock().unwrap().is_none(), "gadgetd not contacted");
}
