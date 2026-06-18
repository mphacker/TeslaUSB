//! `GET /api/chime-scheduler/library/{filename}/audio`,
//! `GET /api/chime-scheduler/library/{filename}/download`, and
//! `POST /api/chime-scheduler/library/{filename}/activate` — the file-backed
//! companions to the media-backed library CRUD aliases in this module.
//!
//! The library lives in the MEDIA partition's root-level `Chimes/` folder;
//! these handlers only *read* that catalog and queue writes through the same
//! gadgetd handoff path used for direct chime installs. They give the SPA's
//! v1-parity chime library its per-row actions:
//!
//!  * **audio** — serve the WAV bytes inline so an `<audio>` element can preview
//!    a library chime.
//!  * **download** — the same bytes with an `attachment` disposition.
//!  * **activate** — promote a library chime to the car's single-slot lock chime
//!    by reading its bytes and routing them through the *same* frictionless
//!    `gadgetd` install primitive ([`crate::route::run_install`]) the direct
//!    `POST /api/chimes` upload uses (MEDIA partition, fixed `LockChime.wav`).
//!    The change is queued and applied at the next safe window, exactly like a
//!    direct chime upload — never a synchronous mid-recording eject.
//!
//! Every handler resolves the client-supplied `{filename}` through
//! [`resolve_library_file`], which fail-closes (`404`) on anything that is not a
//! plain, single-segment `*.wav` name that canonicalizes to a regular file
//! *inside* the canonical library directory — so the path can never be steered
//! out of the library (traversal, absolute paths, symlink escapes).

use axum::Json;
use axum::Router;
use axum::body::Body;
use axum::extract::{Multipart, Path, Query, State};
use axum::http::StatusCode;
use axum::http::header::{CONTENT_DISPOSITION, CONTENT_LENGTH, CONTENT_TYPE};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use serde_json::Value;

use crate::AppState;
use crate::dto::MediaListDto;
use crate::error::ApiError;
use crate::media_upload::{
    BulkDeleteRequest, check_extension, plan_bulk_delete, read_file_upload, sanitise_filename,
};

/// Body of `POST /api/chimes/library/rename`.
#[derive(serde::Deserialize)]
pub(crate) struct RenameRequest {
    /// Current library filename.
    from: String,
    /// New library filename.
    to: String,
}

/// Query for the library DELETE: whether to cascade-scrub scheduler references
/// (default true). The rename source-cleanup passes `cascade=false` so it never
/// scrubs a reference the user may have just re-created on the old name.
#[derive(serde::Deserialize)]
pub(crate) struct RemoveParams {
    #[serde(default = "default_cascade")]
    cascade: bool,
}

fn default_cascade() -> bool {
    true
}

/// The MEDIA partition wire index (`gadgetd` `Partition::P2`) — matches
/// [`crate::chimes`].
const PARTITION_MEDIA: u8 = 2;

/// The fixed destination of the active lock chime at the MEDIA root. Never
/// derived from the library filename (the library file is a *source*, the active
/// slot is always this fixed name).
const CHIME_REL_PATH: &str = "LockChime.wav";

/// The library folder on the MEDIA (p2) partition, visible on the USB drive.
const CHIMES_DIR: &str = "Chimes";

/// Maximum accepted library-chime size (1 MiB).
const CHIME_LIBRARY_MAX_BYTES: usize = 1024 * 1024;

/// `Content-Type` for a served WAV.
const WAV_MIME: &str = "audio/wav";

/// Upper bound on a library filename, mirroring `schedulerd`'s own rule.
const MAX_FILENAME_LEN: usize = 100;

/// Upper bound on a library chime, matching the chime-upload cap. A library file
/// should never exceed this (`webd` validates on upload), so it is a defence
/// against reading a stray oversized file fully into memory on serve/activate.
const MAX_CHIME_BYTES: u64 = 1024 * 1024;

