//! `retentiond` client-side transport for `indexd` archive registration RPC.
//!
//! The wire contract mirrors `indexd::proto` but remains crate-local so
//! `retentiond` and `indexd` stay decoupled.

use std::io::{self, Cursor, Read, Write};

use serde::{Deserialize, Serialize};

/// `indexd` archive-registration Unix socket path.
pub const INDEXD_SOCKET_PATH: &str = "/run/teslausb/indexd.sock";

/// Maximum accepted request/response frame length in bytes.
pub const MAX_REQUEST_FRAME: u32 = 64 * 1024;

/// One archive registration payload sent by `retentiond`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArchiveRegistration {
    /// Clip identity key matching scanner ingest.
    pub canonical_key: String,
    /// Source folder class (`RecentClips`, `SavedClips`, ...).
    pub folder_class: String,
    /// Source partition label.
    pub partition: String,
    /// Clip start epoch seconds.
    pub started_at: i64,
    /// Clip end epoch seconds.
    pub ended_at: i64,
    /// Clip duration in seconds when known.
    pub duration_s: Option<i64>,
    /// Archive item metadata.
    pub archive: ArchiveItemRef,
    /// Per-camera archive-backed angles.
    pub angles: Vec<ArchiveAngleRef>,
}

/// One durable archive item reference.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArchiveItemRef {
    /// Archive-root-relative deterministic path.
    pub path: String,
    /// Total bytes in the archive item.
    pub size_bytes: i64,
    /// Number of files in the archive item.
    pub file_count: i64,
    /// Archive completion epoch seconds.
    pub archived_at: i64,
}

/// One camera angle now backed by archive storage.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArchiveAngleRef {
    /// Camera label (`front`, `back`, `left_repeater`, ...).
    pub camera: String,
    /// Archive-root-relative file reference for playback.
    pub file_ref: String,
    /// Milliseconds relative to clip start.
    pub offset_ms: i64,
    /// Angle duration in seconds when known.
    pub duration_s: Option<i64>,
    /// File size in bytes.
    pub size_bytes: i64,
}

/// Successful registration ids returned by `indexd`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegistrationOk {
    /// The clip row id.
    pub clip_id: i64,
    /// The archive item row id.
    pub archive_item_id: i64,
}

/// Failures while sending/receiving archive registration RPCs.
#[derive(Debug, thiserror::Error)]
pub enum RegisterError {
    /// Transport/framing I/O failure.
    #[error("i/o error: {0}")]
    Io(#[from] io::Error),
    /// Server-reported command failure.
    #[error("indexd rejected registration: {message}")]
    Server {
        /// Human-readable server message.
        message: String,
    },
    /// Deterministic server rejection: indexd validated the payload and refused
    /// it. Retrying is futile; callers must not defer/poison on this.
    #[error("indexd rejected payload: {message}")]
    Rejected {
        /// Human-readable rejection reason.
        message: String,
    },
    /// Received or attempted frame exceeded max size.
    #[error("frame too large: {len} > {cap} bytes")]
    FrameTooLarge {
        /// Observed frame length in bytes.
        len: usize,
        /// Maximum accepted frame length in bytes.
        cap: usize,
    },
    /// JSON decode/encode or semantic parse error.
    #[error("decode error: {0}")]
    Decode(String),
}

/// Archive-registration client seam for `retentiond`.
pub trait RegisterClient {
    /// Send one archive registration to `indexd` and return assigned ids.
    ///
    /// # Errors
    ///
    /// Returns [`RegisterError`] on transport, framing, decode, or
    /// server-reported failures.
    fn register(&self, reg: &ArchiveRegistration) -> Result<RegistrationOk, RegisterError>;

