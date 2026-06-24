//! HTTP routing: the `/api/*` read handlers plus the static-SPA host.
//!
//! Each handler offloads its blocking `rusqlite` work onto a blocking task via
//! [`read`], so no database connection ever crosses an `.await` and the async
//! runtime threads stay free. The `/api` sub-router carries its own JSON `404`
//! fallback so unknown API paths never fall through to the SPA shell.

use std::convert::Infallible;
use std::path::PathBuf;
use std::time::Duration;

use axum::Json;
use axum::Router;
use axum::extract::{DefaultBodyLimit, Path, Query, State};
use axum::http::StatusCode;
use axum::response::Sse;
use axum::response::sse::{Event, KeepAlive};
use axum::routing::{delete, get, post};
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio_stream::wrappers::BroadcastStream;
use tokio_stream::wrappers::errors::BroadcastStreamRecvError;
use tokio_stream::{Stream, StreamExt};
use tower_http::services::{ServeDir, ServeFile};

use crate::dto::{
    AnalyticsDto, ClipDto, DaySummary, EventDto, Page, PrefDto, TripDetailDto, TripDto,
};
use crate::error::ApiError;
use crate::gadget::{self, DeleteRefusal, MutationOutcome, TransportError};
use crate::jobs::{JobEvent, JobState, JobStatus};
use crate::{AppState, Catalog, query};

/// Default page size when `limit` is omitted.
const DEFAULT_LIMIT: i64 = 100;

/// Maximum page size; larger `limit` values are clamped to this.
const MAX_LIMIT: i64 = 500;

/// Maximum paths per single `delete_paths` enqueue: gadgetd's `enqueue_mutation`
/// validates each mutation at enqueue time and rejects a `DeletePaths` whose set
/// exceeds `MAX_DELETE_PATHS=16` (gadgetd `handoff.rs`). [`run_remove_many`]
/// chunks at this size so every individual enqueue stays within the limit.
const DELETE_CHUNK: usize = 16;

/// Assemble the application router: the `/api` read endpoints nested under a
/// JSON-404 fallback, with everything else served by the SPA host.
pub(crate) fn router(state: AppState, static_dir: PathBuf) -> Router {
    let index = static_dir.join("index.html");
    let spa = ServeDir::new(static_dir).fallback(ServeFile::new(index));

    let api = Router::new()
        .route("/days", get(days))
        .route("/trips", get(trips))
        .route("/trips/page", get(trips_page))
        .route("/trips/{id}", get(trip_detail))
        .route("/events", get(events))
        .route("/events/{id}", get(event_detail))
        .route("/media-events", get(media_events_stream))
        .route("/clips", get(clips))
        .route("/clips/{id}", get(clip_detail).delete(delete_clip))
        .route("/clips/{id}/stream", get(crate::media::stream))
        .route("/clips/{id}/export", get(crate::media::export))
        .route("/media/content", get(crate::media::content))
        .route(
            "/clips/{id}/angles/{camera}/download",
            get(crate::media::download),
        )
        .route("/handoff/{id}", get(handoff_status))
        .route("/gadget/status", get(gadget_status))
        .route(
            "/chimes",
            post(crate::chimes::install_chime)
                .get(crate::chimes::list_chimes)
                .layer(DefaultBodyLimit::max(crate::chimes::CHIME_BODY_LIMIT)),
        )
        .route("/chimes/{id}", delete(crate::chimes::remove_chime))
        .route(
            "/boombox",
            get(crate::boombox::list_boombox)
                .post(crate::boombox::install_boombox)
                .layer(DefaultBodyLimit::max(crate::boombox::BOOMBOX_BODY_LIMIT)),
        )
        .route("/boombox/{name}", delete(crate::boombox::remove_boombox))
        .route(
            "/boombox/bulk-delete",
            post(crate::boombox::bulk_delete_boombox),
        )
        .route(
            "/music",
            get(crate::music::list_music)
                .post(crate::music::install_music)
                .layer(DefaultBodyLimit::max(crate::music::MUSIC_BODY_LIMIT)),
        )
        .route("/music/{name}", delete(crate::music::remove_music))
        .route("/music/bulk-delete", post(crate::music::bulk_delete_music))
        .route("/music/folder", post(crate::music::create_folder))
        .route("/music/folder-delete", post(crate::music::delete_folder))
        .route("/music/move", post(crate::music::move_music))
        .route("/music/delete", post(crate::music::delete_music_paths))
        .route(
            "/lightshows",
            get(crate::lightshows::list_lightshows)
                .post(crate::lightshows::install_lightshow)
                .layer(DefaultBodyLimit::max(
                    crate::lightshows::LIGHTSHOW_BODY_LIMIT,
                )),
        )
        .route(
            "/lightshows/{name}",
            delete(crate::lightshows::remove_lightshow),
        )
        .route(
            "/lightshows/bulk-delete",
            post(crate::lightshows::bulk_delete_lightshows),
        )
        .route(
            "/plates",
            get(crate::plates::list_plates)
                .post(crate::plates::install_plate)
                .layer(DefaultBodyLimit::max(crate::plates::PLATES_BODY_LIMIT)),
        )
        .route("/plates/{name}", delete(crate::plates::remove_plate))
        .route(
            "/plates/bulk-delete",
            post(crate::plates::bulk_delete_plates),
        )
        .route(
            "/wraps",
            get(crate::wraps::list_wraps)
                .post(crate::wraps::install_wrap)
                .layer(DefaultBodyLimit::max(crate::wraps::WRAPS_BODY_LIMIT)),
        )
        .route("/wraps/{name}", delete(crate::wraps::remove_wrap))
        .route("/wraps/bulk-delete", post(crate::wraps::bulk_delete_wraps))
        .route("/jobs", get(jobs_stream))
        .route("/jobs/failed", get(jobs_failed))
        .route("/analytics", get(analytics))
        .route("/settings", get(settings).put(put_setting))
        .merge(crate::chime_scheduler::routes())
        .merge(crate::chime_library::routes())
        .merge(crate::health::routes())
        .merge(crate::timezone::routes())
        .fallback(api_not_found)
        .with_state(state);

    Router::new().nest("/api", api).fallback_service(spa)
}