/// The file-backed library sub-routes, merged under `/api` by [`crate::route`].
/// The legacy `/api/chime-scheduler/library/*` aliases resolve to the same
/// handlers so the existing SPA keeps working with the media-backed catalog.
pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route(
            "/chimes/library",
            get(list_library)
                .post(upload_library)
                .layer(axum::extract::DefaultBodyLimit::max(8 * 1024 * 1024)),
        )
        .route("/chimes/library/rename", post(rename_library))
        .route("/chimes/library/bulk-delete", post(bulk_delete_library))
        .route("/chimes/library/{name}", delete(remove_library))
        .route("/chimes/library/{name}/audio", get(serve_audio))
        .route("/chimes/library/{name}/download", get(serve_download))
        .route("/chimes/library/{name}/activate", post(activate))
        .route(
            "/chime-scheduler/library",
            get(list_library)
                .post(upload_library)
                .layer(axum::extract::DefaultBodyLimit::max(8 * 1024 * 1024)),
        )
        .route("/chime-scheduler/library/rename", post(rename_library))
        .route(
            "/chime-scheduler/library/bulk-delete",
            post(bulk_delete_library),
        )
        .route("/chime-scheduler/library/{filename}", delete(remove_library))
        .route(
            "/chime-scheduler/library/{filename}/audio",
            get(serve_audio),
        )
        .route(
            "/chime-scheduler/library/{filename}/download",
            get(serve_download),
        )
        .route(
            "/chime-scheduler/library/{filename}/activate",
            post(activate),
        )
}

/// `GET /api/chimes/library`: list the media-backed library folder.
pub(crate) async fn list_library(
    State(state): State<AppState>,
) -> Result<Json<MediaListDto>, ApiError> {
    let items = crate::route::read(state.catalog, crate::query::list_chime_library).await?;
    Ok(Json(MediaListDto { items }))
}

/// `POST /api/chimes/library`: upload a WAV into the media-backed `Chimes/` folder.
pub(crate) async fn upload_library(
    State(state): State<AppState>,
    multipart: Multipart,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let (raw_name, bytes) = read_file_upload(multipart, "file", CHIME_LIBRARY_MAX_BYTES).await?;
    let name = sanitise_filename(&raw_name)?;
    check_extension(&name, &["wav"])?;
    crate::chimes::validate_lock_chime_wav(&bytes)
        .map_err(|msg| ApiError::status(StatusCode::UNPROCESSABLE_ENTITY, "invalid_wav", msg))?;

    let rel_path = format!("{CHIMES_DIR}/{name}");
    crate::route::run_install(
        state,
        "chime_library_install",
        PARTITION_MEDIA,
        rel_path,
        bytes,
    )
    .await
}

/// `POST /api/chimes/library/rename`: rename a library chime and cascade the new
/// name through the scheduler state. Mirrors `move_music`: webd enqueues the
/// destination install (a copy) only; the SPA deletes the source (with
/// `cascade=false`) once it confirms the copy landed in the catalog.
pub(crate) async fn rename_library(
    State(state): State<AppState>,
    Json(req): Json<RenameRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    if !is_safe_chime_filename(&req.from) {
        return Err(ApiError::NotFound);
    }
    if !is_safe_chime_filename(&req.to) {
        return Err(ApiError::status(
            StatusCode::BAD_REQUEST,
            "invalid_name",
            "destination name is not a valid chime filename",
        ));
    }
    if req.from.eq_ignore_ascii_case(&req.to) {
        return Err(ApiError::status(
            StatusCode::BAD_REQUEST,
            "same_name",
            "source and destination names are the same",
        ));
    }
    let src = resolve_library_file(&state, &req.from)?;
    let bytes = read_capped(&src).await?;

    let root = std::fs::canonicalize(state.media.media_ro_root()).map_err(|_| ApiError::NotFound)?;
    let dest_candidate = root.join(CHIMES_DIR).join(&req.to);
    if tokio::fs::metadata(&dest_candidate).await.is_ok() {
        return Err(ApiError::status(
            StatusCode::CONFLICT,
            "destination_exists",
            "a chime with that name already exists",
        ));
    }

    let rel_path = format!("{CHIMES_DIR}/{}", req.to);
    let result = crate::route::run_install(
        state.clone(),
        "chime_library_rename",
        PARTITION_MEDIA,
        rel_path,
        bytes,
    )
    .await?;
    crate::chime_scheduler::rename_chime_references(&state, &req.from, &req.to).await?;
    Ok(result)
}