    /// Send one quarantined archive registration to `indexd`.
    ///
    /// Deploy ordering is binding: deploy `indexd` before `retentiond`.
    /// Older `indexd` rejects this distinct verb, and `retentiond` fails
    /// closed by deferring the registration pending retry.
    ///
    /// # Errors
    ///
    /// Returns [`RegisterError`] on transport, framing, decode, or
    /// server-reported failures.
    fn register_quarantined(
        &self,
        reg: &ArchiveRegistration,
    ) -> Result<RegistrationOk, RegisterError>;
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum WireRequest {
    RegisterArchivedClip(ArchiveRegistration),
    // Deploy `indexd` before `retentiond`: this distinct verb must fail closed
    // on older `indexd` (unknown cmd), never silently default to LIVE publish.
    RegisterQuarantinedArchive(ArchiveRegistration),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
enum WireResponse {
    Ok { clip_id: i64, archive_item_id: i64 },
    Error { message: String },
    Rejected { message: String },
}

fn frame_cap_usize(cap: u32) -> Result<usize, RegisterError> {
    usize::try_from(cap).map_err(|_| RegisterError::Decode("frame cap overflow".to_owned()))
}

fn read_frame(stream: &mut impl Read, cap: u32) -> Result<Vec<u8>, RegisterError> {
    let mut len_buf = [0_u8; 4];
    stream.read_exact(&mut len_buf)?;
    let len_u32 = u32::from_le_bytes(len_buf);
    let len = usize::try_from(len_u32)
        .map_err(|_| RegisterError::Decode("frame length overflow".to_owned()))?;
    let cap_len = frame_cap_usize(cap)?;
    if len > cap_len {
        return Err(RegisterError::FrameTooLarge { len, cap: cap_len });
    }

    let mut payload = vec![0_u8; len];
    stream.read_exact(&mut payload)?;
    Ok(payload)
}

fn write_frame(stream: &mut impl Write, payload: &[u8], cap: u32) -> Result<(), RegisterError> {
    let cap_len = frame_cap_usize(cap)?;
    if payload.len() > cap_len {
        return Err(RegisterError::FrameTooLarge {
            len: payload.len(),
            cap: cap_len,
        });
    }

    let len_u32 = u32::try_from(payload.len()).map_err(|_| RegisterError::FrameTooLarge {
        len: payload.len(),
        cap: cap_len,
    })?;
    stream.write_all(&len_u32.to_le_bytes())?;
    stream.write_all(payload)?;
    stream.flush()?;
    Ok(())
}

fn decode_response_payload(payload: &[u8]) -> Result<RegistrationOk, RegisterError> {
    let response: WireResponse =
        serde_json::from_slice(payload).map_err(|err| RegisterError::Decode(err.to_string()))?;
    match response {
        WireResponse::Ok {
            clip_id,
            archive_item_id,
        } => Ok(RegistrationOk {
            clip_id,
            archive_item_id,
        }),
        WireResponse::Error { message } => Err(RegisterError::Server { message }),
        WireResponse::Rejected { message } => Err(RegisterError::Rejected { message }),
    }
}

/// Encode one archive registration into a framed wire payload.
///
/// # Errors
///
/// Returns [`RegisterError`] if serialization fails or if the encoded frame
/// exceeds [`MAX_REQUEST_FRAME`].
pub fn encode_request_frame(reg: &ArchiveRegistration) -> Result<Vec<u8>, RegisterError> {
    encode_wire_request_frame(&WireRequest::RegisterArchivedClip(reg.clone()))
}

fn encode_wire_request_frame(request: &WireRequest) -> Result<Vec<u8>, RegisterError> {
    let payload =
        serde_json::to_vec(&request).map_err(|err| RegisterError::Decode(err.to_string()))?;
    let mut framed = Vec::with_capacity(payload.len() + 4);
    write_frame(&mut framed, &payload, MAX_REQUEST_FRAME)?;
    Ok(framed)
}

/// Decode one framed response from `indexd`.
///
/// # Errors
///
/// Returns [`RegisterError`] on framing, decode, or server-reported failures.
pub fn decode_response_frame(frame: &[u8]) -> Result<RegistrationOk, RegisterError> {
    let mut cursor = Cursor::new(frame);
    let payload = read_frame(&mut cursor, MAX_REQUEST_FRAME)?;
    let consumed = usize::try_from(cursor.position())
        .map_err(|_| RegisterError::Decode("cursor position overflow".to_owned()))?;
    if consumed != frame.len() {
        return Err(RegisterError::Decode(
            "trailing bytes after response frame".to_owned(),
        ));
    }
    decode_response_payload(&payload)
}

#[cfg(unix)]
const IO_TIMEOUT_SECS: u64 = 5;

/// Live Unix-domain-socket `indexd` register client.
#[cfg(unix)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnixRegisterClient {
    socket_path: std::path::PathBuf,
}

#[cfg(unix)]
impl UnixRegisterClient {
    /// Build a client that connects to `socket_path`.
    #[must_use]
    pub fn new(socket_path: impl Into<std::path::PathBuf>) -> Self {
        Self {
            socket_path: socket_path.into(),
        }
    }
}

#[cfg(unix)]
impl RegisterClient for UnixRegisterClient {
    fn register(&self, reg: &ArchiveRegistration) -> Result<RegistrationOk, RegisterError> {
        self.send_request(&WireRequest::RegisterArchivedClip(reg.clone()))
    }

