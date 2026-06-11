//! `GET/POST/PUT/DELETE /api/chime-scheduler/*` — the lock-chime scheduler,
//! groups, random-on-boot mode, and chime library. `webd` is a **pure proxy**:
//! every handler forwards a `cmd`-tagged JSON request to `schedulerd` over its
//! control socket (see [`crate::scheduler`]) and relays the answer. All
//! validation and persistence live in `schedulerd`; `webd` owns no state here.
//!
//! The one exception is the library *upload*: a chime file is too large to pass
//! inline through the control frame, so `webd` streams the multipart body to a
//! staged temp file and hands `schedulerd` the *path*, mirroring how the media
//! install path stages a blob for `gadgetd`. `schedulerd` then adopts the staged
//! file into the library directory (the sole writer of that directory).

use axum::Json;
use axum::Router;
use axum::extract::multipart::MultipartError;
use axum::extract::{Multipart, Path, State};
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
        .route(
            "/chime-scheduler/library",
            get(list_library)
                .post(upload_library)
                .layer(axum::extract::DefaultBodyLimit::max(LIBRARY_BODY_LIMIT)),
        )
        .route(
            "/chime-scheduler/library/{filename}",
            axum::routing::delete(delete_library),
        )
}

/// The multipart form field carrying the WAV bytes for a library upload.
const FIELD_NAME: &str = "file";

/// Maximum accepted library-chime size (1 MiB), enforced incrementally while
/// reading the upload so a hostile client cannot force an unbounded buffer.
const LIBRARY_MAX_BYTES: usize = 1024 * 1024;

/// Hard request-body ceiling for a library upload (8 MiB), applied as the
/// route's `DefaultBodyLimit`. Defense-in-depth above the 1 MiB logical cap.
pub(crate) const LIBRARY_BODY_LIMIT: usize = 8 * 1024 * 1024;

/// `GET /api/chime-scheduler`: the full scheduler snapshot — schedules, groups,
/// random-mode, the chime library, and the form menus — in one request so the
/// SPA can bootstrap the page with a single round-trip.
pub(crate) async fn snapshot(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    let resp = call(&state, json!({ "cmd": "snapshot" })).await?;
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

/// `GET /api/chime-scheduler/library`: list the chime library.
pub(crate) async fn list_library(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    let resp = call(&state, json!({ "cmd": "list_library" })).await?;
    Ok(Json(resp))
}

/// `POST /api/chime-scheduler/library`: upload a chime into the library.
///
/// Accepts `multipart/form-data` with a single `file` field. The filename is
/// taken from the multipart part (sanitized by `schedulerd`). The bytes are
/// validated as a 16-bit PCM WAV, staged to a temp file, and `schedulerd` is
/// asked to adopt the staged path into the library directory.
pub(crate) async fn upload_library(
    State(state): State<AppState>,
    multipart: Multipart,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let (filename, bytes) = read_library_upload(multipart).await?;
    crate::chimes::validate_lock_chime_wav(&bytes)
        .map_err(|msg| ApiError::status(StatusCode::UNPROCESSABLE_ENTITY, "invalid_wav", msg))?;

    let staged = stage_upload(&bytes).map_err(|_| {
        ApiError::status(
            StatusCode::BAD_GATEWAY,
            "stage_failed",
            "could not stage upload",
        )
    })?;
    let staged_str = staged.to_string_lossy().into_owned();

    let result = call(
        &state,
        json!({
            "cmd": "add_library_file",
            "staged_path": staged_str,
            "filename": filename,
        }),
    )
    .await;

    // Best-effort cleanup: schedulerd removes the staged file when it adopts it
    // (rename, or copy+remove across filesystems); on any failure it leaves the
    // temp file untouched, so webd must clean it up.
    let _ = std::fs::remove_file(&staged);

    let resp = result?;
    Ok((StatusCode::CREATED, Json(resp)))
}

/// `DELETE /api/chime-scheduler/library/{filename}`: remove a library chime.
pub(crate) async fn delete_library(
    State(state): State<AppState>,
    Path(filename): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let resp = call(
        &state,
        json!({ "cmd": "delete_library_file", "filename": filename }),
    )
    .await?;
    Ok(Json(resp))
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

/// Read the single `file` field from the multipart body, returning its filename
/// and bytes. Enforces the size cap incrementally; a missing/duplicate `file`
/// field is a `400`, oversize is a `422`.
async fn read_library_upload(mut multipart: Multipart) -> Result<(String, Vec<u8>), ApiError> {
    let mut found: Option<(String, Vec<u8>)> = None;
    while let Some(mut field) = multipart.next_field().await.map_err(map_multipart_err)? {
        if field.name() != Some(FIELD_NAME) {
            while field.chunk().await.map_err(map_multipart_err)?.is_some() {}
            continue;
        }
        if found.is_some() {
            return Err(ApiError::bad_request(
                "duplicate_field",
                "multiple `file` fields in upload",
            ));
        }
        let filename = field.file_name().map(ToOwned::to_owned).ok_or_else(|| {
            ApiError::bad_request("filename_required", "upload is missing a filename")
        })?;
        let mut buf: Vec<u8> = Vec::new();
        while let Some(chunk) = field.chunk().await.map_err(map_multipart_err)? {
            let projected = buf.len().saturating_add(chunk.len());
            if projected > LIBRARY_MAX_BYTES {
                return Err(ApiError::status(
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "chime_too_large",
                    format!("chime exceeds the {LIBRARY_MAX_BYTES}-byte limit"),
                ));
            }
            buf.extend_from_slice(&chunk);
        }
        found = Some((filename, buf));
    }
    found.ok_or_else(|| ApiError::bad_request("upload_required", "missing `file` upload field"))
}

/// Map a multipart decode error to a `400`.
#[allow(clippy::needless_pass_by_value)]
fn map_multipart_err(err: MultipartError) -> ApiError {
    ApiError::bad_request("invalid_multipart", format!("malformed upload: {err}"))
}

/// Stage upload bytes to a unique temp file `schedulerd` can adopt. Returns the
/// absolute staged path.
fn stage_upload(bytes: &[u8]) -> std::io::Result<std::path::PathBuf> {
    let dir = std::env::temp_dir().join("teslausb-chime-uploads");
    std::fs::create_dir_all(&dir)?;
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let path = dir.join(format!("upload-{nanos}.wav"));
    std::fs::write(&path, bytes)?;
    Ok(path)
}
