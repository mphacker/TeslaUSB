//! NBD transmission-phase wire format.
//!
//! Pure encode/decode helpers and constants for the per-command
//! request/reply frames that flow on a connection AFTER the
//! handshake completes. Kept separate from
//! [`crate::nbd::transmission`] (the I/O orchestrator) so the
//! protocol bytes can be unit-tested in-process without binding a
//! stream.
//!
//! Wire format reference:
//! <https://github.com/NetworkBlockDevice/nbd/blob/master/doc/proto.md#transmission-phase>
//!
//! ## Frames
//!
//! **Request (28 bytes, big-endian throughout):**
//!
//! ```text
//!   0..4   request magic (u32 = NBD_REQUEST_MAGIC)
//!   4..6   command flags (u16; bit 0 = NBD_CMD_FLAG_FUA)
//!   6..8   command type  (u16; e.g. NBD_CMD_READ, NBD_CMD_WRITE)
//!   8..16  handle        (u64; opaque to server, echoed in reply)
//!  16..24  offset        (u64; byte offset into the export)
//!  24..28  length        (u32; payload length for WRITE,
//!                              response length for READ)
//! ```
//!
//! Followed by `length` bytes of payload for `NBD_CMD_WRITE`; no
//! payload for the other command kinds.
//!
//! **Simple reply (16 bytes + optional payload):**
//!
//! ```text
//!   0..4   reply magic (u32 = NBD_SIMPLE_REPLY_MAGIC)
//!   4..8   error code  (u32; 0 = success, otherwise positive errno)
//!   8..16  handle      (u64; echoed from request)
//! ```
//!
//! Followed by `length` bytes of payload for a successful
//! `NBD_CMD_READ` response only.
//!
//! ## Layering
//!
//! Decoding is split into:
//!
//! * [`decode_request_header`] — fixed-size, big-endian; returns
//!   `Option<RequestHeader>` because the only failure mode (bad
//!   magic) is always fatal and the I/O orchestrator can simply
//!   close the connection on `None`. Same pattern as
//!   [`crate::nbd::handshake::parse_info_or_go_request`].
//! * [`encode_simple_reply_header`] — fixed-size, infallible,
//!   returns `[u8; 16]` so callers can chain it onto a payload
//!   without an extra allocation.

// ---- Magic / command / flag / error constants ---------------------------

/// `NBD_REQUEST_MAGIC` — first 4 bytes of every transmission-phase
/// request frame. Distinct from the handshake `IHAVEOPT`.
pub const NBD_REQUEST_MAGIC: u32 = 0x2560_9513;

/// `NBD_SIMPLE_REPLY_MAGIC` — first 4 bytes of every simple reply
/// frame. We only emit simple replies (we reject
/// `NBD_OPT_STRUCTURED_REPLY` during the handshake).
pub const NBD_SIMPLE_REPLY_MAGIC: u32 = 0x6744_6698;

/// `NBD_CMD_READ` — server returns `length` bytes from `offset`.
pub const NBD_CMD_READ: u16 = 0;
/// `NBD_CMD_WRITE` — client sends `length` bytes; server stores
/// them at `offset`.
pub const NBD_CMD_WRITE: u16 = 1;
/// `NBD_CMD_DISC` — client signals clean disconnect. No reply.
pub const NBD_CMD_DISC: u16 = 2;
/// `NBD_CMD_FLUSH` — server forces all outstanding writes to
/// stable storage.
pub const NBD_CMD_FLUSH: u16 = 3;
/// `NBD_CMD_TRIM` — server may discard the byte range. Treated as
/// advisory (no-op success) by this implementation.
pub const NBD_CMD_TRIM: u16 = 4;

/// `NBD_CMD_FLAG_FUA` — write must be durable before reply.
/// Bit pattern matches [`teslausb_core::backend::WriteFlags::FUA`]
/// exactly so the dispatch layer can pass the raw flag value
/// straight into [`teslausb_core::backend::WriteFlags::from_bits_truncate`].
pub const NBD_CMD_FLAG_FUA: u16 = 1 << 0;