    fn register_quarantined(
        &self,
        reg: &ArchiveRegistration,
    ) -> Result<RegistrationOk, RegisterError> {
        self.send_request(&WireRequest::RegisterQuarantinedArchive(reg.clone()))
    }
}

#[cfg(unix)]
impl UnixRegisterClient {
    fn send_request(&self, request: &WireRequest) -> Result<RegistrationOk, RegisterError> {
        use std::os::unix::net::UnixStream;
        use std::time::Duration;

        let mut stream = UnixStream::connect(&self.socket_path)?;
        let timeout = Duration::from_secs(IO_TIMEOUT_SECS);
        stream.set_read_timeout(Some(timeout))?;
        stream.set_write_timeout(Some(timeout))?;

        let frame = encode_wire_request_frame(request)?;
        stream.write_all(&frame)?;
        stream.flush()?;

        let payload = read_frame(&mut stream, MAX_REQUEST_FRAME)?;
        decode_response_payload(&payload)
    }
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        clippy::panic,
        clippy::indexing_slicing
    )]

    use super::{
        ArchiveAngleRef, ArchiveItemRef, ArchiveRegistration, MAX_REQUEST_FRAME, RegisterError,
        RegistrationOk, WireRequest, WireResponse, decode_response_frame, decode_response_payload,
        encode_request_frame, read_frame, write_frame,
    };
    use std::io::Cursor;

    fn sample_registration() -> ArchiveRegistration {
        ArchiveRegistration {
            canonical_key: "slot0:TeslaCam/RecentClips/2026-06-19/2026-06-19_10-00-00".to_owned(),
            folder_class: "RecentClips".to_owned(),
            partition: "slot0".to_owned(),
            started_at: 1_718_805_600,
            ended_at: 1_718_805_660,
            duration_s: Some(60),
            archive: ArchiveItemRef {
                path: "archive/2026-06-19/clip-001".to_owned(),
                size_bytes: 12_345,
                file_count: 4,
                archived_at: 1_718_805_700,
            },
            angles: vec![ArchiveAngleRef {
                camera: "front".to_owned(),
                file_ref: "archive/2026-06-19/clip-001/front.mp4".to_owned(),
                offset_ms: 0,
                duration_s: Some(60),
                size_bytes: 3_086,
            }],
        }
    }

    #[test]
    fn encode_request_frame_has_expected_wire_shape() {
        let registration = sample_registration();
        let frame = encode_request_frame(&registration).expect("encode request frame");

        let mut cursor = Cursor::new(frame.as_slice());
        let payload = read_frame(&mut cursor, MAX_REQUEST_FRAME).expect("read payload");
        assert_eq!(
            usize::try_from(cursor.position()).expect("cursor position to usize"),
            frame.len()
        );

        let json = String::from_utf8(payload.clone()).expect("request payload should be utf-8");
        assert!(json.contains("\"cmd\":\"register_archived_clip\""));
        for field in [
            "canonical_key",
            "folder_class",
            "partition",
            "started_at",
            "ended_at",
            "duration_s",
            "archive",
            "path",
            "size_bytes",
            "file_count",
            "archived_at",
            "angles",
            "camera",
            "file_ref",
            "offset_ms",
        ] {
            assert!(
                json.contains(&format!("\"{field}\"")),
                "missing field: {field}"
            );
        }

        let value: serde_json::Value = serde_json::from_slice(&payload).expect("valid json");
        assert_eq!(value["cmd"], "register_archived_clip");
        assert_eq!(value["canonical_key"], registration.canonical_key);
        assert_eq!(value["folder_class"], registration.folder_class);
        assert_eq!(value["partition"], registration.partition);
        assert_eq!(value["started_at"], registration.started_at);
        assert_eq!(value["ended_at"], registration.ended_at);
        assert_eq!(
            value["duration_s"],
            registration.duration_s.expect("duration exists")
        );
        assert_eq!(value["archive"]["path"], registration.archive.path);
        assert_eq!(
            value["archive"]["size_bytes"],
            registration.archive.size_bytes
        );
        assert_eq!(
            value["archive"]["file_count"],
            registration.archive.file_count
        );
        assert_eq!(
            value["archive"]["archived_at"],
            registration.archive.archived_at
        );
        assert_eq!(value["angles"][0]["camera"], registration.angles[0].camera);
        assert_eq!(
            value["angles"][0]["file_ref"],
            registration.angles[0].file_ref
        );
        assert_eq!(
            value["angles"][0]["offset_ms"],
            registration.angles[0].offset_ms
        );
        assert_eq!(
            value["angles"][0]["duration_s"],
            registration.angles[0].duration_s.expect("duration exists")
        );
        assert_eq!(
            value["angles"][0]["size_bytes"],
            registration.angles[0].size_bytes
        );
    }

