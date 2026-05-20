//! AVCC NAL-unit iterator + H.264 emulation-prevention strip
//! (Phase 4b.1a).
#![allow(clippy::doc_markdown)] // domain terms ("AVCC", "NAL", "Tesla") need not be backticked
//!
//! The AVCC (a.k.a. ISO/IEC 14496-15 "AVC file format") byte
//! stream packs successive NAL units with a fixed-width
//! big-endian length prefix in front of each unit, **without**
//! Annex-B start codes. Tesla dashcam MP4s use a 4-byte length
//! field — but in principle the length field's width is carried
//! in the `avcC` configuration record's `lengthSizeMinusOne`
//! field (1/2/4 bytes). We hard-code 4 here because:
//!
//! 1. Every Tesla clip we have ever observed (HW3 / HW4, H.264
//!    Main / High profile) uses `lengthSizeMinusOne = 3` →
//!    4-byte prefix. v1's `sei_parser.py` does the same and has
//!    been running in production since 2024.
//! 2. The MP4 spec permits 1 / 2 / 4 only; 3 is reserved. We
//!    add the `avcC` lookup + branch when (if) Tesla ever ships
//!    a 1- or 2-byte variant. Until then it is dead code.
//!
//! Inside a NAL unit, H.264 inserts an "emulation prevention
//! byte" (0x03) after any two consecutive zero bytes so the
//! decoder cannot mistake user data for an Annex-B start code
//! (0x000001 / 0x00000001). The byte must be stripped before
//! the SEI payload protobuf (Phase 4b.1c) can be deserialized.

use std::borrow::Cow;

/// Width of the AVCC length prefix, in bytes. See module docs
/// for why this is hard-coded; widen to `u8` config when Tesla
/// ships a clip with a different `lengthSizeMinusOne`.
const AVCC_LENGTH_PREFIX_BYTES: usize = 4;

/// A single NAL unit borrowed from an AVCC-formatted byte buffer.
///
/// The unit's first byte is the NAL header: the low 5 bits are
/// the [`NalUnit::nal_type`] (0..=31 per H.264 Table 7-1), the
/// next 2 bits are `nal_ref_idc`, and the high bit is the
/// `forbidden_zero_bit` (must be 0; we do NOT enforce — Tesla
/// is the only producer and is well-behaved).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NalUnit<'a> {
    /// Lower 5 bits of `payload[0]`. Pre-extracted for
    /// branch-friendly filtering in the SEI walker.
    pub nal_type: u8,
    /// Full NAL unit bytes, including the header byte at
    /// index 0. Length is the raw prefix from the AVCC stream.
    pub payload: &'a [u8],
}

impl NalUnit<'_> {
    /// SEI (Supplemental Enhancement Information) NAL type per
    /// ITU-T H.264 Table 7-1. Tesla's per-frame telemetry lives
    /// inside these.
    pub const NAL_TYPE_SEI: u8 = 6;
    /// IDR slice (instantaneous decoder refresh, i.e. keyframe).
    /// Used by the v1 walker as a frame-boundary marker.
    pub const NAL_TYPE_IDR: u8 = 5;
    /// Non-IDR slice (P/B frame). Also a frame-boundary marker.
    pub const NAL_TYPE_NON_IDR_SLICE: u8 = 1;
}

/// Errors emitted by the AVCC NAL walker.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NalError {
    /// The remaining buffer is shorter than a 4-byte length
    /// prefix but is non-zero — the stream is truncated in the
    /// middle of a length field. Tesla MP4s sometimes have
    /// trailing zero padding inside `mdat`; the walker treats
    /// that as end-of-stream silently (see [`AvccIter::next`]).
    TruncatedLengthPrefix {
        /// Bytes remaining before the length field would end.
        remaining: usize,
    },
    /// The length prefix names a NAL unit that would extend
    /// past the buffer. Malicious or corrupt clip.
    LengthOverrun {
        /// Length the prefix claims, in bytes.
        claimed: u32,
        /// Bytes actually remaining after the prefix.
        available: usize,
    },
    /// The length prefix is zero. Some encoders pad with
    /// `\x00\x00\x00\x00` in `mdat`; we stop iteration on this
    /// rather than returning empty units, matching v1's
    /// `if nal_size < 1: break` guard.
    EmptyNalUnit,
}

impl std::fmt::Display for NalError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::TruncatedLengthPrefix { remaining } => {
                write!(f, "AVCC length prefix truncated: {remaining} byte(s) left")
            }
            Self::LengthOverrun { claimed, available } => {
                write!(
                    f,
                    "AVCC NAL length {claimed} exceeds available {available} byte(s)"
                )
            }
            Self::EmptyNalUnit => f.write_str("AVCC NAL unit with zero-length prefix"),
        }
    }
}