/// Run a read query on a blocking task using a fresh read-only connection.
///
/// Keeps the non-`Send` [`Connection`] entirely off the async runtime: it is
/// created and dropped inside the blocking closure. Shared with the `media`
/// handlers, which resolve angle file paths through the same path.
pub(crate) async fn read<T, F>(catalog: Catalog, query_fn: F) -> Result<T, ApiError>
where
    F: FnOnce(&Connection) -> Result<T, rusqlite::Error> + Send + 'static,
    T: Send + 'static,
{
    tokio::task::spawn_blocking(move || {
        let conn = catalog.connect()?;
        query_fn(&conn)
    })
    .await
    .map_err(|_| ApiError::Internal)?
    .map_err(ApiError::from)
}

/// Query parameters for `GET /api/trips`.
#[derive(Deserialize)]
struct TripsQuery {
    /// Optional civil-day filter (`YYYY-MM-DD`).
    day: Option<String>,
}

/// Query parameters for cursor-paginated `GET /api/events`.
#[derive(Deserialize)]
struct EventsQuery {
    /// Opaque cursor echoed from `next_cursor`.
    cursor: Option<String>,
    /// Page size (clamped to [`MAX_LIMIT`]).
    limit: Option<i64>,
    /// Optional filter to a single trip.
    trip: Option<i64>,
}

/// Query parameters for cursor-paginated `GET /api/clips`.
#[derive(Deserialize)]
struct ClipsQuery {
    /// Opaque cursor echoed from `next_cursor`.
    cursor: Option<String>,
    /// Page size (clamped to [`MAX_LIMIT`]).
    limit: Option<i64>,
    /// Optional `folder_class` filter.
    folder_class: Option<String>,
}

/// Query parameters for cursor-paginated `GET /api/trips/page`.
#[derive(Deserialize)]
struct TripsPageQuery {
    /// Opaque cursor echoed from `next_cursor`.
    cursor: Option<String>,
    /// Page size (clamped to [`MAX_LIMIT`]).
    limit: Option<i64>,
}

async fn days(State(state): State<AppState>) -> Result<Json<Vec<DaySummary>>, ApiError> {
    let out = read(state.catalog, query::list_days).await?;
    Ok(Json(out))
}

async fn trips(
    State(state): State<AppState>,
    Query(q): Query<TripsQuery>,
) -> Result<Json<Vec<TripDto>>, ApiError> {
    let out = read(state.catalog, move |conn| {
        query::list_trips(conn, q.day.as_deref())
    })
    .await?;
    Ok(Json(out))
}

async fn trips_page(
    State(state): State<AppState>,
    Query(q): Query<TripsPageQuery>,
) -> Result<Json<Page<TripDto>>, ApiError> {
    let limit = validate_limit(q.limit)?;
    let keyset = if let Some(cursor) = q.cursor {
        let (ts, id, snap) = decode_cursor(&cursor, "trips")?;
        query::Keyset {
            snap,
            after: Some((ts, id)),
        }
    } else {
        let Some(snap) = read(state.catalog.clone(), move |conn| {
            query::snapshot_max_id(conn, query::SnapshotResource::Trips)
        })
        .await?
        else {
            return Ok(Json(Page {
                items: vec![],
                next_cursor: None,
                limit,
            }));
        };
        query::Keyset { snap, after: None }
    };
    let snap = keyset.snap;
    let items = read(state.catalog, move |conn| query::list_trips_page(conn, keyset, limit)).await?;
    Ok(Json(into_page(
        items,
        limit,
        snap,
        "trips",
        |trip| trip.started_at,
        |trip| trip.id,
    )))
}

