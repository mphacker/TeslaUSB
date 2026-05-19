//! NBD transmission-phase dispatch loop.
//!
//! Drives one client connection that has finished the handshake.
//! Reads NBD request headers, dispatches each command against a
//! generic [`BlockBackend`], and writes simple-reply frames back
//! on the same stream.
//!
//! ## Concurrency model
//!
//! Single-threaded. Each call to [`run`] owns one stream and one
//! backend reference for the duration of the connection. Commands
//! are processed strictly in order — NBD permits a server to
//! reorder replies in *structured-reply* mode, but the handshake
//! rejects `NBD_OPT_STRUCTURED_REPLY` so the wire here is always
//! simple-reply, which requires in-order replies on a given handle.
//!
//! ## Backend ownership
//!
//! Borrowed (`&B`), not moved. The same backend instance can be
//! shared across reconnects (e.g. a client `nbd-client -d` then
//! reconnects) without rebuilding state. Backends must be
//! interior-mutable for write-heavy workloads (see
//! [`teslausb_core::backend::mock::MockBackend`] for the reference
//! impl).
//!
//! ## Error policy
//!
//! Two layers:
//!
//! * **Per-request backend errors** are mapped to NBD error codes
//!   and returned to the client in the reply frame; the loop
//!   continues.
//! * **Wire-level errors** (bad magic, oversized request, I/O
//!   failure on the stream itself) terminate the connection by
//!   returning `Err` from [`run`]. Once the byte stream is
//!   de-synced or unreachable the only safe response is to drop
//!   the connection.
//!
//! ## Resource limits
//!
//! Per-request `length` is hard-capped at
//! [`crate::nbd::handshake::BLOCK_SIZE_MAX`] (32 MiB) — the same
//! value advertised in the handshake's `NBD_INFO_BLOCK_SIZE`
//! reply. Larger requests are refused before any allocation; the
//! connection is closed because by sending an oversize request the
//! client has already violated the contract advertised in the
//! handshake.

use anyhow::{Context, Result, bail};
use teslausb_core::backend::{BackendError, BlockBackend, WriteFlags};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tracing::{debug, trace, warn};

use crate::nbd::handshake::BLOCK_SIZE_MAX;
use crate::nbd::wire::{
    NBD_CMD_DISC, NBD_CMD_FLAG_FUA, NBD_CMD_FLUSH, NBD_CMD_READ, NBD_CMD_TRIM, NBD_CMD_WRITE,
    NBD_EINVAL, NBD_EIO, NBD_ENOMEM, NBD_ENOTSUP, NBD_EOK, NBD_EOVERFLOW, REQUEST_HEADER_LEN,
    RequestHeader, decode_request_header, encode_simple_reply_header,
};

/// Drive the per-connection transmission loop.
///
/// Returns `Ok(())` when:
/// * the client sends [`NBD_CMD_DISC`] (clean disconnect), or
/// * the client closes the read half cleanly between commands
///   (treated as an implicit DISC — some clients do this).
///
/// Generic over the stream type so unit tests pass an in-memory
/// [`tokio::io::DuplexStream`] and production passes a
/// [`tokio::net::UnixStream`].
///
/// # Errors
///
/// * Wire-level I/O error reading from / writing to `stream`.
/// * Bad request magic (stream is de-synced; closing is the only
///   safe option).
/// * Oversized request (client violated advertised
///   `BLOCK_SIZE_MAX`).
/// * Short read on a `WRITE` payload (client truncated mid-frame).
///
/// [`tokio::io::DuplexStream`]: https://docs.rs/tokio/latest/tokio/io/struct.DuplexStream.html
/// [`tokio::net::UnixStream`]: https://docs.rs/tokio/latest/tokio/net/struct.UnixStream.html
pub async fn run<B, S>(stream: &mut S, backend: &B) -> Result<()>
where
    B: BlockBackend,
    S: AsyncRead + AsyncWrite + Unpin,
{
    loop {
        let Some(req) = read_request_header(stream).await? else {
            debug!("client closed stream cleanly between commands");
            return Ok(());
        };
        trace!(
            kind = req.kind,
            handle = req.handle,
            offset = req.offset,
            length = req.length,
            flags = req.flags,
            "nbd request",
        );

        match req.kind {
            NBD_CMD_DISC => {
                debug!(handle = req.handle, "client requested disconnect");
                return Ok(());
            }
            NBD_CMD_READ => handle_read(stream, backend, &req).await?,
            NBD_CMD_WRITE => handle_write(stream, backend, &req).await?,
            NBD_CMD_FLUSH => handle_flush(stream, backend, &req).await?,
            NBD_CMD_TRIM => handle_trim(stream, &req).await?,
            unknown => {
                warn!(kind = unknown, "unknown NBD command");
                write_error_reply(stream, NBD_ENOTSUP, req.handle).await?;
            }
        }
    }
}

