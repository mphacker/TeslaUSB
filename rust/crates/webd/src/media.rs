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

use std::io;
use std::io::{Seek, SeekFrom};
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

use axum::body::Body;
use axum::body::Bytes;
use axum::extract::{Path as AxumPath, Query, State};
use axum::http::header::{
    ACCEPT_RANGES, CONTENT_DISPOSITION, CONTENT_LENGTH, CONTENT_RANGE, CONTENT_TYPE, RANGE,
    RETRY_AFTER, X_CONTENT_TYPE_OPTIONS,
};
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use scannerd::seiwalk::Waypoint;
use serde::Deserialize;
use teslausb_core::sei::tesla::{AutopilotState, Gear};
use tokio::io::{AsyncReadExt, AsyncSeekExt};
use tokio::sync::{Semaphore, mpsc};
use tokio_stream::wrappers::ReceiverStream;
use tokio_util::io::ReaderStream;

use crate::AppState;
use crate::error::ApiError;
use crate::range::{ParsedRange, parse_byte_range};
use crate::read_client::{MAX_READ_LEN, ReadFileError, ReadFileRequest};

/// The MIME type all angles are served as. Tesla footage is H.264 in an mp4
/// container (SPEC.md §7), played natively by every target browser.
const VIDEO_MIME: &str = "video/mp4";

/// Read/emit chunk size for streamed bodies (bounded memory per connection).
const STREAM_CHUNK: usize = 256 * 1024;
/// Maximum clip size eligible for telemetry parse (bounds memory use on Pi).
const TELEMETRY_MAX_BYTES: u64 = 256 * 1024 * 1024;
/// Hard cap on emitted telemetry samples — bounds the DTO Vec and the JSON
/// output so a pathologically dense SEI track cannot exhaust the recording
/// device's memory. Far above any real clip (~one sample/frame over a ~60 s
/// segment ≈ a few thousand samples).
const TELEMETRY_MAX_SAMPLES: usize = 200_000;

/// The `view_kind` value whose `file_ref` resolves to a playable Pi-side path.
const VIEW_ARCHIVE: &str = "archive";
/// Telemetry parses are serialized so only one full clip buffer exists at once.
static TELEMETRY_PARSE_PERMITS: Semaphore = Semaphore::const_new(1);

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

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct TelemetryDto {
    time: f64,
    speed_mps: f32,
    gear: u32,
    steering_angle: f32,
    blinker_left: bool,
    blinker_right: bool,
    brake_applied: bool,
    accelerator_pedal_position: f32,
    autopilot_state: u32,
}

fn fin32(v: f32) -> f32 {
    if v.is_finite() { v } else { 0.0 }
}

fn fin64(v: f64) -> f64 {
    if v.is_finite() { v } else { 0.0 }
}

fn gear_to_u32(gear: Gear) -> u32 {
    match gear {
        Gear::Park => 0,
        Gear::Drive => 1,
        Gear::Reverse => 2,
        Gear::Neutral => 3,
        Gear::Unknown(v) => v,
    }
}

fn autopilot_to_u32(state: AutopilotState) -> u32 {
    match state {
        AutopilotState::None => 0,
        AutopilotState::SelfDriving => 1,
        AutopilotState::Autosteer => 2,
        AutopilotState::Tacc => 3,
        AutopilotState::Unknown(v) => v,
    }
}

