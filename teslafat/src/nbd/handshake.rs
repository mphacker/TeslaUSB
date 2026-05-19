//! NBD newstyle handshake.
//!
//! Wire format reference:
//!   https://github.com/NetworkBlockDevice/nbd/blob/master/doc/proto.md#fixed-newstyle-negotiation
//!
//! Sequence:
//!   1. Server sends "NBDMAGIC" + IHAVEOPT magic + handshake flags
//!   2. Client replies with client flags
//!   3. Loop: client sends OPT_* requests; server responds
//!   4. Client eventually sends NBD_OPT_GO (or NBD_OPT_EXPORT_NAME);
//!      server replies with export details. Connection then enters
//!      transmission phase.

use anyhow::{bail, Context, Result};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tracing::{debug, trace};

const NBDMAGIC: u64 = 0x4e42444d41474943; // "NBDMAGIC"
const IHAVEOPT: u64 = 0x49484156454F5054; // "IHAVEOPT"
const REPLYMAGIC: u64 = 0x3e889045565a9;

// Handshake flags (server → client)
const HF_FIXED_NEWSTYLE: u16 = 1 << 0;
const HF_NO_ZEROES: u16 = 1 << 1;

// Client flags (client → server)
const CF_FIXED_NEWSTYLE: u32 = 1 << 0;
const CF_NO_ZEROES: u32 = 1 << 1;

// Option types (client → server)
const NBD_OPT_EXPORT_NAME: u32 = 1;
const NBD_OPT_ABORT: u32 = 2;
const NBD_OPT_LIST: u32 = 3;
const NBD_OPT_INFO: u32 = 6;
const NBD_OPT_GO: u32 = 7;
const NBD_OPT_STRUCTURED_REPLY: u32 = 8;

// Option reply types
const NBD_REP_ACK: u32 = 1;
const NBD_REP_INFO: u32 = 3;
const NBD_REP_ERR_UNSUP: u32 = 0x80000000 | 1;
const NBD_REP_ERR_INVALID: u32 = 0x80000000 | 3;

// NBD_INFO_* (sub-replies inside NBD_REP_INFO)
const NBD_INFO_EXPORT: u16 = 0;
const NBD_INFO_BLOCK_SIZE: u16 = 3;

// Transmission flags (server-side capabilities, sent in EXPORT info)
const NBD_FLAG_HAS_FLAGS: u16 = 1 << 0;
const NBD_FLAG_SEND_FLUSH: u16 = 1 << 2;
const NBD_FLAG_SEND_FUA: u16 = 1 << 3;

/// Run the handshake on a fresh client connection. Returns when
/// the client has sent NBD_OPT_GO and we've replied with the
/// export details; the connection is then ready for transmission.
pub async fn run(stream: &mut UnixStream, export_size: u64) -> Result<()> {
    // 1. Server greeting.
    let server_flags: u16 = HF_FIXED_NEWSTYLE | HF_NO_ZEROES;
    let mut greeting = [0u8; 18];
    greeting[0..8].copy_from_slice(&NBDMAGIC.to_be_bytes());
    greeting[8..16].copy_from_slice(&IHAVEOPT.to_be_bytes());
    greeting[16..18].copy_from_slice(&server_flags.to_be_bytes());
    stream.write_all(&greeting).await?;
    stream.flush().await?;

    // 2. Client flags.
    let client_flags = stream.read_u32().await?;
    debug!(client_flags = format!("0x{:x}", client_flags), "client handshake");
    if client_flags & CF_FIXED_NEWSTYLE == 0 {
        bail!("client did not request fixed-newstyle");
    }
    let no_zeroes = (client_flags & CF_NO_ZEROES) != 0;

    // 3. Option loop.
    loop {
        // Read option header.
        let magic = stream.read_u64().await?;
        if magic != IHAVEOPT {
            bail!("bad option magic 0x{:x}", magic);
        }
        let opt = stream.read_u32().await?;
        let data_len = stream.read_u32().await? as usize;
        let mut data = vec![0u8; data_len];
        if data_len > 0 {
            stream
                .read_exact(&mut data)
                .await
                .context("reading option data")?;
        }
        trace!(opt = opt, len = data_len, "option request");

        match opt {
            NBD_OPT_ABORT => {
                debug!("client aborted handshake");
                send_option_reply(stream, opt, NBD_REP_ACK, &[]).await?;
                bail!("client aborted");
            }
            NBD_OPT_LIST => {
                // We have one anonymous export.
                let mut payload = vec![];
                payload.extend_from_slice(&0u32.to_be_bytes()); // name len 0
                send_option_reply(stream, opt, NBD_REP_INFO, &payload).await?;
                send_option_reply(stream, opt, NBD_REP_ACK, &[]).await?;
            }
            NBD_OPT_STRUCTURED_REPLY => {
                // We don't support structured replies; reject.
                send_option_reply(stream, opt, NBD_REP_ERR_UNSUP, &[]).await?;
            }
            NBD_OPT_INFO | NBD_OPT_GO => {
                handle_info_or_go(stream, opt, &data, export_size).await?;
                if opt == NBD_OPT_GO {
                    debug!("handshake complete, entering transmission");
                    return Ok(());
                }
            }
            NBD_OPT_EXPORT_NAME => {
                // Legacy path: kernel client may use this. Reply
                // with the bare export details and switch to
                // transmission immediately.
                let mut reply = vec![];
                reply.extend_from_slice(&export_size.to_be_bytes());
                let xmit_flags = NBD_FLAG_HAS_FLAGS
                    | NBD_FLAG_SEND_FLUSH
                    | NBD_FLAG_SEND_FUA;
                reply.extend_from_slice(&xmit_flags.to_be_bytes());
                if !no_zeroes {
                    reply.extend_from_slice(&[0u8; 124]);
                }
                stream.write_all(&reply).await?;
                stream.flush().await?;
                debug!("handshake complete (legacy EXPORT_NAME)");
                return Ok(());
            }
            _ => {
                debug!(opt = opt, "unsupported option");
                send_option_reply(stream, opt, NBD_REP_ERR_UNSUP, &[]).await?;
            }
        }
    }
}