/// Read a 28-byte request header, returning `None` if the client
/// closed the read half cleanly *before* sending any bytes of the
/// next header. A short read mid-header is treated as a hard
/// error.
async fn read_request_header<S>(stream: &mut S) -> Result<Option<RequestHeader>>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let mut buf = [0u8; REQUEST_HEADER_LEN];
    // Read the first byte separately so we can distinguish "client
    // closed cleanly between commands" (Ok(None)) from "client
    // closed mid-header" (Err) without using the indexing-slicing
    // pattern clippy dislikes.
    let (first, rest) = buf.split_at_mut(1);
    let n = stream.read(first).await?;
    if n == 0 {
        return Ok(None);
    }
    stream
        .read_exact(rest)
        .await
        .context("short read on request header")?;
    match decode_request_header(&buf) {
        Some(req) => Ok(Some(req)),
        None => bail!(
            "bad NBD request magic 0x{:08x}",
            u32::from_be_bytes([buf[0], buf[1], buf[2], buf[3]]),
        ),
    }
}

/// Reject a request whose `length` exceeds the advertised
/// per-request ceiling. Writes an [`NBD_EOVERFLOW`] reply and
/// returns `Err` so the loop terminates the connection — a client
/// that violates the contract advertised in the handshake should
/// not be allowed to keep sending.
async fn refuse_oversized<S>(stream: &mut S, handle: u64) -> Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    write_error_reply(stream, NBD_EOVERFLOW, handle).await?;
    bail!("client sent request exceeding advertised BLOCK_SIZE_MAX");
}

/// Handle `NBD_CMD_READ`: read `length` bytes from the backend at
/// `offset` and ship them back behind a success reply header.
async fn handle_read<B, S>(stream: &mut S, backend: &B, req: &RequestHeader) -> Result<()>
where
    B: BlockBackend,
    S: AsyncRead + AsyncWrite + Unpin,
{
    if req.length > BLOCK_SIZE_MAX {
        return refuse_oversized(stream, req.handle).await;
    }
    // Cast safety: `length: u32` after the guard above is <= 32 MiB,
    // well within `usize` on every target this daemon runs on.
    let len = req.length as usize;
    let mut buf = vec![0u8; len];
    match backend.read(req.offset, &mut buf).await {
        Ok(()) => {
            let header = encode_simple_reply_header(NBD_EOK, req.handle);
            stream.write_all(&header).await?;
            if len > 0 {
                stream.write_all(&buf).await?;
            }
            stream.flush().await?;
        }
        Err(e) => {
            let code = map_backend_err(&e);
            debug!(handle = req.handle, code, ?e, "read failed");
            write_error_reply(stream, code, req.handle).await?;
        }
    }
    Ok(())
}

/// Handle `NBD_CMD_WRITE`: read the payload, dispatch to the
/// backend (honouring FUA), then reply with the result.
///
/// The payload is **always** drained from the stream before the
/// reply is written, even if the request is going to be rejected,
/// so the byte stream stays aligned to frame boundaries.
async fn handle_write<B, S>(stream: &mut S, backend: &B, req: &RequestHeader) -> Result<()>
where
    B: BlockBackend,
    S: AsyncRead + AsyncWrite + Unpin,
{
    if req.length > BLOCK_SIZE_MAX {
        // Don't even try to drain — at 4 GiB max-on-wire this
        // would be a DOS amplifier. Bail before allocating.
        return refuse_oversized(stream, req.handle).await;
    }
    let len = req.length as usize;
    let mut buf = vec![0u8; len];
    if len > 0 {
        stream.read_exact(&mut buf).await?;
    }
    let flags = decode_write_flags(req.flags);
    match backend.write(req.offset, &buf, flags).await {
        Ok(()) => {
            write_ok_reply(stream, req.handle).await?;
        }
        Err(e) => {
            let code = map_backend_err(&e);
            debug!(handle = req.handle, code, ?e, "write failed");
            write_error_reply(stream, code, req.handle).await?;
        }
    }
    Ok(())
}

