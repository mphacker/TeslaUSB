//! `GET/POST/PUT/DELETE /api/chime-scheduler/*` — the lock-chime scheduler,
//! groups, and random-on-boot mode. `webd` is a **pure proxy** for those
//! handlers: each forwards a `cmd`-tagged JSON request to `schedulerd` over its
//! control socket (see [`crate::scheduler`]) and relays the answer. All
//! validation and persistence for that slice live in `schedulerd`; `webd` owns
//! no state there.
//!
//! The library CRUD aliases under `/api/chime-scheduler/library/*` resolve to the
//! media-backed handlers in [`crate::chime_library`], which install/remove the
//! files in `Chimes/` on the MEDIA partition instead of proxying through
//! `schedulerd`.

use axum::Json;
use axum::Router;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::routing::{get, post, put};
use serde_json::{Value, json};

use crate::AppState;
use crate::error::ApiError;
use crate::gadget::TransportError;

/// The chime-scheduler sub-routes, mounted under `/api` by [`crate::route`].
pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/chime-scheduler", get(snapshot))
        .route("/chime-scheduler/schedules", post(add_schedule))
        .route(
            "/chime-scheduler/schedules/{id}",
            put(update_schedule).delete(delete_schedule),
        )
        .route("/chime-scheduler/groups", post(add_group))
        .route(
            "/chime-scheduler/groups/{id}",
            put(update_group).delete(delete_group),
        )
        .route("/chime-scheduler/random-mode", put(set_random_mode))
}

/// `GET /api/chime-scheduler`: the full scheduler snapshot — schedules, groups,
/// random-mode, the chime library, and the form menus — in one request so the
/// SPA can bootstrap the page with a single round-trip.
pub(crate) async fn snapshot(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    let mut resp = call(&state, json!({ "cmd": "snapshot" })).await?;
    let items = crate::route::read(state.catalog, crate::query::list_chime_library).await?;
    let library = items
        .into_iter()
        .map(|item| json!({ "filename": item.name, "bytes": item.size_bytes }))
        .collect::<Vec<_>>();
    resp["library"] = Value::Array(library);
    Ok(Json(resp))
}

/// `POST /api/chime-scheduler/schedules`: create a schedule. The body is the
/// schedule definition (validated by `schedulerd`).
pub(crate) async fn add_schedule(
    State(state): State<AppState>,
    Json(input): Json<Value>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let resp = call(&state, json!({ "cmd": "add_schedule", "input": input })).await?;
    Ok((StatusCode::CREATED, Json(resp)))
}

/// `PUT /api/chime-scheduler/schedules/{id}`: replace a schedule by id.
pub(crate) async fn update_schedule(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(input): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    let resp = call(
        &state,
        json!({ "cmd": "update_schedule", "id": id, "input": input }),
    )
    .await?;
    Ok(Json(resp))
}

/// `DELETE /api/chime-scheduler/schedules/{id}`: delete a schedule by id.
pub(crate) async fn delete_schedule(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let resp = call(&state, json!({ "cmd": "delete_schedule", "id": id })).await?;
    Ok(Json(resp))
}

/// `POST /api/chime-scheduler/groups`: create a chime group.
pub(crate) async fn add_group(
    State(state): State<AppState>,
    Json(input): Json<Value>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let resp = call(&state, json!({ "cmd": "add_group", "input": input })).await?;
    Ok((StatusCode::CREATED, Json(resp)))
}

/// `PUT /api/chime-scheduler/groups/{id}`: replace a group by id.
pub(crate) async fn update_group(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(input): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    let resp = call(
        &state,
        json!({ "cmd": "update_group", "id": id, "input": input }),
    )
    .await?;
    Ok(Json(resp))
}

/// `DELETE /api/chime-scheduler/groups/{id}`: delete a group by id.
pub(crate) async fn delete_group(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let resp = call(&state, json!({ "cmd": "delete_group", "id": id })).await?;
    Ok(Json(resp))
}

/// `PUT /api/chime-scheduler/random-mode`: set the random-on-boot configuration.
pub(crate) async fn set_random_mode(
    State(state): State<AppState>,
    Json(mode): Json<Value>,
) -> Result<Json<Value>, ApiError> {
    let resp = call(&state, json!({ "cmd": "set_random_mode", "mode": mode })).await?;
    Ok(Json(resp))
}

/// Cascade a chime rename through the scheduler state (schedules + groups).
pub(crate) async fn rename_chime_references(
    state: &AppState,
    from: &str,
    to: &str,
) -> Result<(), ApiError> {
    call(
        state,
        json!({ "cmd": "rename_chime_references", "from": from, "to": to }),
    )
    .await?;
    Ok(())
}

/// Cascade a chime delete through the scheduler state.
pub(crate) async fn remove_chime_references(
    state: &AppState,
    filenames: &[String],
) -> Result<(), ApiError> {
    call(
        state,
        json!({ "cmd": "remove_chime_references", "filenames": filenames }),
    )
    .await?;
    Ok(())
}

/// Forward one request to `schedulerd` on a blocking task, relaying the JSON
/// answer or mapping the `{error:{code,message}}` envelope / transport failure
/// onto an [`ApiError`].
async fn call(state: &AppState, request: Value) -> Result<Value, ApiError> {
    let client = state.scheduler.clone();
    let join = tokio::task::spawn_blocking(move || client.call(request)).await;

    let resp = match join {
        Ok(Ok(value)) => value,
        Ok(Err(TransportError::Unavailable(_))) => {
            return Err(ApiError::status(
                StatusCode::SERVICE_UNAVAILABLE,
                "scheduler_unavailable",
                "the chime scheduler service is not reachable",
            ));
        }
        Ok(Err(TransportError::Protocol(_))) => {
            return Err(ApiError::status(
                StatusCode::BAD_GATEWAY,
                "scheduler_protocol",
                "the chime scheduler returned an unreadable reply",
            ));
        }
        Err(_) => return Err(ApiError::Internal),
    };

    if let Some(err) = resp.get("error") {
        let code = err
            .get("code")
            .and_then(Value::as_str)
            .unwrap_or("scheduler_error")
            .to_owned();
        let message = err
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("chime scheduler error")
            .to_owned();
        let status = status_for(&code);
        return Err(ApiError::upstream(status, code, message));
    }
    Ok(resp)
}

/// Map a `schedulerd` error code onto an HTTP status. Unknown codes are treated
/// as client validation errors (`422`) — the conservative default, since the
/// vast majority of `schedulerd` errors are input-validation failures.
fn status_for(code: &str) -> StatusCode {
    match code {
        "not_found" => StatusCode::NOT_FOUND,
        "bad_request" => StatusCode::BAD_REQUEST,
        "io_error" | "locked" => StatusCode::BAD_GATEWAY,
        _ => StatusCode::UNPROCESSABLE_ENTITY,
    }
}
