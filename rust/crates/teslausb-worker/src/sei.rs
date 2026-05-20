//! Tesla SEI walker — Phase 4b.1z.
//!
//! This module is the I/O adapter for the pure-logic SEI
//! primitives in [`teslausb_core::sei`]. It reads a Tesla
//! dashcam MP4 file from disk, drives the
//! `mp4` → `nal` → `payload` → `tesla` pipeline against the
//! file's bytes, and yields per-frame
//! [`teslausb_core::sei::tesla::SeiMessage`] waypoints with
//! frame-accurate ms timestamps.
//!
//! ## Why `std::fs::read` instead of `mmap`
//!
//! v1's Python parser used `mmap.mmap(ACCESS_READ)` because the
//! original implementation loaded the entire clip into RAM as a
//! `bytes` object, and multiple concurrent indexer/archive
//! operations could OOM the Pi Zero 2 W (512 MB) by stacking
//! 30-80 MB clip buffers.
//!
//! The B-1 worker walks clips strictly **one at a time** (the
//! supervisor enforces concurrency=1), so the worst case is one
//! ≤150 MB clip resident at a time — well within the Pi's RAM
//! budget. `std::fs::read` is:
//!
//! * **No new dependency.** Adding `memmap2` (the obvious Rust
//!   equivalent) would trigger an ADR per charter and pull in
//!   an `unsafe` dep whose `Mmap` deref-to-slice safety relies
//!   on the file not being unmapped or truncated underneath us
//!   — which is *exactly* what could happen if `teslafat`
//!   reflows the backing store mid-walk.
//! * **Simpler.** Charter "no shortcuts" cuts both ways: mmap
//!   here is a perf optimisation, not a correctness requirement.
//!
//! If profiling on hardware shows this is a bottleneck the
//! `walk_clip` signature will not change — we will swap the
//! implementation behind an ADR and keep the call sites stable.
//!
//! ## Walker algorithm (parity with v1 `extract_sei_messages`)
//!
//! 1. Read file (capped at [`MAX_CLIP_BYTES`]).
//! 2. Find `moov.trak.mdia.mdhd` → timescale.
//! 3. Find `moov.trak.mdia.minf.stbl.stts` → per-sample
//!    durations in ms (via
//!    [`teslausb_core::sei::mp4::parse_stts_durations`]).
//! 4. Find `mvhd` → authoritative UTC start-of-recording time
//!    (optional — clips without a parseable mvhd still index,
//!    just without an absolute base timestamp).
//! 5. Find `mdat` → drive [`teslausb_core::sei::nal::AvccIter`]
//!    over the contents:
//!    * On SEI NAL (type 6): if `frame_index % sample_rate == 0`,
//!      try [`teslausb_core::sei::payload::extract_tesla_payload`]
//!      then [`teslausb_core::sei::tesla::decode_sei_message`].
//!      A SEI that is not Tesla-shaped (no `0x42` padding, no
//!      `0x69` marker, etc.) is silently skipped — exactly
//!      like v1's `if sei is not None`.
//!    * On VCL NAL (type 1 / type 5): advance `frame_index` and
//!      `cumulative_time_ms`.
//!
//! Errors that prevent any walk (file too big, no `mdat`, etc.)
//! return [`WalkError`]; errors mid-walk (corrupt NAL, decode
//! failure on one SEI) are silently swallowed so a single bad
//! frame doesn't lose the rest of the clip.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use teslausb_core::sei::mp4::{
    self, BoxRef, Mp4Error, find_box, parse_mdhd, parse_mvhd, parse_stts_durations,
};
use teslausb_core::sei::nal::{AvccIter, NalUnit};
use teslausb_core::sei::payload::extract_tesla_payload;
use teslausb_core::sei::tesla::{SeiMessage, decode_sei_message};

