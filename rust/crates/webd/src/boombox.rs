//! `GET /api/boombox` · `POST /api/boombox` · `DELETE /api/boombox/:name`
//!
//! Boombox horn sounds live under `Boombox/` on the MEDIA (p2) partition.
//! Tesla loads the first 5 alphabetically; `.wav` and `.mp3` are supported.
//! WAV files get a full PCM header check (reusing `validate_lock_chime_wav`);
//! MP3 files are accepted on extension + size alone.

use axum::Json;
use axum::extract::{Multipart, Path, State};
use axum::http::StatusCode;
use serde_json::Value;

use crate::AppState;
use crate::dto::MediaListDto;
use crate::error::ApiError;
use crate::media_upload::{check_extension, read_file_upload, sanitise_filename};

/// MEDIA (p2) gadgetd partition index.
const PARTITION_MEDIA: u8 = 2;

/// Category root directory on p2 (exFAT is case-sensitive).
const BOOMBOX_DIR: &str = "Boombox";

/// Maximum accepted boombox file size (1 MiB — these are short audio clips).
const BOOMBOX_MAX_BYTES: usize = 1024 * 1024;

/// Axum `DefaultBodyLimit` for the POST route (8 MiB — defence-in-depth above
/// the 1 MiB logical cap, matching the chimes pattern).
pub(crate) const BOOMBOX_BODY_LIMIT: usize = 8 * 1024 * 1024;

/// `GET /api/boombox` — list installed boombox horn sounds.
///
/// Reads `media_entries` for `partition='slot1' AND rel_path LIKE 'Boombox/%'`.
/// Degrades to `{items:[]}` on a catalog that predates the media inventory.
pub(crate) async fn list_boombox(
    State(state): State<AppState>,
) -> Result<Json<MediaListDto>, ApiError> {
    let items = crate::route::read(state.catalog, crate::query::list_boombox).await?;
    Ok(Json(MediaListDto { items }))
}

/// `POST /api/boombox` — install a boombox horn sound.
///
/// Accepts `multipart/form-data` with a single `file` field holding a `.wav`
/// or `.mp3` file (≤ 1 MiB). The destination path is constructed as
/// `Boombox/<sanitised_filename>` — the client-supplied name is never used raw.
pub(crate) async fn install_boombox(
    State(state): State<AppState>,
    multipart: Multipart,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let (raw_name, bytes) = read_file_upload(multipart, "file", BOOMBOX_MAX_BYTES).await?;
    let name = sanitise_filename(&raw_name)?;
    check_extension(&name, &["wav", "mp3"])?;

    // WAV files: full PCM header check.
    if name.to_ascii_lowercase().ends_with(".wav") {
        crate::chimes::validate_lock_chime_wav(&bytes).map_err(|msg| {
            ApiError::status(StatusCode::UNPROCESSABLE_ENTITY, "invalid_wav", msg)
        })?;
    }

    let rel_path = format!("{BOOMBOX_DIR}/{name}");
    crate::route::run_install(state, "boombox_install", PARTITION_MEDIA, rel_path, bytes).await
}

/// `DELETE /api/boombox/:name` — remove a boombox horn sound.
///
/// `:name` is the file name (e.g. `horn.wav`). The handler reconstructs the
/// p2 `rel_path = Boombox/<name>` — the client never supplies the full path.
pub(crate) async fn remove_boombox(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let name = sanitise_filename(&name)?;
    let rel_path = format!("{BOOMBOX_DIR}/{name}");
    crate::route::run_remove(state, "boombox_remove", PARTITION_MEDIA, rel_path).await
}