async fn trip_detail(
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> Result<Json<TripDetailDto>, ApiError> {
    let out = read(state.catalog, move |conn| query::get_trip(conn, id)).await?;
    out.map(Json).ok_or(ApiError::NotFound)
}

async fn events(
    State(state): State<AppState>,
    Query(q): Query<EventsQuery>,
) -> Result<Json<Page<EventDto>>, ApiError> {
    let limit = validate_limit(q.limit)?;
    let trip = q.trip;
    let keyset = if let Some(cursor) = q.cursor {
        let (ts, id, snap) = decode_cursor(&cursor, "events")?;
        query::Keyset {
            snap,
            after: Some((ts, id)),
        }
    } else {
        let Some(snap) = read(state.catalog.clone(), move |conn| {
            query::snapshot_max_id(conn, query::SnapshotResource::Events)
        })
        .await?
        else {
            return Ok(Json(Page {
                items: vec![],
                next_cursor: None,
                limit,
            }));
        };
        query::Keyset { snap, after: None }
    };
    let snap = keyset.snap;
    let items = read(state.catalog, move |conn| query::list_events(conn, keyset, limit, trip)).await?;
    Ok(Json(into_page(
        items,
        limit,
        snap,
        "events",
        |event| event.t,
        |event| event.id,
    )))
}

async fn event_detail(
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> Result<Json<EventDto>, ApiError> {
    let out = read(state.catalog, move |conn| query::get_event(conn, id)).await?;
    out.map(Json).ok_or(ApiError::NotFound)
}

async fn clips(
    State(state): State<AppState>,
    Query(q): Query<ClipsQuery>,
) -> Result<Json<Page<ClipDto>>, ApiError> {
    let limit = validate_limit(q.limit)?;
    let folder_class = q.folder_class;
    let keyset = if let Some(cursor) = q.cursor {
        let (ts, id, snap) = decode_cursor(&cursor, "clips")?;
        query::Keyset {
            snap,
            after: Some((ts, id)),
        }
    } else {
        let Some(snap) = read(state.catalog.clone(), move |conn| {
            query::snapshot_max_id(conn, query::SnapshotResource::Clips)
        })
        .await?
        else {
            return Ok(Json(Page {
                items: vec![],
                next_cursor: None,
                limit,
            }));
        };
        query::Keyset { snap, after: None }
    };
    let snap = keyset.snap;
    let items = read(state.catalog, move |conn| {
        query::list_clips(conn, keyset, limit, folder_class.as_deref())
    })
    .await?;
    Ok(Json(into_page(
        items,
        limit,
        snap,
        "clips",
        |clip| clip.started_at,
        |clip| clip.id,
    )))
}

async fn clip_detail(
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> Result<Json<ClipDto>, ApiError> {
    let out = read(state.catalog, move |conn| query::get_clip(conn, id)).await?;
    out.map(Json).ok_or(ApiError::NotFound)
}

async fn analytics(State(state): State<AppState>) -> Result<Json<AnalyticsDto>, ApiError> {
    let out = read(state.catalog, query::analytics).await?;
    Ok(Json(out))
}

async fn settings(State(state): State<AppState>) -> Result<Json<Vec<PrefDto>>, ApiError> {
    let out = read(state.catalog, query::list_settings).await?;
    Ok(Json(out))
}

#[derive(Deserialize)]
struct PutSettingBody {
    key: String,
    value: String,
}

async fn put_setting(
    State(state): State<AppState>,
    Json(body): Json<PutSettingBody>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    if !validate_setting(&body.key, &body.value) {
        return Err(ApiError::bad_request(
            "invalid_setting",
            format!("unknown or invalid setting '{}'", body.key),
        ));
    }

    let key = body.key.clone();
    let value = body.value.clone();
    let request_key = key.clone();
    let request_value = value.clone();
    let request = json!({
        "cmd": "set_pref",
        "key": request_key,
        "value": request_value,
    });
    let client = state.indexd.clone();
    let response = tokio::task::spawn_blocking(move || client.call(request))
        .await
        .map_err(|_| ApiError::Internal)?
        .map_err(|err| match err {
            TransportError::Unavailable(_) => ApiError::status(
                StatusCode::SERVICE_UNAVAILABLE,
                "unavailable",
                "settings service unavailable",
            ),
            TransportError::Protocol(_) => ApiError::Internal,
        })?;

    match response
        .get("status")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("")
    {
        "pref_set"
            if response.get("key").and_then(serde_json::Value::as_str) == Some(key.as_str()) =>
        {
            Ok((StatusCode::OK, Json(json!({ "key": key, "value": value }))))
        }
        "error" => {
            let message = response
                .get("message")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("indexd rejected the write");
            Err(ApiError::status(
                StatusCode::BAD_GATEWAY,
                "indexd_error",
                message,
            ))
        }
        _ => Err(ApiError::Internal),
    }
}

fn validate_setting(key: &str, value: &str) -> bool {
    match key {
        "speed_unit" => matches!(value, "mph" | "kph"),
        "clock" => matches!(value, "local" | "utc"),
        _ => false,
    }
}

/// Query parameters for `DELETE /api/clips/:id`.
#[derive(Deserialize)]
struct DeleteQuery {
    /// Delete target: `car` (the Tesla USB volume, via `gadgetd`) is the only
    /// implemented target. `archive`/`both` require `retentiond` (not built) →
    /// `501`. Omitted → `400` (no destructive default; the caller must opt in).
    target: Option<String>,
}

/// `DELETE /api/clips/:id?target=car`: delete a clip's car-visible camera files
/// via a `gadgetd` eject-handoff (contract D2 §2.3).
///
/// Fail-closed at every step: an unknown/non-`car` target, a non-car-deletable
/// clip, or any catalog inconsistency refuses **before** the LUN is touched.
async fn delete_clip(
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<DeleteQuery>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    match q.target.as_deref() {
        Some("car") => {}
        Some("archive" | "both") => {
            return Err(ApiError::status(
                StatusCode::NOT_IMPLEMENTED,
                "not_implemented",
                "archive deletes are not implemented yet (retentiond delete protocol)",
            ));
        }
        Some(other) => {
            return Err(ApiError::bad_request(
                "invalid_target",
                format!("unknown delete target `{other}`; use ?target=car"),
            ));
        }
        None => {
            return Err(ApiError::bad_request(
                "target_required",
                "specify an explicit ?target=car (no destructive default)",
            ));
        }
    }

    // Read the clip + its car-visible angles in one blocking task.
    let facts = read(state.catalog.clone(), move |conn| {
        let Some(clip) = query::get_clip(conn, id)? else {
            return Ok(None);
        };
        let angles = query::list_ro_usb_angles(conn, id)?;
        Ok(Some((clip, angles)))
    })
    .await?;
    let (clip, angles) = facts.ok_or(ApiError::NotFound)?;

    let plan = gadget::plan_car_delete(
        &clip.partition,
        &clip.folder_class,
        &clip.availability,
        &clip.canonical_key,
        &angles,
    )
    .map_err(refusal_to_error)?;

    // Blocking socket round-trip to gadgetd, off the async runtime. Bracket it
    // with job_status events so the SPA can show the delete in flight and learn
    // its terminal state over `GET /api/jobs`.
    let job_id = state.jobs.next_job_id();
    state
        .jobs
        .publish_job(JobStatus::running(job_id, "clip_delete"));

    let client = state.gadget.clone();
    let jobs = state.jobs.clone();
    let request = gadget::delete_request(&plan);
    // Publish the terminal job state from INSIDE the blocking task so the job
    // lifecycle always completes — even if the HTTP request is cancelled (client
    // disconnect) before this `.await` resolves. `spawn_blocking` tasks run to
    // completion regardless of whether their `JoinHandle` is awaited, so a
    // dropped request can never strand a job in `running`.
    let join = tokio::task::spawn_blocking(move || {
        let result = client.call(request);
        match &result {
            Ok(resp) => {
                jobs.publish_job(job_for_outcome(
                    job_id,
                    "clip_delete",
                    &gadget::map_mutation_outcome(resp),
                ));
            }
            Err(transport) => {
                jobs.publish_job(job_failed(
                    job_id,
                    "clip_delete",
                    format!("gadgetd transport: {transport:?}"),
                ));
            }
        }
        result
    })
    .await;

    let resp = match join {
        Ok(Ok(resp)) => resp,
        Ok(Err(transport)) => return Err(transport_to_error(transport)),
        Err(_) => {
            // Join failure (the blocking task panicked): mark the job failed so
            // it does not linger in the active snapshot.
            state.jobs.publish_job(job_failed(
                job_id,
                "clip_delete",
                "blocking task join failed".to_owned(),
            ));
            return Err(ApiError::Internal);
        }
    };

    outcome_to_response(&gadget::map_mutation_outcome(&resp))
}

/// Stage uploaded `bytes` into a fresh `0600` temp file inside `dir` (created
/// `0700` if absent), fsynced so `gadgetd` reads a fully-durable source. The
/// directory is canonicalized so the returned guard's path is absolute (it is
/// consumed by `gadgetd` in a different process), and symlinks in the ancestry
/// are resolved. The returned guard unlinks the file when dropped.
fn stage_upload(dir: &std::path::Path, bytes: &[u8]) -> std::io::Result<tempfile::NamedTempFile> {
    std::fs::create_dir_all(dir)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
    }
    // Canonicalize AFTER creation so `source_path` handed to gadgetd is absolute
    // regardless of `WEBD_CACHE_DIR` being relative.
    let dir = std::fs::canonicalize(dir)?;
    let mut tmp = tempfile::Builder::new()
        .prefix("upload-")
        .suffix(".partial")
        .tempfile_in(&dir)?;
    {
        use std::io::Write;
        tmp.as_file_mut().write_all(bytes)?;
        tmp.as_file_mut().sync_all()?;
    }
    Ok(tmp)
}

/// Stage `bytes` like [`stage_upload`], but DETACH the temp guard so the file
/// is NOT unlinked when it drops: ownership passes to `gadgetd`'s durable
/// mutation queue, which reclaims (unlinks) the blob after the queued entry
/// reaches a terminal state. Returns the absolute `(path, source_path)` of the
/// fsynced blob (`source_path` is the UTF-8 form handed to `gadgetd`). The
/// caller MUST unlink `path` itself on any outcome where `gadgetd` did NOT
/// accept the mutation (rejected / bad reply / transport fault), otherwise the
/// blob leaks in the staging dir.
fn stage_upload_persistent(
    dir: &std::path::Path,
    bytes: &[u8],
) -> std::io::Result<(std::path::PathBuf, String)> {
    let tmp = stage_upload(dir, bytes)?;
    // `keep()` disarms the drop-unlink and yields the durable path.
    let (_file, path) = tmp.keep().map_err(|e| e.error)?;
    if let Some(source_path) = path.to_str() {
        let source_path = source_path.to_owned();
        Ok((path, source_path))
    } else {
        // A non-UTF-8 staging path can't be handed to gadgetd; don't leak it.
        let _ = std::fs::remove_file(&path);
        Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "staged path is not valid UTF-8",
        ))
    }
}

