//! `GET /api/lightshows` · `POST /api/lightshows` · `DELETE /api/lightshows/:name`
//!
//! Light show packages live under `LightShow/` on the MEDIA (p2) partition.
//! Files are `.fseq`, `.mp3`, or `.wav` (≤ 5 MiB). Wrap images live in a
//! separate root-level `Wraps/` folder (see [`crate::wraps`]), so the two
//! categories never overlap on disk.

use axum::Json;
use axum::extract::{Multipart, Path, State};
use axum::http::StatusCode;
use serde_json::Value;

use crate::AppState;
use crate::dto::MediaListDto;
use crate::error::ApiError;
use crate::media_upload::{
    BulkDeleteRequest, check_extension, plan_bulk_delete, read_file_upload, sanitise_filename,
};

const PARTITION_MEDIA: u8 = 2;
const LIGHTSHOW_DIR: &str = "LightShow";

/// Maximum accepted light show file size (32 MiB). Light shows ship with full
/// song audio (`.mp3`/`.wav`), which routinely exceeds 10 MiB; 32 MiB covers a
/// full-length WAV with headroom while staying small against the ~1 GiB
/// `media.img`.
const LIGHTSHOW_MAX_BYTES: usize = 32 * 1024 * 1024;

/// Axum `DefaultBodyLimit` for the POST route (34 MiB — `LIGHTSHOW_MAX_BYTES`
/// plus a 2 MiB margin for multipart framing; defence-in-depth).
pub(crate) const LIGHTSHOW_BODY_LIMIT: usize = 34 * 1024 * 1024;

const LIGHTSHOW_EXTENSIONS: &[&str] = &["fseq", "mp3", "wav"];

/// `GET /api/lightshows` — list installed light shows (excludes wraps).
pub(crate) async fn list_lightshows(
    State(state): State<AppState>,
) -> Result<Json<MediaListDto>, ApiError> {
    let items = crate::route::read(state.catalog, crate::query::list_lightshows).await?;
    Ok(Json(MediaListDto { items }))
}

/// `POST /api/lightshows` — install a light show file at
/// `LightShow/<sanitised_filename>`.
///
/// WAV files get a PCM header check; FSEQ and MP3 are accepted on extension
/// alone (FSEQ has no widely-agreed magic header).
pub(crate) async fn install_lightshow(
    State(state): State<AppState>,
    multipart: Multipart,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let (raw_name, bytes) = read_file_upload(multipart, "file", LIGHTSHOW_MAX_BYTES).await?;
    let name = sanitise_filename(&raw_name)?;
    check_extension(&name, LIGHTSHOW_EXTENSIONS)?;

    if name.to_ascii_lowercase().ends_with(".wav") {
        crate::chimes::validate_lock_chime_wav(&bytes).map_err(|msg| {
            ApiError::status(StatusCode::UNPROCESSABLE_ENTITY, "invalid_wav", msg)
        })?;
    }

    let rel_path = format!("{LIGHTSHOW_DIR}/{name}");
    crate::route::run_install(state, "lightshow_install", PARTITION_MEDIA, rel_path, bytes).await
}

/// `DELETE /api/lightshows/:name` — remove a light show file.
pub(crate) async fn remove_lightshow(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let name = sanitise_filename(&name)?;
    let rel_path = format!("{LIGHTSHOW_DIR}/{name}");
    crate::route::run_remove(state, "lightshow_remove", PARTITION_MEDIA, rel_path).await
}

/// `POST /api/lightshows/bulk-delete` — remove several light-show files in ONE
/// `gadgetd` handoff. Body: `{ "names": ["show.fseq", …] }`. Each name rebuilds
/// `LightShow/<name>`; wraps live in the separate root-level `Wraps/` folder
/// and are unreachable here.
pub(crate) async fn bulk_delete_lightshows(
    State(state): State<AppState>,
    Json(req): Json<BulkDeleteRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let rel_paths = plan_bulk_delete(LIGHTSHOW_DIR, &req.names)?;
    crate::route::run_remove_many(state, "lightshow_remove", PARTITION_MEDIA, rel_paths).await
}
