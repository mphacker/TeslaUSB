//! MP4 / QuickTime BMFF box scanner + `mvhd` extractor
//! (Phase 4b.1a).
#![allow(clippy::doc_markdown)]
// domain terms ("MP4", "BMFF", "QuickTime") need not be backticked

// Every indexing / slicing op in `find_box` is guarded by the
// `while pos + 8 <= end` loop condition (for the first 8 bytes
// of the header) or an explicit `pos + N > end` early return
// (for the extended-size field). Every indexing op in
// `parse_mvhd` is guarded by an explicit `body.len() < N`
// check earlier in the same arm. Suppressing the
// `indexing_slicing` lint at file scope is the project pattern
// (see `fs::exfat::boot_sector` etc.) — adding a per-op
// `#[allow]` would clutter the be-bytes constructors.
#![allow(clippy::indexing_slicing)]
#![allow(clippy::similar_names)] // `size_usize` / `header_usize` deliberately echo `size` / `header_size`
//!
//! ISO/IEC 14496-12 (BMFF) layout: a file is a sequence of
//! "boxes", each prefixed by `[u32 size][u32 type]`. `size`
//! includes the 8-byte header; if `size == 1` the real 64-bit
//! size follows in the next 8 bytes; if `size == 0` the box
//! extends to the end of the file. Tesla dashcam clips use
//! standard 32-bit sizes everywhere except very long `mdat`
//! boxes (where the 64-bit extension is permitted).
//!
//! For SEI extraction (Phase 4b.1) we need:
//!
//! * The `mdat` box's content range — that is where the H.264
//!   elementary stream lives (consumed by [`super::nal::AvccIter`]).
//! * The `moov.trak.mdia.mdhd` box — that is where the
//!   per-frame timescale lives.
//! * The `moov.trak.mdia.minf.stbl.stts` box — that is where
//!   the per-frame duration table lives (used to compute
//!   each SEI frame's timestamp in milliseconds).
//! * The `moov.mvhd` box — that is where the clip's authoritative
//!   UTC creation_time lives (Tesla writes this from its GPS-derived
//!   clock, NOT the onboard local clock — see v1
//!   `extract_mvhd_creation_time` for the full motivation).
//!
//! This module is byte-in / structured-data-out: it takes a
//! borrowed slice of the file (or `mmap` region) and returns
//! [`BoxRef`]s and decoded scalars. No I/O, no allocation in
//! the hot path.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// MP4 / QuickTime epoch is 1904-01-01 UTC; Unix epoch is
/// 1970-01-01 UTC. Difference in seconds. Used to convert
/// `mvhd.creation_time` into a [`SystemTime`].
const MP4_EPOCH_OFFSET_SECONDS: u64 = 2_082_844_800;

/// A located box within an MP4 buffer.
///
/// `start..end` is the half-open byte range of the box's
/// **content** (i.e. excluding the 8-byte or 16-byte header).
/// `body_len` is `end - start` — pre-computed for convenience.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BoxRef {
    /// First byte of the box's content (inclusive).
    pub start: usize,
    /// One past the last byte of the box's content (exclusive).
    pub end: usize,
    /// Box content length in bytes (`end - start`).
    pub body_len: usize,
}

impl BoxRef {
    /// Borrow the box's content bytes from `buf`.
    ///
    /// # Panics
    ///
    /// Never — the box positions are clamped to `buf.len()` at
    /// construction time inside [`find_box`].
    #[must_use]
    pub fn body<'b>(&self, buf: &'b [u8]) -> &'b [u8] {
        &buf[self.start..self.end]
    }
}