/// The disposition of an `enqueue_mutation` round-trip, surfaced from the
/// blocking task so the async caller can build the right HTTP response.
enum EnqueueResult {
    /// `gadgetd` answered; `outcome` distinguishes queued / rejected / bad-reply.
    Outcome(gadget::QueueOutcome),
    /// The `gadgetd` socket round-trip failed (unreachable / protocol).
    Transport(TransportError),
    /// Staging the upload to a durable blob failed before any `gadgetd` call.
    StagingFailed(String),
}

/// Generic p2-media install primitive (the frictionless write path): stage
/// `bytes` to a DURABLE blob, hand the staged path to `gadgetd` as a queued
/// `install_file` mutation at the fixed `rel_path`, and bracket the round-trip
/// with `job_status` events under the given `kind`.
///
/// Unlike the legacy synchronous handoff, `gadgetd` accepts the mutation into
/// its durable queue and answers `202 {state:"queued"}` immediately — it never
/// hard-fails because the car is connected. The change is applied automatically
/// at the next safe window. `gadgetd` owns and reclaims the staged blob on a
/// terminal state; `webd` only unlinks it when `gadgetd` did NOT accept the
/// mutation (rejected / bad reply / transport fault), so nothing leaks.
///
/// To add another media feature, validate + read the upload bytes in a thin
/// handler, then call `run_install` with that feature's `kind`, `partition`,
/// and `rel_path` — no new gadgetd or job plumbing required.
pub(crate) async fn run_install(
    state: AppState,
    kind: &'static str,
    partition: u8,
    rel_path: String,
    bytes: Vec<u8>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let job_id = state.jobs.next_job_id();
    state.jobs.publish_job(JobStatus::running(job_id, kind));

    let client = state.gadget.clone();
    let jobs = state.jobs.clone();
    let staging = state.media.staging_dir();

    let join = tokio::task::spawn_blocking(move || {
        // Stage to a durable blob gadgetd reads at apply time (it reclaims it).
        let (path, source_path) = match stage_upload_persistent(&staging, &bytes) {
            Ok(pair) => pair,
            Err(err) => {
                let detail = format!("staging failed: {err}");
                jobs.publish_job(job_failed(job_id, kind, detail.clone()));
                return EnqueueResult::StagingFailed(detail);
            }
        };
        let request = gadget::enqueue_install_request(partition, &rel_path, &source_path);
        match client.call(request) {
            Ok(resp) => {
                let outcome = gadget::map_queue_outcome(&resp);
                // gadgetd only takes ownership of the blob when it accepts the
                // mutation; on any other outcome webd must unlink it.
                if !matches!(outcome, gadget::QueueOutcome::Queued { .. }) {
                    let _ = std::fs::remove_file(&path);
                }
                jobs.publish_job(job_for_queue_outcome(job_id, kind, &outcome));
                EnqueueResult::Outcome(outcome)
            }
            Err(transport) => {
                let _ = std::fs::remove_file(&path);
                jobs.publish_job(job_failed(
                    job_id,
                    kind,
                    format!("gadgetd transport: {transport:?}"),
                ));
                EnqueueResult::Transport(transport)
            }
        }
    })
    .await;

    match join {
        Ok(EnqueueResult::Outcome(outcome)) => queue_outcome_to_response(&outcome),
        Ok(EnqueueResult::Transport(transport)) => Err(transport_to_error(transport)),
        Ok(EnqueueResult::StagingFailed(detail)) => Err(ApiError::status(
            StatusCode::INTERNAL_SERVER_ERROR,
            "staging_failed",
            detail,
        )),
        Err(_) => {
            state.jobs.publish_job(job_failed(
                job_id,
                kind,
                "blocking task join failed".to_owned(),
            ));
            Err(ApiError::Internal)
        }
    }
}

