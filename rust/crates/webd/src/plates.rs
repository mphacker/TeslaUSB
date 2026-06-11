//! `GET /api/plates` · `POST /api/plates` · `DELETE /api/plates/:name`
//!
//! License plate images live under `LicensePlate/` on the MEDIA (p2) partition.
//! Tesla supports up to 5 plates; only `.png` files are accepted (≤ 512 KiB).
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
    validate_plate_dimensions, validate_plate_filename, validate_png_magic,
};

const PARTITION_MEDIA: u8 = 2;
const PLATES_DIR: &str = "LicensePlate";

/// Maximum accepted license plate image size (512 KiB).
const PLATES_MAX_BYTES: usize = 512 * 1024;

/// Axum `DefaultBodyLimit` for the POST route (4 MiB — defence-in-depth).
pub(crate) const PLATES_BODY_LIMIT: usize = 4 * 1024 * 1024;

/// `GET /api/plates` — list installed license plate images.
pub(crate) async fn list_plates(
    State(state): State<AppState>,
) -> Result<Json<MediaListDto>, ApiError> {
    let items = crate::route::read(state.catalog, crate::query::list_plates).await?;
    Ok(Json(MediaListDto { items }))
}

/// `POST /api/plates` — install a license plate PNG at
/// `LicensePlate/<sanitised_filename>`.
///
/// Enforces v1 parity rules before any gadget round-trip: PNG magic, a 1-12
/// alphanumeric filename, and exact `420x75` (NA) or `492x75` (EU) dimensions.
pub(crate) async fn install_plate(
    State(state): State<AppState>,
    multipart: Multipart,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let (raw_name, bytes) = read_file_upload(multipart, "file", PLATES_MAX_BYTES).await?;
    let name = sanitise_filename(&raw_name)?;
    check_extension(&name, &["png"])?;
    validate_plate_filename(&name)?;
    validate_png_magic(&bytes)?;
    validate_plate_dimensions(&bytes)?;

    let rel_path = format!("{PLATES_DIR}/{name}");
    crate::route::run_install(state, "plate_install", PARTITION_MEDIA, rel_path, bytes).await
}

/// `DELETE /api/plates/:name` — remove a license plate image.
pub(crate) async fn remove_plate(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let name = sanitise_filename(&name)?;
    let rel_path = format!("{PLATES_DIR}/{name}");
    crate::route::run_remove(state, "plate_remove", PARTITION_MEDIA, rel_path).await
}

/// `POST /api/plates/bulk-delete` — remove several license-plate images in ONE
/// `gadgetd` handoff. Body: `{ "names": ["plate.png", …] }`. Each name rebuilds
/// `LicensePlate/<name>`.
pub(crate) async fn bulk_delete_plates(
    State(state): State<AppState>,
    Json(req): Json<BulkDeleteRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let rel_paths = plan_bulk_delete(PLATES_DIR, &req.names)?;
    crate::route::run_remove_many(state, "plate_remove", PARTITION_MEDIA, rel_paths).await
}
