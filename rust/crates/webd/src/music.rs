//! `GET /api/music` · `POST /api/music` · `DELETE /api/music/:name`
//!
//! Music files live under `Music/` on the MEDIA (p2) partition. Tesla supports
//! artist/album subdirectories, so any depth is accepted by the producer. The
//! install endpoint places files at `Music/<sanitised_filename>` (top-level) by
//! default, or under `Music/<validated_subfolder>/<name>` when the optional
//! `path` multipart field is supplied. The folder management endpoints create
//! and remove directories; the move endpoint relocates a file within `Music/`;
//! the nested-delete endpoint bulk-removes arbitrary-depth files in one handoff.
//!
//! Accepted formats: `.mp3`, `.flac`, `.wav`, `.aac`, `.m4a` (≤ 10 MiB).

use axum::Json;
use axum::extract::{Multipart, Path, State};
use axum::http::StatusCode;
use serde::Deserialize;
use serde_json::Value;

use crate::AppState;
use crate::dto::MediaListDto;
use crate::error::ApiError;
use crate::media_upload::{
    BulkDeleteRequest, MAX_BULK_DELETE, check_extension, plan_bulk_delete, sanitise_filename,
};

const PARTITION_MEDIA: u8 = 2;
const MUSIC_DIR: &str = "Music";

/// Maximum accepted music file size (10 MiB).
const MUSIC_MAX_BYTES: usize = 10 * 1024 * 1024;

/// Axum `DefaultBodyLimit` for the POST route (32 MiB — defence-in-depth).
pub(crate) const MUSIC_BODY_LIMIT: usize = 32 * 1024 * 1024;

const MUSIC_EXTENSIONS: &[&str] = &["mp3", "flac", "wav", "aac", "m4a"];

/// The sentinel keep-file written to mark a folder on the exFAT image.
const FOLDER_PLACEHOLDER: &[u8] = b"teslausb-folder-placeholder\n";

/// Validate a caller-supplied subpath (relative under `Music/`).
///
/// Splits on `/`, then validates every component: must be non-empty, not `.`
/// or `..`, must not contain an embedded NUL byte, a backslash `\`, or any
/// ASCII control character (< 0x20 or 0x7f), and must not exceed 255 bytes.
/// The empty string is rejected outright. Returns the cleaned joined subpath
/// on success, or `Err(400 invalid_path)`.
fn validate_music_subpath(raw: &str) -> Result<String, ApiError> {
    if raw.is_empty() {
        return Err(ApiError::status(
            StatusCode::BAD_REQUEST,
            "invalid_path",
            "path must not be empty",
        ));
    }
    let mut components: Vec<&str> = Vec::new();
    for component in raw.split('/') {
        if component.is_empty() {
            return Err(ApiError::status(
                StatusCode::BAD_REQUEST,
                "invalid_path",
                "path component must not be empty (no leading, trailing, or doubled '/')",
            ));
        }
        if component == "." || component == ".." {
            return Err(ApiError::status(
                StatusCode::BAD_REQUEST,
                "invalid_path",
                format!("path component '{component}' is not allowed"),
            ));
        }
        if component.contains('\0') {
            return Err(ApiError::status(
                StatusCode::BAD_REQUEST,
                "invalid_path",
                "path component contains embedded NUL",
            ));
        }
        if component.contains('\\') {
            return Err(ApiError::status(
                StatusCode::BAD_REQUEST,
                "invalid_path",
                "path component contains a backslash",
            ));
        }
        if component.bytes().any(|b| b < 0x20 || b == 0x7f) {
            return Err(ApiError::status(
                StatusCode::BAD_REQUEST,
                "invalid_path",
                "path component contains an ASCII control character",
            ));
        }
        if component.len() > 255 {
            return Err(ApiError::status(
                StatusCode::BAD_REQUEST,
                "invalid_path",
                "path component exceeds 255 bytes",
            ));
        }
        components.push(component);
    }
    // Contract: all music subpaths are relative *under* `Music/`. Reject a
    // leading `Music/` (case-insensitive) so no caller double-prefixes the
    // path internally (e.g. `Music/Music/...`, which matches nothing on disk).
    if components
        .first()
        .is_some_and(|c| c.eq_ignore_ascii_case("music"))
    {
        return Err(ApiError::status(
            StatusCode::BAD_REQUEST,
            "invalid_path",
            "send paths relative under Music/ (no 'Music/' prefix)",
        ));
    }
    Ok(components.join("/"))
}

/// Request body for `POST /api/music/folder` and `POST /api/music/folder-delete`.
#[derive(Deserialize)]
pub(crate) struct FolderRequest {
    path: String,
}