/// Generic p2-media remove primitive: hand `gadgetd` a `delete_paths` mutation
/// for the single `rel_path` (idempotent on an already-absent asset,
/// file-only) and bracket the round-trip with `job_status` events under `kind`.
pub(crate) async fn run_remove(
    state: AppState,
    kind: &'static str,
    partition: u8,
    rel_path: String,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    run_remove_many(state, kind, partition, vec![rel_path]).await
}

/// Generic p2-media bulk-remove primitive (the frictionless write path): chunks
/// `rel_paths` into batches of ≤[`DELETE_CHUNK`] (16) and enqueues each batch
/// as a separate `delete_paths` mutation. gadgetd coalesces all queued partition
/// deletes into a single handoff/eject regardless, so multiple ≤16-path enqueues
/// still produce exactly ONE car-disconnect cycle.
///
/// WHY chunking: gadgetd's `enqueue_mutation` validates each mutation at enqueue
/// time and rejects a `DeletePaths` whose set exceeds `MAX_DELETE_PATHS=16`
/// (gadgetd `handoff.rs`). Without this chunking a folder delete or bulk delete
/// with >16 files would be silently refused.
///
/// Returns the outcome of the LAST chunk. All chunks share one `job_id`; the
/// terminal job-status event is published once after all chunks complete
/// successfully. A transport error or join failure on any chunk aborts early.
///
/// `rel_paths` must be non-empty and already sanitised/validated by the caller
/// (see [`crate::media_upload::plan_bulk_delete`]); this primitive does not
/// re-validate path safety.
pub(crate) async fn run_remove_many(
    state: AppState,
    kind: &'static str,
    partition: u8,
    rel_paths: Vec<String>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let job_id = state.jobs.next_job_id();
    state.jobs.publish_job(JobStatus::running(job_id, kind));

    let mut last_outcome: Option<gadget::QueueOutcome> = None;

    for chunk in rel_paths.chunks(DELETE_CHUNK) {
        let client = state.gadget.clone();
        let request = gadget::enqueue_remove_request_many(partition, chunk);

        let join = tokio::task::spawn_blocking(move || match client.call(request) {
            Ok(resp) => EnqueueResult::Outcome(gadget::map_queue_outcome(&resp)),
            Err(transport) => EnqueueResult::Transport(transport),
        })
        .await;

        match join {
            Ok(EnqueueResult::Outcome(outcome)) => {
                // A rejected or unparseable chunk fails the whole operation
                // immediately. Without this, a later successfully-queued chunk
                // would overwrite `last_outcome` and the endpoint would report
                // success, silently dropping the failed chunk.
                if !matches!(outcome, gadget::QueueOutcome::Queued { .. }) {
                    state
                        .jobs
                        .publish_job(job_for_queue_outcome(job_id, kind, &outcome));
                    return queue_outcome_to_response(&outcome);
                }
                last_outcome = Some(outcome);
            }
            Ok(EnqueueResult::Transport(transport)) => {
                state.jobs.publish_job(job_failed(
                    job_id,
                    kind,
                    format!("gadgetd transport: {transport:?}"),
                ));
                return Err(transport_to_error(transport));
            }
            // A remove stages no blob, so StagingFailed never occurs here.
            Ok(EnqueueResult::StagingFailed(detail)) => {
                return Err(ApiError::status(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "staging_failed",
                    detail,
                ));
            }
            Err(_) => {
                state.jobs.publish_job(job_failed(
                    job_id,
                    kind,
                    "blocking task join failed".to_owned(),
                ));
                return Err(ApiError::Internal);
            }
        }
    }

    let outcome = last_outcome
        .expect("rel_paths was empty; caller must ensure non-empty input");
    state
        .jobs
        .publish_job(job_for_queue_outcome(job_id, kind, &outcome));
    queue_outcome_to_response(&outcome)
}