/// Hard cap on clip size we will read into RAM. v1 enforces the
/// same 150 MB ceiling (`max_file_size = 150 * 1024 * 1024`).
/// Tesla clips top out around 60 MB for a 60 s sentry clip; 150 MB
/// is "leave us headroom but reject anything pathological".
pub const MAX_CLIP_BYTES: u64 = 150 * 1024 * 1024;

/// Minimum bytes for a file to even be considered an MP4
/// candidate (one box header).
pub const MIN_CLIP_BYTES: u64 = 8;

/// Default per-sample duration in ms when the `stts` table is
/// missing, truncated, or shorter than the number of frames in
/// `mdat`. 33.33 ms ≈ 30 fps — Tesla dashcam's standard rate.
/// Matches v1's `default_duration_ms = 33.33`.
pub const DEFAULT_FRAME_DURATION_MS: f64 = 33.333_333_333_333_336_f64;

/// One waypoint yielded by [`walk_clip`].
///
/// A waypoint is a parsed [`SeiMessage`] plus the timing info the
/// indexer needs to build absolute timestamps without going back
/// to the file.
#[derive(Debug, Clone, PartialEq)]
pub struct Waypoint {
    /// Monotonic frame index within the clip, 0-based. Matches
    /// v1's `frame_index`.
    pub frame_index: u32,
    /// Milliseconds since the start of the clip. Computed from
    /// the cumulative `stts` deltas of every preceding frame.
    /// Matches v1's `timestamp_ms`.
    pub timestamp_ms: f64,
    /// The Tesla telemetry decoded from the SEI's protobuf
    /// payload.
    pub message: SeiMessage,
}

/// Summary of a successful clip walk.
#[derive(Debug, Clone, PartialEq)]
pub struct ClipWalk {
    /// Absolute UTC start-of-recording time from the clip's
    /// `mvhd` box, when available. `None` if the box is missing
    /// or its `creation_time` is zero / pre-Unix-epoch (v1
    /// rejects those as uninitialised firmware glitches).
    pub clip_started_utc: Option<SystemTime>,
    /// Track timescale (units per second) from the `mdhd` box.
    /// Used by the indexer to convert frame indices back to
    /// times if the caller didn't capture every waypoint.
    pub timescale: u32,
    /// Number of frames the walker advanced past in `mdat`
    /// (sum of NAL types 1 + 5). NOT the number of waypoints
    /// — `waypoints.len()` is bounded by `sample_rate`.
    pub frame_count: u32,
    /// One [`Waypoint`] per sampled frame whose SEI decoded
    /// cleanly. Always in frame order.
    pub waypoints: Vec<Waypoint>,
}