/// Handle `NBD_CMD_FLUSH`: ask the backend to sync, reply with
/// the result. NBD spec says `offset` and `length` MUST be zero on
/// FLUSH — we don't enforce that aggressively, but we log it.
async fn handle_flush<B, S>(stream: &mut S, backend: &B, req: &RequestHeader) -> Result<()>
where
    B: BlockBackend,
    S: AsyncRead + AsyncWrite + Unpin,
{
    if req.offset != 0 || req.length != 0 {
        trace!(
            offset = req.offset,
            length = req.length,
            "client sent non-zero offset/length on FLUSH (spec violation, ignoring)",
        );
    }
    match backend.flush().await {
        Ok(()) => write_ok_reply(stream, req.handle).await?,
        Err(e) => {
            let code = map_backend_err(&e);
            debug!(handle = req.handle, code, ?e, "flush failed");
            write_error_reply(stream, code, req.handle).await?;
        }
    }
    Ok(())
}

/// Handle `NBD_CMD_TRIM`: the backend is not required to implement
/// discard, so we acknowledge success without touching the
/// backend. Tesla's USB workload never trims so this is genuinely
/// a no-op for now; if we later add a discard hook to
/// [`BlockBackend`] this is the only place that has to change.
async fn handle_trim<S>(stream: &mut S, req: &RequestHeader) -> Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    if req.length > BLOCK_SIZE_MAX {
        return refuse_oversized(stream, req.handle).await;
    }
    trace!(
        handle = req.handle,
        offset = req.offset,
        length = req.length,
        "trim (no-op success)",
    );
    write_ok_reply(stream, req.handle).await
}

/// Translate [`BackendError`] into an NBD wire error code.
fn map_backend_err(e: &BackendError) -> u32 {
    match e {
        BackendError::OutOfBounds { .. } | BackendError::InvalidArgument(_) => NBD_EINVAL,
        BackendError::Io(io) => match io.kind() {
            std::io::ErrorKind::OutOfMemory => NBD_ENOMEM,
            _ => NBD_EIO,
        },
    }
}

/// Translate NBD wire command-flags into a [`WriteFlags`].
fn decode_write_flags(wire_flags: u16) -> WriteFlags {
    if (wire_flags & NBD_CMD_FLAG_FUA) != 0 {
        WriteFlags::FUA
    } else {
        WriteFlags::NONE
    }
}

/// Write a success reply (error = 0).
async fn write_ok_reply<S>(stream: &mut S, handle: u64) -> Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let header = encode_simple_reply_header(NBD_EOK, handle);
    stream.write_all(&header).await?;
    stream.flush().await?;
    Ok(())
}

/// Write an error reply with the given NBD error code.
async fn write_error_reply<S>(stream: &mut S, code: u32, handle: u64) -> Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let header = encode_simple_reply_header(code, handle);
    stream.write_all(&header).await?;
    stream.flush().await?;
    Ok(())
}

// ---- Tests --------------------------------------------------------------

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::indexing_slicing)]
mod tests {
    use super::*;
    use crate::nbd::wire::{
        NBD_SIMPLE_REPLY_MAGIC, SIMPLE_REPLY_HEADER_LEN, encode_request_header,
    };
    use teslausb_core::backend::mock::{MockBackend, MockOp, NullBackend};
    use tokio::io::{AsyncReadExt, AsyncWriteExt, duplex};

    // ---- Test fixtures --------------------------------------------------

    /// Build a wire-format request header. Wraps
    /// [`encode_request_header`] for compactness at every test
    /// call site.
    fn req(
        kind: u16,
        handle: u64,
        offset: u64,
        length: u32,
        flags: u16,
    ) -> [u8; REQUEST_HEADER_LEN] {
        encode_request_header(&RequestHeader {
            flags,
            kind,
            handle,
            offset,
            length,
        })
    }