async fn handle_info_or_go(
    stream: &mut UnixStream,
    opt: u32,
    data: &[u8],
    export_size: u64,
) -> Result<()> {
    // OPT_GO / OPT_INFO data:
    //   u32 name_len, name bytes, u16 nrequests, nrequests * u16 info_id
    if data.len() < 4 {
        send_option_reply(stream, opt, NBD_REP_ERR_INVALID, &[]).await?;
        return Ok(());
    }
    let name_len = u32::from_be_bytes(data[0..4].try_into().unwrap()) as usize;
    if data.len() < 4 + name_len + 2 {
        send_option_reply(stream, opt, NBD_REP_ERR_INVALID, &[]).await?;
        return Ok(());
    }
    let nrequests =
        u16::from_be_bytes(data[4 + name_len..4 + name_len + 2].try_into().unwrap());
    let mut requested_block_size = false;
    for i in 0..nrequests as usize {
        let off = 4 + name_len + 2 + 2 * i;
        if data.len() < off + 2 {
            break;
        }
        let id = u16::from_be_bytes(data[off..off + 2].try_into().unwrap());
        if id == NBD_INFO_BLOCK_SIZE {
            requested_block_size = true;
        }
    }

    // Reply with NBD_INFO_EXPORT (size + transmission flags).
    let xmit_flags = NBD_FLAG_HAS_FLAGS | NBD_FLAG_SEND_FLUSH | NBD_FLAG_SEND_FUA;
    let mut info_export = vec![];
    info_export.extend_from_slice(&NBD_INFO_EXPORT.to_be_bytes());
    info_export.extend_from_slice(&export_size.to_be_bytes());
    info_export.extend_from_slice(&xmit_flags.to_be_bytes());
    send_option_reply(stream, opt, NBD_REP_INFO, &info_export).await?;

    if requested_block_size {
        let mut info_bs = vec![];
        info_bs.extend_from_slice(&NBD_INFO_BLOCK_SIZE.to_be_bytes());
        info_bs.extend_from_slice(&512u32.to_be_bytes()); // min
        info_bs.extend_from_slice(&4096u32.to_be_bytes()); // pref
        info_bs.extend_from_slice(&(32 * 1024 * 1024u32).to_be_bytes()); // max
        send_option_reply(stream, opt, NBD_REP_INFO, &info_bs).await?;
    }

    send_option_reply(stream, opt, NBD_REP_ACK, &[]).await?;
    Ok(())
}

async fn send_option_reply(
    stream: &mut UnixStream,
    opt: u32,
    reply_type: u32,
    payload: &[u8],
) -> Result<()> {
    let mut buf = Vec::with_capacity(20 + payload.len());
    buf.extend_from_slice(&REPLYMAGIC.to_be_bytes());
    buf.extend_from_slice(&opt.to_be_bytes());
    buf.extend_from_slice(&reply_type.to_be_bytes());
    buf.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    buf.extend_from_slice(payload);
    stream.write_all(&buf).await?;
    stream.flush().await?;
    Ok(())
}
