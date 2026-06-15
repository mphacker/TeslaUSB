//! `GET /api/wraps` · `POST /api/wraps` · `DELETE /api/wraps/:name`
//!
//! Wrap images live under `Wraps/` at the root of the MEDIA (p2) partition —
//! the layout Tesla's Paint Shop reads (see `github.com/teslamotors/custom-wraps`:
//! "Create a folder called `Wraps` at the root level of the drive").
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
    validate_png_magic, validate_wrap_dimensions, validate_wrap_filename,
};

const PARTITION_MEDIA: u8 = 2;
const WRAPS_DIR: &str = "Wraps";

/// Maximum accepted wrap image size (1 MiB).
const WRAPS_MAX_BYTES: usize = 1024 * 1024;

/// Tesla's Paint Shop reads up to ~10 wraps; reject uploads beyond this. A
/// re-upload of an existing (exact) name is a replace and is always allowed.
const WRAPS_MAX_FILES: usize = 10;

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
/// `Wraps/<sanitised_filename>`.
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
    validate_wrap_filename(&name)?;
    validate_png_magic(&bytes)?;
    validate_wrap_dimensions(&bytes)?;

    let rel_path = format!("{WRAPS_DIR}/{name}");

    // Capacity: at most WRAPS_MAX_FILES wraps total. An exact re-upload of the
    // same destination path is a replace (net count unchanged) and is permitted
    // even at capacity; a new path at capacity is rejected before any gadgetd
    // handoff. The dedupe identity is the full destination `rel_path`, not the
    // bare file name: `list_wraps` returns every row under `Wraps/%` (including
    // any nested `Wraps/sub/<name>`), so matching on name alone could let a
    // root-level upload masquerade as a replace of a same-named nested file and
    // bypass the cap. The comparison is exact (case-sensitive) to match the
    // case-sensitive p2 store, so a differently-cased path is a distinct file.
    // The catalog count trails an in-flight install by one index pass; the
    // resulting TOCTOU under truly concurrent distinct uploads is accepted (a
    // single-operator appliance that installs one file at a time).
    let existing =
        crate::route::read(state.catalog.clone(), crate::query::list_wraps).await?;
    let is_replace = existing.iter().any(|item| item.rel_path == rel_path);
    if !is_replace && existing.len() >= WRAPS_MAX_FILES {
        return Err(ApiError::status(
            StatusCode::UNPROCESSABLE_ENTITY,
            "wraps_full",
            format!(
                "Wraps folder already holds the maximum of {WRAPS_MAX_FILES} images; delete one before uploading another"
            ),
        ));
    }

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
/// `Wraps/<name>`.
pub(crate) async fn bulk_delete_wraps(
    State(state): State<AppState>,
    Json(req): Json<BulkDeleteRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let rel_paths = plan_bulk_delete(WRAPS_DIR, &req.names)?;
    crate::route::run_remove_many(state, "wrap_remove", PARTITION_MEDIA, rel_paths).await
}