/// Folder-delete write path: enqueue the folder's child-file deletes (chunked
/// ≤[`DELETE_CHUNK`]), THEN enqueue a `remove_empty_dir` prune for the now-empty
/// directory. gadgetd applies the file deletes first (file-only `delete_paths`,
/// which deliberately refuses directories), then the empty-only `remove_dir`
/// prune removes the orphaned directory the deletes leave behind.
///
/// `dir_rel_path` is enqueued even when `file_rel_paths` is empty — that REPAIRS
/// an already-orphaned empty folder (a folder whose files were previously
/// deleted before the prune existed). The prune is idempotent on an absent
/// directory and empty-only, so it can never remove a file.
///
/// `file_rel_paths` must be already sanitised/validated by the caller; this
/// primitive does not re-validate path safety. Returns the outcome of the
/// directory-prune enqueue (the terminal step). A rejected file-delete chunk or
/// any transport error aborts early.
pub(crate) async fn run_folder_delete(
    state: AppState,
    kind: &'static str,
    partition: u8,
    file_rel_paths: Vec<String>,
    dir_rel_path: String,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let job_id = state.jobs.next_job_id();
    state.jobs.publish_job(JobStatus::running(job_id, kind));

    // 1) Enqueue the child-file deletes (chunked ≤16). Each chunk must queue;
    //    a rejected chunk fails the whole operation immediately.
    for chunk in file_rel_paths.chunks(DELETE_CHUNK) {
        let client = state.gadget.clone();
        let request = gadget::enqueue_remove_request_many(partition, chunk);

        let join = tokio::task::spawn_blocking(move || match client.call(request) {
            Ok(resp) => EnqueueResult::Outcome(gadget::map_queue_outcome(&resp)),
            Err(transport) => EnqueueResult::Transport(transport),
        })
        .await;

        match join {
            Ok(EnqueueResult::Outcome(outcome)) => {
                if !matches!(outcome, gadget::QueueOutcome::Queued { .. }) {
                    state
                        .jobs
                        .publish_job(job_for_queue_outcome(job_id, kind, &outcome));
                    return queue_outcome_to_response(&outcome);
                }
            }
            Ok(EnqueueResult::Transport(transport)) => {
                state.jobs.publish_job(job_failed(
                    job_id,
                    kind,
                    format!("gadgetd transport: {transport:?}"),
                ));
                return Err(transport_to_error(transport));
            }
            Ok(EnqueueResult::StagingFailed(detail)) => {
                return Err(ApiError::status(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "staging_failed",
                    detail,
                ));
            }
            Err(_) => {
                state.jobs.publish_job(job_failed(
                    job_id,
                    kind,
                    "blocking task join failed".to_owned(),
                ));
                return Err(ApiError::Internal);
            }
        }
    }

    // 2) Enqueue the empty-directory prune (always — repairs an orphaned folder).
    let client = state.gadget.clone();
    let request = gadget::enqueue_remove_empty_dir_request(partition, &dir_rel_path);

    let join = tokio::task::spawn_blocking(move || match client.call(request) {
        Ok(resp) => EnqueueResult::Outcome(gadget::map_queue_outcome(&resp)),
        Err(transport) => EnqueueResult::Transport(transport),
    })
    .await;

    let outcome = match join {
        Ok(EnqueueResult::Outcome(outcome)) => outcome,
        Ok(EnqueueResult::Transport(transport)) => {
            state.jobs.publish_job(job_failed(
                job_id,
                kind,
                format!("gadgetd transport: {transport:?}"),
            ));
            return Err(transport_to_error(transport));
        }
        // A prune stages no blob, so StagingFailed never occurs here.
        Ok(EnqueueResult::StagingFailed(detail)) => {
            return Err(ApiError::status(
                StatusCode::INTERNAL_SERVER_ERROR,
                "staging_failed",
                detail,
            ));
        }
        Err(_) => {
            state.jobs.publish_job(job_failed(
                job_id,
                kind,
                "blocking task join failed".to_owned(),
            ));
            return Err(ApiError::Internal);
        }
    };

    state
        .jobs
        .publish_job(job_for_queue_outcome(job_id, kind, &outcome));
    queue_outcome_to_response(&outcome)
}
/// `GET /api/media-events`: a Server-Sent Events stream that emits a
/// `media-changed` event whenever the catalog is committed by `indexd` (a media
/// install/delete has been applied and indexed). Browsers subscribe once and
/// refetch the category they are viewing on each tick — replacing per-screen
/// list polling. The payload is a constant `"1"`; the event name carries the
/// signal. A lagged client still receives a single coalesced tick (correct: it
/// just means "refetch"). Keep-alive comments every 15 s hold the connection
/// open through idle periods and proxies.
async fn media_events_stream(
    State(state): State<AppState>,
) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    let stream = BroadcastStream::new(state.media_events.subscribe()).map(|item| {
        let _ = item; // Ok(()) tick or Lagged gap both mean the same: "refetch".
        Ok(Event::default().event("media-changed").data("1"))
    });
    Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
}

/// (contract D2 §2.5/§3). A new subscriber first receives a burst of the
/// currently-running jobs, then live events as they are published.
async fn jobs_stream(
    State(state): State<AppState>,
) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    // Subscribe BEFORE taking the active snapshot so a job that goes terminal in
    // the gap is delivered live (never lost). A job present in both the snapshot
    // and the live buffer is a harmless duplicate the SPA upserts by `job_id`.
    let live = BroadcastStream::new(state.jobs.subscribe()).filter_map(|item| match item {
        Ok(ev) => Some(Ok(event_from(&ev))),
        // A lagged slow client drops the gap and keeps streaming (acceptable at
        // today's low job volume; revisit when high-frequency producers land).
        Err(BroadcastStreamRecvError::Lagged(_)) => None,
    });
    let head = state
        .jobs
        .active_snapshot()
        .into_iter()
        .map(|job| Ok(event_from(&JobEvent::JobStatus(job))));
    let stream = tokio_stream::iter(head).chain(live);
    Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
}

/// `GET /api/jobs/failed`: a REST snapshot of the most-recent failed jobs
/// (contract D2 §2.1), for the failed-jobs screen.
async fn jobs_failed(State(state): State<AppState>) -> Json<Value> {
    Json(json!({ "jobs": state.jobs.failed_snapshot() }))
}

/// Build the SSE frame for a [`JobEvent`], falling back to a comment frame if
/// the payload somehow fails to serialize (it never does for our types).
fn event_from(ev: &JobEvent) -> Event {
    Event::default()
        .event(ev.name())
        .json_data(ev.data())
        .unwrap_or_else(|_| Event::default().comment("job event serialize error"))
}

/// A terminal `failed` job carrying an error detail.
fn job_failed(job_id: u64, kind: &str, detail: String) -> JobStatus {
    JobStatus {
        job_id,
        kind: kind.to_owned(),
        state: JobState::Failed,
        progress: None,
        detail: Some(detail),
        handoff_id: None,
    }
}

/// Map a terminal [`MutationOutcome`] to its `job_status` update for a given job
/// `kind` (e.g. `clip_delete`, `chime_install`, `chime_remove`).
fn job_for_outcome(job_id: u64, kind: &str, outcome: &MutationOutcome) -> JobStatus {
    match outcome {
        MutationOutcome::Done(handoff_id) => JobStatus {
            job_id,
            kind: kind.to_owned(),
            state: JobState::Done,
            progress: Some(1.0),
            detail: None,
            handoff_id: Some(handoff_id.clone()),
        },
        MutationOutcome::Busy(reason) => JobStatus {
            job_id,
            kind: kind.to_owned(),
            state: JobState::Busy,
            progress: None,
            detail: Some(busy_message(reason)),
            handoff_id: None,
        },
        MutationOutcome::Refused(reason) => JobStatus {
            job_id,
            kind: kind.to_owned(),
            state: JobState::Refused,
            progress: None,
            detail: Some(reason.clone()),
            handoff_id: None,
        },
        MutationOutcome::Failed { handoff_id, detail } => JobStatus {
            job_id,
            kind: kind.to_owned(),
            state: JobState::Failed,
            progress: None,
            detail: Some(detail.clone()),
            handoff_id: Some(handoff_id.clone()),
        },
        MutationOutcome::CriticalFault { handoff_id, detail } => JobStatus {
            job_id,
            kind: kind.to_owned(),
            state: JobState::Failed,
            progress: None,
            detail: Some(format!("LUN left ejected: {detail}")),
            handoff_id: Some(handoff_id.clone()),
        },
        MutationOutcome::BadResponse(msg) => JobStatus {
            job_id,
            kind: kind.to_owned(),
            state: JobState::Failed,
            progress: None,
            detail: Some(msg.clone()),
            handoff_id: None,
        },
    }
}