impl std::error::Error for NalError {}

/// Iterator over NAL units in an AVCC byte buffer (typically
/// the contents of an MP4 `mdat` box).
///
/// Stops cleanly at end of buffer, on a zero-length prefix
/// (matching v1's `nal_size < 1: break` guard, which absorbs
/// the trailing zero padding some encoders emit), and on a
/// truncated final length field. A length prefix that names a
/// unit longer than the remaining buffer yields a single
/// [`NalError::LengthOverrun`] error then stops.
#[derive(Debug, Clone)]
pub struct AvccIter<'a> {
    rest: &'a [u8],
    done: bool,
}

impl<'a> AvccIter<'a> {
    /// Construct an iterator over `buf`. `buf` should be the
    /// raw contents of an `mdat` box (no MP4 box header — that
    /// is the caller's responsibility via [`super::mp4`]).
    #[must_use]
    pub fn new(buf: &'a [u8]) -> Self {
        Self {
            rest: buf,
            done: false,
        }
    }
}

impl<'a> Iterator for AvccIter<'a> {
    type Item = Result<NalUnit<'a>, NalError>;

    #[allow(clippy::indexing_slicing)] // every [..] / [n] below is preceded by an explicit `self.rest.len() < N` check
    fn next(&mut self) -> Option<Self::Item> {
        if self.done {
            return None;
        }
        if self.rest.is_empty() {
            return None;
        }
        if self.rest.len() < AVCC_LENGTH_PREFIX_BYTES {
            self.done = true;
            return Some(Err(NalError::TruncatedLengthPrefix {
                remaining: self.rest.len(),
            }));
        }
        // SAFETY (panic-free): bounds checked above.
        let len_bytes: [u8; 4] = [self.rest[0], self.rest[1], self.rest[2], self.rest[3]];
        let nal_size = u32::from_be_bytes(len_bytes);
        self.rest = &self.rest[AVCC_LENGTH_PREFIX_BYTES..];
        if nal_size == 0 {
            // Stop silently on zero-length padding, matching v1.
            self.done = true;
            return None;
        }
        let nal_size_usize = nal_size as usize;
        if nal_size_usize > self.rest.len() {
            self.done = true;
            return Some(Err(NalError::LengthOverrun {
                claimed: nal_size,
                available: self.rest.len(),
            }));
        }
        let (unit, after) = self.rest.split_at(nal_size_usize);
        self.rest = after;
        let nal_type = unit[0] & 0x1F;
        Some(Ok(NalUnit {
            nal_type,
            payload: unit,
        }))
    }
}

/// Strip H.264 emulation-prevention bytes from a NAL payload.
///
/// H.264 inserts a single `0x03` byte after any two consecutive
/// `0x00` bytes inside a NAL unit so the byte stream cannot
/// accidentally form an Annex-B start code (`0x000001` or
/// `0x00000001`). The protobuf payload Tesla embeds is opaque
/// binary data that can perfectly well contain `0x000000`,
/// `0x000001`, etc., so the encoder always inserts these
/// preventions and the decoder must always strip them before
/// handing the bytes to protobuf.
///
/// Returns:
/// - [`Cow::Borrowed(input)`](Cow::Borrowed) if no preventions
///   were found (the common case for short NAL units that happen
///   not to contain `0x0000`).
/// - [`Cow::Owned(Vec<u8>)`](Cow::Owned) with the bytes stripped
///   otherwise.
///
/// The two-zero state machine matches v1 line-for-line:
/// `zeros >= 2 and byte == 0x03 → drop, reset zeros to 0`;
/// otherwise `zeros = zeros + 1 if byte == 0 else 0`.
#[must_use]
#[allow(clippy::indexing_slicing)] // `first_idx` is in 0..input.len() (returned by find_emulation_prevention)
pub fn strip_emulation_prevention(input: &[u8]) -> Cow<'_, [u8]> {
    // Fast scan: find the first prevention triple (00 00 03)
    // without allocating. If there isn't one, return Borrowed.
    let first = find_emulation_prevention(input);
    let Some(first_idx) = first else {
        return Cow::Borrowed(input);
    };
    // Allocate once with a sane size hint. Tesla SEI payloads
    // are ~100-300 bytes; preventions appear maybe once per
    // ~32 bytes worst case, so capacity = input length is a
    // safe upper bound and zero re-allocs.
    let mut out = Vec::with_capacity(input.len());
    out.extend_from_slice(&input[..first_idx]);
    let mut zeros = 0u8;
    // Re-count zeros across the prefix we copied — could end on
    // 2 zeros (since first_idx points AT the 0x03). Saturating
    // add avoids a panic on a pathological all-zeros prefix.
    for &b in &input[..first_idx] {
        zeros = if b == 0 { zeros.saturating_add(1) } else { 0 };
    }
    // We're standing on the 0x03 at first_idx; drop it and
    // continue from first_idx + 1 with the state machine.
    zeros = 0;
    for &b in &input[first_idx + 1..] {
        if zeros >= 2 && b == 0x03 {
            zeros = 0;
            continue;
        }
        out.push(b);
        zeros = if b == 0 { zeros.saturating_add(1) } else { 0 };
    }
    Cow::Owned(out)
}

