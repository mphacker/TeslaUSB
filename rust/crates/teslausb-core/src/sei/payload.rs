//! H.264 SEI envelope + Tesla-specific payload framing
//! (Phase 4b.1b).
#![allow(clippy::doc_markdown)] // domain terms ("SEI", "NAL", "Tesla", "H.264") need not be backticked
#![allow(clippy::indexing_slicing)] // every [..] / [n] is guarded by an explicit `nal_data.len() < N` or `i + N >= len` check
//!
//! Layered on top of `nal` + `mp4` (Phase 4b.1a), this module
//! turns a raw SEI NAL unit (as yielded by
//! [`super::nal::AvccIter`]) into the protobuf-ready byte slice
//! Tesla embeds inside the H.264 SEI `user_data_unregistered`
//! payload.
//!
//! ## The two layers we have to strip
//!
//! 1. **H.264 SEI envelope** (ITU-T H.264 §7.3.2.3.1). Inside
//!    every SEI NAL unit, after the 1-byte NAL header, the
//!    payload is a sequence of `(payload_type, payload_size,
//!    payload_bytes)` triples. `payload_type` is encoded as a
//!    series of `0xFF` bytes followed by a non-`0xFF` byte;
//!    the sum is the type. `payload_size` is encoded the same
//!    way. Tesla always uses `payload_type = 5`
//!    (`user_data_unregistered`), but in principle a single SEI
//!    NAL can carry multiple payloads — we handle the general
//!    case via [`parse_h264_sei_envelope`].
//!
//! 2. **Tesla quirk** (NOT in the H.264 spec). Tesla packs the
//!    `user_data_unregistered` payload as:
//!
//!    ```text
//!    [NAL header byte (0x06)]
//!    [SEI payload_type byte (0x05)]
//!    [SEI payload_size byte]
//!    [variable count of 0x42 padding bytes (≥ 1)]
//!    [0x69 marker byte]
//!    [protobuf payload (with emulation-prevention bytes)]
//!    [0x80 RBSP trailing byte]
//!    ```
//!
//!    The 0x42 padding count is NOT signalled — the parser scans
//!    until the first non-0x42 byte, which must be 0x69.
//!    Mirrors v1's `_decode_sei_nal` at lines 350-382 of
//!    `scripts/web/services/sei_parser.py`.
//!
//! ## Output
//!
//! [`extract_tesla_payload`] returns the bytes between the
//! `0x69` marker and the `0x80` RBSP trailing byte, with
//! H.264 emulation-prevention bytes already stripped by
//! [`super::nal::strip_emulation_prevention`]. The result is
//! ready to feed directly to the protobuf decoder (Phase
//! 4b.1c).

use std::borrow::Cow;

use super::nal::strip_emulation_prevention;

/// SEI `payload_type = 5` per ITU-T H.264 Table 7-1 — the only
/// type Tesla emits. We document it as a public constant so
/// the indexer can filter quickly without re-decoding the
/// envelope.
pub const SEI_PAYLOAD_TYPE_USER_DATA_UNREGISTERED: u32 = 5;

/// Tesla's mandatory padding byte inside the
/// `user_data_unregistered` payload. The padding always
/// appears in a run of length ≥ 1; we scan rather than count.
pub const TESLA_PADDING_BYTE: u8 = 0x42;

/// Tesla's mandatory marker byte that separates the padding
/// run from the protobuf payload.
pub const TESLA_PROTOBUF_MARKER: u8 = 0x69;

/// H.264 RBSP trailing byte that terminates the NAL unit's
/// payload. Always the last byte of the SEI NAL.
pub const RBSP_TRAILING_BYTE: u8 = 0x80;

/// Errors emitted by the SEI envelope / Tesla framing decoder.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SeiError {
    /// The NAL unit is shorter than the minimum frame Tesla
    /// ever emits (NAL header + 0x05 type + size + ≥1 padding +
    /// 0x69 + ≥1 protobuf byte + 0x80 = 7 bytes). v1 uses
    /// `len < 4` which is too lax; we tighten to the real
    /// minimum.
    TooShort {
        /// Actual length the parser saw, in bytes.
        len: usize,
    },
    /// The parser walked past the end of the NAL unit looking
    /// for the next byte (typically the `0x69` marker or the
    /// trailing byte). Indicates a truncated or malformed SEI.
    UnexpectedEnd,
    /// No `0x42` padding bytes were found before the `0x69`
    /// marker. Tesla always emits at least one — its absence
    /// suggests a non-Tesla SEI we should skip.
    NoTeslaPadding,
    /// The byte after the `0x42` padding run is not `0x69`.
    /// Mirrors v1's `nal_data[i] != 0x69` rejection.
    MissingProtobufMarker {
        /// The byte we found instead of `0x69`.
        found: u8,
    },
    /// The H.264 SEI envelope's `payload_type` or
    /// `payload_size` overflowed `u32` due to a pathological
    /// run of `0xFF` bytes. Indicates a malicious clip.
    EnvelopeFieldOverflow,
}