/// Map an `enqueue_mutation` outcome to its `job_status` update. A queued
/// mutation is terminal for the `webd` job (the change is durably saved); the
/// SPA shows "saved — syncing to the car" rather than blocking on the handoff.
fn job_for_queue_outcome(job_id: u64, kind: &str, outcome: &gadget::QueueOutcome) -> JobStatus {
    match outcome {
        gadget::QueueOutcome::Queued { job_id: gadget_job } => JobStatus {
            job_id,
            kind: kind.to_owned(),
            state: JobState::Queued,
            progress: None,
            detail: Some(format!(
                "saved as {gadget_job}; will sync to the car at the next safe window"
            )),
            handoff_id: None,
        },
        gadget::QueueOutcome::Rejected(reason) => JobStatus {
            job_id,
            kind: kind.to_owned(),
            state: JobState::Refused,
            progress: None,
            detail: Some(reason.clone()),
            handoff_id: None,
        },
        gadget::QueueOutcome::BadResponse(msg) => JobStatus {
            job_id,
            kind: kind.to_owned(),
            state: JobState::Failed,
            progress: None,
            detail: Some(msg.clone()),
            handoff_id: None,
        },
    }
}

/// Map an `enqueue_mutation` outcome to its HTTP response: accepted → `202`
/// `{state:"queued", job_id}`; rejected (invalid mutation / full queue) → `422`;
/// an unparseable `gadgetd` reply → `502`. A queued mutation is NOT an error —
/// it is the frictionless success path that never surfaces a transient-busy
/// `409` to the user.
fn queue_outcome_to_response(
    outcome: &gadget::QueueOutcome,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    match outcome {
        gadget::QueueOutcome::Queued { job_id } => Ok((
            StatusCode::ACCEPTED,
            Json(json!({ "state": "queued", "job_id": job_id })),
        )),
        gadget::QueueOutcome::Rejected(reason) => Err(ApiError::status(
            StatusCode::UNPROCESSABLE_ENTITY,
            "refused",
            reason.clone(),
        )),
        gadget::QueueOutcome::BadResponse(msg) => Err(ApiError::status(
            StatusCode::BAD_GATEWAY,
            "gadgetd_protocol",
            msg.clone(),
        )),
    }
}

/// `GET /api/handoff/:id`: poll a prior car-delete handoff, normalized to the D2
/// `{handoff_id, state, detail}` shape.
async fn handoff_status(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let client = state.gadget.clone();
    let request = gadget::status_request(&id);
    let resp = tokio::task::spawn_blocking(move || client.call(request))
        .await
        .map_err(|_| ApiError::Internal)?
        .map_err(transport_to_error)?;
    gadget::map_status(&resp)
        .map(Json)
        .ok_or(ApiError::NotFound)
}

/// `GET /api/gadget/status`: read-only USB-gadget state (present/bound/udc plus
/// the two LUN backing files and the last handoff result) from `gadgetd`'s live
/// control socket — the same socket the car-delete handoff uses. `gadgetd`
/// answers this concurrently with an in-flight handoff. Transport faults map to
/// `503` (gadgetd down) / `502` (unparseable reply).
async fn gadget_status(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    let client = state.gadget.clone();
    let request = gadget::gadget_status_request();
    let resp = tokio::task::spawn_blocking(move || client.call(request))
        .await
        .map_err(|_| ApiError::Internal)?
        .map_err(transport_to_error)?;
    gadget::map_gadget_status(&resp).map(Json).ok_or_else(|| {
        ApiError::status(
            StatusCode::BAD_GATEWAY,
            "gadgetd_protocol",
            "unparseable gadget_status response",
        )
    })
}

/// Map a pre-handoff planning refusal to its HTTP error.
fn refusal_to_error(refusal: DeleteRefusal) -> ApiError {
    match refusal {
        DeleteRefusal::NotCarDeletable(msg) => {
            ApiError::status(StatusCode::UNPROCESSABLE_ENTITY, "not_car_deletable", msg)
        }
        DeleteRefusal::NotPresent => ApiError::status(
            StatusCode::CONFLICT,
            "not_present",
            "clip is not currently on the live USB volume",
        ),
        DeleteRefusal::InvalidClip(msg) => {
            ApiError::status(StatusCode::UNPROCESSABLE_ENTITY, "invalid_clip", msg)
        }
    }
}

/// Map a `gadgetd` transport failure: unreachable → `503`, bad protocol → `502`.
fn transport_to_error(err: TransportError) -> ApiError {
    match err {
        TransportError::Unavailable(msg) => {
            ApiError::status(StatusCode::SERVICE_UNAVAILABLE, "gadgetd_unavailable", msg)
        }
        TransportError::Protocol(msg) => {
            ApiError::status(StatusCode::BAD_GATEWAY, "gadgetd_protocol", msg)
        }
    }
}

/// Map a terminal handoff outcome to an HTTP response.
fn outcome_to_response(outcome: &MutationOutcome) -> Result<(StatusCode, Json<Value>), ApiError> {
    match outcome {
        MutationOutcome::Done(handoff_id) => Ok((
            StatusCode::OK,
            Json(json!({ "handoff_id": handoff_id, "state": "done" })),
        )),
        MutationOutcome::Busy(reason) => Err(ApiError::status(
            StatusCode::CONFLICT,
            "handoff_busy",
            busy_message(reason),
        )),
        MutationOutcome::Refused(reason) => Err(ApiError::status(
            StatusCode::UNPROCESSABLE_ENTITY,
            "refused",
            reason.clone(),
        )),
        MutationOutcome::Failed { handoff_id, detail } => Err(ApiError::status(
            StatusCode::BAD_GATEWAY,
            "handoff_failed",
            format!("handoff {handoff_id} failed: {detail}"),
        )),
        MutationOutcome::CriticalFault { handoff_id, detail } => Err(ApiError::status(
            StatusCode::INTERNAL_SERVER_ERROR,
            "critical_fault",
            format!("handoff {handoff_id} left the LUN ejected: {detail}"),
        )),
        MutationOutcome::BadResponse(msg) => Err(ApiError::status(
            StatusCode::BAD_GATEWAY,
            "gadgetd_protocol",
            msg.clone(),
        )),
    }
}

