//! HTTP routing: the `/api/*` read handlers plus the static-SPA host.
//!
//! Each handler offloads its blocking `rusqlite` work onto a blocking task via
//! [`read`], so no database connection ever crosses an `.await` and the async
//! runtime threads stay free. The `/api` sub-router carries its own JSON `404`
//! fallback so unknown API paths never fall through to the SPA shell.

use std::path::PathBuf;

use axum::Json;
use axum::Router;
use axum::extract::{Path, Query, State};
use axum::routing::get;
use rusqlite::Connection;
use serde::Deserialize;
use tower_http::services::{ServeDir, ServeFile};

use crate::dto::{
    AnalyticsDto, ClipDto, DaySummary, EventDto, Page, PrefDto, TripDetailDto, TripDto,
};
use crate::error::ApiError;
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
        .route("/clips/{id}", get(clip_detail))
        .route("/clips/{id}/stream", get(crate::media::stream))
        .route("/clips/{id}/export", get(crate::media::export))
        .route(
            "/clips/{id}/angles/{camera}/download",
            get(crate::media::download),
        )
        .route("/analytics", get(analytics))
        .route("/settings", get(settings))
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