impl std::fmt::Display for SeiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::TooShort { len } => {
                write!(f, "SEI NAL too short to be Tesla framing: {len} byte(s)")
            }
            Self::UnexpectedEnd => f.write_str("SEI NAL ended before required marker"),
            Self::NoTeslaPadding => f.write_str("SEI NAL missing the mandatory 0x42 padding run"),
            Self::MissingProtobufMarker { found } => write!(
                f,
                "SEI NAL byte after padding is 0x{found:02X}, expected 0x69"
            ),
            Self::EnvelopeFieldOverflow => {
                f.write_str("H.264 SEI payload_type or payload_size overflowed u32")
            }
        }
    }
}

impl std::error::Error for SeiError {}

/// Minimum useful NAL length: 1 (header) + 1 (payload_type)
/// + 1 (payload_size) + 1 (≥ 1 padding byte) + 1 (0x69)
/// + 1 (≥ 1 protobuf byte) + 1 (0x80) = 7.
const MIN_TESLA_SEI_LEN: usize = 7;

/// Extract the protobuf payload from a Tesla SEI NAL unit.
///
/// `nal_data` is the **full** NAL unit as yielded by
/// [`super::nal::AvccIter`], starting at the 1-byte NAL header
/// (whose low 5 bits are 6 for SEI).
///
/// Returns the post-emulation-prevention-strip bytes between
/// the `0x69` marker and the final `0x80` RBSP trailing byte.
/// The result is a [`Cow`]: [`Cow::Borrowed`] when no
/// emulation-prevention triples were present (the common case),
/// [`Cow::Owned`] when at least one `0x03` had to be removed.
///
/// # Errors
///
/// Returns the appropriate [`SeiError`] variant for any
/// framing violation. v1 collapses all such errors to
/// `return None`; we return typed errors so the indexer can
/// log Tesla-vs-non-Tesla SEI distinctly.
pub fn extract_tesla_payload(nal_data: &[u8]) -> Result<Cow<'_, [u8]>, SeiError> {
    if nal_data.len() < MIN_TESLA_SEI_LEN {
        return Err(SeiError::TooShort {
            len: nal_data.len(),
        });
    }
    // v1 hardcodes "skip first 3 bytes" — that is the NAL
    // header byte (0x06), the SEI payload_type byte (0x05),
    // and the SEI payload_size byte. We do the same to
    // preserve byte-for-byte parity with the production parser.
    // (parse_h264_sei_envelope is provided for callers that
    // need spec-correct decoding of multi-payload SEI NALs.)
    let mut i: usize = 3;
    // Scan the 0x42 padding run.
    while i < nal_data.len() && nal_data[i] == TESLA_PADDING_BYTE {
        i = i.saturating_add(1);
    }
    if i <= 3 {
        return Err(SeiError::NoTeslaPadding);
    }
    if i >= nal_data.len() {
        return Err(SeiError::UnexpectedEnd);
    }
    if nal_data[i] != TESLA_PROTOBUF_MARKER {
        return Err(SeiError::MissingProtobufMarker { found: nal_data[i] });
    }
    // Protobuf payload is `[i + 1 .. len - 1]` — drop the
    // trailing 0x80 RBSP byte. We verified len ≥ MIN_TESLA_SEI_LEN
    // so `i + 1 < len - 1` is well-defined when the padding
    // run is exactly one byte AND there is at least one
    // protobuf byte; in extreme degenerate cases (padding
    // consumed everything but the trailing byte) we return
    // an empty borrowed slice rather than erroring, mirroring
    // v1's lenient `nal_data[i+1:len-1]` slicing semantics.
    let start = i.saturating_add(1);
    let end = nal_data.len().saturating_sub(1);
    let raw = if start <= end {
        &nal_data[start..end]
    } else {
        &[][..]
    };
    Ok(strip_emulation_prevention(raw))
}

