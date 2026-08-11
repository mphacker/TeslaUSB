//! Bounded MP4 structural probe for the stability gate.
//!
//! Given the bytes of a candidate clip (read raw through the cluster
//! chain, never via a mount), this answers three questions the gate
//! needs:
//!
//! * **Is the container complete?** `ftyp` + `moov` (with a parseable
//!   `mvhd`/`mdhd`) + `mdat` all present and self-consistent. A clip
//!   still being written is missing a finalized `moov`.
//! * **What codec is it?** Tesla records H.264 (`avc1`/`avcC`). HEVC
//!   (`hvc1`/`hev1`/`hvcC`) is rejected **loudly** rather than silently
//!   mis-indexed — the SEI pipeline only understands H.264.
//! * **A stable content digest** over the structural boxes plus a
//!   bounded data tail, so downstream can detect a same-name file whose
//!   contents changed.
//!
//! Box walking reuses `teslausb_core::sei::mp4`, which already does
//! checked, overflow-safe box parsing.

use teslausb_core::sei::mp4::{BoxRef, find_box, find_box_path, parse_mdhd};

/// Detected video codec of a clip.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Codec {
    /// H.264 / AVC — the only codec the SEI pipeline supports.
    H264,
    /// HEVC / H.265 — explicitly unsupported; flagged, never decoded.
    Hevc,
    /// No recognizable codec configuration box was found.
    Unknown,
}

/// Result of probing a candidate clip.
#[derive(Debug, Clone)]
pub struct Mp4Probe {
    /// `true` if `ftyp` + a finalized `moov` + `mdat` are all present.
    pub complete: bool,
    /// Detected codec.
    pub codec: Codec,
    /// Track duration in seconds from `mdhd`, if parseable.
    pub duration_s: Option<f64>,
    /// 64-bit content digest over structural boxes + bounded data tail.
    pub content_digest: u64,
}

/// FNV-1a 64-bit offset basis.
const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
/// FNV-1a 64-bit prime.
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;
/// Bytes of the `mdat` tail folded into the content digest.
const DATA_TAIL_DIGEST_BYTES: usize = 64 * 1024;

/// Probe `data` (the clip bytes, bounded by `ValidDataLength`).
///
/// Never errors: an unparseable / truncated clip simply comes back
/// `complete = false`, which the gate treats as "not yet stable".
#[must_use]
pub fn probe_mp4(data: &[u8]) -> Mp4Probe {
    let has_ftyp = find_box(data, 0, data.len(), b"ftyp").is_ok();
    let moov = find_box(data, 0, data.len(), b"moov").ok();
    let mdat = find_box(data, 0, data.len(), b"mdat").ok();

    let duration_s = moov.as_ref().and_then(|_| track_duration_s(data));
    let codec = moov
        .as_ref()
        .map_or(Codec::Unknown, |m| detect_codec(m.body(data)));

    // "Complete" requires the structural trio AND a parseable media
    // header — the strongest cheap signal that the moov is finalized.
    let complete = has_ftyp && moov.is_some() && mdat.is_some() && duration_s.is_some();

    let content_digest = digest(data, moov.as_ref(), mdat.as_ref());

    Mp4Probe {
        complete,
        codec,
        duration_s,
        content_digest,
    }
}

/// Track duration in seconds from `moov/trak/mdia/mdhd`.
fn track_duration_s(data: &[u8]) -> Option<f64> {
    let mdhd = find_box_path(data, &[*b"moov", *b"trak", *b"mdia", *b"mdhd"]).ok()?;
    let parsed = parse_mdhd(mdhd.body(data)).ok()?;
    if parsed.timescale == 0 {
        return None;
    }
    #[allow(clippy::cast_precision_loss)]
    Some(parsed.duration as f64 / f64::from(parsed.timescale))
}

/// Detect the codec by scanning the `moov` body for a codec
/// configuration fourcc. H.264 wins if both somehow appear; HEVC is
/// reported so the caller can reject it.
fn detect_codec(moov_body: &[u8]) -> Codec {
    if contains_fourcc(moov_body, *b"avcC") || contains_fourcc(moov_body, *b"avc1") {
        Codec::H264
    } else if contains_fourcc(moov_body, *b"hvcC")
        || contains_fourcc(moov_body, *b"hvc1")
        || contains_fourcc(moov_body, *b"hev1")
    {
        Codec::Hevc
    } else {
        Codec::Unknown
    }
}

