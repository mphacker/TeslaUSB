//! Wire protocol for `retentiond → indexd` archive registration.
//!
//! Frames are 4-byte little-endian length + JSON payload, mirroring
//! `scannerd::proto` and bounded to avoid oversized allocation.

use std::io::{self, Read, Write};

use serde::{Deserialize, Serialize};

/// Maximum accepted request frame for indexd control RPCs.
pub const MAX_REQUEST_FRAME: u32 = 64 * 1024;

/// Inbound control requests.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Request {
    /// Register one completed archive clip copy.
    RegisterArchivedClip(RegisterArchivedClip),
    /// Register one copied-but-undecodable archive clip as quarantined.
    // Deploy `indexd` before `retentiond`: older indexd must reject this
    // unknown verb so retentiond fails closed to pending, never force-publishing.
    RegisterQuarantinedArchive(RegisterArchivedClip),
    /// Set one settings preference value.
    SetPref {
        /// Preference key.
        key: String,
        /// Preference value.
        value: String,
    },
}

/// Archive registration payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RegisterArchivedClip {
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
    /// Archive unit metadata.
    pub archive: ArchiveUnit,
    /// Per-camera archive-backed angles.
    pub angles: Vec<ArchiveAngle>,
}

/// One durable archive item unit.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArchiveUnit {
    /// Deterministic archive-root-relative item path.
    pub path: String,
    /// Total bytes in the archive unit.
    pub size_bytes: i64,
    /// Number of files in the archive unit.
    pub file_count: i64,
    /// Archive completion epoch seconds.
    pub archived_at: i64,
}

/// One camera angle now backed by archive storage.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArchiveAngle {
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

/// Outbound RPC response.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum Response {
    /// Successful archive registration with ids.
    Ok {
        /// The clip row id.
        clip_id: i64,
        /// The archive item row id.
        archive_item_id: i64,
    },
    /// Request/handler failure.
    Error {
        /// Human-readable error message.
        message: String,
    },
    /// Deterministic request rejection: the payload is invalid and will never
    /// succeed on retry. Distinct from `Error` (operational/transient) so
    /// clients can avoid futile retries and poison-drops.
    Rejected {
        /// Human-readable rejection reason.
        message: String,
    },
    /// Preference write acknowledged.
    PrefSet {
        /// The updated preference key.
        key: String,
    },
}

/// Read one framed payload (4-byte LE length then bytes).
///
/// # Errors
///
/// Returns an error if the frame is torn or larger than `cap`.
pub fn read_frame(stream: &mut impl Read, cap: u32) -> io::Result<Vec<u8>> {
    let mut len_buf = [0u8; 4];
    stream.read_exact(&mut len_buf)?;
    let len = u32::from_le_bytes(len_buf);
    if len > cap {
        return Err(io::Error::other(format!("frame too large: {len} > {cap}")));
    }
    let mut payload = vec![0u8; len as usize];
    stream.read_exact(&mut payload)?;
    Ok(payload)
}

/// Write one framed payload (4-byte LE length then bytes).
///
/// # Errors
///
/// Returns an error if the payload cannot be framed or write fails.
pub fn write_frame(stream: &mut impl Write, payload: &[u8]) -> io::Result<()> {
    let len =
        u32::try_from(payload.len()).map_err(|_| io::Error::other("frame exceeds u32 length"))?;
    stream.write_all(&len.to_le_bytes())?;
    stream.write_all(payload)?;
    stream.flush()
}

/// Read and decode one [`Request`] frame.
///
/// # Errors
///
/// Returns an error on framing or JSON decode failures.
pub fn read_request(stream: &mut impl Read) -> io::Result<Request> {
    let payload = read_frame(stream, MAX_REQUEST_FRAME)?;
    serde_json::from_slice(&payload).map_err(io::Error::other)
}

