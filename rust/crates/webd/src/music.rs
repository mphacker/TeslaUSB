//! `GET /api/music` · `POST /api/music` · `DELETE /api/music/:name`
//!
//! Music files live under `Music/` on the MEDIA (p2) partition. Tesla supports
//! artist/album subdirectories, so any depth is accepted by the producer. The
//! install endpoint places files at `Music/<sanitised_filename>` (top-level),
//! which covers the common case; the delete endpoint accepts top-level file
//! names only (no sub-path removal via the API — use the Samba share for
//! directory management).
//!
//! Accepted formats: `.mp3`, `.flac`, `.wav`, `.aac`, `.m4a` (≤ 10 MiB).

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
const MUSIC_DIR: &str = "Music";

/// Maximum accepted music file size (10 MiB).
const MUSIC_MAX_BYTES: usize = 10 * 1024 * 1024;

/// Axum `DefaultBodyLimit` for the POST route (32 MiB — defence-in-depth).
pub(crate) const MUSIC_BODY_LIMIT: usize = 32 * 1024 * 1024;

const MUSIC_EXTENSIONS: &[&str] = &["mp3", "flac", "wav", "aac", "m4a"];

/// `GET /api/music` — list installed music files (any depth on p2 `Music/`).
pub(crate) async fn list_music(
    State(state): State<AppState>,
) -> Result<Json<MediaListDto>, ApiError> {
    let items = crate::route::read(state.catalog, crate::query::list_music).await?;
    Ok(Json(MediaListDto { items }))
}

/// `POST /api/music` — install a music file at `Music/<sanitised_filename>`.
pub(crate) async fn install_music(
    State(state): State<AppState>,
    multipart: Multipart,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let (raw_name, bytes) = read_file_upload(multipart, "file", MUSIC_MAX_BYTES).await?;
    let name = sanitise_filename(&raw_name)?;
    check_extension(&name, MUSIC_EXTENSIONS)?;

    let rel_path = format!("{MUSIC_DIR}/{name}");
    crate::route::run_install(state, "music_install", PARTITION_MEDIA, rel_path, bytes).await
}

/// `DELETE /api/music/:name` — remove a top-level music file.
pub(crate) async fn remove_music(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let name = sanitise_filename(&name)?;
    let rel_path = format!("{MUSIC_DIR}/{name}");
    crate::route::run_remove(state, "music_remove", PARTITION_MEDIA, rel_path).await
}

/// `POST /api/music/bulk-delete` — remove several top-level music files in ONE
/// `gadgetd` handoff. Body: `{ "names": ["track.mp3", …] }`. Each name rebuilds
/// `Music/<name>`; as with the single delete, only top-level files are
/// addressable (sub-path removal is via the Samba share).
pub(crate) async fn bulk_delete_music(
    State(state): State<AppState>,
    Json(req): Json<BulkDeleteRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let rel_paths = plan_bulk_delete(MUSIC_DIR, &req.names)?;
    crate::route::run_remove_many(state, "music_remove", PARTITION_MEDIA, rel_paths).await
}
