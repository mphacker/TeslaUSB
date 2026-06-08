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
use axum::http::{Request, StatusCode};
use rusqlite::{Connection, params};
use serde_json::Value;
use tempfile::TempDir;
use tower::ServiceExt;

use crate::{Catalog, build_router};

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
    let app = build_router(catalog, static_dir);
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
            (1, 1, 'front', 'p1/clip-1/front.mp4', 'live', 0, 60.0, 1111),
            (2, 1, 'back',  'p1/clip-1/back.mp4',  'live', 0, 60.0, 2222),
            (3, 2, 'front', 'p1/clip-2/front.mp4', 'live', 0, 30.0, 3333);
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
    // view_kind passed through opaquely (code writes "live").
    assert_eq!(angles[0]["view_kind"], "live");

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
