//! NBD newstyle handshake.
//!
//! Wire format reference:
//! <https://github.com/NetworkBlockDevice/nbd/blob/master/doc/proto.md#fixed-newstyle-negotiation>
//!
//! Negotiation sequence:
//!   1. Server sends `NBDMAGIC` + `IHAVEOPT` magic + handshake
//!      flags.
//!   2. Client replies with its client flags.
//!   3. Loop: client sends `OPT_*` requests; server responds.
//!   4. Client eventually sends `NBD_OPT_GO` (or legacy
//!      `NBD_OPT_EXPORT_NAME`); server replies with export details
//!      and the connection enters the transmission phase.
//!
//! ## Architecture
//!
//! Pure encode/decode helpers live alongside the I/O orchestrator
//! ([`run`]) so the protocol can be unit-tested in-process without
//! binding a real socket. The orchestrator is generic over
//! `AsyncRead + AsyncWrite + Unpin` so production code passes a
//! [`tokio::net::UnixStream`] and tests pass a
//! [`tokio::io::DuplexStream`].
//!
//! [`tokio::net::UnixStream`]: https://docs.rs/tokio/latest/tokio/net/struct.UnixStream.html
//! [`tokio::io::DuplexStream`]: https://docs.rs/tokio/latest/tokio/io/struct.DuplexStream.html

use anyhow::{Context, Result, bail};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tracing::{debug, trace};

// ---- Wire constants -----------------------------------------------------

/// Server greeting magic — ASCII `"NBDMAGIC"`.
pub const NBDMAGIC: u64 = 0x4e42_444d_4147_4943;
/// Option-request magic — ASCII `"IHAVEOPT"`.
pub const IHAVEOPT: u64 = 0x4948_4156_454f_5054;
/// Option-reply magic — `0x3e88_9045_565a_9` per NBD spec.
pub const REPLYMAGIC: u64 = 0x0003_e889_0455_65a9;

/// Handshake flag (server → client): server speaks fixed-newstyle.
pub const HF_FIXED_NEWSTYLE: u16 = 1 << 0;
/// Handshake flag (server → client): server will skip the 124-byte
/// zero pad on `EXPORT_NAME` replies when the client opts in.
pub const HF_NO_ZEROES: u16 = 1 << 1;

/// Client flag (client → server): client speaks fixed-newstyle.
pub const CF_FIXED_NEWSTYLE: u32 = 1 << 0;
/// Client flag (client → server): client tolerates no zero pad on
/// `EXPORT_NAME` replies.
pub const CF_NO_ZEROES: u32 = 1 << 1;

/// `NBD_OPT_EXPORT_NAME` — legacy single-shot export selection.
pub const NBD_OPT_EXPORT_NAME: u32 = 1;
/// `NBD_OPT_ABORT` — client wants to walk away cleanly.
pub const NBD_OPT_ABORT: u32 = 2;
/// `NBD_OPT_LIST` — enumerate exports (we have one anonymous).
pub const NBD_OPT_LIST: u32 = 3;
/// `NBD_OPT_INFO` — query export metadata without committing.
pub const NBD_OPT_INFO: u32 = 6;
/// `NBD_OPT_GO` — commit to an export and enter transmission.
pub const NBD_OPT_GO: u32 = 7;
/// `NBD_OPT_STRUCTURED_REPLY` — opt into structured replies during
/// transmission. We reject it; the daemon uses simple replies.
pub const NBD_OPT_STRUCTURED_REPLY: u32 = 8;

/// `NBD_REP_ACK` — option accepted with no further data.
pub const NBD_REP_ACK: u32 = 1;
/// `NBD_REP_INFO` — option accepted with one info sub-record.
pub const NBD_REP_INFO: u32 = 3;
/// Error reply: option not supported.
pub const NBD_REP_ERR_UNSUP: u32 = NBD_REP_ERR_BIT | 1;
/// Error reply: option payload was malformed.
pub const NBD_REP_ERR_INVALID: u32 = NBD_REP_ERR_BIT | 3;

