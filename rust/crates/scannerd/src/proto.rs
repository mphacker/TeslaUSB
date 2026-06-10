//! Wire protocol for the `scannerd serve` ↔ `indexd` client seam.
//!
//! `indexd` (the client) drives: it opens a persistent connection to
//! `scannerd` (the server), and once per scan cadence sends a [`Request`]
//! and reads back one length-prefixed [`ScanBatch`] frame of facts. This
//! module is the cfg-agnostic, host-testable core — framing + the request
//! type + the batch codec; the `UnixListener`/`UnixStream` plumbing that
//! uses it is Unix-only (the Pi target).
//!
//! Framing matches the `gadgetd` precedent: a 4-byte little-endian length
//! prefix followed by a JSON payload. Every frame is bounded by
//! [`MAX_FRAME`]; the [`ScanBatch`] itself additionally carries the
//! per-collection caps in [`crate::record`] that the consumer validates,
//! so a well-formed batch is always far under the frame ceiling and a
//! forged oversize frame is refused before allocation.

use std::io::{self, Read, Write};

use serde::{Deserialize, Serialize};

use crate::record::ScanBatch;

/// Maximum accepted frame size for a [`ScanBatch`] response. A realistic
/// per-pass batch (a handful of newly-stable clips, or a full resync replay
/// bounded by the [`crate::record`] caps) is well under this; the ceiling
/// is a denial-of-service guard so a forged length prefix cannot drive an
/// unbounded allocation on the 512 MiB Pi.
pub const MAX_FRAME: u32 = 64 * 1024 * 1024;

/// Maximum accepted frame size for a client→server [`Request`]. A
/// `Request::Scan` serializes to a few dozen bytes, so the server caps its
/// inbound frames far tighter than [`MAX_FRAME`]: a peer cannot force a
/// large allocation before the request even parses.
pub const MAX_REQUEST_FRAME: u32 = 64 * 1024;

/// A client→server request. `scannerd` only answers scan requests; it
/// holds no other state the client can mutate.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Request {
    /// Run one produce pass and stream the resulting batch back, stamped
    /// with `generation`. When `resync` is set the server re-arms every
    /// currently-stable clip so the batch replays the full present set
    /// (used by the client on first connect / after an apply failure to
    /// recover a batch lost before it was durably committed).
    Scan {
        /// Monotonic request id the server stamps onto the response batch.
        generation: u64,
        /// Replay all currently-stable clips, not just newly-eligible ones.
        #[serde(default)]
        resync: bool,
    },
}

/// Read a length-prefixed frame (4-byte LE length, then the payload).
///
/// # Errors
///
/// Returns an error if the stream ends early or the advertised length
/// exceeds `cap`.
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

/// Write a length-prefixed frame.
///
/// # Errors
///
/// Returns an error if the payload exceeds `u32` or the write fails.
pub fn write_frame(stream: &mut impl Write, payload: &[u8]) -> io::Result<()> {
    let len =
        u32::try_from(payload.len()).map_err(|_| io::Error::other("frame exceeds u32 length"))?;
    stream.write_all(&len.to_le_bytes())?;
    stream.write_all(payload)?;
    stream.flush()
}

/// Encode + frame a [`Request`].
///
/// # Errors
///
/// Returns an error if serialization or the write fails.
pub fn write_request(stream: &mut impl Write, request: &Request) -> io::Result<()> {
    let bytes = serde_json::to_vec(request).map_err(io::Error::other)?;
    write_frame(stream, &bytes)
}

/// Read + decode a [`Request`] (bounded by [`MAX_REQUEST_FRAME`]).
///
/// # Errors
///
/// Returns an error if the frame is oversize/torn or the JSON is invalid.
pub fn read_request(stream: &mut impl Read) -> io::Result<Request> {
    let payload = read_frame(stream, MAX_REQUEST_FRAME)?;
    serde_json::from_slice(&payload).map_err(io::Error::other)
}