/// A friendly, user-facing message for a transient `gadgetd` guard refusal
/// (the `409` retry cases). Falls back to the raw reason for forward-compat.
fn busy_message(reason: &str) -> String {
    match reason {
        "handoff_active" => "another change is already in progress; retry shortly".to_owned(),
        "save_active" => "the car is mid-save; retry shortly".to_owned(),
        r if r.starts_with("gadget not bound") => {
            "the car's drive is not currently presented; retry shortly".to_owned()
        }
        r if r.starts_with("hot_handoff_unvalidated") => {
            "the car is connected and using the drive; deletes are blocked until it disconnects"
                .to_owned()
        }
        other => other.to_owned(),
    }
}

/// JSON `404` fallback for unmatched `/api/*` paths.
async fn api_not_found() -> ApiError {
    ApiError::NotFound
}

/// Validate and normalize page-size params.
fn validate_limit(limit: Option<i64>) -> Result<i64, ApiError> {
    match limit {
        None => Ok(DEFAULT_LIMIT),
        Some(value) if value < 1 => {
            Err(ApiError::bad_request("invalid_limit", "limit must be >= 1"))
        }
        Some(value) => Ok(value.min(MAX_LIMIT)),
    }
}

#[derive(Serialize, Deserialize)]
struct CursorPayload {
    v: i64,
    r: String,
    ts: i64,
    id: i64,
    snap: i64,
}

const BASE64URL: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

fn encode_cursor(resource: &str, ts: i64, id: i64, snap: i64) -> String {
    let payload = format!(r#"{{"v":1,"r":"{resource}","ts":{ts},"id":{id},"snap":{snap}}}"#);
    base64url_encode(payload.as_bytes())
}

fn decode_cursor(cursor: &str, expected_resource: &str) -> Result<(i64, i64, i64), ApiError> {
    let decoded = base64url_decode(cursor)?;
    let payload: CursorPayload = serde_json::from_slice(&decoded)
        .map_err(|_| ApiError::bad_request("invalid_cursor", "cursor must be valid JSON"))?;
    if payload.v != 1 {
        return Err(ApiError::bad_request(
            "invalid_cursor",
            "unsupported cursor version",
        ));
    }
    if payload.r != expected_resource {
        return Err(ApiError::bad_request(
            "invalid_cursor",
            "cursor resource does not match endpoint",
        ));
    }
    Ok((payload.ts, payload.id, payload.snap))
}

fn base64url_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity((data.len() * 4).div_ceil(3));
    let mut i = 0usize;
    while i + 3 <= data.len() {
        let chunk = (u32::from(data[i]) << 16) | (u32::from(data[i + 1]) << 8) | u32::from(data[i + 2]);
        out.push(char::from(BASE64URL[((chunk >> 18) & 0x3f) as usize]));
        out.push(char::from(BASE64URL[((chunk >> 12) & 0x3f) as usize]));
        out.push(char::from(BASE64URL[((chunk >> 6) & 0x3f) as usize]));
        out.push(char::from(BASE64URL[(chunk & 0x3f) as usize]));
        i += 3;
    }
    match data.len() - i {
        1 => {
            let chunk = u32::from(data[i]) << 16;
            out.push(char::from(BASE64URL[((chunk >> 18) & 0x3f) as usize]));
            out.push(char::from(BASE64URL[((chunk >> 12) & 0x3f) as usize]));
        }
        2 => {
            let chunk = (u32::from(data[i]) << 16) | (u32::from(data[i + 1]) << 8);
            out.push(char::from(BASE64URL[((chunk >> 18) & 0x3f) as usize]));
            out.push(char::from(BASE64URL[((chunk >> 12) & 0x3f) as usize]));
            out.push(char::from(BASE64URL[((chunk >> 6) & 0x3f) as usize]));
        }
        _ => {}
    }
    out
}

fn base64url_decode(input: &str) -> Result<Vec<u8>, ApiError> {
    if input.len() % 4 == 1 {
        return Err(ApiError::bad_request(
            "invalid_cursor",
            "cursor is not valid base64url",
        ));
    }
    let mut out = Vec::with_capacity(input.len() * 3 / 4);
    let mut bits = 0u8;
    let mut acc = 0u32;
    for &byte in input.as_bytes() {
        let Some(value) = base64url_value(byte) else {
            return Err(ApiError::bad_request(
                "invalid_cursor",
                "cursor is not valid base64url",
            ));
        };
        acc = (acc << 6) | u32::from(value);
        bits += 6;
        while bits >= 8 {
            bits -= 8;
            out.push(((acc >> bits) & 0xff) as u8);
        }
    }
    if bits > 0 {
        let mask = (1u32 << bits) - 1;
        if (acc & mask) != 0 {
            return Err(ApiError::bad_request(
                "invalid_cursor",
                "cursor is not valid base64url",
            ));
        }
    }
    Ok(out)
}

fn base64url_value(byte: u8) -> Option<u8> {
    match byte {
        b'A'..=b'Z' => Some(byte - b'A'),
        b'a'..=b'z' => Some(byte - b'a' + 26),
        b'0'..=b'9' => Some(byte - b'0' + 52),
        b'-' => Some(62),
        b'_' => Some(63),
        _ => None,
    }
}

/// Wrap a result set in a [`Page`], computing an opaque `next_cursor` from the
/// last returned row when `limit+1` rows were fetched.
fn into_page<T, FDate: Fn(&T) -> i64, FId: Fn(&T) -> i64>(
    mut items: Vec<T>,
    limit: i64,
    snap: i64,
    resource: &str,
    date_of: FDate,
    id_of: FId,
) -> Page<T> {
    let cap = match usize::try_from(limit) {
        Ok(value) => value,
        Err(_) => 0,
    };
    let has_more = items.len() > cap;
    if has_more {
        items.truncate(cap);
    }
    let next_cursor = if has_more {
        items.last().map(|item| {
            let ts = date_of(item);
            let id = id_of(item);
            encode_cursor(resource, ts, id, snap)
        })
    } else {
        None
    };
    Page {
        items,
        next_cursor,
        limit,
    }
}