/// Errors emitted by the MP4 box scanner / mvhd extractor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Mp4Error {
    /// The named box was not found within the requested range.
    /// Carries the 4-char box name as ASCII bytes for diagnostics.
    BoxNotFound([u8; 4]),
    /// A box header was truncated (the byte range cannot hold an
    /// 8-byte header). Indicates a corrupt or malicious file.
    TruncatedHeader {
        /// Position where the truncated header starts.
        position: usize,
    },
    /// A box declares an extended (64-bit) size but the extension
    /// field is truncated.
    TruncatedExtendedSize {
        /// Position of the box whose extension is truncated.
        position: usize,
    },
    /// A box's declared size is smaller than its header. Refusing
    /// to advance would loop forever — refusing to skip would
    /// hide every later box. We stop the scan.
    SizeSmallerThanHeader {
        /// Position of the malformed box.
        position: usize,
        /// Size the malformed box declared.
        declared: u64,
        /// Header size we measured (8 or 16).
        header: u64,
    },
    /// A box declares a 64-bit size that does not fit in
    /// `usize` (or overflows when added to the position).
    /// Indicates a malicious file on 32-bit hosts.
    SizeOverflow {
        /// Position of the overflowing box.
        position: usize,
        /// Declared 64-bit size.
        declared: u64,
    },
    /// The `mvhd` box content is shorter than the version-required
    /// minimum (8 bytes for v0, 20 bytes for v1).
    MvhdTruncated {
        /// Box version (0 or 1) parsed from the first content byte.
        version: u8,
        /// Number of body bytes actually present.
        body_len: usize,
    },
    /// `mvhd.creation_time` is zero or so small it would land
    /// before the Unix epoch. v1 rejects these as "uninitialised /
    /// pre-2010 firmware glitch" — we do the same.
    MvhdCreationTimePreEpoch {
        /// Raw value from the file, in MP4-epoch seconds.
        raw_seconds: u64,
    },
}

impl std::fmt::Display for Mp4Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::BoxNotFound(name) => {
                let n = std::str::from_utf8(name).unwrap_or("????");
                write!(f, "MP4 box \"{n}\" not found")
            }
            Self::TruncatedHeader { position } => {
                write!(f, "truncated MP4 box header at position {position}")
            }
            Self::TruncatedExtendedSize { position } => {
                write!(
                    f,
                    "truncated MP4 extended-size field at position {position}"
                )
            }
            Self::SizeSmallerThanHeader {
                position,
                declared,
                header,
            } => write!(
                f,
                "MP4 box at {position} declares size {declared} \
                 < header size {header}"
            ),
            Self::SizeOverflow { position, declared } => write!(
                f,
                "MP4 box at {position} declares size {declared} \
                 that does not fit in usize"
            ),
            Self::MvhdTruncated { version, body_len } => write!(
                f,
                "mvhd box body too short for version {version}: \
                 {body_len} byte(s)"
            ),
            Self::MvhdCreationTimePreEpoch { raw_seconds } => write!(
                f,
                "mvhd creation_time {raw_seconds} is at or before \
                 the Unix epoch — treating as uninitialised"
            ),
        }
    }
}

impl std::error::Error for Mp4Error {}