/// Errors that prevent a clip from being walked at all. Mid-walk
/// errors (one bad NAL, one undecodeable SEI) are silently
/// dropped — the walker yields whatever it could decode, matching
/// v1's `if sei is not None` filter.
#[derive(Debug, thiserror::Error)]
pub enum WalkError {
    /// I/O error opening or reading the file.
    #[error("I/O error reading {path:?}: {source}")]
    Io {
        /// Path that triggered the error.
        path: PathBuf,
        /// Underlying I/O error.
        #[source]
        source: io::Error,
    },
    /// File is smaller than [`MIN_CLIP_BYTES`] — cannot possibly
    /// be a valid MP4.
    #[error("file too small to be an MP4: {actual} byte(s) < {min}")]
    FileTooSmall {
        /// Actual size in bytes.
        actual: u64,
        /// Minimum required ([`MIN_CLIP_BYTES`]).
        min: u64,
    },
    /// File is larger than [`MAX_CLIP_BYTES`] — refused to bound
    /// RSS on the Pi Zero 2 W.
    #[error("file too large: {actual} byte(s) > {max} byte cap")]
    FileTooLarge {
        /// Actual size in bytes.
        actual: u64,
        /// Configured ceiling ([`MAX_CLIP_BYTES`]).
        max: u64,
    },
    /// MP4 structural error: a required box is missing or
    /// malformed (other than the timing tables, which fall back
    /// to defaults).
    #[error("MP4 structure error: {0}")]
    Mp4(#[source] Mp4Error),
}

impl From<Mp4Error> for WalkError {
    fn from(err: Mp4Error) -> Self {
        Self::Mp4(err)
    }
}

/// Walk a Tesla dashcam MP4 clip and return its SEI waypoints.
///
/// `sample_rate` controls how aggressively SEI frames are sampled:
/// `1` decodes every SEI NAL (~30 waypoints/sec on Tesla footage);
/// `30` decodes ~1 waypoint per second — the indexer's default
/// for route mapping. `0` is treated as `1`.
///
/// # Errors
///
/// Returns [`WalkError`] only on structural failure (I/O, size
/// bounds, missing `mdat`). Per-frame decode failures (corrupt
/// NAL, undecodeable SEI, garbled protobuf) are silently skipped
/// — the walker yields what it could.
pub fn walk_clip<P: AsRef<Path>>(path: P, sample_rate: u32) -> Result<ClipWalk, WalkError> {
    let path_ref = path.as_ref();
    let metadata = fs::metadata(path_ref).map_err(|e| WalkError::Io {
        path: path_ref.to_path_buf(),
        source: e,
    })?;
    let size = metadata.len();
    if size < MIN_CLIP_BYTES {
        return Err(WalkError::FileTooSmall {
            actual: size,
            min: MIN_CLIP_BYTES,
        });
    }
    if size > MAX_CLIP_BYTES {
        return Err(WalkError::FileTooLarge {
            actual: size,
            max: MAX_CLIP_BYTES,
        });
    }
    let buf = fs::read(path_ref).map_err(|e| WalkError::Io {
        path: path_ref.to_path_buf(),
        source: e,
    })?;
    walk_clip_bytes(&buf, sample_rate)
}

/// Walk a Tesla dashcam MP4 clip held in memory.
///
/// Same semantics as [`walk_clip`] but takes pre-loaded bytes —
/// useful for tests and for callers that already have the file
/// in RAM (e.g. an indexer batch that's reading metadata for
/// several clips out of one cached page).
///
/// # Errors
///
/// Returns [`WalkError`] on structural failure. The two size
/// guards are skipped (the caller already chose to hold the
/// bytes); only [`WalkError::Mp4`] is reachable from this entry.
pub fn walk_clip_bytes(buf: &[u8], sample_rate: u32) -> Result<ClipWalk, WalkError> {
    let sample_stride = sample_rate.max(1);

    // mvhd is best-effort: a missing or pre-epoch mvhd is a
    // recoverable degraded state, not a walk-killer. v1 only
    // uses mvhd for `extract_mvhd_creation_time`, never for the
    // SEI walk itself.
    let clip_started_utc = read_clip_started_utc(buf);

    // Required boxes for timing + content. mdat is the only one
    // that's a true blocker — without it there are no NAL units
    // to walk.
    let (timescale, durations) = read_track_timing(buf).unwrap_or((30_000, Vec::new()));
    let mdat = find_box(buf, 0, buf.len(), b"mdat")?;
    let mdat_body = mdat.body(buf);

    let mut waypoints = Vec::new();
    let mut frame_index: u32 = 0;
    let mut cumulative_time_ms: f64 = 0.0;

    for nal in AvccIter::new(mdat_body) {
        let Ok(NalUnit { nal_type, payload }) = nal else {
            // Truncated / overrun NAL — v1 `break`s. We do the
            // same: yield whatever we already accumulated rather
            // than losing the whole clip.
            break;
        };
        if nal_type == NalUnit::NAL_TYPE_SEI {
            if frame_index % sample_stride == 0 {
                if let Some(msg) = decode_sei_payload(payload) {
                    let timestamp_ms = cumulative_time_ms;
                    waypoints.push(Waypoint {
                        frame_index,
                        timestamp_ms,
                        message: msg,
                    });
                }
            }
        } else if matches!(
            nal_type,
            NalUnit::NAL_TYPE_IDR | NalUnit::NAL_TYPE_NON_IDR_SLICE
        ) {
            // v1 advances time/index on either keyframe (5) or
            // non-IDR slice (1). Other NAL types (SPS=7, PPS=8,
            // AUD=9, etc.) are not frame boundaries.
            let dur_ms = durations
                .get(frame_index as usize)
                .copied()
                .unwrap_or(DEFAULT_FRAME_DURATION_MS);
            cumulative_time_ms += dur_ms;
            frame_index = frame_index.saturating_add(1);
        }
    }

    Ok(ClipWalk {
        clip_started_utc,
        timescale,
        frame_count: frame_index,
        waypoints,
    })
}

// --- internal helpers ---

/// Resolve `moov.mvhd` and parse its `creation_time` into a
/// [`SystemTime`]. Returns `None` on any failure — every reason
/// to fail here is a recoverable degraded state, not a walker
/// stop condition.
fn read_clip_started_utc(buf: &[u8]) -> Option<SystemTime> {
    let moov = find_box(buf, 0, buf.len(), b"moov").ok()?;
    let mvhd = find_box(buf, moov.start, moov.end, b"mvhd").ok()?;
    let mvhd_parsed = parse_mvhd(mvhd.body(buf)).ok()?;
    Some(mvhd_parsed.creation_time)
}

/// Resolve the video track's timescale and per-frame durations
/// from `moov.trak.mdia.{mdhd, minf.stbl.stts}`.
///
/// Tesla dashcam clips have exactly one video trak so we don't
/// need to disambiguate by handler type — the first `trak` is the
/// one. Returns `None` on any structural miss so the walker can
/// fall back to defaults rather than refusing the whole clip.
fn read_track_timing(buf: &[u8]) -> Option<(u32, Vec<f64>)> {
    let moov = find_box(buf, 0, buf.len(), b"moov").ok()?;
    let trak = find_box(buf, moov.start, moov.end, b"trak").ok()?;
    let mdia = find_box(buf, trak.start, trak.end, b"mdia").ok()?;
    let mdhd_body = find_box_body(buf, &mdia, *b"mdhd")?;
    let mdhd = parse_mdhd(mdhd_body).ok()?;
    let minf = find_box(buf, mdia.start, mdia.end, b"minf").ok()?;
    let stbl = find_box(buf, minf.start, minf.end, b"stbl").ok()?;
    let stts_body = find_box_body(buf, &stbl, *b"stts")?;
    let durations = parse_stts_durations(stts_body, mdhd.timescale).ok()?;
    Some((mdhd.timescale, durations))
}

/// Small helper to find a child box and immediately borrow its
/// body slice. Keeps the timing helper a single linear chain of
/// `?` operators.
fn find_box_body<'b>(buf: &'b [u8], parent: &BoxRef, name: [u8; 4]) -> Option<&'b [u8]> {
    let child = find_box(buf, parent.start, parent.end, &name).ok()?;
    Some(child.body(buf))
}

