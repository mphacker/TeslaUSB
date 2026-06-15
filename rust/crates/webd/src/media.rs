//! Archive-clip video streaming + export handlers (Task 5.1b).
//!
//! Three read-only endpoints that resolve a clip angle to a concrete mp4 on the
//! Pi-side ext4 archive and serve its bytes:
//!
//! * `GET|HEAD /api/clips/{id}/stream?camera=` — full HTTP range-request
//!   streaming of one angle (the `<video>` element source).
//! * `GET|HEAD /api/clips/{id}/angles/{camera}/download` — single-file mp4
//!   download (the "download to view" parity primitive / codec fallback link).
//! * `GET|HEAD /api/clips/{id}/export` — a streamed `ZIP_STORED` of the clip's
//!   archive angles.
//!
//! ## Security
//!
//! `file_ref` is treated as hostile. Every resolved path is jailed under
//! [`MediaConfig::archive_root`]: dangerous components (absolute, `..`) are
//! rejected syntactically, then the path is canonicalised and verified to sit
//! inside the (canonical) archive root with [`std::path::Path::starts_with`]
//! (component-aware, so a sibling like `archive-evil` cannot pass). Anything
//! that escapes — or any non-`archive` `view_kind` — answers `404` (never
//! `403`) so existence is not leaked. `file_ref` is resolved server-side only
//! and never returned in a DTO.
//!
//! Trust assumption: the archive tree under the root is written only by the
//! `TeslaUSB` ingest services (root/`teslausb`-owned), so there is no
//! check-to-open TOCTOU adversary; the jail defends against a hostile
//! `file_ref` value, not a concurrently-malicious filesystem.
//!
//! ## Streaming guarantee
//!
//! Bodies are produced by [`tokio_util::io::ReaderStream`] over a seeked,
//! length-capped [`tokio::fs::File`] — bytes are read in bounded chunks and the
//! whole file is **never** buffered in memory, regardless of clip size. The zip
//! export is built into an anonymous on-disk tempfile (the zip writer needs
//! `Seek`) and then streamed the same way; it is never held wholly in memory.
//!
//! ## Deferred (intentionally not built here)
//!
//! * **Playback lease / heartbeat** (webd.md §2.3): streaming would hold a TTL
//!   lease against `retentiond`'s governor so a file can't be evicted mid-read.
//!   `retentiond` and the D3 lease RPC do not exist yet, so there is nothing to
//!   lease against and no evictor to race. The acquire/heartbeat/release would
//!   hook in at the marked seam in [`stream`] (around the file open) and wrap
//!   the returned body so the lease is released on drop. See the report note.
//! * **`ro_usb` live-clip streaming**: raw exFAT byte-range reads of a live
//!   `disk.img` are a separate hardware-sensitive seam (scannerd's raw reader).
//!   Non-`archive` angles return `404` here.

use std::io::{Seek, SeekFrom};
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

use axum::body::Body;
use axum::extract::{Path as AxumPath, Query, State};
use axum::http::header::{
    ACCEPT_RANGES, CONTENT_DISPOSITION, CONTENT_LENGTH, CONTENT_RANGE, CONTENT_TYPE, RANGE,
    RETRY_AFTER, X_CONTENT_TYPE_OPTIONS,
};
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use serde::Deserialize;
use tokio::io::{AsyncReadExt, AsyncSeekExt};
use tokio_util::io::ReaderStream;

use crate::AppState;
use crate::error::ApiError;
use crate::range::{ParsedRange, parse_byte_range};

/// The MIME type all angles are served as. Tesla footage is H.264 in an mp4
/// container (SPEC.md §7), played natively by every target browser.
const VIDEO_MIME: &str = "video/mp4";

/// Read/emit chunk size for streamed bodies (bounded memory per connection).
const STREAM_CHUNK: usize = 256 * 1024;

/// The `view_kind` value whose `file_ref` resolves to a playable Pi-side path.
const VIEW_ARCHIVE: &str = "archive";

