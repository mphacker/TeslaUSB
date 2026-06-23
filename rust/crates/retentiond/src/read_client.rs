//! `retentiond` client-side transport for `scannerd` `ReadFile`.
//!
//! Wire types intentionally remain crate-local (same pattern as
//! `register_client.rs`): no shared proto crate coupling.

use std::io::{self, Read, Write};

use serde::{Deserialize, Serialize};

/// `scannerd` `ReadFile` socket path.
pub const SCANNERD_READ_SOCKET_PATH: &str = "/run/teslausb/scannerd-read.sock";
/// Maximum JSON control frame size (header/request).
pub const MAX_REQUEST_FRAME: u32 = 64 * 1024;
/// Maximum bytes requested per `ReadFile` window.
pub const MAX_READ_LEN: u32 = 8 * 1024 * 1024;

/// First-chunk identity fence echoed across all windows of one copy.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClipIdentity {
    /// First cluster of the resolved file.
    pub first_cluster: u32,
    /// Resolved file size at first read.
    pub total_size: u64,
    /// exFAT `NameHash` of the resolved leaf.
    pub name_hash: u32,
}

/// One `ReadFile` request.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReadFileRequest {
    /// TeslaCam-volume-root-relative path to read.
    pub path: String,
    /// Byte offset in the file.
    pub offset: u64,
    /// Requested window length in bytes (capped by [`MAX_READ_LEN`]).
    pub len: u32,
    /// Identity fence from a prior chunk (none on first chunk).
    pub handle: Option<ClipIdentity>,
}

/// `ReadFile` response control header.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum ReadFileHeader {
    /// Successful window metadata (followed by a raw-byte tail).
    Ok {
        /// Identity captured/resolved for this file incarnation.
        identity: ClipIdentity,
        /// Current readable ceiling (`valid_data_length`).
        readable_size: u64,
        /// Whether this window reaches EOF.
        eof: bool,
        /// Raw tail byte length that follows this header.
        byte_len: u32,
    },
    /// The file identity no longer matches the echoed handle.
    Changed,
    /// The path did not resolve to a current file.
    NotFound,
    /// Requested offset was beyond current readable range.
    OutOfRange,
    /// Internal server-side error.
    Error {
        /// Human-readable server detail.
        message: String,
    },
}

/// Successful window payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReadFileOk {
    /// Identity for this read stream.
    pub identity: ClipIdentity,
    /// Current readable ceiling reported by scannerd.
    pub readable_size: u64,
    /// Whether this window reaches EOF.
    pub eof: bool,
    /// Raw window bytes.
    pub bytes: Vec<u8>,
}

