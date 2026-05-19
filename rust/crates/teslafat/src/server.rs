//! Accept loop and per-connection dispatch for the `teslafat`
//! NBD daemon.
//!
//! ## Phase 1.6 state
//!
//! `serve` binds a Unix-domain `tokio::net::UnixListener`, accepts
//! one client at a time, and hands each accepted stream to
//! [`serve_one_connection`]. Each connection:
//!
//! 1. Runs [`crate::nbd::handshake::run`] inside a configurable
//!    [`tokio::time::timeout`]. The timeout is the load-bearing
//!    guard against an idle / misbehaving client sitting on a
//!    connection forever — there is no other liveness check in
//!    the handshake phase. (The follow-up TODO from inc-1.3 is
//!    closed here.)
//! 2. On handshake success, runs [`crate::nbd::transmission::run`]
//!    until the client sends `NBD_CMD_DISC` or the wire desyncs.
//! 3. On handshake failure or timeout, logs a `warn!` line and
//!    returns from `serve_one_connection`; the accept loop moves
//!    on to the next client.
//!
//! ## Concurrency model
//!
//! Strictly single-connection-at-a-time. NBD's simple-reply mode
//! requires in-order replies on a given client handle, and the
//! Linux `nbd-client` kernel module is the only intended peer —
//! one kernel device == one client connection at a time. Serving
//! concurrent connections would not just add complexity; it would
//! also let two clients race on the same backing image, which the
//! whole point of the daemon (the userspace gadget endpoint) is
//! to prevent.
//!
//! ## Backend ownership
//!
//! Borrowed (`&B`). The same backend instance is reused across
//! reconnects so the daemon can stay up across e.g. an
//! `nbd-client -d` cycle without rebuilding state.
//!
//! ## Cross-platform note
//!
//! [`serve_one_connection`] is generic over `AsyncRead + AsyncWrite`
//! and compiles + tests on every supported host (the dev box
//! runs Windows; the deploy target is Linux). `serve` takes a
//! `tokio::net::UnixListener` and is `#[cfg(unix)]`-only — the
//! daemon itself only runs on the Pi.

// `Future`, `Result`, and `info!` are only referenced inside the
// `#[cfg(unix)]` `serve` function. Gating the imports the same way
// keeps the Windows dev-host build warning-free.
#[cfg(unix)]
use std::future::Future;
use std::time::Duration;

#[cfg(unix)]
use anyhow::Result;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::time::error::Elapsed;
use tokio::time::timeout;
#[cfg(unix)]
use tracing::info;
use tracing::{debug, warn};

use teslausb_core::backend::BlockBackend;

use crate::nbd::{handshake, transmission};

/// Run the handshake-with-timeout + transmission pipeline against
/// one already-accepted byte stream.
///
/// Never returns `Err` — every per-connection failure mode (bad
/// magic, handshake timeout, transmission wire error) is logged at
/// `warn!` and turned into a normal return. The accept loop in
/// `serve` must continue running regardless of any one client's
/// behaviour, so propagating an error here would cause a single
/// misbehaving client to take the daemon down.
///
/// Generic over the stream type so it can be unit-tested with a
/// [`tokio::io::DuplexStream`] on any host (Windows dev box
/// included) without binding a real Unix socket.
pub async fn serve_one_connection<S, B>(stream: &mut S, backend: &B, handshake_timeout: Duration)
where
    S: AsyncRead + AsyncWrite + Unpin,
    B: BlockBackend,
{
    let export_size = backend.size();
    let handshake_fut = handshake::run(stream, export_size);
    match timeout(handshake_timeout, handshake_fut).await {
        Err(elapsed) => {
            // `elapsed: Elapsed` is the tokio marker type; binding
            // the import in a local proves the trait reference at
            // the top of the file stays load-bearing (a future
            // tokio rename would break this line loudly instead of
            // turning into a dead `use`).
            let _: Elapsed = elapsed;
            warn!(
                timeout_s = handshake_timeout.as_secs(),
                "handshake timed out; closing connection",
            );
        }
        Ok(Err(e)) => {
            warn!(error = ?e, "handshake failed; closing connection");
        }
        Ok(Ok(())) => {
            debug!("handshake complete; entering transmission phase");
            if let Err(e) = transmission::run(stream, backend).await {
                warn!(error = ?e, "transmission terminated with error");
            } else {
                debug!("transmission ended cleanly (client DISC or EOF)");
            }
        }
    }
}