/// Runtime media configuration shared by the streaming/export handlers.
#[derive(Clone, Debug)]
pub struct MediaConfig {
    /// Canonical archive root; every resolved `file_ref` must live inside it.
    archive_root: Arc<PathBuf>,
    /// Directory the zip export writes its (auto-unlinked) tempfile into.
    cache_dir: Arc<PathBuf>,
    /// Read-only media mount root used for direct file-byte serving.
    media_ro_root: Arc<PathBuf>,
}

impl MediaConfig {
    /// Build a [`MediaConfig`] from the archive root and a zip-export cache dir.
    ///
    /// `archive_root` is canonicalised eagerly so the per-request jail compares
    /// like-for-like (both sides canonical). If it cannot be canonicalised yet
    /// (e.g. the mount is not present at construction) the path is kept as-is;
    /// the per-request check still canonicalises the candidate, so an
    /// unresolvable root simply means every stream attempt `404`s until the
    /// mount appears.
    #[must_use]
    pub fn new(archive_root: PathBuf, cache_dir: PathBuf) -> Self {
        let archive_root = std::fs::canonicalize(&archive_root).unwrap_or(archive_root);
        let media_ro_root = std::env::var_os("WEBD_MEDIA_RO_ROOT")
            .map_or_else(|| PathBuf::from("/run/teslausb/media-ro"), PathBuf::from);
        Self {
            archive_root: Arc::new(archive_root),
            cache_dir: Arc::new(cache_dir),
            media_ro_root: Arc::new(media_ro_root),
        }
    }

    /// The canonical archive root — the Pi-side data filesystem the
    /// device-status endpoints probe (`statvfs`, writability, mount facts).
    #[must_use]
    pub(crate) fn archive_root_path(&self) -> PathBuf {
        self.archive_root.as_ref().clone()
    }

    /// The transient staging directory for media uploads (a subdir of the cache
    /// dir). `webd` writes an uploaded asset here, fsyncs it, and passes its
    /// absolute path to `gadgetd` as the install `source_path`; the staged file
    /// is unlinked once the handoff returns. Both daemons run as root, so
    /// `gadgetd` can read the `0600` staged file from this root-owned area.
    #[must_use]
    pub(crate) fn staging_dir(&self) -> PathBuf {
        self.cache_dir.join("media-staging")
    }

    /// Inject a specific read-only media mount root for tests.
    #[must_use]
    #[allow(dead_code)]
    pub(crate) fn with_media_ro_root(mut self, root: PathBuf) -> Self {
        self.media_ro_root = Arc::new(root);
        self
    }

    /// The read-only media mount root that backs byte-serving endpoints.
    #[must_use]
    pub(crate) fn media_ro_root(&self) -> &Path {
        self.media_ro_root.as_ref().as_path()
    }
}

/// Query string of `GET /api/clips/{id}/stream`.
#[derive(Deserialize)]
pub(crate) struct StreamQuery {
    /// Which camera angle to stream; defaults to the front (HUD) camera.
    camera: Option<String>,
}

/// Query string for `GET|HEAD /api/media/content?path=`.
#[derive(Deserialize)]
pub(crate) struct MediaContentQuery {
    /// Relative path under the read-only `media.img` / `lun.1` mount.
    path: String,
}