    #[test]
    fn decode_response_frame_ok() {
        let mut frame = Vec::new();
        let payload = serde_json::to_vec(&WireResponse::Ok {
            clip_id: 7,
            archive_item_id: 11,
        })
        .expect("serialize response");
        write_frame(&mut frame, &payload, MAX_REQUEST_FRAME).expect("write frame");

        let decoded = decode_response_frame(&frame).expect("decode response");
        assert_eq!(
            decoded,
            RegistrationOk {
                clip_id: 7,
                archive_item_id: 11
            }
        );
    }

    #[test]
    fn decode_response_frame_server_error() {
        let mut frame = Vec::new();
        let payload = serde_json::to_vec(&WireResponse::Error {
            message: "invalid registration".to_owned(),
        })
        .expect("serialize response");
        write_frame(&mut frame, &payload, MAX_REQUEST_FRAME).expect("write frame");

        let err = decode_response_frame(&frame).expect_err("server error should propagate");
        match err {
            RegisterError::Server { message } => assert_eq!(message, "invalid registration"),
            other => panic!("unexpected error: {other:?}"),
        }
    }

    #[test]
    fn decode_response_payload_maps_error_and_rejected_distinctly() {
        let error_payload = serde_json::to_vec(&WireResponse::Error {
            message: "db busy".to_owned(),
        })
        .expect("serialize response");
        let error = decode_response_payload(&error_payload).expect_err("error should propagate");
        match error {
            RegisterError::Server { message } => assert_eq!(message, "db busy"),
            other => panic!("unexpected error: {other:?}"),
        }

        let rejected_payload = serde_json::to_vec(&WireResponse::Rejected {
            message: "invalid camera: left".to_owned(),
        })
        .expect("serialize response");
        let rejected =
            decode_response_payload(&rejected_payload).expect_err("rejected should propagate");
        match rejected {
            RegisterError::Rejected { message } => assert_eq!(message, "invalid camera: left"),
            other => panic!("unexpected error: {other:?}"),
        }
    }

    #[test]
    fn decode_response_frame_rejects_oversize() {
        let mut frame = Vec::new();
        frame.extend_from_slice(&(MAX_REQUEST_FRAME + 1).to_le_bytes());

        let err = decode_response_frame(&frame).expect_err("oversize frame should fail");
        assert!(matches!(err, RegisterError::FrameTooLarge { .. }));
    }

    #[test]
    fn wire_request_encodes_quarantine_cmd() {
        let payload = serde_json::to_vec(&WireRequest::RegisterQuarantinedArchive(
            sample_registration(),
        ))
        .expect("serialize request");
        let value: serde_json::Value = serde_json::from_slice(&payload).expect("valid json");
        assert_eq!(value["cmd"], "register_quarantined_archive");
    }
}