/// High bit set on every error-class option-reply type.
const NBD_REP_ERR_BIT: u32 = 0x8000_0000;

/// `NBD_INFO_EXPORT` — size + transmission flags sub-record.
pub const NBD_INFO_EXPORT: u16 = 0;
/// `NBD_INFO_BLOCK_SIZE` — min / pref / max block size sub-record.
pub const NBD_INFO_BLOCK_SIZE: u16 = 3;

/// Transmission flag: this export advertises capability flags
/// (the byte itself being non-zero counts as "yes, please parse
/// the rest").
pub const NBD_FLAG_HAS_FLAGS: u16 = 1 << 0;
/// Transmission flag: `NBD_CMD_FLUSH` is supported on this export.
pub const NBD_FLAG_SEND_FLUSH: u16 = 1 << 2;
/// Transmission flag: `NBD_CMD_FLAG_FUA` is honoured on writes
/// (Force Unit Access — write hits stable storage before the reply).
pub const NBD_FLAG_SEND_FUA: u16 = 1 << 3;

/// Length of the server greeting in bytes: `NBDMAGIC` (8) +
/// `IHAVEOPT` (8) + `handshake flags` (2).
pub const GREETING_LEN: usize = 18;

/// Option-reply header length: `REPLYMAGIC` (8) + `option` (4) +
/// `reply_type` (4) + `data_len` (4).
pub const OPTION_REPLY_HEADER_LEN: usize = 20;

/// Length of the legacy zero pad on `NBD_OPT_EXPORT_NAME` replies
/// when the client did not request `CF_NO_ZEROES`.
pub const LEGACY_EXPORT_PAD_LEN: usize = 124;

/// Block-size sub-record values reported via `NBD_INFO_BLOCK_SIZE`.
/// Matches what the kernel NBD client expects from a sector-aligned
/// 512-byte LUN.
pub const BLOCK_SIZE_MIN: u32 = 512;
/// Preferred I/O block size (matches typical FAT cluster / page).
pub const BLOCK_SIZE_PREF: u32 = 4096;
/// Maximum single-request payload we'll advertise (32 MiB matches
/// the kernel's default `NBD_SET_BLKSIZE` ceiling and keeps a
/// single request well below the Pi Zero 2 W's 512 MiB RAM).
pub const BLOCK_SIZE_MAX: u32 = 32 * 1024 * 1024;

// ---- Pure encode helpers ------------------------------------------------

/// Encode the server greeting: `NBDMAGIC` ‖ `IHAVEOPT` ‖
/// `handshake_flags`. Big-endian throughout per spec.
#[must_use]
pub fn encode_greeting(handshake_flags: u16) -> [u8; GREETING_LEN] {
    let mut out = [0u8; GREETING_LEN];
    out[0..8].copy_from_slice(&NBDMAGIC.to_be_bytes());
    out[8..16].copy_from_slice(&IHAVEOPT.to_be_bytes());
    out[16..18].copy_from_slice(&handshake_flags.to_be_bytes());
    out
}

/// Encode an option-reply: `REPLYMAGIC` ‖ `opt` ‖ `reply_type` ‖
/// `data_len` ‖ `payload`. Returns an owned `Vec` because callers
/// always end up writing it.
///
/// # Errors
///
/// Returns an error if `payload` is larger than `u32::MAX` (the
/// wire `data_len` field is a `u32`). On 32-bit targets this never
/// triggers; on 64-bit dev hosts it remains a real defensive
/// check.
pub fn encode_option_reply(opt: u32, reply_type: u32, payload: &[u8]) -> Result<Vec<u8>> {
    let data_len =
        u32::try_from(payload.len()).context("option-reply payload larger than u32::MAX")?;
    let mut buf = Vec::with_capacity(OPTION_REPLY_HEADER_LEN + payload.len());
    buf.extend_from_slice(&REPLYMAGIC.to_be_bytes());
    buf.extend_from_slice(&opt.to_be_bytes());
    buf.extend_from_slice(&reply_type.to_be_bytes());
    buf.extend_from_slice(&data_len.to_be_bytes());
    buf.extend_from_slice(payload);
    Ok(buf)
}