/// A single SEI payload as decoded from the generic H.264
/// envelope.
///
/// `payload_type` is the value summed from the `0xFF`+terminator
/// chain (`5` for Tesla). `payload` borrows the raw payload bytes
/// from the input — emulation-prevention bytes are NOT yet
/// stripped (the caller decides which payload to keep).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Sei<'a> {
    /// SEI `payload_type` per ITU-T H.264 Table 7-1.
    pub payload_type: u32,
    /// Raw payload bytes. Strip emulation prevention before
    /// decoding (use [`super::nal::strip_emulation_prevention`]).
    pub payload: &'a [u8],
}

/// Decode an H.264 SEI NAL unit's envelope into one or more
/// `(payload_type, payload_size, payload_bytes)` triples per
/// ITU-T H.264 §7.3.2.3.1.
///
/// `nal_data` is the full NAL unit including the 1-byte NAL
/// header. Returns the list of decoded payloads in order.
///
/// This is the **spec-correct** path; [`extract_tesla_payload`]
/// is the fast path that matches v1's behaviour and skips the
/// envelope decode because Tesla's framing is fixed.
///
/// # Errors
///
/// Returns [`SeiError::UnexpectedEnd`] if any field is
/// truncated, [`SeiError::EnvelopeFieldOverflow`] if a `0xFF`
/// chain would overflow `u32` (effectively a malicious-input
/// guard — even a 4-billion-byte payload would not fit in
/// memory, let alone a NAL unit).
pub fn parse_h264_sei_envelope(nal_data: &[u8]) -> Result<Vec<Sei<'_>>, SeiError> {
    if nal_data.is_empty() {
        return Err(SeiError::UnexpectedEnd);
    }
    // Skip the 1-byte NAL header.
    let mut i: usize = 1;
    let len = nal_data.len();
    let mut out = Vec::new();
    while i < len {
        // Stop on the RBSP trailing byte (0x80) — it lives at
        // the end of the payload list per H.264 §7.3.2.11.
        // Some encoders also pad with extra trailing zeros
        // after the 0x80; absorb either.
        if nal_data[i] == RBSP_TRAILING_BYTE {
            break;
        }
        let (payload_type, after_type) = decode_ff_chain(nal_data, i)?;
        let (payload_size, after_size) = decode_ff_chain(nal_data, after_type)?;
        let payload_size_usize =
            usize::try_from(payload_size).map_err(|_| SeiError::EnvelopeFieldOverflow)?;
        let payload_end = after_size
            .checked_add(payload_size_usize)
            .ok_or(SeiError::EnvelopeFieldOverflow)?;
        if payload_end > len {
            return Err(SeiError::UnexpectedEnd);
        }
        out.push(Sei {
            payload_type,
            payload: &nal_data[after_size..payload_end],
        });
        i = payload_end;
    }
    Ok(out)
}