/// `GET|HEAD /api/clips/{id}/stream?camera=` — range-request mp4 streaming.
pub(crate) async fn stream(
    State(state): State<AppState>,
    method: Method,
    AxumPath(id): AxumPath<i64>,
    Query(q): Query<StreamQuery>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    let camera = q
        .camera
        .filter(|c| !c.is_empty())
        .unwrap_or_else(|| "front".to_owned());

    // --- DEFERRED SEAM (webd.md §2.3): acquire the retentiond playback lease
    // here once D3 + retentiond exist, and attach a heartbeat/release guard to
    // the returned body so the file cannot be evicted mid-read. ---
    let (file, size) = open_archive_angle(&state, id, &camera).await?;
    let head = method == Method::HEAD;

    let response = match decide_range(&headers, size) {
        RangeDecision::Full => {
            let body = body_for(head, file, 0, size).await?;
            build_media_response(StatusCode::OK, VIDEO_MIME, size, None, body)
        }
        RangeDecision::Satisfiable { start, end } => {
            let len = end - start + 1;
            let body = body_for(head, file, start, len).await?;
            let content_range = format!("bytes {start}-{end}/{size}");
            build_media_response(
                StatusCode::PARTIAL_CONTENT,
                VIDEO_MIME,
                len,
                Some(content_range),
                body,
            )
        }
        RangeDecision::Unsatisfiable => range_not_satisfiable(size, head),
    };
    Ok(response)
}

/// `GET|HEAD /api/media/content?path=` — range-stream a media file from the
/// read-only `media.img` / `lun.1` mount.
pub(crate) async fn content(
    State(state): State<AppState>,
    method: Method,
    Query(q): Query<MediaContentQuery>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    if q.path.trim().is_empty() {
        return Err(ApiError::NotFound);
    }

    let Ok(root) = tokio::fs::canonicalize(state.media.media_ro_root()).await else {
        return Ok(media_unavailable());
    };

    let path = match resolve_archive_path(&root, &q.path).await {
        Resolved::Ok(path) => path,
        Resolved::Missing | Resolved::Escaped => return Err(ApiError::NotFound),
    };

    // Stat the canonical path BEFORE opening so a non-regular file
    // (directory, FIFO, device) is rejected without ever being opened — an
    // `open` on a FIFO can block, and a device-node open can have side effects.
    // exFAT (the `media.img` filesystem) cannot hold such nodes, so this is
    // defence-in-depth, but it keeps the handler correct by construction.
    let meta = tokio::fs::metadata(&path)
        .await
        .map_err(|_| ApiError::NotFound)?;
    if !meta.is_file() {
        return Err(ApiError::NotFound);
    }
    let size = meta.len();

    let file = tokio::fs::File::open(&path)
        .await
        .map_err(|_| ApiError::NotFound)?;

    let head = method == Method::HEAD;
    let mime = content_type_for(&path);

    let mut response = match decide_range(&headers, size) {
        RangeDecision::Full => {
            let body = body_for(head, file, 0, size).await?;
            build_media_response(StatusCode::OK, mime, size, None, body)
        }
        RangeDecision::Satisfiable { start, end } => {
            let len = end - start + 1;
            let body = body_for(head, file, start, len).await?;
            let content_range = format!("bytes {start}-{end}/{size}");
            build_media_response(
                StatusCode::PARTIAL_CONTENT,
                mime,
                len,
                Some(content_range),
                body,
            )
        }
        RangeDecision::Unsatisfiable => range_not_satisfiable(size, head),
    };

    // These are user-uploaded bytes replayed to a browser; forbid MIME sniffing
    // so a mislabelled upload cannot be reinterpreted as active content.
    insert_header(response.headers_mut(), X_CONTENT_TYPE_OPTIONS, "nosniff");
    Ok(response)
}

/// Path params of `GET /api/clips/{id}/angles/{camera}/download`.
type DownloadPath = (i64, String);

/// `GET|HEAD /api/clips/{id}/angles/{camera}/download` — single-file mp4
/// download with an `attachment` disposition (the codec-fallback link).
pub(crate) async fn download(
    State(state): State<AppState>,
    method: Method,
    AxumPath((id, camera)): AxumPath<DownloadPath>,
) -> Result<Response, ApiError> {
    let (file, size) = open_archive_angle(&state, id, &camera).await?;
    let head = method == Method::HEAD;
    let body = body_for(head, file, 0, size).await?;
    let filename = format!("clip-{id}-{}.mp4", sanitize_token(&camera));

    let mut response = build_media_response(StatusCode::OK, VIDEO_MIME, size, None, body);
    insert_attachment(response.headers_mut(), &filename);
    Ok(response)
}