/// Encode an `NBD_INFO_EXPORT` sub-record body: `info_type (u16)` ‖
/// `export_size (u64)` ‖ `transmission_flags (u16)`.
#[must_use]
pub fn encode_info_export(export_size: u64, transmission_flags: u16) -> Vec<u8> {
    let mut out = Vec::with_capacity(2 + 8 + 2);
    out.extend_from_slice(&NBD_INFO_EXPORT.to_be_bytes());
    out.extend_from_slice(&export_size.to_be_bytes());
    out.extend_from_slice(&transmission_flags.to_be_bytes());
    out
}

/// Encode an `NBD_INFO_BLOCK_SIZE` sub-record body: `info_type (u16)`
/// ‖ `min (u32)` ‖ `preferred (u32)` ‖ `max (u32)`.
#[must_use]
pub fn encode_info_block_size(min: u32, preferred: u32, max: u32) -> Vec<u8> {
    let mut out = Vec::with_capacity(2 + 4 + 4 + 4);
    out.extend_from_slice(&NBD_INFO_BLOCK_SIZE.to_be_bytes());
    out.extend_from_slice(&min.to_be_bytes());
    out.extend_from_slice(&preferred.to_be_bytes());
    out.extend_from_slice(&max.to_be_bytes());
    out
}

/// Encode the legacy `NBD_OPT_EXPORT_NAME` reply body: `export_size`
/// ‖ `transmission_flags` ‖ optional 124-byte zero pad.
#[must_use]
pub fn encode_export_name_reply(
    export_size: u64,
    transmission_flags: u16,
    no_zeroes: bool,
) -> Vec<u8> {
    let total = 8 + 2 + if no_zeroes { 0 } else { LEGACY_EXPORT_PAD_LEN };
    let mut out = Vec::with_capacity(total);
    out.extend_from_slice(&export_size.to_be_bytes());
    out.extend_from_slice(&transmission_flags.to_be_bytes());
    if !no_zeroes {
        out.extend_from_slice(&[0u8; LEGACY_EXPORT_PAD_LEN]);
    }
    out
}

/// Parsed view of an `NBD_OPT_INFO` or `NBD_OPT_GO` request body.
///
/// The request body layout is:
/// `name_len (u32)` ‖ `name (name_len bytes)` ‖
/// `nrequests (u16)` ‖ `nrequests * info_id (u16)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedInfoGoRequest {
    /// True when the client included the [`NBD_INFO_BLOCK_SIZE`]
    /// sub-request id, signalling it wants the daemon to advertise
    /// its block-size constraints in the reply.
    pub requested_block_size: bool,
}

/// Parse an `NBD_OPT_INFO` or `NBD_OPT_GO` request body.
///
/// We don't care about the export name (we have one anonymous
/// export) — only whether the client requested the block-size info
/// sub-record. Returns `None` if the request is malformed; the
/// caller MUST then reply [`NBD_REP_ERR_INVALID`].
#[must_use]
pub fn parse_info_or_go_request(data: &[u8]) -> Option<ParsedInfoGoRequest> {
    let name_len_bytes: [u8; 4] = data.get(0..4)?.try_into().ok()?;
    let name_len = usize::try_from(u32::from_be_bytes(name_len_bytes)).ok()?;
    let nrequests_off = 4_usize.checked_add(name_len)?;
    let nrequests_bytes: [u8; 2] = data
        .get(nrequests_off..nrequests_off.checked_add(2)?)?
        .try_into()
        .ok()?;
    let nrequests = u16::from_be_bytes(nrequests_bytes);

    let mut requested_block_size = false;
    let mut cursor = nrequests_off.checked_add(2)?;
    for _ in 0..nrequests {
        let next = cursor.checked_add(2)?;
        let id_bytes: [u8; 2] = data.get(cursor..next)?.try_into().ok()?;
        if u16::from_be_bytes(id_bytes) == NBD_INFO_BLOCK_SIZE {
            requested_block_size = true;
        }
        cursor = next;
    }

    Some(ParsedInfoGoRequest {
        requested_block_size,
    })
}

