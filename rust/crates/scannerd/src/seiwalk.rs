//! Lossless per-frame SEI waypoint walk over already-read clip bytes.
//!
//! [`scan_sei`](crate::seiscan::scan_sei) deliberately surfaces only the
//! GPS+speed subset needed for the stability/preservation heuristics. The
//! `indexd` trip/event derivation needs the *full* decoded
//! [`SeiMessage`] (acceleration, autopilot state, gear, steering, …) plus
//! a clip-relative frame index and millisecond timestamp computed from the
//! MP4 `stts` table — exactly the walk the v1 production worker performed.
//!
//! This module is an **additive** port of that v1 worker walk
//! (`teslausb-worker/src/sei.rs::walk_clip_bytes`). It reuses the same
//! `teslausb_core::sei` primitives that `scan_sei` already uses; it does
//! not re-implement any raw MP4/NAL parsing and it leaves `scan_sei`
//! untouched. SEI decoding stays inside `scannerd` (per `indexd.md §3`)
//! rather than being forked into a third walker.
//!
//! ## Walk algorithm (parity with the v1 worker)
//!
//! 1. `moov.mvhd` → authoritative GPS-derived UTC start time (best-effort;
//!    a missing/garbled `mvhd` only loses the absolute base, not the walk).
//! 2. `moov.trak.mdia.mdhd` → timescale; `…stbl.stts` → per-frame durations
//!    in ms (falls back to 30 000 / 33.33 ms defaults on any structural
//!    miss).
//! 3. `mdat` → drive [`AvccIter`]: on a VCL NAL (type 1 / type 5) advance
//!    `frame_index` and `cumulative_time_ms`; on an SEI NAL (type 6), if
//!    `frame_index % sample_rate == 0`, decode and emit a waypoint. A
//!    non-Tesla / undecodeable SEI is silently skipped (mirrors v1's
//!    `if sei is not None`).

use std::time::SystemTime;

use teslausb_core::sei::mp4::{
    BoxRef, Mp4Error, find_box, parse_mdhd, parse_mvhd, parse_stts_durations,
};
use teslausb_core::sei::nal::{AvccIter, NalUnit};
use teslausb_core::sei::payload::extract_tesla_payload;
use teslausb_core::sei::tesla::{SeiMessage, decode_sei_message};

/// Default per-sample duration in ms when the `stts` table is missing,
/// truncated, or shorter than the frame count. 33.33 ms ≈ 30 fps — Tesla
/// dashcam's standard rate. Matches the v1 worker's `default_duration_ms`.
pub const DEFAULT_FRAME_DURATION_MS: f64 = 33.333_333_333_333_336_f64;

/// Default track timescale when `mdhd` cannot be parsed (Tesla dashcam
/// video uses 30 000). Matches the v1 worker fallback.
pub const DEFAULT_TIMESCALE: u32 = 30_000;

/// One sampled SEI waypoint: the fully decoded [`SeiMessage`] plus the
/// clip-relative timing the derivation needs to build absolute timestamps
/// without re-reading the file.
#[derive(Debug, Clone, PartialEq)]
pub struct Waypoint {
    /// Monotonic 0-based frame index within the clip (VCL-counted).
    pub frame_index: u32,
    /// Milliseconds since the start of the clip, from the cumulative
    /// `stts` deltas of every preceding frame.
    pub timestamp_ms: f64,
    /// The full Tesla telemetry decoded from the SEI protobuf payload.
    pub message: SeiMessage,
}

/// Result of walking one clip's SEI stream.
#[derive(Debug, Clone, PartialEq)]
pub struct ClipWaypoints {
    /// Absolute UTC start-of-recording time from `mvhd`, when available.
    /// `None` if the box is missing or unparseable. Tesla writes this
    /// from its GPS-derived clock, so it is the authoritative recording
    /// instant; callers fall back to the filename timestamp when absent.
    pub clip_started_utc: Option<SystemTime>,
    /// Track timescale (units per second) from `mdhd`, or
    /// [`DEFAULT_TIMESCALE`].
    pub timescale: u32,
    /// Number of VCL frames the walk advanced past in `mdat` (NOT the
    /// waypoint count, which is decimated by `sample_rate`).
    pub frame_count: u32,
    /// Sampled waypoints whose SEI decoded cleanly, in frame order.
    pub waypoints: Vec<Waypoint>,
}