/// `GET|HEAD /api/clips/{id}/export` — streamed `ZIP_STORED` of the clip's
/// archive angles.
pub(crate) async fn export(
    State(state): State<AppState>,
    method: Method,
    AxumPath(id): AxumPath<i64>,
) -> Result<Response, ApiError> {
    let catalog = state.catalog.clone();
    let angles = crate::route::read(catalog, move |conn| {
        crate::query::list_archive_angles(conn, id)
    })
    .await?;
    if angles.is_empty() {
        return Err(ApiError::NotFound);
    }

    // Resolve every angle's jailed path up front (cheap, and identical for HEAD
    // and GET so a HEAD never claims an export that GET would 404). A path that
    // escapes the jail fails the whole export (an attack); a merely missing
    // file is skipped.
    let mut entries: Vec<(String, PathBuf)> = Vec::with_capacity(angles.len());
    for (camera, file_ref) in angles {
        match resolve_archive_path(state.media.archive_root.as_path(), &file_ref).await {
            Resolved::Ok(path) => {
                entries.push((format!("{}.mp4", sanitize_token(&camera)), path));
            }
            Resolved::Missing => {}
            Resolved::Escaped => return Err(ApiError::NotFound),
        }
    }
    if entries.is_empty() {
        return Err(ApiError::NotFound);
    }

    let filename = format!("clip-{id}.zip");
    if method == Method::HEAD {
        // Never build the zip for a HEAD probe — just describe the response.
        let mut response = (StatusCode::OK, Body::empty()).into_response();
        let h = response.headers_mut();
        insert_header(h, CONTENT_TYPE, "application/zip");
        insert_attachment(h, &filename);
        return Ok(response);
    }

    // Bound concurrent zip builds so a burst cannot exhaust the blocking pool
    // or the cache filesystem.
    let permit = state
        .export_sem
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| ApiError::Internal)?;
    let cache_dir = state.media.cache_dir.as_ref().clone();
    let std_file = tokio::task::spawn_blocking(move || build_zip(&cache_dir, &entries))
        .await
        .map_err(|_| ApiError::Internal)?
        .map_err(|_| ApiError::Internal)?;
    drop(permit);

    let async_file = tokio::fs::File::from_std(std_file);
    let body = Body::from_stream(ReaderStream::with_capacity(async_file, STREAM_CHUNK));
    let mut response = (StatusCode::OK, body).into_response();
    let h = response.headers_mut();
    insert_header(h, CONTENT_TYPE, "application/zip");
    insert_attachment(h, &filename);
    Ok(response)
}

/// Resolve the `(clip_id, camera)` archive angle and open its jailed file,
/// returning the open handle and the file's real size. Maps every miss
/// (no angle / non-archive / outside jail / not a file) to `404`.
async fn open_archive_angle(
    state: &AppState,
    clip_id: i64,
    camera: &str,
) -> Result<(tokio::fs::File, u64), ApiError> {
    let catalog = state.catalog.clone();
    let camera_owned = camera.to_owned();
    let source = crate::route::read(catalog, move |conn| {
        crate::query::angle_source(conn, clip_id, &camera_owned)
    })
    .await?;

    let Some((file_ref, view_kind)) = source else {
        return Err(ApiError::NotFound);
    };
    if view_kind != VIEW_ARCHIVE {
        return Err(ApiError::NotFound);
    }

    let path = match resolve_archive_path(state.media.archive_root.as_path(), &file_ref).await {
        Resolved::Ok(path) => path,
        Resolved::Missing | Resolved::Escaped => return Err(ApiError::NotFound),
    };
    let file = tokio::fs::File::open(&path)
        .await
        .map_err(|_| ApiError::NotFound)?;
    // Trust the real file, not the (possibly stale) angles.size_bytes column.
    let meta = file.metadata().await.map_err(|_| ApiError::Internal)?;
    if !meta.is_file() {
        return Err(ApiError::NotFound);
    }
    Ok((file, meta.len()))
}