fn waypoint_to_dto(waypoint: &Waypoint) -> TelemetryDto {
    let msg = waypoint.message;
    TelemetryDto {
        time: fin64(waypoint.timestamp_ms / 1000.0),
        speed_mps: fin32(msg.vehicle_speed_mps),
        gear: gear_to_u32(msg.gear_state),
        steering_angle: fin32(msg.steering_wheel_angle),
        blinker_left: msg.blinker_on_left,
        blinker_right: msg.blinker_on_right,
        brake_applied: msg.brake_applied,
        accelerator_pedal_position: fin32(msg.accelerator_pedal_position),
        autopilot_state: autopilot_to_u32(msg.autopilot_state),
    }
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
    let head = method == Method::HEAD;
    match open_archive_angle(&state, id, &camera).await {
        Ok((file, size)) => {
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
        Err(ApiError::NotFound) => {
            stream_non_archive_angle(&state, id, &camera, head, &headers).await
        }
        Err(err) => Err(err),
    }
}

/// `GET /api/clips/{id}/telemetry?camera=` — parse Tesla SEI telemetry from the
/// Pi-side archive clip and return HUD samples.
///
/// `eprintln!` is used for graceful-degradation breadcrumbs (oversize / read /
/// parse failures land in `journalctl -u webd`); webd carries no `tracing`/`log`
/// dependency, matching the existing `media_events.rs` convention.
#[allow(clippy::print_stderr)]
pub(crate) async fn telemetry(
    State(state): State<AppState>,
    AxumPath(id): AxumPath<i64>,
    Query(q): Query<StreamQuery>,
) -> Result<Response, ApiError> {
    let camera = q
        .camera
        .filter(|c| !c.is_empty())
        .unwrap_or_else(|| "front".to_owned());

    let catalog = state.catalog.clone();
    let camera_owned = camera.clone();
    let source = crate::route::read(catalog, move |conn| {
        crate::query::angle_source(conn, id, &camera_owned)
    })
    .await?;
    let Some((file_ref, view_kind)) = source else {
        return Err(ApiError::NotFound);
    };
    if view_kind != VIEW_ARCHIVE {
        return telemetry_non_archive(&state, id, &camera).await;
    }

    let path = match resolve_archive_path(state.media.archive_root.as_path(), &file_ref).await {
        Resolved::Ok(path) => path,
        Resolved::Missing | Resolved::Escaped => return Err(ApiError::NotFound),
    };

    let metadata = tokio::fs::metadata(&path)
        .await
        .map_err(|_| ApiError::NotFound)?;
    if metadata.len() > TELEMETRY_MAX_BYTES {
        eprintln!(
            "webd: telemetry parse skipped for clip {id} camera {camera}: file too large ({})",
            metadata.len()
        );
        return Ok(empty_telemetry_response());
    }

    let _permit = TELEMETRY_PARSE_PERMITS
        .acquire()
        .await
        .map_err(|_| ApiError::Internal)?;

    let json = match tokio::task::spawn_blocking(move || {
        telemetry_json_blocking(&path, id, &camera)
    })
    .await
    {
        Ok(json) => json,
        Err(err) => {
            eprintln!("webd: telemetry parse task failed for clip {id}: {err}");
            b"[]".to_vec()
        }
    };

    Ok(([(CONTENT_TYPE, "application/json")], json).into_response())
}

/// Telemetry for a non-archive (`ro_usb`) clip: parse SEI from the car-volume
/// file via the scannerd readfile socket, mirroring `stream_non_archive_angle`'s
/// read path but for the whole (capped) file. Best-effort — returns an empty
/// `[]` (never an error) for a missing angle, an unverifiable size, an oversize
/// clip, or any read/parse failure.
#[allow(clippy::print_stderr)]
async fn telemetry_non_archive(
    state: &AppState,
    id: i64,
    camera: &str,
) -> Result<Response, ApiError> {
    let catalog = state.catalog.clone();
    let camera_owned = camera.to_owned();
    let source = crate::route::read(catalog, move |conn| {
        crate::query::non_archive_angle_source(conn, id, &camera_owned)
    })
    .await?;
    // No non-archive angle, or a NULL/non-positive (unverifiable) catalog size →
    // graceful empty, mirroring the streaming path's fail-closed size gate.
    let Some((file_ref, Some(size))) = source else {
        return Ok(empty_telemetry_response());
    };
    if size <= 0 {
        return Ok(empty_telemetry_response());
    }
    let expected_size = u64::try_from(size).unwrap_or(u64::MAX);
    if expected_size > TELEMETRY_MAX_BYTES {
        eprintln!(
            "webd: telemetry ro_usb skipped for clip {id} camera {camera}: catalog size too large ({size})"
        );
        return Ok(empty_telemetry_response());
    }

    let _permit = TELEMETRY_PARSE_PERMITS
        .acquire()
        .await
        .map_err(|_| ApiError::Internal)?;

    let client = Arc::clone(&state.read_client);
    let camera_owned = camera.to_owned();
    let json = match tokio::task::spawn_blocking(move || {
        telemetry_json_ro_usb_blocking(&*client, &file_ref, expected_size, id, &camera_owned)
    })
    .await
    {
        Ok(json) => json,
        Err(err) => {
            eprintln!("webd: telemetry ro_usb parse task failed for clip {id}: {err}");
            b"[]".to_vec()
        }
    };

    Ok(([(CONTENT_TYPE, "application/json")], json).into_response())
}

/// A bare `[]` JSON telemetry response — the graceful empty state returned for a
/// non-archive angle, an oversize clip, or any parse failure.
fn empty_telemetry_response() -> Response {
    (
        [(CONTENT_TYPE, "application/json")],
        Bytes::from_static(b"[]"),
    )
        .into_response()
}

/// A `Write` sink that accumulates bytes but fails once it would exceed `cap`,
/// giving the `ro_usb` telemetry read the same hard memory bound the archive path
/// gets from `Read::take`.
struct CappedWriter {
    buf: Vec<u8>,
    cap: usize,
}

impl std::io::Write for CappedWriter {
    fn write(&mut self, data: &[u8]) -> std::io::Result<usize> {
        if self.buf.len().saturating_add(data.len()) > self.cap {
            return Err(std::io::Error::new(
                std::io::ErrorKind::Other,
                "telemetry read exceeded byte cap",
            ));
        }
        self.buf.extend_from_slice(data);
        Ok(data.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// SEI-parse already-read clip bytes into pre-serialised telemetry JSON (always
/// a valid array; `[]` on parse failure). Sample count is capped at
/// [`TELEMETRY_MAX_SAMPLES`]. Shared by the archive (fs) and `ro_usb` (`read_client`)
/// telemetry paths.
fn parse_sei_to_json(bytes: &[u8]) -> Vec<u8> {
    let dtos: Vec<TelemetryDto> = match scannerd::seiwalk::walk_clip_waypoints(bytes, 1) {
        Ok(waypoints) => waypoints
            .waypoints
            .iter()
            .take(TELEMETRY_MAX_SAMPLES)
            .map(waypoint_to_dto)
            .collect(),
        Err(_) => Vec::new(),
    };
    serde_json::to_vec(&dtos).unwrap_or_else(|_| b"[]".to_vec())
}

/// Read (hard-capped), SEI-parse and JSON-serialise clip telemetry entirely on a
/// blocking thread, returning pre-serialised JSON bytes (always a valid array;
/// `[]` on any failure). Every allocation is bounded so a pathological clip
/// cannot exhaust the recording device's memory: the file read is capped at
/// [`TELEMETRY_MAX_BYTES`] regardless of the (advisory) metadata pre-check, the
/// sample count at [`TELEMETRY_MAX_SAMPLES`], and serialisation runs here (off
/// the async reactor) rather than in the response path.
#[allow(clippy::print_stderr)]
fn telemetry_json_blocking(path: &Path, id: i64, camera: &str) -> Vec<u8> {
    use std::io::Read;
    let file = match std::fs::File::open(path) {
        Ok(file) => file,
        Err(err) => {
            eprintln!("webd: telemetry parse read failed for clip {id} camera {camera}: {err}");
            return b"[]".to_vec();
        }
    };
    // Cap the read at the limit (+1 byte so an at-read-time oversize file is
    // still detectable): the async metadata check above is advisory only, so
    // this is the authoritative memory bound on the buffer.
    let mut bytes = Vec::new();
    if let Err(err) = file.take(TELEMETRY_MAX_BYTES + 1).read_to_end(&mut bytes) {
        eprintln!("webd: telemetry parse read failed for clip {id} camera {camera}: {err}");
        return b"[]".to_vec();
    }
    if bytes.len() as u64 > TELEMETRY_MAX_BYTES {
        eprintln!(
            "webd: telemetry parse skipped for clip {id} camera {camera}: file too large at read time ({}+ bytes)",
            bytes.len()
        );
        return b"[]".to_vec();
    }
    parse_sei_to_json(&bytes)
}

/// Read (identity-fenced, hard-capped), SEI-parse and JSON-serialise telemetry
/// for a non-archive (`ro_usb`) clip whose bytes live on the car-visible USB
/// volume and are read through the scannerd readfile socket. Best-effort: any
/// read/fence/parse failure yields `[]` (never an error to the client). The
/// `CappedWriter` bounds the buffer at [`TELEMETRY_MAX_BYTES`] regardless of the
/// advisory catalog size, so a racing/growing file cannot exhaust memory. The
/// read is additionally anchored to `expected_size` (the catalog `size_bytes`):
/// both the file allocation (`identity.total_size`) and the bytes actually read
/// must equal it, else the file was recreated/resized since ingest and `[]` is
/// returned rather than telemetry from a substituted file.
#[allow(clippy::print_stderr)]
fn telemetry_json_ro_usb_blocking(
    client: &dyn crate::read_client::ReadFileClient,
    file_ref: &str,
    expected_size: u64,
    id: i64,
    camera: &str,
) -> Vec<u8> {
    let cap = usize::try_from(TELEMETRY_MAX_BYTES).unwrap_or(usize::MAX);
    let mut writer = CappedWriter {
        buf: Vec::new(),
        cap,
    };
    let identity = match crate::read_client::read_full_file_to_writer(
        client,
        file_ref,
        MAX_READ_LEN,
        &mut writer,
    ) {
        Ok(identity) => identity,
        Err(err) => {
            eprintln!("webd: telemetry ro_usb read failed for clip {id} camera {camera}: {err}");
            return b"[]".to_vec();
        }
    };
    // Fail closed if the file was recreated/resized since catalog ingest: the
    // bytes actually read (valid_data_length) AND the file allocation
    // (`identity.total_size` = data_length) must both equal the catalog
    // `size_bytes` — the same stable-file anchor the streaming path enforces
    // before serving bytes, so a substituted file yields `[]` not wrong data.
    if identity.total_size != expected_size || writer.buf.len() as u64 != expected_size {
        eprintln!(
            "webd: telemetry ro_usb size/identity mismatch for clip {id} camera {camera}: expected {expected_size}, read {} bytes, total_size {}",
            writer.buf.len(),
            identity.total_size
        );
        return b"[]".to_vec();
    }
    parse_sei_to_json(&writer.buf)
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

async fn stream_non_archive_angle(
    state: &AppState,
    clip_id: i64,
    camera: &str,
    head: bool,
    headers: &HeaderMap,
) -> Result<Response, ApiError> {
    let catalog = state.catalog.clone();
    let camera_owned = camera.to_owned();
    let source = crate::route::read(catalog, move |conn| {
        crate::query::non_archive_angle_source(conn, clip_id, &camera_owned)
    })
    .await?;
    let Some((file_ref, expected_size)) = source else {
        return Err(ApiError::NotFound);
    };
    // Fail closed: a non-archive angle the catalog cannot describe with a positive
    // stable size cannot be identity-verified on the first read, so refuse rather
    // than serve unverified bytes (scannerd-readfile.md §5). Real indexd-ingested
    // non-archive angles always carry a positive size (valid_data_length).
    let Some(expected_size) = expected_size.filter(|&s| s > 0) else {
        return Err(ApiError::NotFound);
    };

    let probe_req = ReadFileRequest {
        path: file_ref.clone(),
        offset: 0,
        len: 1,
        handle: None,
    };
    let probe = read_file_once(Arc::clone(&state.read_client), probe_req).await?;
    // First-read stable-identity gate (scannerd-readfile.md §5). The catalog lists
    // this clip as stable with a known size. `readable_size` is the file's current
    // `valid_data_length` (the exact byte count we stream) and `total_size` is its
    // `data_length`; indexd records `angles.size_bytes` from `valid_data_length` at
    // ingest, and a stable file has `valid_data_length == data_length`. So a legit,
    // unchanged clip matches BOTH — requiring both rejects a substitution that
    // preserves only one dimension. A mismatch means the path was recreated/changed
    // since ingest: fail closed with 410 rather than serving wrong/partial bytes.
    // The per-request ClipIdentity fence still covers any mid-stream change.
    let expected = u64::try_from(expected_size).unwrap_or(u64::MAX);
    if probe.readable_size != expected || probe.identity.total_size != expected {
        return Err(ApiError::status(
            StatusCode::GONE,
            "clip_changed",
            "clip changed before streaming",
        ));
    }
    let size = probe.readable_size;

    let response = match decide_range(headers, size) {
        RangeDecision::Full => {
            let body = if head {
                Body::empty()
            } else {
                body_for_non_archive_range(
                    Arc::clone(&state.read_client),
                    file_ref.clone(),
                    0,
                    size,
                    probe.identity,
                )
                .await?
            };
            build_media_response(StatusCode::OK, VIDEO_MIME, size, None, body)
        }
        RangeDecision::Satisfiable { start, end } => {
            let len = end - start + 1;
            let body = if head {
                Body::empty()
            } else {
                body_for_non_archive_range(
                    Arc::clone(&state.read_client),
                    file_ref.clone(),
                    start,
                    len,
                    probe.identity,
                )
                .await?
            };
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

fn map_read_file_error(err: &ReadFileError) -> ApiError {
    match err {
        ReadFileError::Changed => ApiError::status(
            StatusCode::GONE,
            "clip_changed",
            "clip changed while streaming",
        ),
        ReadFileError::NotFound | ReadFileError::OutOfRange => ApiError::NotFound,
        ReadFileError::Io(_)
        | ReadFileError::FrameTooLarge { .. }
        | ReadFileError::Decode(_)
        | ReadFileError::Server { .. } => ApiError::Internal,
    }
}

async fn read_file_once(
    client: Arc<dyn crate::read_client::ReadFileClient + Send + Sync>,
    req: ReadFileRequest,
) -> Result<crate::read_client::ReadFileOk, ApiError> {
    tokio::task::spawn_blocking(move || client.read_file(&req))
        .await
        .map_err(|_| ApiError::Internal)?
        .map_err(|err| map_read_file_error(&err))
}

async fn body_for_non_archive_range(
    client: Arc<dyn crate::read_client::ReadFileClient + Send + Sync>,
    file_ref: String,
    start: u64,
    len: u64,
    expected_identity: crate::read_client::ClipIdentity,
) -> Result<Body, ApiError> {
    if len == 0 {
        return Ok(Body::empty());
    }

    let first_req = ReadFileRequest {
        path: file_ref.clone(),
        offset: start,
        len: request_len_for(len),
        handle: Some(expected_identity),
    };
    let first = read_file_once(Arc::clone(&client), first_req).await?;
    let first_identity = first.identity;
    let mut first_bytes = first.bytes;
    let first_len = u64::try_from(first_bytes.len()).map_err(|_| ApiError::Internal)?;
    if first_len == 0 || first_len > len {
        return Err(ApiError::Internal);
    }

    let mut remaining = len.saturating_sub(first_len);
    let mut next_offset = start.saturating_add(first_len);

    if remaining > 0 && first.eof {
        return Err(ApiError::Internal);
    }

    // Guard the first boundary before sending headers: if the identity already
    // changed between adjacent windows, fail closed with `410`.
    if remaining > 0 {
        let guard_req = ReadFileRequest {
            path: file_ref.clone(),
            offset: next_offset,
            len: request_len_for(remaining),
            handle: Some(first_identity),
        };
        let _ = read_file_once(Arc::clone(&client), guard_req).await?;
    }

    let (tx, rx) = mpsc::channel::<Result<Bytes, io::Error>>(1);
    tokio::task::spawn_blocking(move || {
        if tx
            .blocking_send(Ok(Bytes::from(std::mem::take(&mut first_bytes))))
            .is_err()
        {
            return;
        }

        let mut handle = Some(first_identity);
        while remaining > 0 {
            let req = ReadFileRequest {
                path: file_ref.clone(),
                offset: next_offset,
                len: request_len_for(remaining),
                handle,
            };
            let window = match client.read_file(&req) {
                Ok(window) => window,
                Err(err) => {
                    let _ = tx.blocking_send(Err(io::Error::other(err.to_string())));
                    return;
                }
            };

            if handle != Some(window.identity) {
                let _ = tx.blocking_send(Err(io::Error::other("clip changed while streaming")));
                return;
            }

            let Ok(chunk_len) = u64::try_from(window.bytes.len()) else {
                let _ = tx.blocking_send(Err(io::Error::other("window length overflow")));
                return;
            };
            if chunk_len == 0 || chunk_len > remaining {
                let _ = tx.blocking_send(Err(io::Error::other("invalid read window length")));
                return;
            }
            if tx.blocking_send(Ok(Bytes::from(window.bytes))).is_err() {
                return;
            }

            remaining = remaining.saturating_sub(chunk_len);
            next_offset = next_offset.saturating_add(chunk_len);
            handle = Some(window.identity);

            if window.eof && remaining > 0 {
                let _ = tx.blocking_send(Err(io::Error::other("unexpected eof while streaming")));
                return;
            }
            if window.eof {
                break;
            }
        }
    });

    Ok(Body::from_stream(ReceiverStream::new(rx)))
}

fn request_len_for(remaining: u64) -> u32 {
    u32::try_from(remaining.min(u64::from(MAX_READ_LEN))).unwrap_or(MAX_READ_LEN)
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
        Body::from(
            r#"{"error":{"code":"media_unavailable","message":"media not mounted"}}"#.to_owned(),
        ),
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