/// Decode a single `0xFF`-chain field per H.264 §7.3.2.3.1:
/// sum the `0xFF` bytes plus the first non-`0xFF` byte.
///
/// Returns `(value, position_after_terminator)`.
fn decode_ff_chain(buf: &[u8], start: usize) -> Result<(u32, usize), SeiError> {
    let mut value: u32 = 0;
    let mut i = start;
    loop {
        if i >= buf.len() {
            return Err(SeiError::UnexpectedEnd);
        }
        let b = buf[i];
        value = value
            .checked_add(u32::from(b))
            .ok_or(SeiError::EnvelopeFieldOverflow)?;
        i = i.saturating_add(1);
        if b != 0xFF {
            return Ok((value, i));
        }
    }
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

    // ─────────────────── Tesla framing fast path ──────────────

    fn tesla_sei_nal(padding_count: usize, protobuf: &[u8]) -> Vec<u8> {
        // [0x06 NAL header][0x05 sei_type][size byte][0x42 × pad][0x69][protobuf...][0x80]
        let mut v = Vec::new();
        v.push(0x06); // SEI NAL header (forbidden=0, ref_idc=0, type=6)
        v.push(0x05); // sei payload_type = user_data_unregistered
        v.push(u8::try_from(padding_count + 1 + protobuf.len() + 1).unwrap_or(0xFF));
        v.extend(std::iter::repeat_n(TESLA_PADDING_BYTE, padding_count));
        v.push(TESLA_PROTOBUF_MARKER);
        v.extend_from_slice(protobuf);
        v.push(RBSP_TRAILING_BYTE);
        v
    }

    #[test]
    fn extract_tesla_payload_returns_protobuf_bytes_in_happy_path() {
        let proto = [0xAA, 0xBB, 0xCC, 0xDD, 0xEE];
        let nal = tesla_sei_nal(3, &proto);
        let out = extract_tesla_payload(&nal).unwrap();
        assert_eq!(&*out, &proto[..]);
        // No emulation-prevention triples, expect Borrowed.
        assert!(matches!(out, Cow::Borrowed(_)));
    }

    #[test]
    fn extract_tesla_payload_strips_emulation_prevention_when_present() {
        // 0x00 0x00 0x03 0xAA → 0x00 0x00 0xAA after strip
        let proto = [0x00, 0x00, 0x03, 0xAA];
        let nal = tesla_sei_nal(2, &proto);
        let out = extract_tesla_payload(&nal).unwrap();
        assert_eq!(&*out, &[0x00, 0x00, 0xAA][..]);
        assert!(matches!(out, Cow::Owned(_)));
    }

    #[test]
    fn extract_tesla_payload_works_with_minimum_one_padding_byte() {
        let proto = [0x42]; // protobuf may itself start with 0x42 — that's fine, we already passed the padding scan by then
        let nal = tesla_sei_nal(1, &proto);
        let out = extract_tesla_payload(&nal).unwrap();
        assert_eq!(&*out, &proto[..]);
    }

    #[test]
    fn extract_tesla_payload_rejects_too_short_nal() {
        let nal = [0x06, 0x05, 0x02, 0x42, 0x69]; // 5 bytes < 7
        let err = extract_tesla_payload(&nal).unwrap_err();
        assert!(matches!(err, SeiError::TooShort { len: 5 }));
    }

    #[test]
    fn extract_tesla_payload_rejects_no_padding_run() {
        // Skip 3 bytes, then byte[3] is not 0x42 → NoTeslaPadding.
        let nal = vec![0x06, 0x05, 0x04, 0x69, 0xAA, 0xBB, 0x80];
        let err = extract_tesla_payload(&nal).unwrap_err();
        assert!(matches!(err, SeiError::NoTeslaPadding));
    }

    #[test]
    fn extract_tesla_payload_rejects_wrong_byte_after_padding() {
        // After 0x42 padding, next byte is 0xAB (not 0x69).
        let nal = vec![0x06, 0x05, 0x05, 0x42, 0x42, 0xAB, 0xCC, 0x80];
        let err = extract_tesla_payload(&nal).unwrap_err();
        assert!(matches!(
            err,
            SeiError::MissingProtobufMarker { found: 0xAB }
        ));
    }

    #[test]
    fn extract_tesla_payload_handles_long_padding_run() {
        // 32 padding bytes — well beyond Tesla's typical 3-10.
        let proto = vec![0x01, 0x02, 0x03_u8];
        let nal = tesla_sei_nal(32, &proto);
        let out = extract_tesla_payload(&nal).unwrap();
        // 0x01 0x02 0x03 has no preceding zeros, so 0x03 stays.
        assert_eq!(&*out, &proto[..]);
    }

    #[test]
    fn extract_tesla_payload_rejects_unterminated_padding_run() {
        // All bytes after position 3 are 0x42 — never hit 0x69.
        let nal = vec![0x06, 0x05, 0x06, 0x42, 0x42, 0x42, 0x42];
        let err = extract_tesla_payload(&nal).unwrap_err();
        // len = 7 (meets MIN), so we proceed past the length
        // check; scan walks to EOF → UnexpectedEnd.
        assert!(matches!(err, SeiError::UnexpectedEnd));
    }

    // ─────────────────── Generic H.264 SEI envelope ───────────

    #[test]
    fn parse_h264_sei_envelope_decodes_single_user_data_unregistered() {
        // [0x06][type=5][size=4][AA BB CC DD][0x80]
        let nal = [0x06, 0x05, 0x04, 0xAA, 0xBB, 0xCC, 0xDD, 0x80];
        let v = parse_h264_sei_envelope(&nal).unwrap();
        assert_eq!(v.len(), 1);
        assert_eq!(v[0].payload_type, 5);
        assert_eq!(v[0].payload, &[0xAA, 0xBB, 0xCC, 0xDD]);
    }

    #[test]
    fn parse_h264_sei_envelope_decodes_ff_chain_payload_type() {
        // type = 0xFF + 0xFF + 0x01 = 511; size = 2; payload = [0x42, 0x42]
        let nal = [0x06, 0xFF, 0xFF, 0x01, 0x02, 0x42, 0x42, 0x80];
        let v = parse_h264_sei_envelope(&nal).unwrap();
        assert_eq!(v.len(), 1);
        assert_eq!(v[0].payload_type, 0xFF + 0xFF + 1);
        assert_eq!(v[0].payload, &[0x42, 0x42]);
    }

    #[test]
    fn parse_h264_sei_envelope_decodes_ff_chain_payload_size() {
        // type = 5; size = 0xFF + 0x02 = 257? no, build smaller for test
        // type = 5; size = 0xFF + 0x01 = 256
        let mut nal = vec![0x06, 0x05, 0xFF, 0x01];
        nal.extend(std::iter::repeat_n(0x77_u8, 256));
        nal.push(0x80);
        let v = parse_h264_sei_envelope(&nal).unwrap();
        assert_eq!(v.len(), 1);
        assert_eq!(v[0].payload_type, 5);
        assert_eq!(v[0].payload.len(), 256);
    }

    #[test]
    fn parse_h264_sei_envelope_decodes_multiple_payloads_in_order() {
        // [hdr][t=1, s=2, aa bb][t=5, s=3, cc dd ee][0x80]
        let nal = [
            0x06, 0x01, 0x02, 0xAA, 0xBB, 0x05, 0x03, 0xCC, 0xDD, 0xEE, 0x80,
        ];
        let v = parse_h264_sei_envelope(&nal).unwrap();
        assert_eq!(v.len(), 2);
        assert_eq!(v[0].payload_type, 1);
        assert_eq!(v[0].payload, &[0xAA, 0xBB]);
        assert_eq!(v[1].payload_type, 5);
        assert_eq!(v[1].payload, &[0xCC, 0xDD, 0xEE]);
    }

    #[test]
    fn parse_h264_sei_envelope_rejects_truncated_payload() {
        // Claims size=10 but only 2 payload bytes are present.
        let nal = [0x06, 0x05, 0x0A, 0xAA, 0xBB, 0x80];
        let err = parse_h264_sei_envelope(&nal).unwrap_err();
        assert!(matches!(err, SeiError::UnexpectedEnd));
    }

    #[test]
    fn parse_h264_sei_envelope_rejects_truncated_type_field() {
        // Only header + a single 0xFF, no terminator.
        let nal = [0x06, 0xFF];
        let err = parse_h264_sei_envelope(&nal).unwrap_err();
        assert!(matches!(err, SeiError::UnexpectedEnd));
    }

    #[test]
    fn parse_h264_sei_envelope_rejects_empty_nal() {
        let err = parse_h264_sei_envelope(&[]).unwrap_err();
        assert!(matches!(err, SeiError::UnexpectedEnd));
    }

    #[test]
    fn parse_h264_sei_envelope_stops_cleanly_at_rbsp_trailing() {
        // Two payloads then 0x80 — trailing zeros after 0x80
        // (encoder padding) must not become a third "payload".
        let nal = [0x06, 0x01, 0x01, 0xAA, 0x05, 0x01, 0xBB, 0x80, 0x00, 0x00];
        let v = parse_h264_sei_envelope(&nal).unwrap();
        assert_eq!(v.len(), 2);
    }

    #[test]
    fn parse_h264_sei_envelope_handles_zero_length_payload() {
        // type = 5, size = 0 → empty payload.
        let nal = [0x06, 0x05, 0x00, 0x80];
        let v = parse_h264_sei_envelope(&nal).unwrap();
        assert_eq!(v.len(), 1);
        assert!(v[0].payload.is_empty());
    }

    #[test]
    fn decode_ff_chain_sums_bytes_correctly() {
        let buf = [0xFF, 0xFF, 0xFF, 0x05];
        let (value, after) = decode_ff_chain(&buf, 0).unwrap();
        assert_eq!(value, 0xFF * 3 + 5);
        assert_eq!(after, 4);
    }

    #[test]
    fn decode_ff_chain_immediate_terminator() {
        let buf = [0x07_u8];
        let (value, after) = decode_ff_chain(&buf, 0).unwrap();
        assert_eq!(value, 7);
        assert_eq!(after, 1);
    }

    #[test]
    fn decode_ff_chain_overflow_is_rejected() {
        // Need to push value past u32::MAX. (u32::MAX / 255) + 1
        // bytes of 0xFF + a terminator would overflow.
        let bytes_needed = (u32::MAX / 255) as usize + 2;
        let mut buf = vec![0xFF_u8; bytes_needed];
        buf.push(0x00); // terminator
        let err = decode_ff_chain(&buf, 0).unwrap_err();
        assert!(matches!(err, SeiError::EnvelopeFieldOverflow));
    }
}