/// `ReadFile` failures.
#[derive(Debug, thiserror::Error)]
pub enum ReadFileError {
    /// Transport/framing I/O failure.
    #[error("i/o error: {0}")]
    Io(#[from] io::Error),
    /// Request/response frame exceeded configured cap.
    #[error("frame too large: {len} > {cap} bytes")]
    FrameTooLarge {
        /// Observed frame size.
        len: usize,
        /// Maximum allowed frame size.
        cap: usize,
    },
    /// JSON/framing semantic decode failure.
    #[error("decode error: {0}")]
    Decode(String),
    /// Scannerd reported `changed`.
    #[error("read file changed while streaming")]
    Changed,
    /// Scannerd reported `not_found`.
    #[error("read file not found")]
    NotFound,
    /// Scannerd reported `out_of_range`.
    #[error("read file out of range")]
    OutOfRange,
    /// Scannerd reported `error`.
    #[error("scannerd read error: {message}")]
    Server {
        /// Human-readable server detail.
        message: String,
    },
}

/// `ReadFile` transport seam (host-testable).
pub trait ReadFileClient {
    /// Read one window.
    ///
    /// # Errors
    ///
    /// Returns [`ReadFileError`] on transport, framing, decode, or server status.
    fn read_file(&self, req: &ReadFileRequest) -> Result<ReadFileOk, ReadFileError>;
}

/// Read a full file by looping `ReadFile` windows with the identity fence.
///
/// On the first chunk this captures `ClipIdentity`; every subsequent request
/// echoes it in `handle`. Any non-`ok` response aborts the copy.
///
/// # Errors
///
/// Returns [`ReadFileError`] on transport, framing, decode, or if scannerd
/// reports `changed` / `not_found` / `out_of_range` / `error`.
pub fn read_full_file(
    client: &dyn ReadFileClient,
    path: &str,
    chunk_len: u32,
) -> Result<(ClipIdentity, Vec<u8>), ReadFileError> {
    let mut out = Vec::new();
    let identity = read_full_file_to_writer(client, path, chunk_len, &mut out)?;
    Ok((identity, out))
}

/// Read a full file by looping `ReadFile` windows and writing each window to
/// `out` as it arrives.
///
/// On the first chunk this captures `ClipIdentity`; every subsequent request
/// echoes it in `handle`. Any non-`ok` response aborts the copy.
///
/// # Errors
///
/// Returns [`ReadFileError`] on transport, framing, decode, writer failure, or
/// if scannerd reports `changed` / `not_found` / `out_of_range` / `error`.
pub fn read_full_file_to_writer(
    client: &dyn ReadFileClient,
    path: &str,
    chunk_len: u32,
    out: &mut dyn Write,
) -> Result<ClipIdentity, ReadFileError> {
    let mut offset = 0_u64;
    let mut handle: Option<ClipIdentity> = None;
    let req_len = chunk_len.min(MAX_READ_LEN);
    loop {
        let req = ReadFileRequest {
            path: path.to_owned(),
            offset,
            len: req_len,
            handle,
        };
        let window = client.read_file(&req)?;
        if let Some(expected) = handle {
            if expected != window.identity {
                return Err(ReadFileError::Changed);
            }
        } else {
            handle = Some(window.identity);
        }
        offset = offset.saturating_add(window.bytes.len() as u64);
        out.write_all(&window.bytes)?;
        if window.eof {
            let identity = handle.ok_or_else(|| {
                ReadFileError::Decode("missing identity on successful read".to_owned())
            })?;
            return Ok(identity);
        }
        if window.bytes.is_empty() {
            return Err(ReadFileError::Decode(
                "non-eof read returned zero bytes".to_owned(),
            ));
        }
    }
}

fn frame_cap_usize(cap: u32) -> Result<usize, ReadFileError> {
    usize::try_from(cap).map_err(|_| ReadFileError::Decode("frame cap overflow".to_owned()))
}

fn read_frame(stream: &mut impl Read, cap: u32) -> Result<Vec<u8>, ReadFileError> {
    let mut len_buf = [0_u8; 4];
    stream.read_exact(&mut len_buf)?;
    let len_u32 = u32::from_le_bytes(len_buf);
    let len = usize::try_from(len_u32)
        .map_err(|_| ReadFileError::Decode("frame length overflow".to_owned()))?;
    let cap_len = frame_cap_usize(cap)?;
    if len > cap_len {
        return Err(ReadFileError::FrameTooLarge { len, cap: cap_len });
    }
    let mut payload = vec![0_u8; len];
    stream.read_exact(&mut payload)?;
    Ok(payload)
}

fn write_frame(stream: &mut impl Write, payload: &[u8], cap: u32) -> Result<(), ReadFileError> {
    let cap_len = frame_cap_usize(cap)?;
    if payload.len() > cap_len {
        return Err(ReadFileError::FrameTooLarge {
            len: payload.len(),
            cap: cap_len,
        });
    }
    let len_u32 = u32::try_from(payload.len()).map_err(|_| ReadFileError::FrameTooLarge {
        len: payload.len(),
        cap: cap_len,
    })?;
    stream.write_all(&len_u32.to_le_bytes())?;
    stream.write_all(payload)?;
    stream.flush()?;
    Ok(())
}

fn validate_tail_len(len: u32, requested_len: u32) -> Result<(), ReadFileError> {
    let cap = MAX_READ_LEN.min(requested_len);
    if len > cap {
        return Err(ReadFileError::FrameTooLarge {
            len: usize::try_from(len)
                .map_err(|_| ReadFileError::Decode("tail length overflow".to_owned()))?,
            cap: usize::try_from(cap)
                .map_err(|_| ReadFileError::Decode("tail cap overflow".to_owned()))?,
        });
    }
    Ok(())
}

fn read_raw_tail(
    stream: &mut impl Read,
    expected_len: u32,
    requested_len: u32,
) -> Result<Vec<u8>, ReadFileError> {
    let mut len_buf = [0_u8; 4];
    stream.read_exact(&mut len_buf)?;
    let len = u32::from_le_bytes(len_buf);
    if len != expected_len {
        return Err(ReadFileError::Decode(format!(
            "raw tail length mismatch: header={expected_len} tail={len}"
        )));
    }
    validate_tail_len(len, requested_len)?;
    let usize_len = usize::try_from(len)
        .map_err(|_| ReadFileError::Decode("tail length overflow".to_owned()))?;
    let mut payload = vec![0_u8; usize_len];
    stream.read_exact(&mut payload)?;
    Ok(payload)
}

#[cfg(unix)]
// Reads can carry up to an 8 MiB window from contended microSD media; keep this
// client less aggressive than scannerd's 120s read / 30s write server limits.
const READ_TIMEOUT_SECS: u64 = 60;
#[cfg(unix)]
const WRITE_TIMEOUT_SECS: u64 = 10;

/// Live Unix-domain-socket `ReadFile` client.
#[cfg(unix)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnixReadFileClient {
    socket_path: std::path::PathBuf,
}