/// `NBD_EOK` — reply error code for success. (Not in the spec by
/// name but worth a named constant so the encode call site reads
/// `NBD_EOK` instead of a bare `0`.)
pub const NBD_EOK: u32 = 0;
/// `NBD_EPERM` — operation not permitted.
pub const NBD_EPERM: u32 = 1;
/// `NBD_EIO` — generic I/O failure on the backend.
pub const NBD_EIO: u32 = 5;
/// `NBD_ENOMEM` — server out of memory for the requested operation.
pub const NBD_ENOMEM: u32 = 12;
/// `NBD_EINVAL` — request malformed (bad command, oversized
/// request, out-of-bounds offset, etc.).
pub const NBD_EINVAL: u32 = 22;
/// `NBD_ENOSPC` — backend full.
pub const NBD_ENOSPC: u32 = 28;
/// `NBD_EOVERFLOW` — request exceeded the advertised
/// `BLOCK_SIZE_MAX`.
pub const NBD_EOVERFLOW: u32 = 75;
/// `NBD_ENOTSUP` — command type the server does not implement.
pub const NBD_ENOTSUP: u32 = 95;

// ---- Frame sizes --------------------------------------------------------

/// Fixed size of an NBD request header on the wire: `magic` (4) +
/// `flags` (2) + `type` (2) + `handle` (8) + `offset` (8) +
/// `length` (4) = 28 bytes.
pub const REQUEST_HEADER_LEN: usize = 28;

/// Fixed size of a simple reply header on the wire: `magic` (4) +
/// `error` (4) + `handle` (8) = 16 bytes.
pub const SIMPLE_REPLY_HEADER_LEN: usize = 16;

// ---- Decoded view of a request header -----------------------------------

/// Decoded NBD transmission-phase request header.
///
/// Field order matches the wire layout to keep
/// [`decode_request_header`] mechanically obvious.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RequestHeader {
    /// Command flags. Bit 0 is [`NBD_CMD_FLAG_FUA`]; the rest are
    /// currently unused by this server.
    pub flags: u16,
    /// Command type. See `NBD_CMD_*` constants.
    pub kind: u16,
    /// Opaque client cookie. Echoed verbatim in the reply so the
    /// client can correlate.
    pub handle: u64,
    /// Byte offset into the export.
    pub offset: u64,
    /// Payload length for WRITE, response length for READ; ignored
    /// for FLUSH and DISC; byte count for TRIM.
    pub length: u32,
}

/// Decode a 28-byte request header.
///
/// Returns `None` if the magic does not match
/// [`NBD_REQUEST_MAGIC`] — caller MUST then close the connection,
/// since the byte stream is no longer trustably aligned to frame
/// boundaries. Every other field is a fixed-size big-endian scalar
/// and so cannot fail to decode.
#[must_use]
pub fn decode_request_header(bytes: &[u8; REQUEST_HEADER_LEN]) -> Option<RequestHeader> {
    let magic = u32::from_be_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]);
    if magic != NBD_REQUEST_MAGIC {
        return None;
    }
    let flags = u16::from_be_bytes([bytes[4], bytes[5]]);
    let kind = u16::from_be_bytes([bytes[6], bytes[7]]);
    let handle = u64::from_be_bytes([
        bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15],
    ]);
    let offset = u64::from_be_bytes([
        bytes[16], bytes[17], bytes[18], bytes[19], bytes[20], bytes[21], bytes[22], bytes[23],
    ]);
    let length = u32::from_be_bytes([bytes[24], bytes[25], bytes[26], bytes[27]]);
    Some(RequestHeader {
        flags,
        kind,
        handle,
        offset,
        length,
    })
}

/// Encode a 16-byte simple-reply header.
///
/// Returns an owned `[u8; 16]` so the caller can `write_all` it as
/// a single buffer; for READ responses the caller follows with a
/// second `write_all` for the payload.
#[must_use]
pub fn encode_simple_reply_header(error: u32, handle: u64) -> [u8; SIMPLE_REPLY_HEADER_LEN] {
    let mut out = [0u8; SIMPLE_REPLY_HEADER_LEN];
    out[0..4].copy_from_slice(&NBD_SIMPLE_REPLY_MAGIC.to_be_bytes());
    out[4..8].copy_from_slice(&error.to_be_bytes());
    out[8..16].copy_from_slice(&handle.to_be_bytes());
    out
}