/// `DELETE /api/chimes/library/{name}`: remove a media-backed library chime.
pub(crate) async fn remove_library(
    State(state): State<AppState>,
    Path(name): Path<String>,
    Query(params): Query<RemoveParams>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    if !is_safe_chime_filename(&name) {
        return Err(ApiError::NotFound);
    }
    check_extension(&name, &["wav"])?;
    let rel_path = format!("{CHIMES_DIR}/{name}");
    let result =
        crate::route::run_remove(state.clone(), "chime_library_remove", PARTITION_MEDIA, rel_path)
            .await?;
    if params.cascade {
        crate::chime_scheduler::remove_chime_references(&state, std::slice::from_ref(&name)).await?;
    }
    Ok(result)
}

/// `POST /api/chime-scheduler/library/bulk-delete` — remove several library
/// chimes in ONE `gadgetd` handoff (one eject/remount for the batch, not one
/// per file). Body: `{ "names": ["Horn.wav", …] }`. Each name is a bare file
/// name; the handler rebuilds `Chimes/<name>`, so a client can never address a
/// file outside the library folder. Mirrors the toybox media bulk-delete
/// endpoints; `run_remove_many` chunks internally (≤16 paths per enqueue).
pub(crate) async fn bulk_delete_library(
    State(state): State<AppState>,
    Json(req): Json<BulkDeleteRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let rel_paths = plan_bulk_delete(CHIMES_DIR, &req.names)?;
    // Cascade with the SAME sanitised basenames the file op uses, not the raw
    // request names: `plan_bulk_delete` collapses path-y input to its last
    // component (e.g. `../Horn.wav` -> `Chimes/Horn.wav`), so cascading on
    // `req.names` would scrub the wrong (or no) reference and could orphan the
    // real one when schedulerd rejects the path-y name.
    let prefix = format!("{CHIMES_DIR}/");
    let names: Vec<String> = rel_paths
        .iter()
        .map(|p| p.strip_prefix(&prefix).unwrap_or(p).to_owned())
        .collect();
    let result = crate::route::run_remove_many(
        state.clone(),
        "chime_library_remove",
        PARTITION_MEDIA,
        rel_paths,
    )
    .await?;
    crate::chime_scheduler::remove_chime_references(&state, &names).await?;
    Ok(result)
}

/// `GET …/library/{filename}/audio`: stream the library chime inline (the
/// per-row `<audio>` preview).
pub(crate) async fn serve_audio(
    State(state): State<AppState>,
    Path(filename): Path<String>,
) -> Result<Response, ApiError> {
    serve_bytes(&state, &filename, false).await
}

/// `GET …/library/{filename}/download`: the same bytes with an `attachment`
/// disposition so the browser saves rather than plays.
pub(crate) async fn serve_download(
    State(state): State<AppState>,
    Path(filename): Path<String>,
) -> Result<Response, ApiError> {
    serve_bytes(&state, &filename, true).await
}

/// Install the named library chime as the car's active `LockChime.wav` via the
/// frictionless `gadgetd` queue. Returns the same `202 {state:"queued"}` /
/// `200 {state:"done"}` shape as a direct chime upload; the change applies at
/// the next safe window.
pub(crate) async fn install_library_chime_as_active(
    state: AppState,
    kind: &'static str,
    name: &str,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let path = resolve_library_file(&state, name)?;
    let bytes = read_capped(&path).await?;
    crate::chimes::validate_lock_chime_wav(&bytes)
        .map_err(|msg| ApiError::status(StatusCode::UNPROCESSABLE_ENTITY, "invalid_wav", msg))?;
    crate::route::run_install(state, kind, PARTITION_MEDIA, CHIME_REL_PATH.to_owned(), bytes).await
}