#[cfg(unix)]
impl UnixReadFileClient {
    /// Build a client that connects to `socket_path`.
    #[must_use]
    pub fn new(socket_path: impl Into<std::path::PathBuf>) -> Self {
        Self {
            socket_path: socket_path.into(),
        }
    }
}

#[cfg(unix)]
impl ReadFileClient for UnixReadFileClient {
    fn read_file(&self, req: &ReadFileRequest) -> Result<ReadFileOk, ReadFileError> {
        use std::os::unix::net::UnixStream;
        use std::time::Duration;

        let mut stream = UnixStream::connect(&self.socket_path)?;
        stream.set_read_timeout(Some(Duration::from_secs(READ_TIMEOUT_SECS)))?;
        stream.set_write_timeout(Some(Duration::from_secs(WRITE_TIMEOUT_SECS)))?;

        let payload =
            serde_json::to_vec(req).map_err(|err| ReadFileError::Decode(err.to_string()))?;
        write_frame(&mut stream, &payload, MAX_REQUEST_FRAME)?;
        let header_payload = read_frame(&mut stream, MAX_REQUEST_FRAME)?;
        let header: ReadFileHeader = serde_json::from_slice(&header_payload)
            .map_err(|err| ReadFileError::Decode(err.to_string()))?;
        match header {
            ReadFileHeader::Ok {
                identity,
                readable_size,
                eof,
                byte_len,
            } => {
                validate_tail_len(byte_len, req.len)?;
                let bytes = read_raw_tail(&mut stream, byte_len, req.len)?;
                Ok(ReadFileOk {
                    identity,
                    readable_size,
                    eof,
                    bytes,
                })
            }
            ReadFileHeader::Changed => Err(ReadFileError::Changed),
            ReadFileHeader::NotFound => Err(ReadFileError::NotFound),
            ReadFileHeader::OutOfRange => Err(ReadFileError::OutOfRange),
            ReadFileHeader::Error { message } => Err(ReadFileError::Server { message }),
        }
    }
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing,
    clippy::similar_names
)]
mod tests {
    use std::cell::RefCell;
    use std::collections::VecDeque;
    use std::io::Cursor;

    use super::{
        ClipIdentity, MAX_REQUEST_FRAME, ReadFileClient, ReadFileError, ReadFileHeader,
        ReadFileOk, ReadFileRequest, read_frame, read_full_file, read_full_file_to_writer,
        read_raw_tail, write_frame, MAX_READ_LEN,
    };

    #[test]
    fn read_file_wire_json_matches_adr_0004_fixtures() {
        let req = ReadFileRequest {
            path: "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4".to_owned(),
            offset: 0,
            len: 8_388_608,
            handle: None,
        };
        let req_json = serde_json::to_string(&req).expect("serialize request");
        assert_eq!(
            req_json,
            "{\"path\":\"TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4\",\"offset\":0,\"len\":8388608,\"handle\":null}"
        );

        let identity = ClipIdentity {
            first_cluster: 1234,
            total_size: 2_097_152,
            name_hash: 3_735_928_559,
        };
        let req2 = ReadFileRequest {
            path: "...".to_owned(),
            offset: 8_388_608,
            len: 8_388_608,
            handle: Some(identity),
        };
        let req2_json = serde_json::to_string(&req2).expect("serialize request with handle");
        assert_eq!(
            req2_json,
            "{\"path\":\"...\",\"offset\":8388608,\"len\":8388608,\"handle\":{\"first_cluster\":1234,\"total_size\":2097152,\"name_hash\":3735928559}}"
        );

        let ok = ReadFileHeader::Ok {
            identity,
            readable_size: 2_097_152,
            eof: true,
            byte_len: 1_048_576,
        };
        assert_eq!(
            serde_json::to_string(&ok).expect("serialize ok"),
            "{\"status\":\"ok\",\"identity\":{\"first_cluster\":1234,\"total_size\":2097152,\"name_hash\":3735928559},\"readable_size\":2097152,\"eof\":true,\"byte_len\":1048576}"
        );
        assert_eq!(
            serde_json::to_string(&ReadFileHeader::Changed).expect("serialize changed"),
            "{\"status\":\"changed\"}"
        );
        assert_eq!(
            serde_json::to_string(&ReadFileHeader::NotFound).expect("serialize not_found"),
            "{\"status\":\"not_found\"}"
        );
        assert_eq!(
            serde_json::to_string(&ReadFileHeader::OutOfRange).expect("serialize out_of_range"),
            "{\"status\":\"out_of_range\"}"
        );
        assert_eq!(
            serde_json::to_string(&ReadFileHeader::Error {
                message: "...".to_owned(),
            })
            .expect("serialize error"),
            "{\"status\":\"error\",\"message\":\"...\"}"
        );
    }