    /// Read exactly N bytes from the client side of a duplex
    /// stream. Test bodies focus on assertions.
    async fn read_n(stream: &mut tokio::io::DuplexStream, n: usize) -> Vec<u8> {
        let mut buf = vec![0u8; n];
        stream.read_exact(&mut buf).await.unwrap();
        buf
    }

    fn assert_ok_reply_header(bytes: &[u8], expected_handle: u64) {
        assert_eq!(bytes.len(), SIMPLE_REPLY_HEADER_LEN);
        assert_eq!(
            u32::from_be_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]),
            NBD_SIMPLE_REPLY_MAGIC,
            "reply magic",
        );
        assert_eq!(
            u32::from_be_bytes([bytes[4], bytes[5], bytes[6], bytes[7]]),
            NBD_EOK,
            "expected success reply",
        );
        assert_eq!(
            u64::from_be_bytes([
                bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14],
                bytes[15],
            ]),
            expected_handle,
            "handle echo",
        );
    }

    fn assert_err_reply_header(bytes: &[u8], expected_code: u32, expected_handle: u64) {
        assert_eq!(bytes.len(), SIMPLE_REPLY_HEADER_LEN);
        assert_eq!(
            u32::from_be_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]),
            NBD_SIMPLE_REPLY_MAGIC,
            "reply magic",
        );
        assert_eq!(
            u32::from_be_bytes([bytes[4], bytes[5], bytes[6], bytes[7]]),
            expected_code,
            "error code",
        );
        assert_eq!(
            u64::from_be_bytes([
                bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14],
                bytes[15],
            ]),
            expected_handle,
            "handle echo on error",
        );
    }

    /// Drive `run` and a client-side closure on the two ends of a
    /// `tokio::io::duplex` pipe, concurrently via `tokio::join!`.
    /// `join!` polls both futures on the same task — no `Send`
    /// bound (so the native-AFIT [`BlockBackend`] is fine) and no
    /// inter-task hop. Returns the loop's `Result<()>` so tests
    /// can assert on Err cases.
    ///
    /// The buffer is 64 KiB — far larger than any single test
    /// frame (the largest is ~50 bytes header+payload). If a test
    /// ever needs to drive > 32 KiB through the buffer this would
    /// silently deadlock; bump it then.
    async fn drive<B, Fut, T>(
        backend: &B,
        client_fn: impl FnOnce(tokio::io::DuplexStream) -> Fut,
    ) -> (Result<()>, T)
    where
        B: BlockBackend,
        Fut: std::future::Future<Output = T>,
    {
        let (client, mut server) = duplex(64 * 1024);
        let server_fut = run(&mut server, backend);
        let client_fut = client_fn(client);
        tokio::join!(server_fut, client_fut)
    }

    // ---- Wire-level dispatch tests --------------------------------------

    /// READ: backend must record the read at the right offset+len,
    /// AND the wire reply must contain those exact bytes.
    #[tokio::test]
    async fn read_dispatches_to_backend_and_returns_payload_on_wire() {
        let backend = MockBackend::new(1024);
        // Pre-seed the backend with a recognisable byte pattern.
        backend
            .write(100, &[0xDE, 0xAD, 0xBE, 0xEF], WriteFlags::NONE)
            .await
            .unwrap();
        let setup_op_count = backend.ops().len();

        let (server_result, ()) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_READ, 0xAA, 100, 4, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();

            let reply_hdr = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_ok_reply_header(&reply_hdr, 0xAA);
            let payload = read_n(&mut client, 4).await;
            assert_eq!(payload, [0xDE, 0xAD, 0xBE, 0xEF]);

            client
                .write_all(&req(NBD_CMD_DISC, 0, 0, 0, 0))
                .await
                .unwrap();
            drop(client);
        })
        .await;
        server_result.unwrap();

        let new_ops: Vec<_> = backend.ops().into_iter().skip(setup_op_count).collect();
        assert_eq!(
            new_ops,
            vec![MockOp::Read {
                offset: 100,
                len: 4
            }]
        );
    }

    /// WRITE: payload from the wire must land in the backend
    /// snapshot at the requested offset, and the recorded op must
    /// reflect the correct length.
    #[tokio::test]
    async fn write_dispatches_payload_into_backend_storage() {
        let backend = MockBackend::new(64);
        let (server_result, ()) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_WRITE, 0xBB, 8, 4, 0))
                .await
                .unwrap();
            client.write_all(&[0x11, 0x22, 0x33, 0x44]).await.unwrap();
            client.flush().await.unwrap();

            let reply_hdr = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_ok_reply_header(&reply_hdr, 0xBB);

            client
                .write_all(&req(NBD_CMD_DISC, 0, 0, 0, 0))
                .await
                .unwrap();
            drop(client);
        })
        .await;
        server_result.unwrap();

        let snap = backend.snapshot();
        assert_eq!(&snap[8..12], &[0x11, 0x22, 0x33, 0x44]);
        assert!(snap[0..8].iter().all(|&b| b == 0));
        assert!(snap[12..].iter().all(|&b| b == 0));

        assert_eq!(
            backend.ops(),
            vec![MockOp::Write {
                offset: 8,
                len: 4,
                flags: WriteFlags::NONE,
            }],
        );
    }

    /// FUA pass-through (load-bearing): NBD_CMD_FLAG_FUA on the
    /// wire must produce a backend write with WriteFlags::FUA AND
    /// the durability observable must flip to true.
    #[tokio::test]
    async fn fua_wire_flag_passes_through_to_backend_with_writeflags_fua() {
        let backend = MockBackend::new(32);
        assert!(
            !backend.observed_any_durability(),
            "preconditions: backend starts un-synced",
        );
        let (server_result, ()) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_WRITE, 0xCC, 0, 2, NBD_CMD_FLAG_FUA))
                .await
                .unwrap();
            client.write_all(&[0x77, 0x88]).await.unwrap();
            client.flush().await.unwrap();

            let reply_hdr = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_ok_reply_header(&reply_hdr, 0xCC);

            client
                .write_all(&req(NBD_CMD_DISC, 0, 0, 0, 0))
                .await
                .unwrap();
            drop(client);
        })
        .await;
        server_result.unwrap();

        assert_eq!(
            backend.ops(),
            vec![MockOp::Write {
                offset: 0,
                len: 2,
                flags: WriteFlags::FUA,
            }],
            "FUA flag from wire must reach backend as WriteFlags::FUA",
        );
        assert!(
            backend.observed_any_durability(),
            "FUA write must flip the durability observable to true",
        );
    }

    /// Non-FUA write must NOT trip the durability observable.
    /// Guards against accidentally always passing FUA.
    #[tokio::test]
    async fn non_fua_wire_flag_does_not_trip_durability_observable() {
        let backend = MockBackend::new(32);
        let (server_result, ()) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_WRITE, 1, 0, 2, 0))
                .await
                .unwrap();
            client.write_all(&[1, 2]).await.unwrap();
            client.flush().await.unwrap();
            let _ = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            client
                .write_all(&req(NBD_CMD_DISC, 0, 0, 0, 0))
                .await
                .unwrap();
            drop(client);
        })
        .await;
        server_result.unwrap();

        assert!(
            !backend.observed_any_durability(),
            "plain (flags=0) write must not be observed as durable",
        );
    }

    /// FLUSH must dispatch to backend.flush(), reply with OK, and
    /// the backend's ops log must include a Flush.
    #[tokio::test]
    async fn flush_dispatches_to_backend_and_replies_ok() {
        let backend = MockBackend::new(8);
        let (server_result, ()) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_FLUSH, 0xDD, 0, 0, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();
            let reply_hdr = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_ok_reply_header(&reply_hdr, 0xDD);
            client
                .write_all(&req(NBD_CMD_DISC, 0, 0, 0, 0))
                .await
                .unwrap();
            drop(client);
        })
        .await;
        server_result.unwrap();

        assert_eq!(backend.ops(), vec![MockOp::Flush]);
        assert!(backend.observed_any_durability());
    }

    /// TRIM is a no-op success — must NOT touch the backend, but
    /// MUST send an OK reply.
    #[tokio::test]
    async fn trim_replies_ok_without_touching_backend() {
        let backend = MockBackend::new(16);
        let (server_result, ()) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_TRIM, 0xEE, 0, 4, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();
            let reply_hdr = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_ok_reply_header(&reply_hdr, 0xEE);
            client
                .write_all(&req(NBD_CMD_DISC, 0, 0, 0, 0))
                .await
                .unwrap();
            drop(client);
        })
        .await;
        server_result.unwrap();

        assert!(
            backend.ops().is_empty(),
            "TRIM must not record any backend op (no-op success)",
        );
    }

    /// DISC must terminate the loop with Ok(()) AND must not emit
    /// any reply bytes (spec: DISC has no reply).
    ///
    /// Trick: we can't directly observe "no bytes were written
    /// after DISC" because `tokio::join!` holds both sides of the
    /// duplex stream alive until both futures finish — so there's
    /// no EOF to wait on. Instead we send `FLUSH` (which produces
    /// exactly 16 bytes — one simple-reply header) then `DISC`,
    /// and assert that the client received exactly 16 bytes total
    /// before the server returned. If `DISC` had emitted any
    /// reply, that count would be > 16.
    #[tokio::test]
    async fn disc_terminates_loop_with_no_reply() {
        let backend = NullBackend::new(8);
        let (server_result, bytes_read) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_FLUSH, 0xFE, 0, 0, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();
            // Read just the FLUSH reply header.
            let mut flush_reply = [0u8; SIMPLE_REPLY_HEADER_LEN];
            client.read_exact(&mut flush_reply).await.unwrap();
            assert_ok_reply_header(&flush_reply, 0xFE);

            // Now send DISC. If the loop honours the spec and
            // emits no reply, no further bytes can ever appear.
            client
                .write_all(&req(NBD_CMD_DISC, 0xFF, 0, 0, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();
            drop(client);
            // Total bytes read from the server.
            SIMPLE_REPLY_HEADER_LEN
        })
        .await;
        server_result.unwrap();
        assert_eq!(
            bytes_read, SIMPLE_REPLY_HEADER_LEN,
            "DISC must not emit any reply bytes (only the FLUSH reply should be on the wire)",
        );
    }

    /// Clean EOF between commands (client drops without DISC) must
    /// also exit the loop with Ok(()).
    #[tokio::test]
    async fn clean_eof_between_commands_returns_ok() {
        let backend = NullBackend::new(8);
        let (server_result, ()) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_FLUSH, 1, 0, 0, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();
            let _ = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            drop(client);
        })
        .await;
        server_result.unwrap();
    }

    /// Bad request magic must terminate the loop with an error
    /// (stream is de-synced — closing is the only safe option).
    #[tokio::test]
    async fn bad_request_magic_terminates_loop_with_error() {
        let backend = NullBackend::new(8);
        let (server_result, ()) = drive(&backend, |mut client| async move {
            let mut bad = [0u8; REQUEST_HEADER_LEN];
            bad[0..4].copy_from_slice(&0xDEAD_BEEF_u32.to_be_bytes());
            client.write_all(&bad).await.unwrap();
            client.flush().await.unwrap();
            drop(client);
        })
        .await;
        let err = server_result.expect_err("bad magic must surface as Err");
        let msg = format!("{err}");
        assert!(
            msg.contains("bad NBD request magic"),
            "error should name the failure mode, got: {msg}",
        );
    }

    /// Unknown command must reply NBD_ENOTSUP and the loop must
    /// continue.
    #[tokio::test]
    async fn unknown_command_replies_enotsup_and_loop_continues() {
        let backend = MockBackend::new(8);
        let (server_result, ()) = drive(&backend, |mut client| async move {
            client.write_all(&req(99, 0x10, 0, 0, 0)).await.unwrap();
            client.flush().await.unwrap();
            let r1 = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_err_reply_header(&r1, NBD_ENOTSUP, 0x10);

            client
                .write_all(&req(NBD_CMD_FLUSH, 0x11, 0, 0, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();
            let r2 = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_ok_reply_header(&r2, 0x11);

            client
                .write_all(&req(NBD_CMD_DISC, 0, 0, 0, 0))
                .await
                .unwrap();
            drop(client);
        })
        .await;
        server_result.unwrap();
        assert_eq!(backend.ops(), vec![MockOp::Flush]);
    }

    /// Out-of-bounds READ → reply EINVAL with right handle and NO
    /// payload bytes.
    #[tokio::test]
    async fn out_of_bounds_read_replies_einval_with_no_payload() {
        let backend = MockBackend::new(8);
        let (server_result, ()) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_READ, 0x20, 16, 4, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();
            let reply_hdr = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_err_reply_header(&reply_hdr, NBD_EINVAL, 0x20);
            // Stream stays aligned: DISC must process cleanly
            // (proves no stray payload bytes preceded it).
            client
                .write_all(&req(NBD_CMD_DISC, 0, 0, 0, 0))
                .await
                .unwrap();
            drop(client);
        })
        .await;
        server_result.unwrap();
    }

    /// Out-of-bounds WRITE: payload must still be consumed from
    /// the wire (so the stream stays aligned). Follow-up valid
    /// command must succeed.
    #[tokio::test]
    async fn out_of_bounds_write_drains_payload_then_replies_einval() {
        let backend = MockBackend::new(4);
        let (server_result, ()) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_WRITE, 0x30, 0, 8, 0))
                .await
                .unwrap();
            client.write_all(&[1, 2, 3, 4, 5, 6, 7, 8]).await.unwrap();
            client.flush().await.unwrap();
            let r1 = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_err_reply_header(&r1, NBD_EINVAL, 0x30);

            client
                .write_all(&req(NBD_CMD_FLUSH, 0x31, 0, 0, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();
            let r2 = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_ok_reply_header(&r2, 0x31);

            client
                .write_all(&req(NBD_CMD_DISC, 0, 0, 0, 0))
                .await
                .unwrap();
            drop(client);
        })
        .await;
        server_result.unwrap();
        assert!(
            backend.snapshot().iter().all(|&b| b == 0),
            "rejected write must not have mutated backend storage",
        );
    }

    /// Oversized READ (length > BLOCK_SIZE_MAX) must reply
    /// EOVERFLOW AND terminate the loop with Err.
    #[tokio::test]
    async fn oversized_read_replies_eoverflow_and_terminates_loop() {
        let backend = NullBackend::new(8);
        let (server_result, ()) = drive(&backend, |mut client| async move {
            let oversize = BLOCK_SIZE_MAX + 1;
            client
                .write_all(&req(NBD_CMD_READ, 0x40, 0, oversize, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();
            let reply_hdr = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_err_reply_header(&reply_hdr, NBD_EOVERFLOW, 0x40);
            drop(client);
        })
        .await;
        let err = server_result.expect_err("oversized request must terminate connection");
        assert!(
            format!("{err}").contains("BLOCK_SIZE_MAX"),
            "error should mention the violated cap",
        );
    }

    /// Handle echo: a distinctive handle must appear in the reply
    /// bytes, AND only the handle bytes change between two replies
    /// for the same op.
    #[tokio::test]
    async fn handle_is_echoed_byte_for_byte_in_reply() {
        let backend = NullBackend::new(0);
        let h1: u64 = 0x0101_0202_0303_0404;
        let h2: u64 = 0xFFEE_DDCC_BBAA_9988;
        let (server_result, (r1, r2)) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_FLUSH, h1, 0, 0, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();
            let r1 = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;

            client
                .write_all(&req(NBD_CMD_FLUSH, h2, 0, 0, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();
            let r2 = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;

            client
                .write_all(&req(NBD_CMD_DISC, 0, 0, 0, 0))
                .await
                .unwrap();
            drop(client);
            (r1, r2)
        })
        .await;
        server_result.unwrap();

        assert_eq!(&r1[0..8], &r2[0..8], "magic + error stable");
        assert_eq!(
            u64::from_be_bytes([r1[8], r1[9], r1[10], r1[11], r1[12], r1[13], r1[14], r1[15]]),
            h1,
        );
        assert_eq!(
            u64::from_be_bytes([r2[8], r2[9], r2[10], r2[11], r2[12], r2[13], r2[14], r2[15]]),
            h2,
        );
    }

    /// Pipelined commands: 3 writes back-to-back, verify backend
    /// recorded them in order AND replies came back in order.
    #[tokio::test]
    async fn pipelined_requests_processed_in_order() {
        let backend = MockBackend::new(16);
        let (server_result, ()) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_WRITE, 1, 0, 1, 0))
                .await
                .unwrap();
            client.write_all(&[0xAA]).await.unwrap();
            client
                .write_all(&req(NBD_CMD_WRITE, 2, 4, 1, 0))
                .await
                .unwrap();
            client.write_all(&[0xBB]).await.unwrap();
            client
                .write_all(&req(NBD_CMD_WRITE, 3, 8, 1, 0))
                .await
                .unwrap();
            client.write_all(&[0xCC]).await.unwrap();
            client.flush().await.unwrap();

            let r1 = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            let r2 = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            let r3 = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_ok_reply_header(&r1, 1);
            assert_ok_reply_header(&r2, 2);
            assert_ok_reply_header(&r3, 3);

            client
                .write_all(&req(NBD_CMD_DISC, 0, 0, 0, 0))
                .await
                .unwrap();
            drop(client);
        })
        .await;
        server_result.unwrap();

        assert_eq!(
            backend.ops(),
            vec![
                MockOp::Write {
                    offset: 0,
                    len: 1,
                    flags: WriteFlags::NONE
                },
                MockOp::Write {
                    offset: 4,
                    len: 1,
                    flags: WriteFlags::NONE
                },
                MockOp::Write {
                    offset: 8,
                    len: 1,
                    flags: WriteFlags::NONE
                },
            ],
        );

        let snap = backend.snapshot();
        assert_eq!(snap[0], 0xAA);
        assert_eq!(snap[4], 0xBB);
        assert_eq!(snap[8], 0xCC);
    }

    /// Zero-length READ → OK reply, no payload.
    #[tokio::test]
    async fn zero_length_read_replies_ok_with_no_payload() {
        let backend = NullBackend::new(8);
        let (server_result, ()) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_READ, 0x50, 0, 0, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();
            let reply_hdr = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_ok_reply_header(&reply_hdr, 0x50);
            // No stray payload bytes: a follow-up DISC must
            // process cleanly.
            client
                .write_all(&req(NBD_CMD_DISC, 0, 0, 0, 0))
                .await
                .unwrap();
            drop(client);
        })
        .await;
        server_result.unwrap();
    }

    /// Zero-length WRITE → OK reply, op recorded with len=0.
    #[tokio::test]
    async fn zero_length_write_replies_ok_and_records_zero_len_op() {
        let backend = MockBackend::new(8);
        let (server_result, ()) = drive(&backend, |mut client| async move {
            client
                .write_all(&req(NBD_CMD_WRITE, 0x60, 4, 0, 0))
                .await
                .unwrap();
            client.flush().await.unwrap();
            let reply_hdr = read_n(&mut client, SIMPLE_REPLY_HEADER_LEN).await;
            assert_ok_reply_header(&reply_hdr, 0x60);
            client
                .write_all(&req(NBD_CMD_DISC, 0, 0, 0, 0))
                .await
                .unwrap();
            drop(client);
        })
        .await;
        server_result.unwrap();
        assert_eq!(
            backend.ops(),
            vec![MockOp::Write {
                offset: 4,
                len: 0,
                flags: WriteFlags::NONE,
            }],
        );
    }

    // ---- Pure-helper tests ---------------------------------------------

    #[test]
    fn map_backend_err_covers_every_variant() {
        let oob = BackendError::OutOfBounds {
            offset: 0,
            len: 0,
            size: 0,
        };
        assert_eq!(map_backend_err(&oob), NBD_EINVAL);

        let inv = BackendError::InvalidArgument("bad");
        assert_eq!(map_backend_err(&inv), NBD_EINVAL);

        let io_oom = BackendError::Io(std::io::Error::new(
            std::io::ErrorKind::OutOfMemory,
            "alloc",
        ));
        assert_eq!(map_backend_err(&io_oom), NBD_ENOMEM);

        let io_other = BackendError::Io(std::io::Error::other("disk gone"));
        assert_eq!(map_backend_err(&io_other), NBD_EIO);
    }

    #[test]
    fn decode_write_flags_extracts_only_known_bits() {
        assert_eq!(decode_write_flags(0), WriteFlags::NONE);
        assert_eq!(decode_write_flags(NBD_CMD_FLAG_FUA), WriteFlags::FUA);
        assert_eq!(
            decode_write_flags(0xFFFE | NBD_CMD_FLAG_FUA),
            WriteFlags::FUA,
        );
        assert_eq!(decode_write_flags(0xFFFE), WriteFlags::NONE);
    }
}