/// Return the index of the first emulation-prevention byte
/// (`0x03` preceded by two zeros) in `buf`, or `None` if none.
/// Used by [`strip_emulation_prevention`] to skip the
/// allocation in the common no-prevention case.
fn find_emulation_prevention(buf: &[u8]) -> Option<usize> {
    let mut zeros = 0u8;
    for (i, &b) in buf.iter().enumerate() {
        if zeros >= 2 && b == 0x03 {
            return Some(i);
        }
        zeros = if b == 0 { zeros.saturating_add(1) } else { 0 };
    }
    None
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::expect_used,
        clippy::indexing_slicing,
        clippy::panic,
        clippy::unwrap_used,
        clippy::useless_vec
    )]

    use super::*;

    // ───────────────────────── AvccIter ────────────────────────

    fn make_avcc_unit(nal_header_byte: u8, body: &[u8]) -> Vec<u8> {
        let mut v = Vec::new();
        let nal_size = u32::try_from(1 + body.len()).unwrap();
        v.extend_from_slice(&nal_size.to_be_bytes());
        v.push(nal_header_byte);
        v.extend_from_slice(body);
        v
    }

    #[test]
    fn iter_empty_buffer_yields_nothing() {
        let mut it = AvccIter::new(&[]);
        assert!(it.next().is_none());
    }

    #[test]
    fn iter_one_sei_unit_yields_one_then_stops() {
        // NAL header = 0x06 → SEI (type 6, ref_idc 0)
        let buf = make_avcc_unit(0x06, &[0xAA, 0xBB, 0xCC]);
        let units: Vec<_> = AvccIter::new(&buf).collect::<Result<_, _>>().unwrap();
        assert_eq!(units.len(), 1);
        assert_eq!(units[0].nal_type, NalUnit::NAL_TYPE_SEI);
        assert_eq!(units[0].payload, &[0x06, 0xAA, 0xBB, 0xCC]);
    }

    #[test]
    fn iter_multiple_units_yields_in_order_with_correct_types() {
        let mut buf = Vec::new();
        // SEI, then IDR, then non-IDR slice
        buf.extend_from_slice(&make_avcc_unit(0x06, &[0x05]));
        buf.extend_from_slice(&make_avcc_unit(0x25, &[0xDE, 0xAD])); // 0x25 & 0x1F = 5
        buf.extend_from_slice(&make_avcc_unit(0x41, &[0xBE])); // 0x41 & 0x1F = 1
        let types: Vec<u8> = AvccIter::new(&buf)
            .map(Result::unwrap)
            .map(|u| u.nal_type)
            .collect();
        assert_eq!(
            types,
            vec![
                NalUnit::NAL_TYPE_SEI,
                NalUnit::NAL_TYPE_IDR,
                NalUnit::NAL_TYPE_NON_IDR_SLICE,
            ]
        );
    }

    #[test]
    fn iter_zero_length_prefix_stops_silently_matching_v1() {
        // v1: `if nal_size < 1: break`
        let mut buf = make_avcc_unit(0x06, &[0xAA]);
        buf.extend_from_slice(&[0x00, 0x00, 0x00, 0x00]); // padding
        let units: Vec<_> = AvccIter::new(&buf).collect::<Result<_, _>>().unwrap();
        assert_eq!(units.len(), 1, "padding must end iteration without error");
    }

    #[test]
    fn iter_truncated_length_prefix_errors_once_then_stops() {
        let mut buf = make_avcc_unit(0x06, &[0xAA]);
        buf.extend_from_slice(&[0x00, 0x00]); // 2 bytes — too short for a prefix
        let mut it = AvccIter::new(&buf);
        let _good = it.next().unwrap().expect("first unit fine");
        let err = it.next().unwrap().expect_err("truncated prefix errors");
        assert!(matches!(
            err,
            NalError::TruncatedLengthPrefix { remaining: 2 }
        ));
        assert!(it.next().is_none(), "iterator stops after error");
    }

    #[test]
    fn iter_length_overrun_errors_once_then_stops() {
        // Claim 100 bytes but only 3 follow.
        let mut buf = Vec::new();
        buf.extend_from_slice(&100u32.to_be_bytes());
        buf.extend_from_slice(&[0xDE, 0xAD, 0xBE]);
        let mut it = AvccIter::new(&buf);
        let err = it.next().unwrap().expect_err("overrun errors");
        assert!(
            matches!(
                err,
                NalError::LengthOverrun {
                    claimed: 100,
                    available: 3
                }
            ),
            "got {err:?}"
        );
        assert!(it.next().is_none());
    }

    #[test]
    fn iter_nal_type_strips_high_three_bits() {
        // 0xE6 = 0b1110_0110 — forbidden bit + ref_idc set, low 5 bits = 6 (SEI)
        let buf = make_avcc_unit(0xE6, &[]);
        let unit = AvccIter::new(&buf).next().unwrap().unwrap();
        assert_eq!(unit.nal_type, NalUnit::NAL_TYPE_SEI);
    }

    // ───────────────────── emulation prevention ────────────────

    #[test]
    fn strip_returns_borrowed_when_no_preventions_present() {
        let input = [0xAA, 0xBB, 0xCC, 0x00, 0x01, 0x00, 0xFF];
        let out = strip_emulation_prevention(&input);
        assert!(matches!(out, Cow::Borrowed(_)));
        assert_eq!(&*out, &input);
    }

    #[test]
    fn strip_removes_single_prevention_byte() {
        // 0x00 0x00 0x03 0xAA → 0x00 0x00 0xAA (drop 0x03)
        let input = [0x00, 0x00, 0x03, 0xAA];
        let out = strip_emulation_prevention(&input);
        assert_eq!(&*out, &[0x00, 0x00, 0xAA][..]);
    }

    #[test]
    fn strip_preserves_03_when_not_preceded_by_two_zeros() {
        // Only one zero → 0x03 stays.
        let input = [0x00, 0x03, 0xAA];
        let out = strip_emulation_prevention(&input);
        assert!(matches!(out, Cow::Borrowed(_)));
        assert_eq!(&*out, &input);
    }

    #[test]
    fn strip_resets_zero_counter_after_dropping() {
        // 0x00 0x00 0x03 0x03 → first 0x03 dropped, second 0x03
        // kept (zero counter reset to 0 by the drop).
        let input = [0x00, 0x00, 0x03, 0x03];
        let out = strip_emulation_prevention(&input);
        assert_eq!(&*out, &[0x00, 0x00, 0x03][..]);
    }

    #[test]
    fn strip_handles_consecutive_prevention_triples() {
        // 0x00 0x00 0x03 0x00 0x00 0x03 0xFF →
        // 0x00 0x00 0x00 0x00 0xFF (both 0x03s dropped)
        let input = [0x00, 0x00, 0x03, 0x00, 0x00, 0x03, 0xFF];
        let out = strip_emulation_prevention(&input);
        assert_eq!(&*out, &[0x00, 0x00, 0x00, 0x00, 0xFF][..]);
    }

    #[test]
    fn strip_handles_three_zeros_then_03() {
        // 0x00 0x00 0x00 0x03 → counter is 3 after third zero,
        // still ≥ 2 so the 0x03 is dropped.
        let input = [0x00, 0x00, 0x00, 0x03];
        let out = strip_emulation_prevention(&input);
        assert_eq!(&*out, &[0x00, 0x00, 0x00][..]);
    }

    #[test]
    fn strip_empty_input_returns_empty_borrowed() {
        let out = strip_emulation_prevention(&[]);
        assert!(matches!(out, Cow::Borrowed(_)));
        assert!(out.is_empty());
    }

    #[test]
    fn strip_long_payload_with_one_prevention_in_middle_matches_v1_semantics() {
        // Build a payload whose only 0x000003 lives at byte 50,
        // and confirm only that one 0x03 is dropped.
        let mut input = vec![0xAB; 100];
        input[48] = 0x00;
        input[49] = 0x00;
        input[50] = 0x03;
        let out = strip_emulation_prevention(&input);
        let mut expected = vec![0xAB; 99];
        expected[48] = 0x00;
        expected[49] = 0x00;
        assert_eq!(&*out, &expected[..]);
    }
}