    struct FakeReadFileClient {
        responses: RefCell<VecDeque<Result<ReadFileOk, ReadFileError>>>,
        requests: RefCell<Vec<ReadFileRequest>>,
    }

    impl FakeReadFileClient {
        fn new(responses: Vec<Result<ReadFileOk, ReadFileError>>) -> Self {
            Self {
                responses: RefCell::new(responses.into()),
                requests: RefCell::new(Vec::new()),
            }
        }
    }

    impl ReadFileClient for FakeReadFileClient {
        fn read_file(&self, req: &ReadFileRequest) -> Result<ReadFileOk, ReadFileError> {
            self.requests.borrow_mut().push(req.clone());
            self.responses
                .borrow_mut()
                .pop_front()
                .unwrap_or_else(|| Err(ReadFileError::Decode("missing fake response".to_owned())))
        }
    }

    #[test]
    fn read_full_file_loops_chunks_and_echoes_identity() {
        let identity = ClipIdentity {
            first_cluster: 1,
            total_size: 7,
            name_hash: 9,
        };
        let client = FakeReadFileClient::new(vec![
            Ok(ReadFileOk {
                identity,
                readable_size: 7,
                eof: false,
                bytes: b"abc".to_vec(),
            }),
            Ok(ReadFileOk {
                identity,
                readable_size: 7,
                eof: true,
                bytes: b"defg".to_vec(),
            }),
        ]);

        let (got_identity, bytes) =
            read_full_file(&client, "TeslaCam/RecentClips/file.mp4", 4).expect("read full file");
        assert_eq!(got_identity, identity);
        assert_eq!(bytes, b"abcdefg");

        let requests = client.requests.borrow();
        assert_eq!(requests.len(), 2);
        assert!(requests[0].handle.is_none());
        assert_eq!(requests[1].handle, Some(identity));
        assert_eq!(requests[0].offset, 0);
        assert_eq!(requests[1].offset, 3);
    }

    #[test]
    fn read_full_file_returns_changed_error() {
        let client = FakeReadFileClient::new(vec![Err(ReadFileError::Changed)]);
        let err = read_full_file(&client, "TeslaCam/RecentClips/file.mp4", 4)
            .expect_err("changed must fail closed");
        assert!(matches!(err, ReadFileError::Changed));
    }

    #[test]
    fn read_full_file_to_writer_streams_windows_and_echoes_identity() {
        let identity = ClipIdentity {
            first_cluster: 1,
            total_size: 7,
            name_hash: 9,
        };
        let client = FakeReadFileClient::new(vec![
            Ok(ReadFileOk {
                identity,
                readable_size: 7,
                eof: false,
                bytes: b"abc".to_vec(),
            }),
            Ok(ReadFileOk {
                identity,
                readable_size: 7,
                eof: true,
                bytes: b"defg".to_vec(),
            }),
        ]);
        let mut out = Vec::new();
        let got = read_full_file_to_writer(&client, "TeslaCam/RecentClips/file.mp4", 4, &mut out)
            .expect("streaming read");
        assert_eq!(got, identity);
        assert_eq!(out, b"abcdefg");

        let requests = client.requests.borrow();
        assert_eq!(requests.len(), 2);
        assert!(requests[0].handle.is_none());
        assert_eq!(requests[1].handle, Some(identity));
        assert_eq!(requests[0].offset, 0);
        assert_eq!(requests[1].offset, 3);
    }

    #[test]
    fn read_raw_tail_rejects_oversized_advertised_len_before_body_read() {
        let oversized = MAX_READ_LEN.saturating_add(1);
        let mut frame = Vec::new();
        frame.extend_from_slice(&oversized.to_le_bytes());
        let mut cursor = Cursor::new(frame);
        let err = read_raw_tail(&mut cursor, oversized, MAX_READ_LEN)
            .expect_err("oversized header-advertised window must fail closed");
        assert!(matches!(
            err,
            ReadFileError::FrameTooLarge { len, cap }
            if len == usize::try_from(oversized).expect("u32 -> usize")
                && cap == usize::try_from(MAX_READ_LEN).expect("u32 -> usize")
        ));
        assert_eq!(cursor.position(), 4, "must fail before payload read");
    }

    #[test]
    fn frame_helpers_roundtrip_json_payload() {
        let req = ReadFileRequest {
            path: "p".to_owned(),
            offset: 1,
            len: 2,
            handle: None,
        };
        let payload = serde_json::to_vec(&req).expect("serialize");
        let mut frame = Vec::new();
        write_frame(&mut frame, &payload, MAX_REQUEST_FRAME).expect("write frame");
        let decoded = read_frame(&mut frame.as_slice(), MAX_REQUEST_FRAME).expect("read frame");
        assert_eq!(decoded, payload);
    }
}