/// Errors that prevent walking a clip at all. Mid-walk failures (one bad
/// NAL, one undecodeable SEI) are silently dropped — the walk yields
/// whatever it could decode.
#[derive(Debug, thiserror::Error)]
pub enum WalkError {
    /// `mdat` is missing or the MP4 box structure is malformed — there
    /// are no NAL units to walk.
    #[error("MP4 structure error: {0}")]
    Mp4(#[source] Mp4Error),
}

impl From<Mp4Error> for WalkError {
    fn from(err: Mp4Error) -> Self {
        Self::Mp4(err)
    }
}

/// Walk a Tesla dashcam clip held in memory and return its SEI waypoints.
///
/// `sample_rate` decimates SEI sampling: `1` decodes every SEI NAL
/// (~30 waypoints/sec on Tesla footage), `30` decodes ~1 waypoint/sec
/// (the indexer default). `0` is treated as `1`.
///
/// # Errors
///
/// Returns [`WalkError::Mp4`] only when `mdat` cannot be located. Missing
/// timing boxes fall back to defaults; per-frame decode failures are
/// skipped.
pub fn walk_clip_waypoints(clip: &[u8], sample_rate: u32) -> Result<ClipWaypoints, WalkError> {
    let sample_stride = sample_rate.max(1);

    // mvhd is best-effort: a missing/garbled mvhd only loses the absolute
    // base timestamp, never the walk itself.
    let clip_started_utc = read_clip_started_utc(clip);

    let (timescale, durations) = read_track_timing(clip).unwrap_or((DEFAULT_TIMESCALE, Vec::new()));
    let mdat = find_box(clip, 0, clip.len(), b"mdat")?;
    let mdat_body = mdat.body(clip);

    let mut waypoints = Vec::new();
    let mut frame_index: u32 = 0;
    let mut cumulative_time_ms: f64 = 0.0;

    for nal in AvccIter::new(mdat_body) {
        let Ok(NalUnit { nal_type, payload }) = nal else {
            // Truncated / overrun NAL — yield what we accumulated rather
            // than losing the rest of the clip (v1 `break`s here too).
            break;
        };
        if nal_type == NalUnit::NAL_TYPE_SEI {
            if frame_index % sample_stride == 0 {
                if let Some(message) = decode_sei_payload(payload) {
                    waypoints.push(Waypoint {
                        frame_index,
                        timestamp_ms: cumulative_time_ms,
                        message,
                    });
                }
            }
        } else if matches!(
            nal_type,
            NalUnit::NAL_TYPE_IDR | NalUnit::NAL_TYPE_NON_IDR_SLICE
        ) {
            // A frame boundary is either a keyframe (5) or non-IDR slice
            // (1); SPS/PPS/AUD are not frames.
            let dur_ms = durations
                .get(frame_index as usize)
                .copied()
                .unwrap_or(DEFAULT_FRAME_DURATION_MS);
            cumulative_time_ms += dur_ms;
            frame_index = frame_index.saturating_add(1);
        }
    }

    Ok(ClipWaypoints {
        clip_started_utc,
        timescale,
        frame_count: frame_index,
        waypoints,
    })
}

/// Resolve `moov.mvhd` and parse its `creation_time`. Returns `None` on
/// any structural failure — a degraded state, not a walk stop condition.
fn read_clip_started_utc(buf: &[u8]) -> Option<SystemTime> {
    let moov = find_box(buf, 0, buf.len(), b"moov").ok()?;
    let mvhd = find_box(buf, moov.start, moov.end, b"mvhd").ok()?;
    let parsed = parse_mvhd(mvhd.body(buf)).ok()?;
    Some(parsed.creation_time)
}

/// Resolve the video track's timescale and per-frame durations from
/// `moov.trak.mdia.{mdhd, minf.stbl.stts}`. Tesla clips have exactly one
/// video trak, so the first `trak` is the one. Returns `None` on any
/// structural miss so the walk can fall back to defaults.
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

/// Find a child box and borrow its body slice in one `?`-chainable step.
fn find_box_body<'b>(buf: &'b [u8], parent: &BoxRef, name: [u8; 4]) -> Option<&'b [u8]> {
    let child = find_box(buf, parent.start, parent.end, &name).ok()?;
    Some(child.body(buf))
}