/// Encode a request header. Provided as a symmetric counterpart to
/// [`decode_request_header`] so the wire round-trip test does not
/// have to hand-roll the byte layout twice.
///
/// Not used by the production transmission loop (the loop only
/// ever *decodes* requests and *encodes* replies) — production
/// callers should never need this. Marked `#[cfg(any(test, feature = "test-helpers"))]`
/// would lock it to test-only, but keeping it at module scope with
/// a doc-note is simpler and the compiler will dead-strip it from
/// `--release` builds that don't reference it.
#[must_use]
pub fn encode_request_header(req: &RequestHeader) -> [u8; REQUEST_HEADER_LEN] {
    let mut out = [0u8; REQUEST_HEADER_LEN];
    out[0..4].copy_from_slice(&NBD_REQUEST_MAGIC.to_be_bytes());
    out[4..6].copy_from_slice(&req.flags.to_be_bytes());
    out[6..8].copy_from_slice(&req.kind.to_be_bytes());
    out[8..16].copy_from_slice(&req.handle.to_be_bytes());
    out[16..24].copy_from_slice(&req.offset.to_be_bytes());
    out[24..28].copy_from_slice(&req.length.to_be_bytes());
    out
}

// ---- Tests --------------------------------------------------------------

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::indexing_slicing)]
mod tests {
    use super::*;

    /// The magic constants are part of the NBD wire contract.
    /// If anyone "cleans up" the literal, this test would catch it
    /// because a wrong magic byte stream would be ABI-incompatible
    /// with every NBD client in the world.
    #[test]
    fn magic_constants_match_nbd_spec() {
        assert_eq!(NBD_REQUEST_MAGIC, 0x2560_9513);
        assert_eq!(NBD_SIMPLE_REPLY_MAGIC, 0x6744_6698);
    }

    #[test]
    fn frame_size_constants_match_layout_math() {
        // Request: magic 4 + flags 2 + kind 2 + handle 8 + offset 8 + length 4
        assert_eq!(REQUEST_HEADER_LEN, 4 + 2 + 2 + 8 + 8 + 4);
        // Simple reply: magic 4 + error 4 + handle 8
        assert_eq!(SIMPLE_REPLY_HEADER_LEN, 4 + 4 + 8);
    }

    /// Encode then decode a known-distinctive header; assert every
    /// field round-trips. Catches any single-byte misalignment in
    /// either direction (encode using wrong offset, decode using
    /// wrong offset, swapped fields, wrong endianness).
    #[test]
    fn decode_request_header_round_trips_through_encode() {
        let original = RequestHeader {
            flags: 0xA5A5,
            kind: NBD_CMD_WRITE,
            handle: 0x1122_3344_5566_7788,
            offset: 0xDEAD_BEEF_CAFE_BABE,
            length: 0x1234_5678,
        };
        let bytes = encode_request_header(&original);
        let decoded = decode_request_header(&bytes).expect("valid magic");
        assert_eq!(decoded, original);
    }

    /// Bad magic must be rejected. Use a value that is NOT the
    /// reply magic either — proves the function checks the actual
    /// request-magic constant, not just "is not zero".
    #[test]
    fn decode_request_header_rejects_bad_magic() {
        let mut bytes = encode_request_header(&RequestHeader {
            flags: 0,
            kind: NBD_CMD_READ,
            handle: 0,
            offset: 0,
            length: 0,
        });
        // Flip one bit in the magic.
        bytes[0] ^= 0x01;
        assert!(decode_request_header(&bytes).is_none());
    }

    /// Decoder must extract the FUA bit from the spec-defined
    /// flags slot at bytes [4..6]. Hand-rolls the byte layout
    /// rather than using `encode_request_header`, so a symmetric
    /// encoder/decoder field-swap bug cannot mask the failure.
    /// Picks a `kind` value (`NBD_CMD_TRIM = 4`) that is distinct
    /// from any flag bit so the assertions can't be satisfied by
    /// numeric coincidence.
    #[test]
    fn decode_places_fua_into_flags_field_per_spec_byte_layout() {
        let mut bytes = [0u8; REQUEST_HEADER_LEN];
        // Magic at [0..4].
        bytes[0..4].copy_from_slice(&NBD_REQUEST_MAGIC.to_be_bytes());
        // FUA in flags slot at [4..6].
        bytes[4..6].copy_from_slice(&NBD_CMD_FLAG_FUA.to_be_bytes());
        // NBD_CMD_TRIM (= 4) in kind slot at [6..8]. Picked
        // because 4 is not equal to FUA's value (1) — so a swap
        // would make `kind == 1` and `flags == 4`, both of which
        // would fail the assertions below.
        bytes[6..8].copy_from_slice(&NBD_CMD_TRIM.to_be_bytes());

        let decoded = decode_request_header(&bytes).unwrap();
        assert_eq!(
            decoded.flags, NBD_CMD_FLAG_FUA,
            "FUA byte pattern must decode into the `flags` field",
        );
        assert_eq!(
            decoded.kind, NBD_CMD_TRIM,
            "kind byte pattern must decode into the `kind` field",
        );
    }