/// `POST …/library/{filename}/activate`: install the named library chime as the
/// car's active `LockChime.wav` via the frictionless `gadgetd` queue.
pub(crate) async fn activate(
    State(state): State<AppState>,
    Path(filename): Path<String>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    install_library_chime_as_active(state, "chime_set_active", &filename).await
}

/// Read a validated library file fully (chimes are ≤1 MiB) and build a `200`
/// response with the WAV `Content-Type`, optionally as an `attachment`.
async fn serve_bytes(
    state: &AppState,
    filename: &str,
    attachment: bool,
) -> Result<Response, ApiError> {
    let path = resolve_library_file(state, filename)?;
    let bytes = read_capped(&path).await?;
    let len = bytes.len();

    let mut response = (StatusCode::OK, Body::from(bytes)).into_response();
    let headers = response.headers_mut();
    set_header(headers, CONTENT_TYPE, WAV_MIME);
    set_header(headers, CONTENT_LENGTH, &len.to_string());
    // The same URL can serve different bytes after a same-name re-upload, so the
    // browser must revalidate rather than replay a stale preview/download.
    set_header(headers, axum::http::header::CACHE_CONTROL, "no-cache");
    if attachment {
        set_header(
            headers,
            CONTENT_DISPOSITION,
            &format!("attachment; filename=\"{filename}\""),
        );
    }
    Ok(response)
}

/// Validate `filename` and resolve it to an absolute path that is provably a
/// regular file *inside* the canonical library directory, or `404`.
///
/// Defence in depth: the syntactic [`is_safe_chime_filename`] guard rejects the
/// obvious traversal/separator shapes BEFORE touching the filesystem, then
/// `canonicalize` + a `starts_with` jail proves the resolved target (after any
/// symlink resolution) still lives under the library root. Any rejection is a
/// flat `404` so the endpoint never reveals why a name was refused or whether a
/// path outside the jail exists.
fn resolve_library_file(state: &AppState, filename: &str) -> Result<std::path::PathBuf, ApiError> {
    if !is_safe_chime_filename(filename) {
        return Err(ApiError::NotFound);
    }

    let root =
        std::fs::canonicalize(state.media.media_ro_root()).map_err(|_| ApiError::NotFound)?;
    let chimes_root = root.join(CHIMES_DIR);
    let canonical_root = std::fs::canonicalize(&chimes_root).map_err(|_| ApiError::NotFound)?;
    let candidate = canonical_root.join(filename);
    let real = std::fs::canonicalize(&candidate).map_err(|_| ApiError::NotFound)?;
    if !real.starts_with(&canonical_root) {
        return Err(ApiError::NotFound);
    }
    let meta = std::fs::metadata(&real).map_err(|_| ApiError::NotFound)?;
    if !meta.is_file() {
        return Err(ApiError::NotFound);
    }
    Ok(real)
}

/// Open a resolved library file and read at most `MAX_CHIME_BYTES + 1` bytes,
/// bounding memory regardless of the file's size on disk. Anything larger than
/// the 1 MiB cap is rejected as a flat `404` — uniform with the path jail, so
/// the endpoint never reveals that a safe-named (but oversized) file exists, and
/// it closes the check-then-read race (a real chime is always ≤1 MiB; schedulerd
/// validates on write, so an oversized file here is anomalous, not legitimate).
async fn read_capped(path: &std::path::Path) -> Result<Vec<u8>, ApiError> {
    use tokio::io::AsyncReadExt;
    let file = tokio::fs::File::open(path)
        .await
        .map_err(|_| ApiError::NotFound)?;
    let mut buf = Vec::new();
    file.take(MAX_CHIME_BYTES + 1)
        .read_to_end(&mut buf)
        .await
        .map_err(|_| ApiError::NotFound)?;
    if buf.len() as u64 > MAX_CHIME_BYTES {
        return Err(ApiError::NotFound);
    }
    Ok(buf)
}