/// Encode + frame a [`ScanBatch`].
///
/// The serialized payload is checked against [`MAX_FRAME`] *before* it is
/// written, so an over-cap batch fails loudly here on the producer side
/// rather than being sent as a frame the consumer would reject (which would
/// otherwise spin a reconnect loop).
///
/// # Errors
///
/// Returns an error if serialization fails, the payload exceeds
/// [`MAX_FRAME`], or the write fails.
pub fn write_batch(stream: &mut impl Write, batch: &ScanBatch) -> io::Result<()> {
    let bytes = serde_json::to_vec(batch).map_err(io::Error::other)?;
    if bytes.len() > MAX_FRAME as usize {
        return Err(io::Error::other(format!(
            "batch frame too large: {} > {MAX_FRAME}",
            bytes.len()
        )));
    }
    write_frame(stream, &bytes)
}

/// Read + decode a [`ScanBatch`] (bounded by [`MAX_FRAME`]). The caller
/// must still run [`ScanBatch::validate`](crate::record::ScanBatch::validate)
/// before trusting the contents.
///
/// # Errors
///
/// Returns an error if the frame is oversize/torn or the JSON is invalid.
pub fn read_batch(stream: &mut impl Read) -> io::Result<ScanBatch> {
    let payload = read_frame(stream, MAX_FRAME)?;
    serde_json::from_slice(&payload).map_err(io::Error::other)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use std::io::Cursor;

    use super::{
        MAX_FRAME, Request, read_batch, read_frame, read_request, write_batch, write_frame,
        write_request,
    };
    use crate::record::{PROTOCOL_VERSION, ProducerStats, ScanBatch};

    #[test]
    fn frame_roundtrips() {
        let mut buf = Vec::new();
        write_frame(&mut buf, b"hello").unwrap();
        let mut cur = Cursor::new(buf);
        assert_eq!(read_frame(&mut cur, MAX_FRAME).unwrap(), b"hello");
    }

    #[test]
    fn read_frame_rejects_oversize() {
        let mut buf = Vec::new();
        buf.extend_from_slice(&(MAX_FRAME + 1).to_le_bytes());
        let mut cur = Cursor::new(buf);
        assert!(read_frame(&mut cur, MAX_FRAME).is_err());
    }

    #[test]
    fn request_roundtrips() {
        for req in [
            Request::Scan {
                generation: 7,
                resync: false,
            },
            Request::Scan {
                generation: 9,
                resync: true,
            },
        ] {
            let mut buf = Vec::new();
            write_request(&mut buf, &req).unwrap();
            let mut cur = Cursor::new(buf);
            assert_eq!(read_request(&mut cur).unwrap(), req);
        }
    }

    #[test]
    fn request_resync_defaults_false() {
        let req: Request = serde_json::from_slice(br#"{"cmd":"scan","generation":3}"#).unwrap();
        assert_eq!(
            req,
            Request::Scan {
                generation: 3,
                resync: false
            }
        );
    }

    #[test]
    fn read_request_rejects_oversize_request_frame() {
        use super::MAX_REQUEST_FRAME;
        let mut buf = Vec::new();
        // A length prefix just over the request cap must be refused before
        // any payload is read/allocated.
        buf.extend_from_slice(&(MAX_REQUEST_FRAME + 1).to_le_bytes());
        let mut cur = Cursor::new(buf);
        assert!(read_request(&mut cur).is_err());
    }

    #[test]
    fn batch_roundtrips_over_a_stream() {
        let batch = ScanBatch {
            version: PROTOCOL_VERSION,
            generation: 11,
            complete: true,
            stats: ProducerStats::default(),
            present_keys: vec!["0:TeslaCam/SavedClips/x".to_owned()],
            records: Vec::new(),
            media: Vec::new(),
            media_present_paths: Vec::new(),
            media_inventory: false,
        };
        let mut buf = Vec::new();
        write_batch(&mut buf, &batch).unwrap();
        let mut cur = Cursor::new(buf);
        assert_eq!(read_batch(&mut cur).unwrap(), batch);
    }
}