// ---- I/O orchestrator ---------------------------------------------------

/// Combined export transmission flags advertised for the
/// synthesised volume: capability bit + FLUSH + FUA. Pulled into a
/// helper so the two call sites (info reply + legacy export reply)
/// agree.
#[must_use]
fn export_transmission_flags() -> u16 {
    NBD_FLAG_HAS_FLAGS | NBD_FLAG_SEND_FLUSH | NBD_FLAG_SEND_FUA
}

/// Run the newstyle handshake on a fresh client connection.
///
/// Returns when the client has sent `NBD_OPT_GO` (or
/// `NBD_OPT_EXPORT_NAME`) and we've replied with the export
/// details. The caller then enters the transmission phase on the
/// same stream.
///
/// Generic over the stream type so unit tests can pass an in-
/// memory [`tokio::io::DuplexStream`] instead of a real
/// `UnixStream`.
///
/// # Errors
///
/// * I/O error reading from / writing to `stream`.
/// * Client did not request `CF_FIXED_NEWSTYLE`.
/// * Option-request magic was not `IHAVEOPT`.
/// * Client sent `NBD_OPT_ABORT`.
///
/// [`tokio::io::DuplexStream`]: https://docs.rs/tokio/latest/tokio/io/struct.DuplexStream.html
pub async fn run<S>(stream: &mut S, export_size: u64) -> Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let handshake_flags = HF_FIXED_NEWSTYLE | HF_NO_ZEROES;
    let greeting = encode_greeting(handshake_flags);
    stream.write_all(&greeting).await?;
    stream.flush().await?;

    let client_flags = stream.read_u32().await?;
    debug!(
        client_flags = format!("0x{client_flags:x}"),
        "client handshake"
    );
    if client_flags & CF_FIXED_NEWSTYLE == 0 {
        bail!("client did not request fixed-newstyle");
    }
    let no_zeroes = (client_flags & CF_NO_ZEROES) != 0;

    loop {
        let (opt, data) = read_option_request(stream).await?;
        trace!(opt, len = data.len(), "option request");
        if dispatch_option(stream, opt, &data, export_size, no_zeroes).await? {
            return Ok(());
        }
    }
}

/// Read one option-request frame: `IHAVEOPT` ‖ `opt (u32)` ‖
/// `data_len (u32)` ‖ `data`.
async fn read_option_request<S>(stream: &mut S) -> Result<(u32, Vec<u8>)>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let magic = stream.read_u64().await?;
    if magic != IHAVEOPT {
        bail!("bad option magic 0x{magic:x}");
    }
    let opt = stream.read_u32().await?;
    let data_len_u32 = stream.read_u32().await?;
    let data_len = usize::try_from(data_len_u32).context("option data_len larger than usize")?;
    let mut data = vec![0u8; data_len];
    if data_len > 0 {
        stream
            .read_exact(&mut data)
            .await
            .context("reading option data")?;
    }
    Ok((opt, data))
}