/// A conservative single-segment `*.wav` filename guard, mirroring `schedulerd`'s
/// own library-filename rule: non-empty, ≤100 chars, ASCII with no control
/// bytes, no path separators or `..`, and a `.wav` extension.
fn is_safe_chime_filename(name: &str) -> bool {
    if name.is_empty() || name.len() > MAX_FILENAME_LEN {
        return false;
    }
    // Conservative allowlist: ASCII alphanumerics plus a handful of safe
    // punctuation. In one pass this rejects path separators, control bytes
    // (incl. CR/LF), quotes and semicolons (`Content-Disposition`
    // header-injection vectors), and any non-ASCII byte.
    if !name
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b' ' | b'.' | b'_' | b'-' | b'(' | b')'))
    {
        return false;
    }
    // Belt-and-suspenders: no `..` even though separators are already excluded.
    if name.contains("..") {
        return false;
    }
    let lower = name.to_ascii_lowercase();
    std::path::Path::new(&lower)
        .extension()
        .is_some_and(|ext| ext == "wav")
}

/// Insert an ASCII header value, silently skipping an unencodable value (all
/// values here are ASCII, so this never drops a real header).
fn set_header(headers: &mut axum::http::HeaderMap, name: axum::http::HeaderName, value: &str) {
    if let Ok(value) = axum::http::HeaderValue::from_str(value) {
        headers.insert(name, value);
    }
}

#[cfg(test)]
mod filename_tests {
    use super::is_safe_chime_filename;

    #[test]
    fn accepts_plain_wav_names() {
        assert!(is_safe_chime_filename("Classic.wav"));
        assert!(is_safe_chime_filename("Horn.WAV"));
        assert!(is_safe_chime_filename("my-chime_2.wav"));
    }

    #[test]
    fn rejects_traversal_and_separators() {
        assert!(!is_safe_chime_filename("../secret.wav"));
        assert!(!is_safe_chime_filename("a/b.wav"));
        assert!(!is_safe_chime_filename("a\\b.wav"));
        assert!(!is_safe_chime_filename("..wav")); // contains ".."
    }

    #[test]
    fn rejects_non_wav_and_edge_shapes() {
        assert!(!is_safe_chime_filename(""));
        assert!(!is_safe_chime_filename(".wav")); // no stem
        assert!(!is_safe_chime_filename("chime.mp3"));
        assert!(!is_safe_chime_filename("chime.wav\0"));
        assert!(!is_safe_chime_filename("naïve.wav")); // non-ASCII
        assert!(!is_safe_chime_filename(&format!("{}.wav", "x".repeat(100))));
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::panic, clippy::indexing_slicing)]
mod handler_tests {
    use super::*;
    use axum::Router;
    use axum::body::Body;
    use axum::http::{Method, Request, StatusCode};
    use rusqlite::Connection;
    use std::sync::{Arc, Mutex};
    use tempfile::TempDir;
    use tower::ServiceExt;

    use crate::gadget::{GadgetClient, TransportError};
    use crate::scheduler::SchedulerClient;
    use crate::{Catalog, MediaConfig};

    struct MockGadget {
        calls: Arc<Mutex<Vec<Value>>>,
    }

    impl GadgetClient for MockGadget {
        fn call(&self, request: Value) -> Result<Value, TransportError> {
            self.calls.lock().unwrap().push(request);
            Ok(serde_json::json!({ "job_id": "m-1", "state": "queued" }))
        }
    }

    struct MockScheduler {
        calls: Arc<Mutex<Vec<Value>>>,
    }

    impl SchedulerClient for MockScheduler {
        fn call(&self, request: Value) -> Result<Value, TransportError> {
            self.calls.lock().unwrap().push(request);
            Ok(serde_json::json!({}))
        }
    }

    struct Fixture {
        _dir: TempDir,
        app: Router,
        gadget_calls: Arc<Mutex<Vec<Value>>>,
        scheduler_calls: Arc<Mutex<Vec<Value>>>,
        library_dir: std::path::PathBuf,
    }