/// Outcome of jailing a `file_ref` under the archive root.
enum Resolved {
    /// A canonical path safely inside the archive root.
    Ok(PathBuf),
    /// The path could not be canonicalised (treated as a missing file).
    Missing,
    /// The path canonicalised to a location outside the jail (an attack).
    Escaped,
}

/// Jail `file_ref` under `archive_root`: reject dangerous components, then
/// canonicalise and confirm containment with component-aware `starts_with`.
async fn resolve_archive_path(archive_root: &Path, file_ref: &str) -> Resolved {
    if file_ref.is_empty() {
        return Resolved::Escaped;
    }
    let rel = Path::new(file_ref);
    // Reject absolute paths and any `..`/root/prefix component up front so a
    // join can never escape the root before canonicalisation even runs.
    for component in rel.components() {
        match component {
            Component::Normal(_) | Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                return Resolved::Escaped;
            }
        }
    }
    let candidate = archive_root.join(rel);
    let Ok(canonical) = tokio::fs::canonicalize(&candidate).await else {
        return Resolved::Missing;
    };
    if canonical.starts_with(archive_root) {
        Resolved::Ok(canonical)
    } else {
        Resolved::Escaped
    }
}

/// A parsed `Range` decision over a known size.
enum RangeDecision {
    /// No (single, well-formed) `Range` header — serve the full body.
    Full,
    /// A satisfiable inclusive byte range.
    Satisfiable {
        /// First byte (inclusive).
        start: u64,
        /// Last byte (inclusive).
        end: u64,
    },
    /// A present-but-unsatisfiable range — answer `416`.
    Unsatisfiable,
}

/// Interpret the request's `Range` header(s). Multiple `Range` headers (which a
/// client could use to smuggle a multi-range past a single-value check) are
/// rejected as unsatisfiable.
fn decide_range(headers: &HeaderMap, size: u64) -> RangeDecision {
    let mut values = headers.get_all(RANGE).iter();
    let Some(first) = values.next() else {
        return RangeDecision::Full;
    };
    if values.next().is_some() {
        return RangeDecision::Unsatisfiable;
    }
    let Ok(value) = first.to_str() else {
        return RangeDecision::Unsatisfiable;
    };
    match parse_byte_range(value, size) {
        ParsedRange::Satisfiable { start, end } => RangeDecision::Satisfiable { start, end },
        ParsedRange::Unsatisfiable => RangeDecision::Unsatisfiable,
    }
}

/// Build the streamed body for a GET, or an empty body for a HEAD. The file is
/// seeked to `start` and capped to `len` bytes so memory stays bounded.
async fn body_for(
    head: bool,
    mut file: tokio::fs::File,
    start: u64,
    len: u64,
) -> Result<Body, ApiError> {
    if head {
        return Ok(Body::empty());
    }
    if start > 0 {
        file.seek(SeekFrom::Start(start))
            .await
            .map_err(|_| ApiError::Internal)?;
    }
    let stream = ReaderStream::with_capacity(file.take(len), STREAM_CHUNK);
    Ok(Body::from_stream(stream))
}

/// Map a media file extension to its HTTP content type.
pub(crate) fn content_type_for(path: &Path) -> &'static str {
    let ext = path
        .extension()
        .and_then(|ext| ext.to_str())
        .map(str::to_ascii_lowercase);

    match ext.as_deref() {
        Some("wav") => "audio/wav",
        Some("mp3") => "audio/mpeg",
        Some("flac") => "audio/flac",
        Some("aac") => "audio/aac",
        Some("m4a") => "audio/mp4",
        Some("ogg") => "audio/ogg",
        Some("png") => "image/png",
        Some("jpg" | "jpeg") => "image/jpeg",
        Some(_) | None => "application/octet-stream",
    }
}