/// Write one framed [`Response`].
///
/// # Errors
///
/// Returns an error on JSON encode or socket write failures.
pub fn write_response(stream: &mut impl Write, response: &Response) -> io::Result<()> {
    let payload = serde_json::to_vec(response).map_err(io::Error::other)?;
    write_frame(stream, &payload)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use std::io::Cursor;

    use super::{
        ArchiveAngle, ArchiveUnit, MAX_REQUEST_FRAME, RegisterArchivedClip, Request, Response,
        read_frame, read_request, write_frame, write_response,
    };

    #[test]
    fn request_roundtrip_frame_codec() {
        let req = Request::RegisterArchivedClip(RegisterArchivedClip {
            canonical_key: "slot0:TeslaCam/RecentClips/2026-06-19/2026-06-19_10-00-00".to_owned(),
            folder_class: "RecentClips".to_owned(),
            partition: "slot0".to_owned(),
            started_at: 1_718_805_600,
            ended_at: 1_718_805_660,
            duration_s: Some(60),
            archive: ArchiveUnit {
                path: "archive/2026-06-19/clip-001".to_owned(),
                size_bytes: 12_345,
                file_count: 4,
                archived_at: 1_718_805_700,
            },
            angles: vec![ArchiveAngle {
                camera: "front".to_owned(),
                file_ref: "archive/2026-06-19/clip-001/front.mp4".to_owned(),
                offset_ms: 0,
                duration_s: Some(60),
                size_bytes: 3_086,
            }],
        });

        let mut buf = Vec::new();
        let json = serde_json::to_vec(&req).unwrap();
        write_frame(&mut buf, &json).unwrap();
        let mut cur = Cursor::new(buf);
        let decoded = read_request(&mut cur).unwrap();
        assert_eq!(decoded, req);
    }

    #[test]
    fn quarantined_request_roundtrip_frame_codec() {
        let req = Request::RegisterQuarantinedArchive(RegisterArchivedClip {
            canonical_key: "slot0:TeslaCam/RecentClips/2026-06-19/2026-06-19_10-00-00".to_owned(),
            folder_class: "RecentClips".to_owned(),
            partition: "slot0".to_owned(),
            started_at: 1_718_805_600,
            ended_at: 1_718_805_660,
            duration_s: Some(60),
            archive: ArchiveUnit {
                path: "archive/2026-06-19/clip-001".to_owned(),
                size_bytes: 12_345,
                file_count: 4,
                archived_at: 1_718_805_700,
            },
            angles: vec![ArchiveAngle {
                camera: "front".to_owned(),
                file_ref: "archive/2026-06-19/clip-001/front.mp4".to_owned(),
                offset_ms: 0,
                duration_s: Some(60),
                size_bytes: 3_086,
            }],
        });

        let mut buf = Vec::new();
        let json = serde_json::to_vec(&req).unwrap();
        write_frame(&mut buf, &json).unwrap();
        let mut cur = Cursor::new(buf);
        let decoded = read_request(&mut cur).unwrap();
        assert_eq!(decoded, req);
    }

    #[test]
    fn read_frame_rejects_oversize() {
        let mut buf = Vec::new();
        buf.extend_from_slice(&(MAX_REQUEST_FRAME + 1).to_le_bytes());
        let mut cur = Cursor::new(buf);
        assert!(read_frame(&mut cur, MAX_REQUEST_FRAME).is_err());
    }

    #[test]
    fn response_roundtrip_frame_codec() {
        let response = Response::Ok {
            clip_id: 7,
            archive_item_id: 11,
        };
        let mut buf = Vec::new();
        write_response(&mut buf, &response).unwrap();
        let mut cur = Cursor::new(buf);
        let payload = read_frame(&mut cur, MAX_REQUEST_FRAME).unwrap();
        let decoded: Response = serde_json::from_slice(&payload).unwrap();
        assert_eq!(decoded, response);
    }

    #[test]
    fn request_set_pref_serializes_with_set_pref_cmd() {
        let request = Request::SetPref {
            key: "speed_unit".to_owned(),
            value: "kph".to_owned(),
        };
        let encoded = serde_json::to_value(&request).unwrap();
        assert_eq!(
            encoded.get("cmd").and_then(serde_json::Value::as_str),
            Some("set_pref")
        );
        let decoded: Request = serde_json::from_value(encoded).unwrap();
        assert_eq!(decoded, request);
    }

    #[test]
    fn response_pref_set_serializes_with_pref_set_status() {
        let response = Response::PrefSet {
            key: "speed_unit".to_owned(),
        };
        let encoded = serde_json::to_value(&response).unwrap();
        assert_eq!(
            encoded.get("status").and_then(serde_json::Value::as_str),
            Some("pref_set")
        );
        let decoded: Response = serde_json::from_value(encoded).unwrap();
        assert_eq!(decoded, response);
    }

    #[test]
    fn response_rejected_serializes_with_rejected_status() {
        let response = Response::Rejected {
            message: "invalid camera: left_pillar".to_owned(),
        };
        let encoded = serde_json::to_value(&response).unwrap();
        assert_eq!(
            encoded.get("status").and_then(serde_json::Value::as_str),
            Some("rejected")
        );
        let decoded: Response = serde_json::from_value(encoded).unwrap();
        assert_eq!(decoded, response);
    }
}