    fn fixture() -> Fixture {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("catalog.db");
        {
            let mut conn = Connection::open(&db_path).unwrap();
            conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
            indexd::db::apply_migrations(&mut conn).unwrap();
        }

        let static_dir = dir.path().join("static");
        std::fs::create_dir_all(&static_dir).unwrap();
        std::fs::write(static_dir.join("index.html"), "<!doctype html>shell").unwrap();

        let archive = dir.path().join("archive");
        let cache = dir.path().join("cache");
        std::fs::create_dir_all(&archive).unwrap();
        std::fs::create_dir_all(&cache).unwrap();

        let media_ro = dir.path().join("media-ro");
        let library_dir = media_ro.join(CHIMES_DIR);
        std::fs::create_dir_all(&library_dir).unwrap();

        let catalog = Catalog::open(&db_path).unwrap();
        let media = MediaConfig::new(archive, cache).with_media_ro_root(media_ro);

        let gadget_calls = Arc::new(Mutex::new(Vec::new()));
        let scheduler_calls = Arc::new(Mutex::new(Vec::new()));
        let gadget: Arc<dyn GadgetClient> = Arc::new(MockGadget {
            calls: Arc::clone(&gadget_calls),
        });
        let scheduler: Arc<dyn SchedulerClient> = Arc::new(MockScheduler {
            calls: Arc::clone(&scheduler_calls),
        });
        let app = crate::router_with_clients(
            catalog,
            static_dir,
            media,
            gadget,
            scheduler,
            library_dir.clone(),
        );
        Fixture {
            _dir: dir,
            app,
            gadget_calls,
            scheduler_calls,
            library_dir,
        }
    }

