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
use crate::media_upload::{
    BulkDeleteRequest, check_extension, plan_bulk_delete, read_file_upload, sanitise_filename,
};

/// MEDIA (p2) gadgetd partition index.
const PARTITION_MEDIA: u8 = 2;

/// Category root directory on p2 (exFAT is case-sensitive).
const BOOMBOX_DIR: &str = "Boombox";

/// Maximum accepted boombox file size (8 MiB). Boombox sounds are short
/// external-speaker clips, but `.mp3`/`.wav` at higher bitrates (or a few
/// seconds of WAV) can exceed the old 1 MiB cap; 8 MiB is generous yet safe
/// against the ~1 GiB `media.img` (the library is capped at 5 files).
const BOOMBOX_MAX_BYTES: usize = 8 * 1024 * 1024;

/// Tesla loads the first 5 boombox sounds alphabetically; reject uploads that
/// would grow the library beyond this. Re-uploading an existing name (a
/// replace) is always allowed because it does not increase the total.
const BOOMBOX_MAX_FILES: usize = 5;

/// Axum `DefaultBodyLimit` for the POST route (10 MiB — `BOOMBOX_MAX_BYTES`
/// plus a 2 MiB margin for multipart framing; defence-in-depth).
pub(crate) const BOOMBOX_BODY_LIMIT: usize = 10 * 1024 * 1024;

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
/// or `.mp3` file (≤ 8 MiB). The destination path is constructed as
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

    // Capacity: at most BOOMBOX_MAX_FILES sounds total. A re-upload of the same
    // destination path is a replace (net count unchanged), so it is permitted
    // even at capacity; a brand-new path at capacity is rejected before any
    // gadgetd handoff. The dedupe identity is the full destination `rel_path`,
    // not the bare file name: `list_boombox` returns every row under `Boombox/%`
    // (including any nested `Boombox/sub/<name>`), so matching on name alone
    // could let a root-level upload masquerade as a replace of a same-named
    // nested file and bypass the cap. The comparison is exact (case-sensitive)
    // to match how p2 stores and addresses files (see `BOOMBOX_DIR` note above):
    // a differently-cased path (`c.mp3` vs `C.MP3`) is a distinct file, so
    // treating it as a replace would let the library grow past the cap.
    //
    // The count is read from the catalog, which trails an in-flight install by
    // one index pass; two genuinely concurrent uploads of distinct new names
    // could therefore both pass this check. That race is accepted: this is a
    // single-operator appliance whose UI installs one file at a time and each
    // install briefly ejects the USB drive, so uploads are effectively
    // serialised in practice.
    let rel_path = format!("{BOOMBOX_DIR}/{name}");
    let existing = crate::route::read(state.catalog.clone(), crate::query::list_boombox).await?;
    let is_replace = existing.iter().any(|item| item.rel_path == rel_path);
    if !is_replace && existing.len() >= BOOMBOX_MAX_FILES {
        return Err(ApiError::status(
            StatusCode::UNPROCESSABLE_ENTITY,
            "boombox_full",
            format!(
                "Boombox already holds the maximum of {BOOMBOX_MAX_FILES} sounds; delete one before uploading another"
            ),
        ));
    }

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

/// `POST /api/boombox/bulk-delete` — remove several boombox sounds in ONE
/// `gadgetd` handoff. Body: `{ "names": ["horn.wav", …] }`. Each name is a bare
/// file name; the handler rebuilds `Boombox/<name>`, so a client can never
/// address a file outside this category.
pub(crate) async fn bulk_delete_boombox(
    State(state): State<AppState>,
    Json(req): Json<BulkDeleteRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let rel_paths = plan_bulk_delete(BOOMBOX_DIR, &req.names)?;
    crate::route::run_remove_many(state, "boombox_remove", PARTITION_MEDIA, rel_paths).await
}