/// Run the SEI payload pipeline against one NAL unit's full
/// bytes. Returns `Some` only if the NAL is a Tesla-shaped SEI
/// AND its protobuf decodes — v1 returns `None` on any failure
/// and we mirror that.
fn decode_sei_payload(nal_payload: &[u8]) -> Option<SeiMessage> {
    let payload = extract_tesla_payload(nal_payload).ok()?;
    decode_sei_message(&payload).ok()
}

// Re-export the Mp4Error type so callers can match on it without
// reaching into teslausb-core's module tree. Pure ergonomics.
pub use mp4::Mp4Error as ReexportedMp4Error;

#[cfg(test)]
mod tests {
    #![allow(
        clippy::expect_used,
        clippy::indexing_slicing,
        clippy::panic,
        clippy::unwrap_used,
        clippy::cast_possible_truncation,
        clippy::cast_lossless,
        clippy::float_cmp,
        clippy::doc_markdown
    )]

    use super::*;
    use std::io::Write;
    use teslausb_core::sei::payload::{
        RBSP_TRAILING_BYTE, TESLA_PADDING_BYTE, TESLA_PROTOBUF_MARKER,
    };

    // ---- Box / fixture builders ----

    fn mk_box(name: [u8; 4], body: &[u8]) -> Vec<u8> {
        let size = u32::try_from(8 + body.len()).unwrap();
        let mut v = Vec::with_capacity(8 + body.len());
        v.extend_from_slice(&size.to_be_bytes());
        v.extend_from_slice(&name);
        v.extend_from_slice(body);
        v
    }

    fn mvhd_v0_body(creation_time_raw: u32) -> Vec<u8> {
        let mut v = vec![0u8; 100];
        v[0] = 0;
        v[4..8].copy_from_slice(&creation_time_raw.to_be_bytes());
        v
    }

    fn mdhd_v0_body(timescale: u32) -> Vec<u8> {
        let mut v = vec![0u8; 24];
        v[0] = 0;
        v[12..16].copy_from_slice(&timescale.to_be_bytes());
        v
    }

    fn stts_body_one_entry(count: u32, delta: u32) -> Vec<u8> {
        let mut v = vec![0u8; 16];
        v[4..8].copy_from_slice(&1u32.to_be_bytes());
        v[8..12].copy_from_slice(&count.to_be_bytes());
        v[12..16].copy_from_slice(&delta.to_be_bytes());
        v
    }

    /// Build a Tesla-shaped SEI NAL payload (without the 4-byte
    /// AVCC length prefix). The minimum valid form per
    /// `teslausb_core::sei::payload::extract_tesla_payload` is:
    ///
    ///   `[nal_hdr=0x06]` (SEI NAL header, type 6)
    ///   `[payload_type=5]` (SEI user_data_unregistered)
    ///   one or more `0x42` padding bytes
    ///   `[0x69]` (Tesla protobuf marker)
    ///   `[protobuf bytes …]`
    ///   `[0x80]` (RBSP trailing)
    /// Build a Tesla-shaped SEI NAL payload (without the 4-byte
    /// AVCC length prefix). The minimum valid form per
    /// `teslausb_core::sei::payload::extract_tesla_payload` is:
    ///
    ///   index 0: `0x06` (NAL header, type 6 = SEI)
    ///   index 1: `0x05` (SEI payload_type = user_data_unregistered)
    ///   index 2: payload_size byte (extract_tesla_payload skips this)
    ///   index 3+: one or more `0x42` padding bytes
    ///            then `0x69` (Tesla protobuf marker)
    ///            then protobuf bytes
    ///            then `0x80` (RBSP trailing)
    fn build_sei_nal(protobuf_payload: &[u8]) -> Vec<u8> {
        let mut v = Vec::new();
        v.push(0x06); // NAL header: type 6 (SEI), nal_ref_idc=0
        v.push(5u8); // SEI payload_type = user_data_unregistered (5)
        // payload_size byte: declared body length after this byte.
        // extract_tesla_payload starts at index 3 regardless of this
        // value (v1 parity), but we set it sensibly for any caller
        // using parse_h264_sei_envelope.
        let body_len = 1 /* 0x42 */ + 1 /* 0x69 */ + protobuf_payload.len() + 1 /* 0x80 */;
        v.push(u8::try_from(body_len).unwrap_or(255));
        v.push(TESLA_PADDING_BYTE); // one 0x42 padding byte
        v.push(TESLA_PROTOBUF_MARKER);
        v.extend_from_slice(protobuf_payload);
        v.push(RBSP_TRAILING_BYTE);
        v
    }

    /// Build a VCL (frame boundary) NAL. nal_type=1 is non-IDR
    /// slice, sufficient for the walker's frame counter.
    fn build_vcl_nal() -> Vec<u8> {
        // 4 bytes minimum so length prefix + payload look real;
        // walker only inspects byte 0 for the type nibble.
        vec![0x41, 0x00, 0x00, 0x00] // nal_type = 1
    }

    /// Encode a single protobuf field 4 (vehicle_speed_mps,
    /// wire type 5 / fixed32) holding the given f32. Useful for
    /// asserting the walker really did decode the protobuf, not
    /// just route bytes through.
    fn encode_speed_mps(speed: f32) -> Vec<u8> {
        let mut v = Vec::with_capacity(5);
        v.push(0x25); // tag: field 4, wire 5
        v.extend_from_slice(&speed.to_le_bytes());
        v
    }

    fn build_avcc(units: &[Vec<u8>]) -> Vec<u8> {
        let mut v = Vec::new();
        for u in units {
            let len = u32::try_from(u.len()).unwrap();
            v.extend_from_slice(&len.to_be_bytes());
            v.extend_from_slice(u);
        }
        v
    }

    fn build_clip(
        with_mvhd: bool,
        with_timing: bool,
        nal_units: &[Vec<u8>],
        creation_time_raw: u32,
    ) -> Vec<u8> {
        let mut moov_body = Vec::new();
        if with_mvhd {
            moov_body.extend_from_slice(&mk_box(*b"mvhd", &mvhd_v0_body(creation_time_raw)));
        }
        if with_timing {
            let mut trak_body = Vec::new();
            let mut mdia_body = Vec::new();
            mdia_body.extend_from_slice(&mk_box(*b"mdhd", &mdhd_v0_body(30_000)));
            let mut minf_body = Vec::new();
            let mut stbl_body = Vec::new();
            stbl_body.extend_from_slice(&mk_box(*b"stts", &stts_body_one_entry(1800, 1000)));
            minf_body.extend_from_slice(&mk_box(*b"stbl", &stbl_body));
            mdia_body.extend_from_slice(&mk_box(*b"minf", &minf_body));
            trak_body.extend_from_slice(&mk_box(*b"mdia", &mdia_body));
            moov_body.extend_from_slice(&mk_box(*b"trak", &trak_body));
        }

        let mut clip = Vec::new();
        clip.extend_from_slice(&mk_box(*b"ftyp", b"isom"));
        clip.extend_from_slice(&mk_box(*b"moov", &moov_body));
        let mdat_body = build_avcc(nal_units);
        clip.extend_from_slice(&mk_box(*b"mdat", &mdat_body));
        clip
    }

    // ---- Walker behaviour ----

    #[test]
    fn walks_single_sei_clip_and_returns_one_waypoint() {
        let sei = build_sei_nal(&encode_speed_mps(15.5));
        let clip = build_clip(true, true, &[sei.clone(), build_vcl_nal()], 2_082_844_900);
        let result = walk_clip_bytes(&clip, 1).unwrap();
        assert_eq!(result.waypoints.len(), 1);
        assert_eq!(result.frame_count, 1);
        assert!((result.waypoints[0].message.vehicle_speed_mps - 15.5).abs() < 1e-6);
        assert_eq!(result.waypoints[0].frame_index, 0);
        assert_eq!(result.waypoints[0].timestamp_ms, 0.0);
        assert!(result.clip_started_utc.is_some());
    }

    #[test]
    fn frame_counter_advances_on_each_vcl_nal() {
        // SEI / VCL / SEI / VCL / SEI / VCL
        let s = build_sei_nal(&encode_speed_mps(0.0));
        let v = build_vcl_nal();
        let clip = build_clip(
            true,
            true,
            &[s.clone(), v.clone(), s.clone(), v.clone(), s.clone(), v],
            2_082_844_900,
        );
        let result = walk_clip_bytes(&clip, 1).unwrap();
        assert_eq!(result.waypoints.len(), 3);
        assert_eq!(result.frame_count, 3);
        assert_eq!(result.waypoints[0].frame_index, 0);
        assert_eq!(result.waypoints[1].frame_index, 1);
        assert_eq!(result.waypoints[2].frame_index, 2);
        // 1000 / 30000 * 1000 = 33.333… ms per frame
        assert!((result.waypoints[1].timestamp_ms - 33.333).abs() < 0.01);
        assert!((result.waypoints[2].timestamp_ms - 66.666).abs() < 0.01);
    }

    #[test]
    fn sample_rate_decimates_waypoints() {
        // SEI before every frame; sample_rate = 2 → keep frames 0 and 2.
        let s = || build_sei_nal(&encode_speed_mps(0.0));
        let v = build_vcl_nal;
        let units = vec![s(), v(), s(), v(), s(), v(), s(), v()];
        let result = walk_clip_bytes(&build_clip(true, true, &units, 2_082_844_900), 2).unwrap();
        assert_eq!(result.frame_count, 4);
        assert_eq!(result.waypoints.len(), 2);
        assert_eq!(result.waypoints[0].frame_index, 0);
        assert_eq!(result.waypoints[1].frame_index, 2);
    }

    #[test]
    fn sample_rate_zero_treated_as_one() {
        let s = build_sei_nal(&encode_speed_mps(0.0));
        let v = build_vcl_nal();
        let clip = build_clip(true, true, &[s, v], 2_082_844_900);
        let result = walk_clip_bytes(&clip, 0).unwrap();
        assert_eq!(result.waypoints.len(), 1);
    }

    #[test]
    fn non_tesla_sei_is_silently_skipped() {
        // A SEI NAL that fails extract_tesla_payload (no 0x42 padding).
        let mut bad_sei = vec![0x06, 0x05]; // SEI NAL hdr + payload_type
        bad_sei.push(0x99); // not 0x42
        bad_sei.push(0x80); // RBSP trailing
        let good_sei = build_sei_nal(&encode_speed_mps(7.5));
        let v = build_vcl_nal();
        let clip = build_clip(
            true,
            true,
            &[bad_sei, v.clone(), good_sei, v],
            2_082_844_900,
        );
        let result = walk_clip_bytes(&clip, 1).unwrap();
        // Bad SEI dropped; good SEI kept.
        assert_eq!(result.waypoints.len(), 1);
        assert!((result.waypoints[0].message.vehicle_speed_mps - 7.5).abs() < 1e-6);
        // Both VCLs still advanced the frame counter.
        assert_eq!(result.frame_count, 2);
    }

    #[test]
    fn missing_mvhd_falls_back_to_none_but_still_walks() {
        let s = build_sei_nal(&encode_speed_mps(3.0));
        let v = build_vcl_nal();
        let clip = build_clip(false, true, &[s, v], 0);
        let result = walk_clip_bytes(&clip, 1).unwrap();
        assert!(result.clip_started_utc.is_none());
        assert_eq!(result.waypoints.len(), 1);
    }

    #[test]
    fn missing_timing_uses_default_frame_duration() {
        let s = build_sei_nal(&encode_speed_mps(0.0));
        let v = build_vcl_nal();
        let clip = build_clip(true, false, &[s.clone(), v.clone(), s, v], 2_082_844_900);
        let result = walk_clip_bytes(&clip, 1).unwrap();
        // Timescale falls back to 30000 default per `read_track_timing`'s
        // unwrap_or, so cumulative_time_ms is computed against an empty
        // durations vec → DEFAULT_FRAME_DURATION_MS per frame.
        assert!((result.waypoints[1].timestamp_ms - DEFAULT_FRAME_DURATION_MS).abs() < 1e-6);
    }

    #[test]
    fn missing_mdat_is_walk_error() {
        let mut clip = Vec::new();
        clip.extend_from_slice(&mk_box(*b"ftyp", b"isom"));
        // no moov, no mdat
        match walk_clip_bytes(&clip, 1) {
            Err(WalkError::Mp4(Mp4Error::BoxNotFound(name))) => {
                assert_eq!(&name, b"mdat");
            }
            other => panic!("expected Mp4(BoxNotFound mdat), got {other:?}"),
        }
    }

    #[test]
    fn truncated_nal_stops_iteration_without_walk_error() {
        // Build a real mdat then snip the last few bytes off so
        // the final NAL length prefix overruns the buffer.
        let s = build_sei_nal(&encode_speed_mps(1.0));
        let v = build_vcl_nal();
        let mut clip = build_clip(
            true,
            true,
            &[s.clone(), v.clone(), s.clone(), v],
            2_082_844_900,
        );
        // Lop off the trailing 6 bytes — mid-NAL truncation.
        clip.truncate(clip.len() - 6);
        let result = walk_clip_bytes(&clip, 1).unwrap();
        // First SEI / first VCL definitely processed; subsequent
        // walk stopped early. We don't care exactly how many — only
        // that it didn't propagate as a WalkError.
        assert!(!result.waypoints.is_empty());
    }

    #[test]
    fn no_vcl_nals_means_zero_frame_count() {
        let s = build_sei_nal(&encode_speed_mps(0.0));
        let clip = build_clip(true, true, &[s.clone(), s], 2_082_844_900);
        let result = walk_clip_bytes(&clip, 1).unwrap();
        // Both SEIs share frame_index 0 because the counter never
        // advances. Sample_rate=1, 0 % 1 == 0 for both.
        assert_eq!(result.frame_count, 0);
        assert_eq!(result.waypoints.len(), 2);
        assert!(
            result
                .waypoints
                .iter()
                .all(|w| w.frame_index == 0 && w.timestamp_ms == 0.0)
        );
    }

    #[test]
    fn idr_slice_advances_frame_counter_same_as_non_idr() {
        // nal_type = 5 (IDR keyframe)
        let idr = vec![0x65, 0, 0, 0];
        let s = build_sei_nal(&encode_speed_mps(0.0));
        let clip = build_clip(true, true, &[s.clone(), idr, s], 2_082_844_900);
        let result = walk_clip_bytes(&clip, 1).unwrap();
        assert_eq!(result.frame_count, 1);
        assert_eq!(result.waypoints[1].frame_index, 1);
    }

    // ---- Top-level walk_clip (with real tempfile I/O) ----

    #[test]
    fn walk_clip_reads_file_from_disk() {
        let s = build_sei_nal(&encode_speed_mps(42.0));
        let v = build_vcl_nal();
        let clip = build_clip(true, true, &[s, v], 2_082_844_900);
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.mp4");
        let mut f = fs::File::create(&path).unwrap();
        f.write_all(&clip).unwrap();
        drop(f);
        let result = walk_clip(&path, 1).unwrap();
        assert_eq!(result.waypoints.len(), 1);
        assert!((result.waypoints[0].message.vehicle_speed_mps - 42.0).abs() < 1e-6);
    }

    #[test]
    fn walk_clip_rejects_missing_file() {
        let err = walk_clip("/nonexistent/path/to/clip.mp4", 1).unwrap_err();
        assert!(matches!(err, WalkError::Io { .. }));
    }

    #[test]
    fn walk_clip_rejects_tiny_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("tiny.mp4");
        fs::write(&path, b"abc").unwrap();
        match walk_clip(&path, 1) {
            Err(WalkError::FileTooSmall { actual: 3, min: 8 }) => {}
            other => panic!("expected FileTooSmall, got {other:?}"),
        }
    }

    #[test]
    fn re_export_mp4_error_is_same_type() {
        // Compile-time check: ReexportedMp4Error == Mp4Error.
        fn _assert_same(_: ReexportedMp4Error) -> Mp4Error {
            unreachable!()
        }
    }

    #[test]
    fn waypoint_message_default_has_no_gps_fix() {
        // Sanity: the SeiMessage we routed through really is the
        // teslausb-core type with has_gps_fix().
        let msg = SeiMessage::default();
        assert!(!msg.has_gps_fix());
    }

    #[test]
    fn max_clip_bytes_constant_matches_v1() {
        assert_eq!(MAX_CLIP_BYTES, 150 * 1024 * 1024);
    }
}