/// Find the first child box named `name` within `buf[start..end]`.
///
/// Box scanning is sibling-level only — to descend into nested
/// boxes (e.g. `moov.trak.mdia`), call `find_box` recursively
/// against the parent's body slice. See [`find_box_path`] for
/// a slash-path convenience wrapper.
///
/// # Errors
///
/// Returns [`Mp4Error::BoxNotFound`] if no sibling matches.
/// Returns the truncation / overflow variants if a malformed
/// box header is encountered before the target — refusing to
/// silently skip past corruption matches v1 semantics (v1
/// `_find_box` `break`s on truncation, which is operationally
/// equivalent to "give up").
pub fn find_box(buf: &[u8], start: usize, end: usize, name: &[u8; 4]) -> Result<BoxRef, Mp4Error> {
    let end = end.min(buf.len());
    let mut pos = start;
    while pos + 8 <= end {
        let size_u32 = u32::from_be_bytes([buf[pos], buf[pos + 1], buf[pos + 2], buf[pos + 3]]);
        let box_type = [buf[pos + 4], buf[pos + 5], buf[pos + 6], buf[pos + 7]];
        let (size, header_size): (u64, u64) = match size_u32 {
            0 => {
                // Size 0 → box extends to end of containing range.
                let span = u64::try_from(end - pos).map_err(|_| Mp4Error::SizeOverflow {
                    position: pos,
                    declared: u64::from(size_u32),
                })?;
                (span, 8)
            }
            1 => {
                // 64-bit extended size in the next 8 bytes.
                if pos + 16 > end {
                    return Err(Mp4Error::TruncatedExtendedSize { position: pos });
                }
                let ext = u64::from_be_bytes([
                    buf[pos + 8],
                    buf[pos + 9],
                    buf[pos + 10],
                    buf[pos + 11],
                    buf[pos + 12],
                    buf[pos + 13],
                    buf[pos + 14],
                    buf[pos + 15],
                ]);
                (ext, 16)
            }
            n => (u64::from(n), 8),
        };
        if size < header_size {
            return Err(Mp4Error::SizeSmallerThanHeader {
                position: pos,
                declared: size,
                header: header_size,
            });
        }
        let size_usize = usize::try_from(size).map_err(|_| Mp4Error::SizeOverflow {
            position: pos,
            declared: size,
        })?;
        let header_usize = usize::try_from(header_size).map_err(|_| Mp4Error::SizeOverflow {
            position: pos,
            declared: header_size,
        })?;
        let box_end = pos.checked_add(size_usize).ok_or(Mp4Error::SizeOverflow {
            position: pos,
            declared: size,
        })?;
        // Clamp box_end to the containing range. Some malicious
        // clips claim a larger size than the file allows; v1
        // either clamps (if this is the target box) or breaks.
        // We do the same with explicit branches.
        let effective_end = box_end.min(end);
        if box_type == *name {
            return Ok(BoxRef {
                start: pos + header_usize,
                end: effective_end,
                body_len: effective_end - (pos + header_usize),
            });
        }
        // Not the target — advance to the next sibling. If the
        // box overflows the container, stop scanning (mirrors
        // v1's `break`).
        if box_end > end {
            return Err(Mp4Error::BoxNotFound(*name));
        }
        pos = box_end;
    }
    Err(Mp4Error::BoxNotFound(*name))
}

/// Descend a slash-separated box path from the top of `buf`,
/// returning the deepest box's content range.
///
/// `path` is a list of 4-byte ASCII box names, e.g.
/// `[b"moov", b"trak", b"mdia", b"mdhd"]`. Each element must
/// be exactly 4 bytes; this is a compile-time guarantee from
/// the `[u8; 4]` type.
///
/// # Errors
///
/// Returns the first [`Mp4Error`] encountered along the path.
pub fn find_box_path(buf: &[u8], path: &[[u8; 4]]) -> Result<BoxRef, Mp4Error> {
    let mut range = BoxRef {
        start: 0,
        end: buf.len(),
        body_len: buf.len(),
    };
    for name in path {
        range = find_box(buf, range.start, range.end, name)?;
    }
    Ok(range)
}

/// Parsed `mvhd` (Movie Header) box.
///
/// Carries only the fields the SEI walker actually consumes —
/// per-frame `timescale` lives on the `mdhd` box for the video
/// track, not on `mvhd`. We add fields here as needs arise.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Mvhd {
    /// Box version: 0 (32-bit time fields) or 1 (64-bit).
    pub version: u8,
    /// Creation time as a wall-clock [`SystemTime`]. Per v1's
    /// `extract_mvhd_creation_time`, Tesla writes this with its
    /// GPS-derived UTC time, so this is the authoritative
    /// start-of-recording timestamp.
    pub creation_time: SystemTime,
    /// Raw creation_time in MP4-epoch seconds. Kept for
    /// diagnostics; the `creation_time` field is the field
    /// callers should use.
    pub creation_time_raw_seconds: u64,
}