/// Handle one option request. Returns `Ok(true)` when the handshake
/// is complete and the caller should enter transmission; `Ok(false)`
/// to keep looping.
async fn dispatch_option<S>(
    stream: &mut S,
    opt: u32,
    data: &[u8],
    export_size: u64,
    no_zeroes: bool,
) -> Result<bool>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    match opt {
        NBD_OPT_ABORT => {
            debug!("client aborted handshake");
            write_option_reply(stream, opt, NBD_REP_ACK, &[]).await?;
            bail!("client aborted");
        }
        NBD_OPT_LIST => {
            let mut payload = Vec::with_capacity(4);
            payload.extend_from_slice(&0u32.to_be_bytes());
            write_option_reply(stream, opt, NBD_REP_INFO, &payload).await?;
            write_option_reply(stream, opt, NBD_REP_ACK, &[]).await?;
            Ok(false)
        }
        NBD_OPT_STRUCTURED_REPLY => {
            write_option_reply(stream, opt, NBD_REP_ERR_UNSUP, &[]).await?;
            Ok(false)
        }
        NBD_OPT_INFO | NBD_OPT_GO => {
            handle_info_or_go(stream, opt, data, export_size).await?;
            Ok(opt == NBD_OPT_GO)
        }
        NBD_OPT_EXPORT_NAME => {
            let body =
                encode_export_name_reply(export_size, export_transmission_flags(), no_zeroes);
            stream.write_all(&body).await?;
            stream.flush().await?;
            debug!("handshake complete (legacy EXPORT_NAME)");
            Ok(true)
        }
        unknown => {
            debug!(opt = unknown, "unsupported option");
            write_option_reply(stream, opt, NBD_REP_ERR_UNSUP, &[]).await?;
            Ok(false)
        }
    }
}

/// Reply to an `NBD_OPT_INFO` or `NBD_OPT_GO`. Emits the export
/// metadata then the terminating ACK.
async fn handle_info_or_go<S>(stream: &mut S, opt: u32, data: &[u8], export_size: u64) -> Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let Some(parsed) = parse_info_or_go_request(data) else {
        write_option_reply(stream, opt, NBD_REP_ERR_INVALID, &[]).await?;
        return Ok(());
    };
    let info_export = encode_info_export(export_size, export_transmission_flags());
    write_option_reply(stream, opt, NBD_REP_INFO, &info_export).await?;
    if parsed.requested_block_size {
        let info_bs = encode_info_block_size(BLOCK_SIZE_MIN, BLOCK_SIZE_PREF, BLOCK_SIZE_MAX);
        write_option_reply(stream, opt, NBD_REP_INFO, &info_bs).await?;
    }
    write_option_reply(stream, opt, NBD_REP_ACK, &[]).await?;
    Ok(())
}