#[cfg(all(test, unix))]
mod unix_tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        clippy::panic,
        clippy::indexing_slicing
    )]

    use super::{
        ArchiveAngleRef, ArchiveItemRef, ArchiveRegistration, MAX_REQUEST_FRAME, RegisterClient,
        RegisterError, RegistrationOk, UnixRegisterClient, WireRequest, WireResponse, read_frame,
        write_frame,
    };
    use std::fs;
    use std::os::unix::net::UnixListener;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::thread;

    static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn new_temp_dir() -> PathBuf {
        let unique = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let name = format!("retentiond-register-{}-{unique}", std::process::id());
        let dir = std::env::temp_dir().join(name);
        fs::create_dir_all(&dir).expect("create temp dir");
        dir
    }

    fn sample_registration() -> ArchiveRegistration {
        ArchiveRegistration {
            canonical_key: "slot0:TeslaCam/RecentClips/2026-06-19/2026-06-19_10-00-00".to_owned(),
            folder_class: "RecentClips".to_owned(),
            partition: "slot0".to_owned(),
            started_at: 1_718_805_600,
            ended_at: 1_718_805_660,
            duration_s: Some(60),
            archive: ArchiveItemRef {
                path: "archive/2026-06-19/clip-001".to_owned(),
                size_bytes: 12_345,
                file_count: 4,
                archived_at: 1_718_805_700,
            },
            angles: vec![ArchiveAngleRef {
                camera: "front".to_owned(),
                file_ref: "archive/2026-06-19/clip-001/front.mp4".to_owned(),
                offset_ms: 0,
                duration_s: Some(60),
                size_bytes: 3_086,
            }],
        }
    }

    #[test]
    fn unix_register_client_roundtrip_ok() {
        let temp_dir = new_temp_dir();
        let socket_path = temp_dir.join("indexd.sock");
        let listener = UnixListener::bind(&socket_path).expect("bind listener");
        let expected_registration = sample_registration();
        let thread_registration = expected_registration.clone();

        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request frame");
            let request: WireRequest = serde_json::from_slice(&payload).expect("decode request");
            assert_eq!(
                request,
                WireRequest::RegisterArchivedClip(thread_registration)
            );

            let payload = serde_json::to_vec(&WireResponse::Ok {
                clip_id: 23,
                archive_item_id: 29,
            })
            .expect("encode response");
            write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
        });

        let client = UnixRegisterClient::new(socket_path);
        let got = client
            .register(&expected_registration)
            .expect("register request");
        assert_eq!(
            got,
            RegistrationOk {
                clip_id: 23,
                archive_item_id: 29
            }
        );
        server.join().expect("server join");

        let _ = fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn unix_register_client_server_error() {
        let temp_dir = new_temp_dir();
        let socket_path = temp_dir.join("indexd.sock");
        let listener = UnixListener::bind(&socket_path).expect("bind listener");
        let expected_registration = sample_registration();

        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request frame");
            let _request: WireRequest = serde_json::from_slice(&payload).expect("decode request");

            let payload = serde_json::to_vec(&WireResponse::Error {
                message: "clip not found".to_owned(),
            })
            .expect("encode response");
            write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
        });

        let client = UnixRegisterClient::new(socket_path);
        let err = client
            .register(&expected_registration)
            .expect_err("server error should map to RegisterError::Server");
        match err {
            RegisterError::Server { message } => assert_eq!(message, "clip not found"),
            other => panic!("unexpected error: {other:?}"),
        }
        server.join().expect("server join");

        let _ = fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn unix_register_client_rejected_error() {
        let temp_dir = new_temp_dir();
        let socket_path = temp_dir.join("indexd.sock");
        let listener = UnixListener::bind(&socket_path).expect("bind listener");
        let expected_registration = sample_registration();

        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request frame");
            let _request: WireRequest = serde_json::from_slice(&payload).expect("decode request");

            let payload = serde_json::to_vec(&WireResponse::Rejected {
                message: "invalid camera: left".to_owned(),
            })
            .expect("encode response");
            write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
        });

        let client = UnixRegisterClient::new(socket_path);
        let err = client
            .register(&expected_registration)
            .expect_err("rejected should map to RegisterError::Rejected");
        match err {
            RegisterError::Rejected { message } => assert_eq!(message, "invalid camera: left"),
            other => panic!("unexpected error: {other:?}"),
        }
        server.join().expect("server join");

        let _ = fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn unix_register_client_quarantine_roundtrip_ok() {
        let temp_dir = new_temp_dir();
        let socket_path = temp_dir.join("indexd.sock");
        let listener = UnixListener::bind(&socket_path).expect("bind listener");
        let expected_registration = sample_registration();
        let thread_registration = expected_registration.clone();

        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request frame");
            let request: WireRequest = serde_json::from_slice(&payload).expect("decode request");
            assert_eq!(
                request,
                WireRequest::RegisterQuarantinedArchive(thread_registration)
            );

            let payload = serde_json::to_vec(&WireResponse::Ok {
                clip_id: 41,
                archive_item_id: 43,
            })
            .expect("encode response");
            write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
        });

        let client = UnixRegisterClient::new(socket_path);
        let got = client
            .register_quarantined(&expected_registration)
            .expect("quarantine register request");
        assert_eq!(
            got,
            RegistrationOk {
                clip_id: 41,
                archive_item_id: 43
            }
        );
        server.join().expect("server join");

        let _ = fs::remove_dir_all(temp_dir);
    }
}