    /// Encoder must place the FUA bit into the spec-defined flags
    /// slot at bytes [4..6]. Hand-asserts the byte layout rather
    /// than going through the decoder.
    #[test]
    fn encode_places_fua_into_spec_flags_byte_offset() {
        let bytes = encode_request_header(&RequestHeader {
            flags: NBD_CMD_FLAG_FUA,
            // Use TRIM (4) to disambiguate from FUA (1) — see the
            // matching decode test.
            kind: NBD_CMD_TRIM,
            handle: 0,
            offset: 0,
            length: 0,
        });
        assert_eq!(
            u16::from_be_bytes([bytes[4], bytes[5]]),
            NBD_CMD_FLAG_FUA,
            "flags field must encode at bytes [4..6]",
        );
        assert_eq!(
            u16::from_be_bytes([bytes[6], bytes[7]]),
            NBD_CMD_TRIM,
            "kind field must encode at bytes [6..8]",
        );
    }

    /// Verify the exact byte layout of a known reply (not a
    /// round-trip via a sibling helper — a hand-asserted layout).
    /// Catches reordered fields and wrong endianness in
    /// [`encode_simple_reply_header`] specifically, independent of
    /// any decode helper.
    #[test]
    fn encode_simple_reply_header_byte_layout_is_exact() {
        let buf = encode_simple_reply_header(NBD_EIO, 0xCAFE_F00D_DEAD_BEEF);
        assert_eq!(buf.len(), SIMPLE_REPLY_HEADER_LEN);
        // Magic at [0..4], big-endian.
        assert_eq!(&buf[0..4], &NBD_SIMPLE_REPLY_MAGIC.to_be_bytes());
        // Error at [4..8], big-endian (NBD_EIO = 5).
        assert_eq!(u32::from_be_bytes([buf[4], buf[5], buf[6], buf[7]]), 5);
        // Handle at [8..16], big-endian, exact echo.
        assert_eq!(
            u64::from_be_bytes([
                buf[8], buf[9], buf[10], buf[11], buf[12], buf[13], buf[14], buf[15],
            ]),
            0xCAFE_F00D_DEAD_BEEF,
        );
    }

    /// Encoding a success reply (error = 0) must leave bytes [4..8]
    /// as all-zero. Catches a bug where the encoder `ORed` something
    /// into the error field.
    #[test]
    fn encode_simple_reply_header_success_has_zero_error_bytes() {
        let buf = encode_simple_reply_header(NBD_EOK, 0);
        assert_eq!(&buf[4..8], &[0, 0, 0, 0]);
    }

    /// Encoding two replies with different handles must produce
    /// byte streams that differ ONLY in the handle slot — proves
    /// the handle is not accidentally mixed into magic / error.
    #[test]
    fn encode_simple_reply_header_handle_only_affects_handle_slot() {
        let a = encode_simple_reply_header(NBD_EOK, 0);
        let b = encode_simple_reply_header(NBD_EOK, 0x0102_0304_0506_0708);
        assert_eq!(&a[0..8], &b[0..8], "magic + error must match");
        assert_ne!(&a[8..16], &b[8..16], "handle bytes must differ");
    }

    /// Each command-type constant must be distinct. Catches a
    /// future copy/paste like `NBD_CMD_TRIM: u16 = 3;` colliding
    /// with `FLUSH`.
    #[test]
    fn command_type_constants_are_pairwise_distinct() {
        let all = [
            NBD_CMD_READ,
            NBD_CMD_WRITE,
            NBD_CMD_DISC,
            NBD_CMD_FLUSH,
            NBD_CMD_TRIM,
        ];
        for (i, a) in all.iter().enumerate() {
            for b in &all[i + 1..] {
                assert_ne!(a, b, "command-type constants must be distinct");
            }
        }
    }

    /// FUA bit pattern must match the
    /// [`teslausb_core::backend::WriteFlags::FUA`] bit pattern
    /// exactly — that equality is the load-bearing assumption of
    /// the FUA pass-through in `transmission.rs`. If anyone
    /// changes either constant in isolation, this test fires.
    #[test]
    fn nbd_cmd_flag_fua_matches_writeflags_fua_bit_pattern() {
        use teslausb_core::backend::WriteFlags;
        assert_eq!(u32::from(NBD_CMD_FLAG_FUA), WriteFlags::FUA.bits());
    }
}