/// Request body for `POST /api/music/move`.
#[derive(Deserialize)]
pub(crate) struct MoveRequest {
    from: String,
    to: String,
}

/// Request body for `POST /api/music/delete`.
#[derive(Deserialize)]
pub(crate) struct DeletePathsRequest {
    paths: Vec<String>,
}

/// `GET /api/music` — list installed music files (any depth on p2 `Music/`).
pub(crate) async fn list_music(
    State(state): State<AppState>,
) -> Result<Json<MediaListDto>, ApiError> {
    let items = crate::route::read(state.catalog, crate::query::list_music).await?;
    Ok(Json(MediaListDto { items }))
}

/// `POST /api/music` — install a music file, optionally into a subdirectory.
///
/// Accepts a multipart body with a required `file` field and an optional `path`
/// text field. When `path` is present and non-empty it is validated via
/// [`validate_music_subpath`] and the file is placed at
/// `Music/<path>/<sanitised_filename>`; otherwise it lands at `Music/<name>`.
pub(crate) async fn install_music(
    State(state): State<AppState>,
    mut multipart: Multipart,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let mut raw_name: Option<String> = None;
    let mut file_bytes: Option<Vec<u8>> = None;
    let mut subfolder: Option<String> = None;

    while let Some(field) = multipart.next_field().await.map_err(|e| {
        ApiError::status(
            StatusCode::BAD_REQUEST,
            "invalid_multipart",
            format!("multipart error: {e}"),
        )
    })? {
        let field_name = field.name().unwrap_or("").to_owned();
        match field_name.as_str() {
            "file" => {
                if file_bytes.is_some() {
                    return Err(ApiError::status(
                        StatusCode::BAD_REQUEST,
                        "invalid_multipart",
                        "duplicate 'file' field",
                    ));
                }
                let fname =
                    field.file_name().map_or_else(|| "upload".to_owned(), str::to_owned);
                let mut buf = Vec::with_capacity(4096);
                let mut stream = field;
                while let Some(chunk) = stream.chunk().await.map_err(|e| {
                    ApiError::status(
                        StatusCode::BAD_REQUEST,
                        "invalid_multipart",
                        format!("read error: {e}"),
                    )
                })? {
                    if buf.len() + chunk.len() > MUSIC_MAX_BYTES {
                        return Err(ApiError::status(
                            StatusCode::UNPROCESSABLE_ENTITY,
                            "file_too_large",
                            format!("file exceeds {MUSIC_MAX_BYTES} bytes"),
                        ));
                    }
                    buf.extend_from_slice(&chunk);
                }
                file_bytes = Some(buf);
                raw_name = Some(fname);
            }
            "path" => {
                let text = field.text().await.map_err(|e| {
                    ApiError::status(
                        StatusCode::BAD_REQUEST,
                        "invalid_multipart",
                        format!("multipart error: {e}"),
                    )
                })?;
                if subfolder.is_none() {
                    subfolder = Some(text);
                }
            }
            _ => {
                let _ = field.bytes().await;
            }
        }
    }

    let (raw_name, bytes) = match (raw_name, file_bytes) {
        (Some(n), Some(b)) => (n, b),
        _ => {
            return Err(ApiError::status(
                StatusCode::BAD_REQUEST,
                "missing_file",
                "expected a 'file' multipart field",
            ))
        }
    };

    let name = sanitise_filename(&raw_name)?;
    check_extension(&name, MUSIC_EXTENSIONS)?;

    let rel_path = if let Some(path) = subfolder.filter(|p| !p.is_empty()) {
        let validated = validate_music_subpath(&path)?;
        format!("{MUSIC_DIR}/{validated}/{name}")
    } else {
        format!("{MUSIC_DIR}/{name}")
    };

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
/// `gadgetd` handoff. Body: `{ "names": ["track.mp3", …] }`.
pub(crate) async fn bulk_delete_music(
    State(state): State<AppState>,
    Json(req): Json<BulkDeleteRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let rel_paths = plan_bulk_delete(MUSIC_DIR, &req.names)?;
    crate::route::run_remove_many(state, "music_remove", PARTITION_MEDIA, rel_paths).await
}

/// `POST /api/music/folder` — create a subdirectory under `Music/` by
/// installing a sentinel `.teslausb-keep` file. Body: `{ "path": "<subpath>" }`.
pub(crate) async fn create_folder(
    State(state): State<AppState>,
    Json(req): Json<FolderRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let validated = validate_music_subpath(&req.path)?;
    let rel_path = format!("{MUSIC_DIR}/{validated}/.teslausb-keep");
    crate::route::run_install(
        state,
        "music_install",
        PARTITION_MEDIA,
        rel_path,
        FOLDER_PLACEHOLDER.to_vec(),
    )
    .await
}

/// `POST /api/music/folder-delete` — remove a subdirectory under `Music/`.
/// Body: `{ "path": "<subpath>" }`.
///
/// gadgetd's durable queue re-synthesizes every delete as a regular-file-only
/// `delete_paths` mutation (`queue.rs::plan_batch` flattens `DeletePath` to a
/// plain delete effect and rebuilds it as `DeletePaths`), so a recursive
/// `delete_path` cannot survive the queue — it would be refused at apply time.
/// We therefore enumerate the folder's child files from the **authoritative
/// media-ro filesystem** (the catalog lags disk by the scan interval and can
/// miss just-applied files, leaving orphans that reappear) and delete them as
/// files. [`run_remove_many`] chunks the list internally (≤16 per enqueue) and
/// gadgetd coalesces all queued deletes into a single handoff/eject. The
/// now-empty exFAT directory is invisible in the catalog — folders are derived
/// from their files — so the folder disappears from the UI.
pub(crate) async fn delete_folder(
    State(state): State<AppState>,
    Json(req): Json<FolderRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let validated = validate_music_subpath(&req.path)?;

    // Canonicalize the read-only media root (returns NotFound if not mounted).
    let Ok(root) = tokio::fs::canonicalize(state.media.media_ro_root()).await else {
        return Err(ApiError::NotFound);
    };

    // Resolve and jail the folder path.
    let folder_candidate = root.join(format!("{MUSIC_DIR}/{validated}"));
    let folder_canonical = match tokio::fs::canonicalize(&folder_candidate).await {
        Ok(p) if p.starts_with(&root) => p,
        _ => return Err(ApiError::NotFound),
    };

    // Assert it is a directory (not a file).
    let meta = tokio::fs::metadata(&folder_canonical)
        .await
        .map_err(|_| ApiError::NotFound)?;
    if !meta.is_dir() {
        return Err(ApiError::NotFound);
    }

    // Walk the directory synchronously collecting every regular file.
    // Explicit stack/queue — no additional crate dependency needed.
    // Symlinks are skipped to prevent traversal attacks.
    let rel_paths = tokio::task::spawn_blocking(move || -> Result<Vec<String>, ApiError> {
        let mut stack = vec![folder_canonical];
        let mut files: Vec<String> = Vec::new();

        while let Some(dir) = stack.pop() {
            let entries = std::fs::read_dir(&dir).map_err(|_| ApiError::Internal)?;
            for entry in entries {
                let entry = match entry {
                    Ok(e) => e,
                    Err(_) => continue,
                };
                let ft = match entry.file_type() {
                    Ok(t) => t,
                    Err(_) => continue,
                };
                if ft.is_symlink() {
                    continue; // never follow symlinks
                }
                if ft.is_dir() {
                    stack.push(entry.path());
                } else if ft.is_file() {
                    let abs = entry.path();
                    let rel = abs.strip_prefix(&root).map_err(|_| ApiError::Internal)?;
                    // Rebuild as a forward-slash path regardless of OS separator.
                    let rel_str = rel
                        .components()
                        .map(|c| c.as_os_str().to_string_lossy().into_owned())
                        .collect::<Vec<_>>()
                        .join("/");
                    files.push(rel_str);
                }
            }
        }
        Ok(files)
    })
    .await
    .map_err(|_| ApiError::Internal)??;

    if rel_paths.is_empty() {
        return Err(ApiError::status(
            StatusCode::NOT_FOUND,
            "folder_not_found",
            "no files found under the given folder",
        ));
    }

    let mut rel_paths = rel_paths;
    rel_paths.sort();
    rel_paths.dedup();

    // run_remove_many chunks internally (≤16 per enqueue); gadgetd coalesces
    // all into a single handoff.
    crate::route::run_remove_many(state, "music_remove", PARTITION_MEDIA, rel_paths).await
}

/// `POST /api/music/move` — copy a music file to a new location within `Music/`.
///
/// Body: `{ "from": "<src subpath>", "to": "<dest subpath incl filename>" }`.
///
/// ## Safety — copy only; the SPA deletes the source after convergence
///
/// gadgetd's durable queue applies DELETES BEFORE INSTALLS within a single
/// handoff (`queue.rs::plan_batch` builds `applies` as delete chunks first, then
/// installs). So enqueueing the source delete alongside the destination install
/// here would remove the original *before* the copy lands — if the copy then
/// failed the file would be LOST. Instead this endpoint enqueues ONLY the
/// destination install (a copy). The SPA's convergence poll waits until the
/// destination is present in the catalog, then issues a separate
/// `POST /api/music/delete` for the source. Worst-case interruption leaves the
/// file in BOTH locations (a harmless duplicate), never in neither.
///
/// Both subpaths are validated via [`validate_music_subpath`]. The source bytes
/// are read from the read-only media mount using the traversal-safe
/// canonicalize-under-root pattern (same jail as `GET /api/media/content`).
/// The destination is checked for prior existence (409 if present — no silent
/// clobber).
pub(crate) async fn move_music(
    State(state): State<AppState>,
    Json(req): Json<MoveRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let from = validate_music_subpath(&req.from)?;
    let to = validate_music_subpath(&req.to)?;

    // exFAT is case-insensitive, so a case-only rename targets the same on-disk
    // file; reject it (and exact matches) up front.
    if from.eq_ignore_ascii_case(&to) {
        return Err(ApiError::status(
            StatusCode::BAD_REQUEST,
            "invalid_move",
            "from and to must be different paths",
        ));
    }

    // Extension check on the destination filename (last component of `to`).
    let to_name = to.rsplit('/').next().unwrap_or(to.as_str());
    check_extension(to_name, MUSIC_EXTENSIONS)?;

    // Canonicalize the read-only media root (returns NotFound if not mounted).
    let Ok(root) = tokio::fs::canonicalize(state.media.media_ro_root()).await else {
        return Err(ApiError::NotFound);
    };

    // Resolve the source path: canonicalize and assert it remains inside root.
    let src_rel = format!("{MUSIC_DIR}/{from}");
    let src_candidate = root.join(&src_rel);
    let src_canonical = match tokio::fs::canonicalize(&src_candidate).await {
        Ok(p) if p.starts_with(&root) => p,
        _ => return Err(ApiError::NotFound),
    };

    // Stat BEFORE reading (reject directories and surface missing files).
    let meta = tokio::fs::metadata(&src_canonical)
        .await
        .map_err(|_| ApiError::NotFound)?;
    if !meta.is_file() {
        return Err(ApiError::NotFound);
    }
    if meta.len() > MUSIC_MAX_BYTES as u64 {
        return Err(ApiError::status(
            StatusCode::UNPROCESSABLE_ENTITY,
            "file_too_large",
            format!("source file exceeds {MUSIC_MAX_BYTES} bytes"),
        ));
    }

    let bytes = tokio::fs::read(&src_canonical)
        .await
        .map_err(|_| ApiError::NotFound)?;

    // Overwrite guard: refuse if the destination already exists on the mount.
    let dest_rel = format!("{MUSIC_DIR}/{to}");
    let dest_candidate = root.join(&dest_rel);
    if tokio::fs::metadata(&dest_candidate).await.is_ok() {
        return Err(ApiError::status(
            StatusCode::CONFLICT,
            "already_exists",
            "destination already exists; use a different path to avoid overwriting",
        ));
    }

    // Enqueue the destination install (copy) ONLY. The SPA deletes the source
    // after it confirms the destination has landed in the catalog.
    crate::route::run_install(state, "music_install", PARTITION_MEDIA, dest_rel, bytes).await
}

/// `POST /api/music/delete` — bulk-remove arbitrary-depth music files in ONE
/// `gadgetd` handoff.
///
/// Body: `{ "paths": ["<subpath>", …] }`. Each subpath is relative under
/// `Music/` — do NOT include the `Music/` prefix (the handler prepends it).
/// Including a `Music/` prefix would produce a `Music/Music/…` double-prefix
/// bug and is rejected with 400 `invalid_path`. Capped at [`MAX_BULK_DELETE`]
/// entries; over-cap → `422`. Duplicate paths are de-duplicated.
/// [`run_remove_many`] chunks internally (≤16 per enqueue).
pub(crate) async fn delete_music_paths(
    State(state): State<AppState>,
    Json(req): Json<DeletePathsRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    if req.paths.is_empty() {
        return Err(ApiError::status(
            StatusCode::BAD_REQUEST,
            "empty_batch",
            "expected at least one path",
        ));
    }
    if req.paths.len() > MAX_BULK_DELETE {
        return Err(ApiError::status(
            StatusCode::UNPROCESSABLE_ENTITY,
            "batch_too_large",
            format!("at most {MAX_BULK_DELETE} paths may be deleted at once"),
        ));
    }
    let mut rel_paths: Vec<String> = Vec::with_capacity(req.paths.len());
    for raw in &req.paths {
        let validated = validate_music_subpath(raw)?;
        let rel_path = format!("{MUSIC_DIR}/{validated}");
        if !rel_paths.contains(&rel_path) {
            rel_paths.push(rel_path);
        }
    }
    crate::route::run_remove_many(state, "music_remove", PARTITION_MEDIA, rel_paths).await
}