/// Return a `503` media-unavailable response with the required JSON body.
fn media_unavailable() -> Response {
    let mut response = (
        StatusCode::SERVICE_UNAVAILABLE,
        Body::from(r#"{"error":{"code":"media_unavailable","message":"media not mounted"}}"#.to_owned()),
    )
        .into_response();
    let headers = response.headers_mut();
    insert_header(headers, CONTENT_TYPE, "application/json");
    insert_header(headers, RETRY_AFTER, "2");
    response
}

/// Assemble a `200`/`206` media response with the common headers.
fn build_media_response(
    status: StatusCode,
    content_type: &str,
    content_length: u64,
    content_range: Option<String>,
    body: Body,
) -> Response {
    let mut response = (status, body).into_response();
    let h = response.headers_mut();
    insert_header(h, CONTENT_TYPE, content_type);
    insert_header(h, ACCEPT_RANGES, "bytes");
    insert_header(h, CONTENT_LENGTH, &content_length.to_string());
    if let Some(range) = content_range {
        insert_header(h, CONTENT_RANGE, &range);
    }
    response
}

/// Build the `416 Range Not Satisfiable` response (with `Content-Range: */N`).
fn range_not_satisfiable(size: u64, head: bool) -> Response {
    let body = if head {
        Body::empty()
    } else {
        Body::from(
            r#"{"error":{"code":"range_not_satisfiable","message":"requested range not satisfiable"}}"#
                .to_owned(),
        )
    };
    let mut response = (StatusCode::RANGE_NOT_SATISFIABLE, body).into_response();
    let h = response.headers_mut();
    insert_header(h, CONTENT_TYPE, "application/json");
    insert_header(h, ACCEPT_RANGES, "bytes");
    insert_header(h, CONTENT_RANGE, &format!("bytes */{size}"));
    response
}

/// Insert a header, silently skipping a value that cannot be encoded (the
/// values here are always ASCII, so this never drops a real header).
fn insert_header(headers: &mut HeaderMap, name: axum::http::HeaderName, value: &str) {
    if let Ok(value) = axum::http::HeaderValue::from_str(value) {
        headers.insert(name, value);
    }
}

/// Set `Content-Disposition: attachment; filename="…"` with a safe filename.
fn insert_attachment(headers: &mut HeaderMap, filename: &str) {
    insert_header(
        headers,
        CONTENT_DISPOSITION,
        &format!("attachment; filename=\"{filename}\""),
    );
}

/// Reduce an attacker-influenced token (camera name) to a strict, separator-free
/// slug safe for zip entry names and `Content-Disposition` filenames.
fn sanitize_token(token: &str) -> String {
    let cleaned: String = token
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect();
    if cleaned.is_empty() {
        "angle".to_owned()
    } else {
        cleaned
    }
}

/// Build a `ZIP_STORED` archive of `entries` into an anonymous tempfile in
/// `cache_dir`, returning the rewound file ready to stream. The file is
/// unnamed/auto-unlinked, so it disappears when the streamed handle is dropped.
///
/// `ZIP_STORED` (no compression): mp4 is already H.264-compressed, so deflating
/// burns CPU for ~0% gain. Each member is copied in a `std::io::copy` loop, so
/// peak memory stays bounded regardless of clip size.
fn build_zip(cache_dir: &Path, entries: &[(String, PathBuf)]) -> std::io::Result<std::fs::File> {
    let tmp = tempfile::tempfile_in(cache_dir)?;
    let mut writer = zip::ZipWriter::new(tmp);
    let options = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Stored)
        .large_file(true);
    for (name, path) in entries {
        let mut source = std::fs::File::open(path)?;
        writer
            .start_file(name.as_str(), options)
            .map_err(std::io::Error::other)?;
        std::io::copy(&mut source, &mut writer)?;
    }
    let mut file = writer.finish().map_err(std::io::Error::other)?;
    file.seek(SeekFrom::Start(0))?;
    Ok(file)
}