/// `true` if the 4-byte tag appears anywhere in `hay`.
fn contains_fourcc(hay: &[u8], needle: [u8; 4]) -> bool {
    hay.windows(4).any(|w| w == needle)
}

/// FNV-1a digest over the `moov` box bytes plus the tail of `mdat`
/// (bounded), giving a content fingerprint that changes if either the
/// structure or the recorded data near the end changes.
fn digest(data: &[u8], moov: Option<&BoxRef>, mdat: Option<&BoxRef>) -> u64 {
    let mut h = FNV_OFFSET;
    let mut fold = |bytes: &[u8]| {
        for &b in bytes {
            h ^= u64::from(b);
            h = h.wrapping_mul(FNV_PRIME);
        }
    };
    fold(&(data.len() as u64).to_le_bytes());
    if let Some(m) = moov {
        if let Some(slice) = data.get(m.start..m.end) {
            fold(slice);
        }
    }
    if let Some(m) = mdat {
        if let Some(body) = data.get(m.start..m.end) {
            let tail_start = body.len().saturating_sub(DATA_TAIL_DIGEST_BYTES);
            if let Some(tail) = body.get(tail_start..) {
                fold(tail);
            }
        }
    }
    h
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::indexing_slicing,
    clippy::panic,
    clippy::trivially_copy_pass_by_ref
)]
mod tests {
    use super::*;

    /// Build a 4-byte-size + fourcc + body box.
    fn box_bytes(name: &[u8; 4], body: &[u8]) -> Vec<u8> {
        let size = u32::try_from(8 + body.len()).unwrap();
        let mut v = size.to_be_bytes().to_vec();
        v.extend_from_slice(name);
        v.extend_from_slice(body);
        v
    }

    fn minimal_mdhd_body() -> Vec<u8> {
        // version(0)+flags(3) + creation(4)+modification(4)
        // + timescale(4)+duration(4) + lang(2)+pre(2)
        let mut b = vec![0u8; 4];
        b.extend_from_slice(&0u32.to_be_bytes()); // creation
        b.extend_from_slice(&0u32.to_be_bytes()); // modification
        b.extend_from_slice(&1000u32.to_be_bytes()); // timescale
        b.extend_from_slice(&60000u32.to_be_bytes()); // duration = 60s
        b.extend_from_slice(&[0, 0, 0, 0]);
        b
    }

    fn assemble_clip(codec_tag: [u8; 4]) -> Vec<u8> {
        let mdhd = box_bytes(b"mdhd", &minimal_mdhd_body());
        let mdia = box_bytes(b"mdia", &mdhd);
        // stsd-ish payload carrying the codec config fourcc.
        let codec_cfg = box_bytes(&codec_tag, &[1, 2, 3, 4]);
        let trak_body = [mdia, codec_cfg].concat();
        let trak = box_bytes(b"trak", &trak_body);
        let moov = box_bytes(b"moov", &trak);
        let ftyp = box_bytes(b"ftyp", b"isom");
        let mdat = box_bytes(b"mdat", &[0xAB; 256]);
        [ftyp, moov, mdat].concat()
    }

    #[test]
    fn complete_h264_clip() {
        let clip = assemble_clip(*b"avcC");
        let p = probe_mp4(&clip);
        assert!(p.complete);
        assert_eq!(p.codec, Codec::H264);
        assert_eq!(p.duration_s, Some(60.0));
    }

    #[test]
    fn detects_and_flags_hevc() {
        let clip = assemble_clip(*b"hvcC");
        let p = probe_mp4(&clip);
        assert_eq!(p.codec, Codec::Hevc);
    }

    #[test]
    fn in_progress_clip_without_moov_is_incomplete() {
        // A recording-in-progress has a growing mdat but no finalized
        // moov yet — the strongest "not done" signal.
        let ftyp = box_bytes(b"ftyp", b"isom");
        let mdat = box_bytes(b"mdat", &[0xAB; 4096]);
        let in_progress = [ftyp, mdat].concat();
        let p = probe_mp4(&in_progress);
        assert!(!p.complete);
        assert_eq!(p.duration_s, None);
    }

    #[test]
    fn digest_changes_with_content() {
        let a = assemble_clip(*b"avcC");
        let mut b = a.clone();
        let last = b.len() - 1;
        b[last] ^= 0xFF;
        assert_ne!(probe_mp4(&a).content_digest, probe_mp4(&b).content_digest);
    }
}