/// Run the Tesla SEI payload pipeline against one NAL's bytes. Returns
/// `Some` only if the NAL is Tesla-shaped AND its protobuf decodes — v1
/// returns `None` on any failure and we mirror that.
fn decode_sei_payload(nal_payload: &[u8]) -> Option<SeiMessage> {
    let payload = extract_tesla_payload(nal_payload).ok()?;
    decode_sei_message(&payload).ok()
}

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
        clippy::similar_names,
        clippy::trivially_copy_pass_by_ref
    )]

    use super::{DEFAULT_FRAME_DURATION_MS, walk_clip_waypoints};
    use teslausb_core::sei::payload::{
        RBSP_TRAILING_BYTE, TESLA_PADDING_BYTE, TESLA_PROTOBUF_MARKER,
    };

    fn mk_box(name: &[u8; 4], body: &[u8]) -> Vec<u8> {
        let size = u32::try_from(8 + body.len()).unwrap();
        let mut v = Vec::with_capacity(8 + body.len());
        v.extend_from_slice(&size.to_be_bytes());
        v.extend_from_slice(name);
        v.extend_from_slice(body);
        v
    }

    fn nested(name: &[u8; 4], children: &[Vec<u8>]) -> Vec<u8> {
        let mut body = Vec::new();
        for c in children {
            body.extend_from_slice(c);
        }
        mk_box(name, &body)
    }

    fn mvhd_v0_body(creation_time_raw: u32) -> Vec<u8> {
        let mut v = vec![0u8; 100];
        v[4..8].copy_from_slice(&creation_time_raw.to_be_bytes());
        v
    }

    fn mdhd_v0_body(timescale: u32) -> Vec<u8> {
        let mut v = vec![0u8; 24];
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

    /// Minimal varint-free Tesla SEI protobuf: a single field is enough
    /// for `decode_sei_message` to succeed. Field 4 (`vehicle_speed`) as a
    /// fixed32 float keeps it trivial; we only assert decode success +
    /// timing here, not field values.
    fn tesla_sei_nal(speed_mps: f32) -> Vec<u8> {
        // protobuf: field 4, wire type 5 (fixed32) → tag = (4<<3)|5 = 0x25
        let mut proto = vec![0x25u8];
        proto.extend_from_slice(&speed_mps.to_le_bytes());
        // Layout per `extract_tesla_payload`: it skips 3 prefix bytes
        // (NAL header 0x06, SEI payload_type 0x05, payload_size), THEN
        // scans the 0x42 padding run, then the 0x69 marker.
        let payload_size = u8::try_from(proto.len() + 2).unwrap();
        let mut nal = vec![
            0x06u8,
            0x05u8,
            payload_size,
            TESLA_PADDING_BYTE,
            TESLA_PROTOBUF_MARKER,
        ];
        nal.extend_from_slice(&proto);
        nal.push(RBSP_TRAILING_BYTE);
        nal
    }

    fn vcl_nal(nal_type: u8) -> Vec<u8> {
        vec![nal_type, 0x00, 0x00]
    }

    fn avcc(nal: &[u8]) -> Vec<u8> {
        let mut v = Vec::new();
        v.extend_from_slice(&u32::try_from(nal.len()).unwrap().to_be_bytes());
        v.extend_from_slice(nal);
        v
    }

    fn build_clip(stts: Option<(u32, u32)>, nals: &[Vec<u8>]) -> Vec<u8> {
        let mut stbl_children = Vec::new();
        if let Some((count, delta)) = stts {
            stbl_children.push(mk_box(b"stts", &stts_body_one_entry(count, delta)));
        }
        let stbl = nested(b"stbl", &stbl_children);
        let minf = nested(b"minf", &[stbl]);
        let mdhd = mk_box(b"mdhd", &mdhd_v0_body(30_000));
        let mdia = nested(b"mdia", &[mdhd, minf]);
        let trak = nested(b"trak", &[mdia]);
        let mvhd = mk_box(b"mvhd", &mvhd_v0_body(0xD000_0000));
        let moov = nested(b"moov", &[mvhd, trak]);

        let mut mdat_body = Vec::new();
        for nal in nals {
            mdat_body.extend_from_slice(&avcc(nal));
        }
        let mdat = mk_box(b"mdat", &mdat_body);

        let mut clip = Vec::new();
        clip.extend_from_slice(&moov);
        clip.extend_from_slice(&mdat);
        clip
    }

    #[test]
    fn missing_mdat_is_error() {
        let clip = nested(b"moov", &[mk_box(b"mvhd", &mvhd_v0_body(0xD000_0000))]);
        assert!(walk_clip_waypoints(&clip, 1).is_err());
    }

    #[test]
    fn walks_frames_and_samples_every_sei() {
        // Sequence: SEI, IDR, SEI, slice, SEI. With sample_rate=1 each SEI
        // is decoded; frame_index advances only on the two VCL NALs.
        let nals = vec![
            tesla_sei_nal(10.0),
            vcl_nal(super::NalUnit::NAL_TYPE_IDR),
            tesla_sei_nal(11.0),
            vcl_nal(super::NalUnit::NAL_TYPE_NON_IDR_SLICE),
            tesla_sei_nal(12.0),
        ];
        let clip = build_clip(None, &nals);
        let walk = walk_clip_waypoints(&clip, 1).expect("walk");
        assert_eq!(walk.frame_count, 2);
        assert_eq!(walk.waypoints.len(), 3);
        // First SEI at frame 0 → t=0; second after one frame → default dur;
        // third after two frames → 2× default dur (no stts table).
        assert_eq!(walk.waypoints[0].frame_index, 0);
        assert_eq!(walk.waypoints[0].timestamp_ms, 0.0);
        assert_eq!(walk.waypoints[1].frame_index, 1);
        assert!((walk.waypoints[1].timestamp_ms - DEFAULT_FRAME_DURATION_MS).abs() < 1e-6);
        assert!((walk.waypoints[2].timestamp_ms - 2.0 * DEFAULT_FRAME_DURATION_MS).abs() < 1e-6);
    }

    #[test]
    fn sample_rate_decimates() {
        // Frames 0..4 each preceded by an SEI; sample_rate=2 keeps SEIs at
        // frame_index 0 and 2 only.
        let nals = vec![
            tesla_sei_nal(1.0),
            vcl_nal(super::NalUnit::NAL_TYPE_IDR),
            tesla_sei_nal(2.0),
            vcl_nal(super::NalUnit::NAL_TYPE_NON_IDR_SLICE),
            tesla_sei_nal(3.0),
            vcl_nal(super::NalUnit::NAL_TYPE_NON_IDR_SLICE),
        ];
        let clip = build_clip(None, &nals);
        let walk = walk_clip_waypoints(&clip, 2).expect("walk");
        let indices: Vec<u32> = walk.waypoints.iter().map(|w| w.frame_index).collect();
        assert_eq!(indices, vec![0, 2]);
    }

    #[test]
    fn stts_durations_drive_timestamps() {
        // One stts entry: 10 frames @ 3000 units, timescale 30000 → 100 ms.
        let nals = vec![vcl_nal(super::NalUnit::NAL_TYPE_IDR), tesla_sei_nal(5.0)];
        let clip = build_clip(Some((10, 3000)), &nals);
        let walk = walk_clip_waypoints(&clip, 1).expect("walk");
        assert_eq!(walk.waypoints.len(), 1);
        // SEI is after one frame whose duration is 100 ms.
        assert!((walk.waypoints[0].timestamp_ms - 100.0).abs() < 1e-6);
    }
}