/// Thin wrapper around [`encode_option_reply`] that writes + flushes
/// the encoded bytes to the stream.
async fn write_option_reply<S>(
    stream: &mut S,
    opt: u32,
    reply_type: u32,
    payload: &[u8],
) -> Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let buf = encode_option_reply(opt, reply_type, payload)?;
    stream.write_all(&buf).await?;
    stream.flush().await?;
    Ok(())
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::indexing_slicing)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt, duplex};

    // ---- Pure helper tests ---------------------------------------------

    #[test]
    fn encode_greeting_layout_matches_spec() {
        let g = encode_greeting(HF_FIXED_NEWSTYLE | HF_NO_ZEROES);
        assert_eq!(&g[0..8], b"NBDMAGIC");
        assert_eq!(&g[8..16], b"IHAVEOPT");
        assert_eq!(u16::from_be_bytes([g[16], g[17]]), 0x0003);
    }

    #[test]
    fn encode_option_reply_empty_payload_is_header_only() {
        let buf = encode_option_reply(NBD_OPT_GO, NBD_REP_ACK, &[]).unwrap();
        assert_eq!(buf.len(), OPTION_REPLY_HEADER_LEN);
        assert_eq!(&buf[0..8], &REPLYMAGIC.to_be_bytes());
        assert_eq!(&buf[8..12], &NBD_OPT_GO.to_be_bytes());
        assert_eq!(&buf[12..16], &NBD_REP_ACK.to_be_bytes());
        assert_eq!(&buf[16..20], &0u32.to_be_bytes());
    }

    #[test]
    fn encode_option_reply_with_payload_appends_correctly() {
        let payload = [0xAA, 0xBB, 0xCC];
        let buf = encode_option_reply(NBD_OPT_INFO, NBD_REP_INFO, &payload).unwrap();
        assert_eq!(buf.len(), OPTION_REPLY_HEADER_LEN + payload.len());
        assert_eq!(&buf[16..20], &3u32.to_be_bytes());
        assert_eq!(&buf[20..], &payload);
    }

    #[test]
    fn encode_info_export_layout_matches_spec() {
        let buf = encode_info_export(1024, NBD_FLAG_HAS_FLAGS | NBD_FLAG_SEND_FUA);
        assert_eq!(buf.len(), 12);
        assert_eq!(u16::from_be_bytes([buf[0], buf[1]]), NBD_INFO_EXPORT);
        assert_eq!(
            u64::from_be_bytes([
                buf[2], buf[3], buf[4], buf[5], buf[6], buf[7], buf[8], buf[9]
            ]),
            1024,
        );
        assert_eq!(u16::from_be_bytes([buf[10], buf[11]]), 0x0009);
    }

    #[test]
    fn encode_info_block_size_layout_matches_spec() {
        let buf = encode_info_block_size(512, 4096, 1 << 25);
        assert_eq!(buf.len(), 14);
        assert_eq!(u16::from_be_bytes([buf[0], buf[1]]), NBD_INFO_BLOCK_SIZE);
        assert_eq!(u32::from_be_bytes([buf[2], buf[3], buf[4], buf[5]]), 512);
        assert_eq!(u32::from_be_bytes([buf[6], buf[7], buf[8], buf[9]]), 4096);
        assert_eq!(
            u32::from_be_bytes([buf[10], buf[11], buf[12], buf[13]]),
            1 << 25
        );
    }

    #[test]
    fn encode_export_name_reply_with_zeroes_pads_to_134() {
        let buf = encode_export_name_reply(2048, 0x9, false);
        assert_eq!(buf.len(), 8 + 2 + LEGACY_EXPORT_PAD_LEN);
        assert!(buf[10..].iter().all(|&b| b == 0));
    }

    #[test]
    fn encode_export_name_reply_no_zeroes_skips_pad() {
        let buf = encode_export_name_reply(2048, 0x9, true);
        assert_eq!(buf.len(), 10);
    }

    #[test]
    fn parse_info_or_go_request_happy_no_block_size() {
        let data = [0u8, 0, 0, 0, 0, 0];
        let parsed = parse_info_or_go_request(&data).unwrap();
        assert!(!parsed.requested_block_size);
    }

    #[test]
    fn parse_info_or_go_request_with_block_size_request() {
        let mut data = vec![0u8, 0, 0, 0];
        data.extend_from_slice(&1u16.to_be_bytes());
        data.extend_from_slice(&NBD_INFO_BLOCK_SIZE.to_be_bytes());
        let parsed = parse_info_or_go_request(&data).unwrap();
        assert!(parsed.requested_block_size);
    }

    #[test]
    fn parse_info_or_go_request_with_non_block_size_request_only() {
        let mut data = vec![0u8, 0, 0, 0];
        data.extend_from_slice(&1u16.to_be_bytes());
        data.extend_from_slice(&NBD_INFO_EXPORT.to_be_bytes());
        let parsed = parse_info_or_go_request(&data).unwrap();
        assert!(!parsed.requested_block_size);
    }

    #[test]
    fn parse_info_or_go_request_too_short_returns_none() {
        assert!(parse_info_or_go_request(&[0u8, 0, 0]).is_none());
        assert!(parse_info_or_go_request(&[0u8, 0, 0, 0]).is_none());
    }

    #[test]
    fn parse_info_or_go_request_name_len_overflow_returns_none() {
        let mut data = vec![];
        data.extend_from_slice(&u32::MAX.to_be_bytes());
        assert!(parse_info_or_go_request(&data).is_none());
    }

    #[test]
    fn error_codes_have_high_bit_set() {
        assert_eq!(NBD_REP_ERR_UNSUP & NBD_REP_ERR_BIT, NBD_REP_ERR_BIT);
        assert_eq!(NBD_REP_ERR_INVALID & NBD_REP_ERR_BIT, NBD_REP_ERR_BIT);
        assert_eq!(NBD_REP_ACK & NBD_REP_ERR_BIT, 0);
        assert_eq!(NBD_REP_INFO & NBD_REP_ERR_BIT, 0);
    }

    // ---- Async orchestrator tests --------------------------------------

    async fn drive_client_go(
        client: &mut tokio::io::DuplexStream,
        request_block_size: bool,
    ) -> Vec<u8> {
        let mut greeting = [0u8; GREETING_LEN];
        client.read_exact(&mut greeting).await.unwrap();
        client
            .write_all(&(CF_FIXED_NEWSTYLE | CF_NO_ZEROES).to_be_bytes())
            .await
            .unwrap();
        let mut go_data = vec![];
        go_data.extend_from_slice(&0u32.to_be_bytes());
        let nrequests: u16 = u16::from(request_block_size);
        go_data.extend_from_slice(&nrequests.to_be_bytes());
        if request_block_size {
            go_data.extend_from_slice(&NBD_INFO_BLOCK_SIZE.to_be_bytes());
        }
        client.write_all(&IHAVEOPT.to_be_bytes()).await.unwrap();
        client.write_all(&NBD_OPT_GO.to_be_bytes()).await.unwrap();
        let data_len = u32::try_from(go_data.len()).unwrap();
        client.write_all(&data_len.to_be_bytes()).await.unwrap();
        client.write_all(&go_data).await.unwrap();
        client.flush().await.unwrap();
        let expected_replies = if request_block_size { 3 } else { 2 };
        let mut replies = vec![];
        for _ in 0..expected_replies {
            let mut header = [0u8; OPTION_REPLY_HEADER_LEN];
            client.read_exact(&mut header).await.unwrap();
            let payload_len_bytes: [u8; 4] = header[16..20].try_into().unwrap();
            let payload_len = usize::try_from(u32::from_be_bytes(payload_len_bytes)).unwrap();
            let mut payload = vec![0u8; payload_len];
            if payload_len > 0 {
                client.read_exact(&mut payload).await.unwrap();
            }
            replies.extend_from_slice(&header);
            replies.extend_from_slice(&payload);
        }
        let mut full = greeting.to_vec();
        full.extend_from_slice(&replies);
        full
    }

    #[tokio::test]
    async fn run_completes_on_opt_go_no_block_size() {
        let (mut server, mut client) = duplex(64 * 1024);
        let server_task = tokio::spawn(async move {
            run(&mut server, 1024 * 1024).await.unwrap();
        });
        let bytes = drive_client_go(&mut client, false).await;
        server_task.await.unwrap();
        assert_eq!(&bytes[0..8], b"NBDMAGIC");
        let first_payload_start = GREETING_LEN + OPTION_REPLY_HEADER_LEN;
        assert_eq!(
            u16::from_be_bytes([bytes[first_payload_start], bytes[first_payload_start + 1]]),
            NBD_INFO_EXPORT,
        );
    }

    #[tokio::test]
    async fn run_completes_on_opt_go_with_block_size_request() {
        let (mut server, mut client) = duplex(64 * 1024);
        let server_task = tokio::spawn(async move {
            run(&mut server, 1024 * 1024).await.unwrap();
        });
        let bytes = drive_client_go(&mut client, true).await;
        server_task.await.unwrap();
        let first_payload_len_off = GREETING_LEN + 16;
        let first_payload_len = usize::try_from(u32::from_be_bytes([
            bytes[first_payload_len_off],
            bytes[first_payload_len_off + 1],
            bytes[first_payload_len_off + 2],
            bytes[first_payload_len_off + 3],
        ]))
        .unwrap();
        let second_payload_start = GREETING_LEN + 2 * OPTION_REPLY_HEADER_LEN + first_payload_len;
        assert_eq!(
            u16::from_be_bytes([bytes[second_payload_start], bytes[second_payload_start + 1],]),
            NBD_INFO_BLOCK_SIZE,
        );
    }

    #[tokio::test]
    async fn run_rejects_client_without_fixed_newstyle_flag() {
        let (mut server, mut client) = duplex(64 * 1024);
        let server_task = tokio::spawn(async move { run(&mut server, 4096).await });
        let mut greeting = [0u8; GREETING_LEN];
        client.read_exact(&mut greeting).await.unwrap();
        client.write_all(&0u32.to_be_bytes()).await.unwrap();
        client.flush().await.unwrap();
        let result = server_task.await.unwrap();
        assert!(result.is_err(), "expected handshake rejection");
        let msg = format!("{}", result.unwrap_err());
        assert!(msg.contains("fixed-newstyle"), "unexpected error: {msg}");
    }

    #[tokio::test]
    async fn run_rejects_bad_option_magic() {
        let (mut server, mut client) = duplex(64 * 1024);
        let server_task = tokio::spawn(async move { run(&mut server, 4096).await });
        let mut greeting = [0u8; GREETING_LEN];
        client.read_exact(&mut greeting).await.unwrap();
        client
            .write_all(&CF_FIXED_NEWSTYLE.to_be_bytes())
            .await
            .unwrap();
        client
            .write_all(&0xDEAD_BEEF_DEAD_BEEFu64.to_be_bytes())
            .await
            .unwrap();
        client.flush().await.unwrap();
        let result = server_task.await.unwrap();
        assert!(result.is_err(), "expected magic rejection");
        let msg = format!("{}", result.unwrap_err());
        assert!(msg.contains("magic"), "unexpected error: {msg}");
    }

    #[tokio::test]
    async fn run_aborts_on_opt_abort() {
        let (mut server, mut client) = duplex(64 * 1024);
        let server_task = tokio::spawn(async move { run(&mut server, 4096).await });
        let mut greeting = [0u8; GREETING_LEN];
        client.read_exact(&mut greeting).await.unwrap();
        client
            .write_all(&CF_FIXED_NEWSTYLE.to_be_bytes())
            .await
            .unwrap();
        client.write_all(&IHAVEOPT.to_be_bytes()).await.unwrap();
        client
            .write_all(&NBD_OPT_ABORT.to_be_bytes())
            .await
            .unwrap();
        client.write_all(&0u32.to_be_bytes()).await.unwrap();
        client.flush().await.unwrap();
        let mut reply = [0u8; OPTION_REPLY_HEADER_LEN];
        let _ = client.read_exact(&mut reply).await;
        let result = server_task.await.unwrap();
        assert!(result.is_err(), "abort should propagate as Err");
        assert!(format!("{}", result.unwrap_err()).contains("aborted"));
    }

    #[tokio::test]
    async fn run_handles_legacy_export_name_path() {
        let (mut server, mut client) = duplex(64 * 1024);
        let server_task = tokio::spawn(async move {
            run(&mut server, 2_048_000).await.unwrap();
        });
        let mut greeting = [0u8; GREETING_LEN];
        client.read_exact(&mut greeting).await.unwrap();
        client
            .write_all(&(CF_FIXED_NEWSTYLE | CF_NO_ZEROES).to_be_bytes())
            .await
            .unwrap();
        client.write_all(&IHAVEOPT.to_be_bytes()).await.unwrap();
        client
            .write_all(&NBD_OPT_EXPORT_NAME.to_be_bytes())
            .await
            .unwrap();
        client.write_all(&0u32.to_be_bytes()).await.unwrap();
        client.flush().await.unwrap();
        let mut reply = [0u8; 10];
        client.read_exact(&mut reply).await.unwrap();
        server_task.await.unwrap();
        let export_size = u64::from_be_bytes([
            reply[0], reply[1], reply[2], reply[3], reply[4], reply[5], reply[6], reply[7],
        ]);
        let xmit = u16::from_be_bytes([reply[8], reply[9]]);
        assert_eq!(export_size, 2_048_000);
        assert_eq!(
            xmit,
            NBD_FLAG_HAS_FLAGS | NBD_FLAG_SEND_FLUSH | NBD_FLAG_SEND_FUA
        );
    }
}