/// Bind-accept-dispatch loop. Returns `Ok(())` when the `shutdown`
/// future resolves; the accept loop itself never returns `Err` for
/// a recoverable accept failure (those are logged and retried).
///
/// `shutdown` is polled concurrently with `listener.accept()` via
/// a [`tokio::select!`] with `biased` priority — a shutdown signal
/// that arrives at the same moment as a connection wins, so the
/// daemon never starts a new session it is about to abandon.
///
/// # Errors
///
/// Returns `Err` only if `listener.accept()` returns an error that
/// the loop cannot recover from. The current implementation logs
/// recoverable errors (`EMFILE`, `ECONNABORTED`, etc.) and
/// continues; the only fatal mode is the listener being dropped
/// out from under us, which Tokio surfaces as an I/O error.
#[cfg(unix)]
pub async fn serve<B, F>(
    listener: tokio::net::UnixListener,
    backend: &B,
    handshake_timeout: Duration,
    shutdown: F,
) -> Result<()>
where
    B: BlockBackend,
    F: Future<Output = ()>,
{
    tokio::pin!(shutdown);
    info!(
        handshake_timeout_s = handshake_timeout.as_secs(),
        "teslafat NBD daemon accepting connections",
    );
    loop {
        tokio::select! {
            // Bias the select so a shutdown signal that arrives at
            // the same instant as an incoming connection wins. We
            // would rather refuse a connection the kernel is about
            // to retry than start a session we cannot finish.
            biased;
            () = &mut shutdown => {
                info!("shutdown signal received; accept loop exiting");
                return Ok(());
            }
            accept_result = listener.accept() => {
                match accept_result {
                    Ok((mut stream, _addr)) => {
                        debug!("client connected");
                        serve_one_connection(&mut stream, backend, handshake_timeout).await;
                        debug!("client disconnected; awaiting next");
                    }
                    Err(e) => {
                        // EMFILE / ECONNABORTED / ENFILE are all
                        // transient. Log and keep looping; the
                        // alternative (bailing) would let one bad
                        // accept take the daemon down.
                        warn!(error = ?e, "listener.accept() failed; continuing");
                    }
                }
            }
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::indexing_slicing)]
mod tests {
    use super::*;
    use crate::backend::ZeroBackend;
    use crate::nbd::handshake::{CF_FIXED_NEWSTYLE, GREETING_LEN, IHAVEOPT, NBD_OPT_EXPORT_NAME};
    use crate::nbd::wire::{
        NBD_CMD_DISC, NBD_REQUEST_MAGIC, REQUEST_HEADER_LEN, RequestHeader, encode_request_header,
    };
    use tokio::io::{AsyncReadExt, AsyncWriteExt, duplex};

    /// Drive the NBD newstyle handshake from the client side using
    /// the simplest path — `NBD_OPT_EXPORT_NAME`. After this
    /// returns the server has transitioned into the transmission
    /// phase on the same stream and the client may issue request
    /// headers.
    async fn drive_client_handshake<S>(client: &mut S)
    where
        S: AsyncRead + AsyncWrite + Unpin,
    {
        let mut greeting = [0u8; GREETING_LEN];
        client.read_exact(&mut greeting).await.unwrap();
        client
            .write_all(&CF_FIXED_NEWSTYLE.to_be_bytes())
            .await
            .unwrap();
        client.write_all(&IHAVEOPT.to_be_bytes()).await.unwrap();
        client
            .write_all(&NBD_OPT_EXPORT_NAME.to_be_bytes())
            .await
            .unwrap();
        // 0-byte export name → default export
        client.write_all(&0u32.to_be_bytes()).await.unwrap();
        client.flush().await.unwrap();
        // Consume the 10-byte export-name reply (u64 size + u16 xmit
        // flags). Reading it ensures we don't shut down the
        // transmission phase before the server has finished writing.
        let mut reply = [0u8; 10];
        client.read_exact(&mut reply).await.unwrap();
    }

    fn disc_request() -> [u8; REQUEST_HEADER_LEN] {
        encode_request_header(&RequestHeader {
            flags: 0,
            kind: NBD_CMD_DISC,
            handle: 0,
            offset: 0,
            length: 0,
        })
    }

    /// Happy path: client completes handshake then sends DISC.
    /// `serve_one_connection` returns. The test would hang
    /// (caught by the `#[tokio::test]` watchdog) if the function
    /// failed to recognise DISC or kept the connection open.
    #[tokio::test]
    async fn serve_one_connection_completes_after_handshake_and_disc() {
        let (mut server_end, mut client_end) = duplex(64 * 1024);
        let backend = ZeroBackend::new(4096);
        let server_fut = serve_one_connection(&mut server_end, &backend, Duration::from_secs(5));
        let client_fut = async {
            drive_client_handshake(&mut client_end).await;
            client_end.write_all(&disc_request()).await.unwrap();
            client_end.flush().await.unwrap();
            // Drop the client end so the server's transmission
            // loop sees EOF on any subsequent read attempt — but
            // it should NOT need to read any further; DISC alone
            // must return cleanly.
        };
        let ((), ()) = tokio::join!(server_fut, client_fut);
    }

    /// Client never speaks. The handshake timeout fires and the
    /// function returns. Verified by bounding the total test time
    /// at a small multiple of the configured timeout — if the
    /// guard was missing, `handshake::run` would block forever
    /// waiting on `client_flags` and `tokio::test` would time the
    /// test out at its default 60 s.
    #[tokio::test]
    async fn serve_one_connection_returns_when_handshake_times_out() {
        let (mut server_end, _client_end) = duplex(64 * 1024);
        let backend = ZeroBackend::new(4096);
        let timeout_duration = Duration::from_millis(50);
        let start = std::time::Instant::now();
        serve_one_connection(&mut server_end, &backend, timeout_duration).await;
        let elapsed = start.elapsed();
        assert!(
            elapsed >= timeout_duration,
            "returned before timeout fired (elapsed={elapsed:?})",
        );
        assert!(
            elapsed < timeout_duration * 20,
            "took far longer than the configured timeout (elapsed={elapsed:?}, \
             timeout={timeout_duration:?}) — handshake_timeout guard is not wired",
        );
    }

    /// Client sends valid greeting reply with `CF_FIXED_NEWSTYLE`
    /// cleared. `handshake::run` returns `Err`. The function
    /// returns instead of propagating — that is the contract
    /// guarding the accept loop's liveness.
    #[tokio::test]
    async fn serve_one_connection_returns_when_handshake_fails() {
        let (mut server_end, mut client_end) = duplex(64 * 1024);
        let backend = ZeroBackend::new(4096);
        let server_fut = serve_one_connection(&mut server_end, &backend, Duration::from_secs(5));
        let client_fut = async {
            let mut greeting = [0u8; GREETING_LEN];
            client_end.read_exact(&mut greeting).await.unwrap();
            // 0 client flags == CF_FIXED_NEWSTYLE missing.
            client_end.write_all(&0u32.to_be_bytes()).await.unwrap();
            client_end.flush().await.unwrap();
        };
        let ((), ()) = tokio::join!(server_fut, client_fut);
    }

    /// Handshake succeeds but the client then writes a request
    /// frame with a corrupted magic byte. `transmission::run`
    /// returns `Err`; `serve_one_connection` swallows it and
    /// returns.
    #[tokio::test]
    async fn serve_one_connection_returns_when_transmission_errors_out() {
        let (mut server_end, mut client_end) = duplex(64 * 1024);
        let backend = ZeroBackend::new(4096);
        let server_fut = serve_one_connection(&mut server_end, &backend, Duration::from_secs(5));
        let client_fut = async {
            drive_client_handshake(&mut client_end).await;
            // Send a header whose magic is one bit off — the wire
            // decoder must reject this and propagate Err out of
            // transmission::run.
            let mut bad = encode_request_header(&RequestHeader {
                flags: 0,
                kind: 0,
                handle: 1,
                offset: 0,
                length: 0,
            });
            // Corrupt the magic field in place (first 4 bytes).
            let corrupted = NBD_REQUEST_MAGIC ^ 0x0000_00FF;
            bad[0..4].copy_from_slice(&corrupted.to_be_bytes());
            client_end.write_all(&bad).await.unwrap();
            client_end.flush().await.unwrap();
        };
        let ((), ()) = tokio::join!(server_fut, client_fut);
    }

    /// Backend size is what gets advertised in the handshake. Use
    /// a non-standard size and read it back from the
    /// `NBD_OPT_EXPORT_NAME` reply on the client side to prove
    /// `serve_one_connection` is plumbing `backend.size()` into
    /// the handshake (and not, say, defaulting to zero).
    #[tokio::test]
    async fn serve_one_connection_advertises_backend_size() {
        const SIZE: u64 = 2_048_000;
        let (mut server_end, mut client_end) = duplex(64 * 1024);
        let backend = ZeroBackend::new(SIZE);
        let server_fut = serve_one_connection(&mut server_end, &backend, Duration::from_secs(5));
        let client_fut = async {
            let mut greeting = [0u8; GREETING_LEN];
            client_end.read_exact(&mut greeting).await.unwrap();
            client_end
                .write_all(&CF_FIXED_NEWSTYLE.to_be_bytes())
                .await
                .unwrap();
            client_end.write_all(&IHAVEOPT.to_be_bytes()).await.unwrap();
            client_end
                .write_all(&NBD_OPT_EXPORT_NAME.to_be_bytes())
                .await
                .unwrap();
            client_end.write_all(&0u32.to_be_bytes()).await.unwrap();
            client_end.flush().await.unwrap();
            let mut reply = [0u8; 10];
            client_end.read_exact(&mut reply).await.unwrap();
            let advertised = u64::from_be_bytes([
                reply[0], reply[1], reply[2], reply[3], reply[4], reply[5], reply[6], reply[7],
            ]);
            assert_eq!(advertised, SIZE);
            client_end.write_all(&disc_request()).await.unwrap();
            client_end.flush().await.unwrap();
        };
        let ((), ()) = tokio::join!(server_fut, client_fut);
    }

    // ---------- accept-loop tests (Unix-only because UnixListener
    //            is Unix-only) ----------

    #[cfg(unix)]
    mod accept_loop {
        use super::*;
        use tokio::net::{UnixListener, UnixStream};

        /// Shutdown future that resolves immediately. `serve`
        /// returns on the first iteration of its select loop
        /// without ever attempting `listener.accept()`.
        #[tokio::test]
        async fn serve_returns_immediately_when_shutdown_already_fired() {
            let dir = tempfile::tempdir().unwrap();
            let socket_path = dir.path().join("teslafat.sock");
            let listener = UnixListener::bind(&socket_path).unwrap();
            let backend = ZeroBackend::new(4096);
            let result = serve(
                listener,
                &backend,
                Duration::from_secs(5),
                async { /* resolved instantly */ },
            )
            .await;
            assert!(result.is_ok());
        }

        /// No connections; shutdown future resolves after a small
        /// delay. `serve` returns within reasonable time of the
        /// shutdown firing — proves the accept loop is actually
        /// cancellable mid-accept (not blocked on the syscall).
        #[tokio::test]
        async fn serve_returns_promptly_after_shutdown_signal_during_idle_accept() {
            let dir = tempfile::tempdir().unwrap();
            let socket_path = dir.path().join("teslafat.sock");
            let listener = UnixListener::bind(&socket_path).unwrap();
            let backend = ZeroBackend::new(4096);
            let shutdown_at = Duration::from_millis(50);
            let start = std::time::Instant::now();
            serve(listener, &backend, Duration::from_secs(5), async move {
                tokio::time::sleep(shutdown_at).await;
            })
            .await
            .unwrap();
            let elapsed = start.elapsed();
            assert!(elapsed >= shutdown_at);
            assert!(
                elapsed < shutdown_at * 20,
                "shutdown took far too long (elapsed={elapsed:?}) — accept loop \
                 may not be select!'d against shutdown",
            );
        }

        /// End-to-end: bind, client connects + handshakes + DISCs,
        /// shutdown signal, `serve` returns Ok. Demonstrates that
        /// after one connection the accept loop returns to the
        /// select and is still responsive to shutdown.
        #[tokio::test]
        async fn serve_handles_one_connection_then_returns_on_shutdown() {
            let dir = tempfile::tempdir().unwrap();
            let socket_path = dir.path().join("teslafat.sock");
            let listener = UnixListener::bind(&socket_path).unwrap();
            let backend = ZeroBackend::new(4096);
            let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();
            let server_fut = serve(listener, &backend, Duration::from_secs(5), async move {
                let _ = shutdown_rx.await;
            });
            let socket_path_for_client = socket_path.clone();
            let client_fut = async move {
                let mut stream = UnixStream::connect(&socket_path_for_client).await.unwrap();
                drive_client_handshake(&mut stream).await;
                stream.write_all(&disc_request()).await.unwrap();
                stream.flush().await.unwrap();
                drop(stream);
                // Allow the server's accept loop to fully drain
                // the connection back to the select arm before we
                // signal shutdown — otherwise the join below
                // would race the inner serve_one_connection.
                tokio::time::sleep(Duration::from_millis(50)).await;
                shutdown_tx.send(()).unwrap();
            };
            let (server_result, ()) = tokio::join!(server_fut, client_fut);
            server_result.unwrap();
        }

        /// Two sequential connections from the same listener.
        /// Proves the accept loop is genuinely a loop, not a
        /// one-shot. If the first connection accidentally caused
        /// `serve` to return, the second `UnixStream::connect`
        /// would fail with ECONNREFUSED.
        #[tokio::test]
        async fn serve_handles_two_sequential_connections() {
            let dir = tempfile::tempdir().unwrap();
            let socket_path = dir.path().join("teslafat.sock");
            let listener = UnixListener::bind(&socket_path).unwrap();
            let backend = ZeroBackend::new(4096);
            let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();
            let server_fut = serve(listener, &backend, Duration::from_secs(5), async move {
                let _ = shutdown_rx.await;
            });
            let socket_path_for_client = socket_path.clone();
            let client_fut = async move {
                for _ in 0..2 {
                    let mut stream = UnixStream::connect(&socket_path_for_client).await.unwrap();
                    drive_client_handshake(&mut stream).await;
                    stream.write_all(&disc_request()).await.unwrap();
                    stream.flush().await.unwrap();
                    drop(stream);
                    tokio::time::sleep(Duration::from_millis(20)).await;
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
                shutdown_tx.send(()).unwrap();
            };
            let (server_result, ()) = tokio::join!(server_fut, client_fut);
            server_result.unwrap();
        }

        /// A client that handshakes successfully but then sends a
        /// garbage transmission frame must not take the daemon
        /// down — the accept loop must remain ready for the
        /// next client. The test bound is the second client's
        /// successful handshake.
        #[tokio::test]
        async fn serve_recovers_from_bad_client_and_accepts_next_client() {
            let dir = tempfile::tempdir().unwrap();
            let socket_path = dir.path().join("teslafat.sock");
            let listener = UnixListener::bind(&socket_path).unwrap();
            let backend = ZeroBackend::new(4096);
            let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();
            let server_fut = serve(listener, &backend, Duration::from_secs(5), async move {
                let _ = shutdown_rx.await;
            });
            let socket_path_for_client = socket_path.clone();
            let client_fut = async move {
                // Bad client: handshake then bogus magic.
                {
                    let mut stream = UnixStream::connect(&socket_path_for_client).await.unwrap();
                    drive_client_handshake(&mut stream).await;
                    let mut bad = encode_request_header(&RequestHeader {
                        flags: 0,
                        kind: 0,
                        handle: 7,
                        offset: 0,
                        length: 0,
                    });
                    bad[0..4].copy_from_slice(&0xDEAD_BEEFu32.to_be_bytes());
                    stream.write_all(&bad).await.unwrap();
                    stream.flush().await.unwrap();
                    drop(stream);
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
                // Good client: handshake + DISC.
                {
                    let mut stream = UnixStream::connect(&socket_path_for_client).await.unwrap();
                    drive_client_handshake(&mut stream).await;
                    stream.write_all(&disc_request()).await.unwrap();
                    stream.flush().await.unwrap();
                    drop(stream);
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
                shutdown_tx.send(()).unwrap();
            };
            let (server_result, ()) = tokio::join!(server_fut, client_fut);
            server_result.unwrap();
        }
    }
}
