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
use serde::Deserialize;
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

/// Assemble the application router: the `/api` read endpoints nested under a
/// JSON-404 fallback, with everything else served by the SPA host.
pub(crate) fn router(state: AppState, static_dir: PathBuf) -> Router {
    let index = static_dir.join("index.html");
    let spa = ServeDir::new(static_dir).fallback(ServeFile::new(index));

    let api = Router::new()
        .route("/days", get(days))
        .route("/trips", get(trips))
        .route("/trips/{id}", get(trip_detail))
        .route("/events", get(events))
        .route("/clips", get(clips))
        .route("/clips/{id}", get(clip_detail).delete(delete_clip))
        .route("/clips/{id}/stream", get(crate::media::stream))
        .route("/clips/{id}/export", get(crate::media::export))
        .route(
            "/clips/{id}/angles/{camera}/download",
            get(crate::media::download),
        )
        .route("/handoff/{id}", get(handoff_status))
        .route(
            "/chimes",
            post(crate::chimes::install_chime)
                .layer(DefaultBodyLimit::max(crate::chimes::CHIME_BODY_LIMIT)),
        )
        .route("/chimes/{id}", delete(crate::chimes::remove_chime))
        .route("/jobs", get(jobs_stream))
        .route("/jobs/failed", get(jobs_failed))
        .route("/analytics", get(analytics))
        .route("/settings", get(settings))
        .merge(crate::health::routes())
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
    /// Return events with `id` strictly greater than this cursor.
    after: Option<i64>,
    /// Page size (clamped to [`MAX_LIMIT`]).
    limit: Option<i64>,
    /// Optional filter to a single trip.
    trip: Option<i64>,
}

/// Query parameters for cursor-paginated `GET /api/clips`.
#[derive(Deserialize)]
struct ClipsQuery {
    /// Return clips with `id` strictly greater than this cursor.
    after: Option<i64>,
    /// Page size (clamped to [`MAX_LIMIT`]).
    limit: Option<i64>,
    /// Optional `folder_class` filter.
    folder_class: Option<String>,
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
    let (after, limit) = resolve_page(q.after, q.limit)?;
    let trip = q.trip;
    let items = read(state.catalog, move |conn| {
        query::list_events(conn, after, limit, trip)
    })
    .await?;
    Ok(Json(into_page(items, limit, |event| event.id)))
}

async fn clips(
    State(state): State<AppState>,
    Query(q): Query<ClipsQuery>,
) -> Result<Json<Page<ClipDto>>, ApiError> {
    let (after, limit) = resolve_page(q.after, q.limit)?;
    let folder_class = q.folder_class;
    let items = read(state.catalog, move |conn| {
        query::list_clips(conn, after, limit, folder_class.as_deref())
    })
    .await?;
    Ok(Json(into_page(items, limit, |clip| clip.id)))
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

/// Generic p2-media install primitive: stage `bytes`, hand the staged path to
/// `gadgetd` as an `install_file` mutation at the fixed `rel_path`, and bracket
/// the round-trip with `job_status` events under the given `kind`.
///
/// The staged file is created and unlinked entirely inside the blocking task,
/// so it is present for the whole `gadgetd` read and is always cleaned up — on
/// success, gadget failure, transport error, or a cancelled HTTP request (a
/// `spawn_blocking` task runs to completion regardless of its `JoinHandle`).
///
/// To add another media feature, validate + read the upload bytes in a thin
/// handler, then call `run_install` with that feature's `kind`, `partition`,
/// and fixed `rel_path` — no new gadgetd or job plumbing required.
pub(crate) async fn run_install(
    state: AppState,
    kind: &'static str,
    partition: u8,
    rel_path: &'static str,
    bytes: Vec<u8>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let job_id = state.jobs.next_job_id();
    state.jobs.publish_job(JobStatus::running(job_id, kind));

    let client = state.gadget.clone();
    let jobs = state.jobs.clone();
    let staging = state.media.staging_dir();

    let join = tokio::task::spawn_blocking(move || {
        let tmp = match stage_upload(&staging, &bytes) {
            Ok(tmp) => tmp,
            Err(err) => {
                let detail = format!("staging failed: {err}");
                jobs.publish_job(job_failed(job_id, kind, detail.clone()));
                return Err(detail);
            }
        };
        let Some(source_path) = tmp.path().to_str().map(str::to_owned) else {
            let detail = "staged path is not valid UTF-8".to_owned();
            jobs.publish_job(job_failed(job_id, kind, detail.clone()));
            return Err(detail);
        };
        let request = gadget::install_request(partition, rel_path, &source_path);
        let result = client.call(request);
        match &result {
            Ok(resp) => {
                jobs.publish_job(job_for_outcome(
                    job_id,
                    kind,
                    &gadget::map_mutation_outcome(resp),
                ));
            }
            Err(transport) => {
                jobs.publish_job(job_failed(
                    job_id,
                    kind,
                    format!("gadgetd transport: {transport:?}"),
                ));
            }
        }
        // `tmp` drops here, unlinking the staged file on every path.
        Ok(result)
    })
    .await;

    let resp = match join {
        Ok(Ok(Ok(resp))) => resp,
        Ok(Ok(Err(transport))) => return Err(transport_to_error(transport)),
        Ok(Err(detail)) => {
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

    outcome_to_response(&gadget::map_mutation_outcome(&resp))
}

/// Generic p2-media remove primitive: hand `gadgetd` a `delete_paths` mutation
/// for the single fixed `rel_path` (idempotent on an already-absent asset,
/// file-only) and bracket the round-trip with `job_status` events under `kind`.
pub(crate) async fn run_remove(
    state: AppState,
    kind: &'static str,
    partition: u8,
    rel_path: &'static str,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let job_id = state.jobs.next_job_id();
    state.jobs.publish_job(JobStatus::running(job_id, kind));

    let client = state.gadget.clone();
    let jobs = state.jobs.clone();
    let request = gadget::remove_request(partition, rel_path);

    let join = tokio::task::spawn_blocking(move || {
        let result = client.call(request);
        match &result {
            Ok(resp) => {
                jobs.publish_job(job_for_outcome(
                    job_id,
                    kind,
                    &gadget::map_mutation_outcome(resp),
                ));
            }
            Err(transport) => {
                jobs.publish_job(job_failed(
                    job_id,
                    kind,
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
            state.jobs.publish_job(job_failed(
                job_id,
                kind,
                "blocking task join failed".to_owned(),
            ));
            return Err(ApiError::Internal);
        }
    };

    outcome_to_response(&gadget::map_mutation_outcome(&resp))
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

/// Validate and normalize cursor-pagination params.
fn resolve_page(after: Option<i64>, limit: Option<i64>) -> Result<(i64, i64), ApiError> {
    let after = after.unwrap_or(0);
    if after < 0 {
        return Err(ApiError::bad_request("invalid_after", "after must be >= 0"));
    }
    let limit = match limit {
        None => DEFAULT_LIMIT,
        Some(value) if value < 1 => {
            return Err(ApiError::bad_request("invalid_limit", "limit must be >= 1"));
        }
        Some(value) => value.min(MAX_LIMIT),
    };
    Ok((after, limit))
}

/// Wrap a result set in a [`Page`], computing the next cursor from the last
/// item's id when a full page was returned.
fn into_page<T, F: Fn(&T) -> i64>(items: Vec<T>, limit: i64, id_of: F) -> Page<T> {
    let full = usize::try_from(limit).is_ok_and(|cap| items.len() == cap);
    let next_cursor = if full { items.last().map(id_of) } else { None };
    Page {
        items,
        next_cursor,
        limit,
    }
}