/// Parse an `mvhd` box body into a structured [`Mvhd`].
///
/// `body` must be the content slice of the mvhd box (use
/// [`find_box_path`] with `[b"moov", b"mvhd"]` then `.body(buf)`).
///
/// # Errors
///
/// * [`Mp4Error::MvhdTruncated`] if `body` is shorter than the
///   version-specific minimum.
/// * [`Mp4Error::MvhdCreationTimePreEpoch`] if the parsed
///   creation_time is at or before the MP4-to-Unix epoch offset
///   (i.e. the Unix epoch). v1 rejects these.
pub fn parse_mvhd(body: &[u8]) -> Result<Mvhd, Mp4Error> {
    // First byte is version; next 3 are flags (ignored).
    if body.is_empty() {
        return Err(Mp4Error::MvhdTruncated {
            version: 0,
            body_len: 0,
        });
    }
    let version = body[0];
    let (creation_time_raw, need): (u64, usize) = if version == 1 {
        // version 1 layout (offsets within body):
        //   [0]      version
        //   [1..4]   flags
        //   [4..12]  creation_time (u64 BE)
        //   [12..20] modification_time (u64 BE)
        if body.len() < 20 {
            return Err(Mp4Error::MvhdTruncated {
                version: 1,
                body_len: body.len(),
            });
        }
        let ct = u64::from_be_bytes([
            body[4], body[5], body[6], body[7], body[8], body[9], body[10], body[11],
        ]);
        (ct, 20)
    } else {
        // version 0 (and any forward-compat version we
        // don't recognise — v1 takes the same fallback):
        //   [0]     version
        //   [1..4]  flags
        //   [4..8]  creation_time (u32 BE)
        //   [8..12] modification_time (u32 BE)
        if body.len() < 8 {
            return Err(Mp4Error::MvhdTruncated {
                version,
                body_len: body.len(),
            });
        }
        let ct = u32::from_be_bytes([body[4], body[5], body[6], body[7]]);
        (u64::from(ct), 8)
    };
    debug_assert!(body.len() >= need);
    if creation_time_raw <= MP4_EPOCH_OFFSET_SECONDS {
        return Err(Mp4Error::MvhdCreationTimePreEpoch {
            raw_seconds: creation_time_raw,
        });
    }
    let unix_seconds = creation_time_raw - MP4_EPOCH_OFFSET_SECONDS;
    let creation_time = UNIX_EPOCH + Duration::from_secs(unix_seconds);
    Ok(Mvhd {
        version,
        creation_time,
        creation_time_raw_seconds: creation_time_raw,
    })
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::expect_used,
        clippy::indexing_slicing,
        clippy::panic,
        clippy::unwrap_used,
        clippy::cast_possible_truncation,
        clippy::cast_lossless
    )]

    use super::*;
    // total size (header + body) the box should declare.
    fn box_with_declared_size(name: [u8; 4], declared_size: u32, body: &[u8]) -> Vec<u8> {
        let mut v = Vec::new();
        v.extend_from_slice(&declared_size.to_be_bytes());
        v.extend_from_slice(&name);
        v.extend_from_slice(body);
        v
    }

    fn simple_box(name: [u8; 4], body: &[u8]) -> Vec<u8> {
        let size = u32::try_from(8 + body.len()).unwrap();
        box_with_declared_size(name, size, body)
    }

    #[test]
    fn find_box_locates_top_level_sibling() {
        let mut buf = Vec::new();
        buf.extend_from_slice(&simple_box(*b"ftyp", &[0xAA, 0xBB]));
        buf.extend_from_slice(&simple_box(*b"moov", &[0xCC; 10]));
        let r = find_box(&buf, 0, buf.len(), b"moov").unwrap();
        assert_eq!(r.body(&buf), &[0xCC; 10]);
    }

    #[test]
    fn find_box_returns_not_found_when_absent() {
        let buf = simple_box(*b"ftyp", &[]);
        let err = find_box(&buf, 0, buf.len(), b"moov").unwrap_err();
        assert!(matches!(err, Mp4Error::BoxNotFound(name) if &name == b"moov"));
    }

    #[test]
    fn find_box_handles_extended_64bit_size() {
        // size_u32 = 1 → next 8 bytes are u64 size including the
        // 16-byte header.
        let mut buf = Vec::new();
        let body = vec![0x11_u8; 5];
        let total: u64 = 16 + body.len() as u64;
        buf.extend_from_slice(&1u32.to_be_bytes());
        buf.extend_from_slice(b"mdat");
        buf.extend_from_slice(&total.to_be_bytes());
        buf.extend_from_slice(&body);
        let r = find_box(&buf, 0, buf.len(), b"mdat").unwrap();
        assert_eq!(r.body(&buf), &body[..]);
    }

    #[test]
    fn find_box_zero_size_means_extends_to_end_of_container() {
        // Size = 0 → extends to end of buf. Useful for the last
        // mdat in a streaming file.
        let mut buf = Vec::new();
        buf.extend_from_slice(&0u32.to_be_bytes());
        buf.extend_from_slice(b"mdat");
        buf.extend_from_slice(&[0xEE; 7]);
        let r = find_box(&buf, 0, buf.len(), b"mdat").unwrap();
        assert_eq!(r.body(&buf), &[0xEE; 7][..]);
    }

    #[test]
    fn find_box_size_smaller_than_header_is_rejected() {
        let buf = box_with_declared_size(*b"junk", 4, &[]);
        let err = find_box(&buf, 0, buf.len(), b"junk").unwrap_err();
        assert!(matches!(err, Mp4Error::SizeSmallerThanHeader { .. }));
    }

    #[test]
    fn find_box_truncated_extended_size_is_rejected() {
        // size_u32 = 1 but only 4 bytes for the 64-bit extension.
        let mut buf = Vec::new();
        buf.extend_from_slice(&1u32.to_be_bytes());
        buf.extend_from_slice(b"mdat");
        buf.extend_from_slice(&[0xAB, 0xCD, 0xEF, 0x01]);
        let err = find_box(&buf, 0, buf.len(), b"mdat").unwrap_err();
        assert!(matches!(err, Mp4Error::TruncatedExtendedSize { .. }));
    }

    #[test]
    fn find_box_path_descends_nested_boxes() {
        // moov { trak { mdia { mdhd[1..=12] } } }
        let mdhd = simple_box(*b"mdhd", &[0xAA; 4]);
        let mdia = simple_box(*b"mdia", &mdhd);
        let trak = simple_box(*b"trak", &mdia);
        let moov = simple_box(*b"moov", &trak);
        let r = find_box_path(&moov, &[*b"moov", *b"trak", *b"mdia", *b"mdhd"]).unwrap();
        assert_eq!(r.body(&moov), &[0xAA; 4]);
    }

    #[test]
    fn find_box_path_errors_on_first_missing_segment() {
        let buf = simple_box(*b"moov", &[]);
        let err = find_box_path(&buf, &[*b"moov", *b"trak"]).unwrap_err();
        assert!(matches!(err, Mp4Error::BoxNotFound(n) if &n == b"trak"));
    }

    // ───────────────────────── mvhd ────────────────────────────

    fn mvhd_v0_body(creation_time_raw: u32) -> Vec<u8> {
        let mut v = Vec::new();
        v.push(0); // version
        v.extend_from_slice(&[0, 0, 0]); // flags
        v.extend_from_slice(&creation_time_raw.to_be_bytes());
        v.extend_from_slice(&0u32.to_be_bytes()); // modification_time
        // (remaining fields ignored)
        v
    }

    fn mvhd_v1_body(creation_time_raw: u64) -> Vec<u8> {
        let mut v = Vec::new();
        v.push(1); // version
        v.extend_from_slice(&[0, 0, 0]); // flags
        v.extend_from_slice(&creation_time_raw.to_be_bytes());
        v.extend_from_slice(&0u64.to_be_bytes()); // modification_time
        v
    }

    #[test]
    fn parse_mvhd_v0_returns_unix_time_for_valid_creation() {
        // 2_082_844_800 = Unix epoch in MP4 seconds, so we need
        // raw > that. Use raw = MP4_EPOCH_OFFSET + 1000 → Unix
        // time = 1000 (= 1970-01-01 00:16:40 UTC).
        let raw = u32::try_from(MP4_EPOCH_OFFSET_SECONDS + 1000).unwrap();
        let mvhd = parse_mvhd(&mvhd_v0_body(raw)).unwrap();
        assert_eq!(mvhd.version, 0);
        assert_eq!(mvhd.creation_time, UNIX_EPOCH + Duration::from_secs(1000));
        assert_eq!(mvhd.creation_time_raw_seconds, u64::from(raw));
    }

    #[test]
    fn parse_mvhd_v1_returns_unix_time_for_valid_creation() {
        let raw = MP4_EPOCH_OFFSET_SECONDS + 1_700_000_000;
        let mvhd = parse_mvhd(&mvhd_v1_body(raw)).unwrap();
        assert_eq!(mvhd.version, 1);
        assert_eq!(
            mvhd.creation_time,
            UNIX_EPOCH + Duration::from_secs(1_700_000_000)
        );
    }

    #[test]
    fn parse_mvhd_zero_creation_time_rejected_as_pre_epoch() {
        let err = parse_mvhd(&mvhd_v0_body(0)).unwrap_err();
        assert!(matches!(
            err,
            Mp4Error::MvhdCreationTimePreEpoch { raw_seconds: 0 }
        ));
    }

    #[test]
    fn parse_mvhd_creation_time_exactly_unix_epoch_rejected() {
        // raw == MP4_EPOCH_OFFSET would be Unix time = 0, which
        // v1 rejects with `<= _MP4_EPOCH_OFFSET`.
        let raw = u32::try_from(MP4_EPOCH_OFFSET_SECONDS).unwrap();
        let err = parse_mvhd(&mvhd_v0_body(raw)).unwrap_err();
        assert!(matches!(err, Mp4Error::MvhdCreationTimePreEpoch { .. }));
    }

    #[test]
    fn parse_mvhd_truncated_v0_body_rejected() {
        let body = vec![0u8, 0, 0, 0, 1, 2]; // version + flags + 2 bytes of ct
        let err = parse_mvhd(&body).unwrap_err();
        assert!(matches!(err, Mp4Error::MvhdTruncated { version: 0, .. }));
    }

    #[test]
    fn parse_mvhd_truncated_v1_body_rejected() {
        // version 1 needs 20 body bytes; give it 15.
        let body = vec![1u8; 15];
        let err = parse_mvhd(&body).unwrap_err();
        assert!(matches!(
            err,
            Mp4Error::MvhdTruncated {
                version: 1,
                body_len: 15
            }
        ));
    }

    #[test]
    fn parse_mvhd_empty_body_rejected() {
        let err = parse_mvhd(&[]).unwrap_err();
        assert!(matches!(err, Mp4Error::MvhdTruncated { .. }));
    }

    #[test]
    fn parse_mvhd_unknown_version_falls_back_to_v0_layout_like_v1() {
        // v1 Python: `if version == 1: ... else: ...` — any
        // unknown version takes the v0 path. Mirror here.
        let mut body = mvhd_v0_body(u32::try_from(MP4_EPOCH_OFFSET_SECONDS + 5).unwrap());
        body[0] = 7; // unknown version
        let mvhd = parse_mvhd(&body).unwrap();
        assert_eq!(mvhd.version, 7);
        assert_eq!(mvhd.creation_time, UNIX_EPOCH + Duration::from_secs(5));
    }
}
