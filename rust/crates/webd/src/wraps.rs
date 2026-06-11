//! `GET /api/wraps` · `POST /api/wraps` · `DELETE /api/wraps/:name`
//!
//! Wrap images live under `LightShow/wraps/` on the MEDIA (p2) partition.
//! Tesla supports up to 10 wraps; only `.png` files are accepted (≤ 1 MiB).
//! PNG magic bytes (`\x89PNG\r\n\x1a\n`) are verified before any gadget round-trip.

use axum::Json;
use axum::extract::{Multipart, Path, State};
use axum::http::StatusCode;
use serde_json::Value;

use crate::AppState;
use crate::dto::MediaListDto;
use crate::error::ApiError;
use crate::media_upload::{
    BulkDeleteRequest, check_extension, plan_bulk_delete, read_file_upload, sanitise_filename,
    validate_png_magic, validate_wrap_dimensions,
};

const PARTITION_MEDIA: u8 = 2;
const WRAPS_DIR: &str = "LightShow/wraps";

/// Maximum accepted wrap image size (1 MiB).
const WRAPS_MAX_BYTES: usize = 1024 * 1024;

/// Axum `DefaultBodyLimit` for the POST route (8 MiB — defence-in-depth).
pub(crate) const WRAPS_BODY_LIMIT: usize = 8 * 1024 * 1024;

/// `GET /api/wraps` — list installed wrap images.
pub(crate) async fn list_wraps(
    State(state): State<AppState>,
) -> Result<Json<MediaListDto>, ApiError> {
    let items = crate::route::read(state.catalog, crate::query::list_wraps).await?;
    Ok(Json(MediaListDto { items }))
}

/// `POST /api/wraps` — install a wrap PNG at
/// `LightShow/wraps/<sanitised_filename>`.
///
/// Enforces v1 parity before any gadget round-trip: PNG magic and both sides
/// within `512..=1024` pixels.
pub(crate) async fn install_wrap(
    State(state): State<AppState>,
    multipart: Multipart,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let (raw_name, bytes) = read_file_upload(multipart, "file", WRAPS_MAX_BYTES).await?;
    let name = sanitise_filename(&raw_name)?;
    check_extension(&name, &["png"])?;
    validate_png_magic(&bytes)?;
    validate_wrap_dimensions(&bytes)?;

    let rel_path = format!("{WRAPS_DIR}/{name}");
    crate::route::run_install(state, "wrap_install", PARTITION_MEDIA, rel_path, bytes).await
}

/// `DELETE /api/wraps/:name` — remove a wrap image.
pub(crate) async fn remove_wrap(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let name = sanitise_filename(&name)?;
    let rel_path = format!("{WRAPS_DIR}/{name}");
    crate::route::run_remove(state, "wrap_remove", PARTITION_MEDIA, rel_path).await
}

/// `POST /api/wraps/bulk-delete` — remove several wrap images in ONE `gadgetd`
/// handoff. Body: `{ "names": ["wrap.png", …] }`. Each name rebuilds
/// `LightShow/wraps/<name>`.
pub(crate) async fn bulk_delete_wraps(
    State(state): State<AppState>,
    Json(req): Json<BulkDeleteRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let rel_paths = plan_bulk_delete(WRAPS_DIR, &req.names)?;
    crate::route::run_remove_many(state, "wrap_remove", PARTITION_MEDIA, rel_paths).await
}