    async fn post_json(app: &Router, uri: &str, body: Value) -> (StatusCode, Value) {
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri(uri)
                    .header(axum::http::header::CONTENT_TYPE, "application/json")
                    .body(Body::from(serde_json::to_vec(&body).unwrap()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = resp.status();
        let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
            .await
            .unwrap();
        let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        (status, value)
    }

    async fn delete_json(app: &Router, uri: &str) -> (StatusCode, Value) {
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method(Method::DELETE)
                    .uri(uri)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = resp.status();
        let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
            .await
            .unwrap();
        let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        (status, value)
    }

    #[tokio::test]
    async fn rename_rejects_same_name_case_insensitive() {
        let fx = fixture();
        let (status, body) = post_json(
            &fx.app,
            "/api/chimes/library/rename",
            serde_json::json!({ "from": "A.wav", "to": "a.wav" }),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["error"]["code"], "same_name");
        assert!(fx.gadget_calls.lock().unwrap().is_empty());
        assert!(fx.scheduler_calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn rename_rejects_invalid_destination_name() {
        let fx = fixture();
        let (status, body) = post_json(
            &fx.app,
            "/api/chimes/library/rename",
            serde_json::json!({ "from": "A.wav", "to": "../bad.wav" }),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["error"]["code"], "invalid_name");
        assert!(fx.gadget_calls.lock().unwrap().is_empty());
        assert!(fx.scheduler_calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn rename_missing_source_is_404() {
        let fx = fixture();
        let (status, _) = post_json(
            &fx.app,
            "/api/chimes/library/rename",
            serde_json::json!({ "from": "Missing.wav", "to": "New.wav" }),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert!(fx.gadget_calls.lock().unwrap().is_empty());
        assert!(fx.scheduler_calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn rename_destination_exists_is_409() {
        let fx = fixture();
        std::fs::write(fx.library_dir.join("Old.wav"), b"old").unwrap();
        std::fs::write(fx.library_dir.join("New.wav"), b"new").unwrap();

        let (status, body) = post_json(
            &fx.app,
            "/api/chimes/library/rename",
            serde_json::json!({ "from": "Old.wav", "to": "New.wav" }),
        )
        .await;
        assert_eq!(status, StatusCode::CONFLICT);
        assert_eq!(body["error"]["code"], "destination_exists");
        assert!(fx.gadget_calls.lock().unwrap().is_empty());
        assert!(fx.scheduler_calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn rename_success_enqueues_install_and_cascades_scheduler() {
        let fx = fixture();
        std::fs::write(fx.library_dir.join("Old.wav"), b"old").unwrap();

        let (status, body) = post_json(
            &fx.app,
            "/api/chimes/library/rename",
            serde_json::json!({ "from": "Old.wav", "to": "New.wav" }),
        )
        .await;
        assert_eq!(status, StatusCode::ACCEPTED);
        assert_eq!(body["state"], "queued");

        let gadget_calls = fx.gadget_calls.lock().unwrap().clone();
        assert_eq!(gadget_calls.len(), 1);
        assert_eq!(gadget_calls[0]["cmd"], "enqueue_mutation");
        assert_eq!(gadget_calls[0]["mutation"]["op"], "install_file");
        assert_eq!(gadget_calls[0]["mutation"]["rel_path"], "Chimes/New.wav");

        let scheduler_calls = fx.scheduler_calls.lock().unwrap().clone();
        assert_eq!(scheduler_calls.len(), 1);
        assert_eq!(scheduler_calls[0]["cmd"], "rename_chime_references");
        assert_eq!(scheduler_calls[0]["from"], "Old.wav");
        assert_eq!(scheduler_calls[0]["to"], "New.wav");
    }

    #[tokio::test]
    async fn remove_library_default_cascade_calls_scheduler() {
        let fx = fixture();
        let (status, body) = delete_json(&fx.app, "/api/chimes/library/Horn.wav").await;
        assert_eq!(status, StatusCode::ACCEPTED);
        assert_eq!(body["state"], "queued");

        let scheduler_calls = fx.scheduler_calls.lock().unwrap().clone();
        assert_eq!(scheduler_calls.len(), 1);
        assert_eq!(scheduler_calls[0]["cmd"], "remove_chime_references");
        assert_eq!(scheduler_calls[0]["filenames"], serde_json::json!(["Horn.wav"]));
    }

    #[tokio::test]
    async fn remove_library_cascade_false_skips_scheduler() {
        let fx = fixture();
        let (status, body) = delete_json(&fx.app, "/api/chimes/library/Horn.wav?cascade=false").await;
        assert_eq!(status, StatusCode::ACCEPTED);
        assert_eq!(body["state"], "queued");
        assert!(fx.scheduler_calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn bulk_delete_library_cascades_scheduler_remove_references() {
        let fx = fixture();
        let (status, body) = post_json(
            &fx.app,
            "/api/chimes/library/bulk-delete",
            serde_json::json!({ "names": ["A.wav", "B.wav"] }),
        )
        .await;
        assert_eq!(status, StatusCode::ACCEPTED);
        assert_eq!(body["state"], "queued");

        let scheduler_calls = fx.scheduler_calls.lock().unwrap().clone();
        assert_eq!(scheduler_calls.len(), 1);
        assert_eq!(scheduler_calls[0]["cmd"], "remove_chime_references");
        assert_eq!(scheduler_calls[0]["filenames"], serde_json::json!(["A.wav", "B.wav"]));
    }

    #[tokio::test]
    async fn bulk_delete_library_cascades_sanitised_basenames() {
        let fx = fixture();
        let (status, _) = post_json(
            &fx.app,
            "/api/chimes/library/bulk-delete",
            serde_json::json!({ "names": ["../Horn.wav"] }),
        )
        .await;
        assert_eq!(status, StatusCode::ACCEPTED);

        // The cascade must use the sanitised basename (matching the file op),
        // never the raw path-y request name, so the real reference is scrubbed.
        let scheduler_calls = fx.scheduler_calls.lock().unwrap().clone();
        assert_eq!(scheduler_calls.len(), 1);
        assert_eq!(scheduler_calls[0]["filenames"], serde_json::json!(["Horn.wav"]));
    }
}
