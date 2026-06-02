//! Phase 1.7 dev-box smoke test harness.
//!
//! Spawns the compiled `teslafat` binary as a subprocess against a
//! fixture TOML config that points the NBD listen socket at a
//! tempdir-local path and `backing_root` at an empty tempdir
//! directory, then drives the NBD newstyle handshake +
//! transmission protocol over a real `tokio::net::UnixStream` from
//! the test process. The backend ([`teslafat::backend::SynthBackend`],
//! wrapped in [`teslafat::backend::ReloadableBackend`]) synthesises a
//! FAT32 view of the (empty) backing tree, so the smoke test asserts:
//!
//! 1. The daemon binds the configured socket and accepts a real
//!    Unix-domain client.
//! 2. The handshake negotiates `cfg.volume_size_gb * GiB` as the
//!    export size — proving the [`teslafat::config::Config`] →
//!    [`teslafat::server::serve`] → handshake plumbing is wired
//!    end-to-end through the binary's `main`.
//! 3. A `NBD_CMD_READ` of 4 KiB at offset 0 returns the synthesized
//!    FAT32 boot sector (jump `EB 58 90`, `MSWIN4.1`, `FAT32   `,
//!    `55 AA`) — proving the transmission loop dispatches commands
//!    against the live `SynthBackend` view.
//! 4. The daemon exits cleanly on `SIGTERM` with no clients
//!    connected — proving the signal-handling + accept-loop
//!    shutdown wiring works.
//! 5. The daemon survives a deliberate-bad-handshake client (no
//!    `CF_FIXED_NEWSTYLE`) and still accepts the next client
//!    cleanly — proving the ADR-0006 §B "per-connection errors
//!    never propagate to the accept loop" contract holds in the
//!    binary (not just in `serve_one_connection`'s isolated unit
//!    tests).
//! 6. A `SIGHUP` re-walks the backing tree and swaps in a fresh
//!    view once the LUN is quiescent, without changing the export
//!    size — proving the Phase-1 live-reload wiring.
//!
//! The test deliberately speaks the NBD wire protocol directly over
//! the Unix socket instead of going through the kernel `nbd-client`
//! tool. Rationale:
//!
//! * `nbd-client` requires `CAP_SYS_ADMIN` and a loaded kernel
//!   `nbd` module + `/dev/nbdN` device node, which is not available
//!   on a stock CI container or a normal Linux user account.
//! * What we want to exercise is **our server**'s correctness on
//!   the wire, not the kernel client. Hand-rolling the client side
//!   tests exactly that contract.
//! * The kernel-client smoke is the right shape of test for the
//!   H1 hardware deploy (`docs/00-PLAN.md` H1) where the Pi has
//!   root + the module loaded by design.
//!
//! The entire file is `#[cfg(unix)]` because
//! `tokio::net::UnixListener` (and therefore the daemon itself)
//! is Unix-only — see `serve` in `crate::server`.

#![cfg(unix)]
// Integration tests are a separate compilation unit; the
// `#![cfg_attr(test, allow(clippy::unwrap_used))]` attribute on
// `src/main.rs` does not reach here. `unwrap` on test setup
// (tempfile creation, socket connect retries with deterministic
// preconditions) is idiomatic in integration tests — the charter
// explicitly carves out tests in the §"Lints" discussion. `panic`
// is allowed for the same reason: a panicked smoke test surfaces
// the captured daemon stderr via `DaemonHandle::Drop`, which is
// strictly more useful than a swallowed `Err`. `print_stderr` is
// allowed because the failure-path stderr dump
// (`dump_stderr_on_failure`) deliberately writes the captured
// daemon log to the test harness's stderr so a CI failure is
// self-diagnosing.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing,
    clippy::print_stderr,
    clippy::missing_panics_doc,
    clippy::missing_errors_doc
)]

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use tempfile::TempDir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::runtime::Builder;
use tokio::time::timeout;

use teslafat::nbd::handshake::{
    CF_FIXED_NEWSTYLE, CF_NO_ZEROES, GREETING_LEN, IHAVEOPT, NBD_OPT_EXPORT_NAME,
};
use teslafat::nbd::wire::{
    NBD_CMD_DISC, NBD_CMD_READ, NBD_EOK, NBD_SIMPLE_REPLY_MAGIC, RequestHeader,
    SIMPLE_REPLY_HEADER_LEN, encode_request_header,
};

/// Bytes in one GiB. Mirrors the constant the daemon uses in
/// `main::unix_serve` to size the `SynthBackend` from
/// `cfg.volume_size_gb`. Kept as a smoke-test local constant so a
/// future renumbering in `main` (e.g. switching to GB instead of
/// GiB) would surface here as an assertion failure rather than
/// silently accept the new size.
const BYTES_PER_GIB: u64 = 1024 * 1024 * 1024;

/// Reserved region before the first partition in the synthesised MBR
/// disk: one 1 MiB alignment unit (2048 sectors) holding sector 0.
/// Matches `teslausb_core::fs::mbr::DEFAULT_ALIGNMENT_SECTORS`.
const MBR_GAP_BYTES: u64 = 1024 * 1024;

/// Total NBD export size the daemon advertises for a single-partition
/// disk: the MBR/alignment gap plus the partition's volume bytes. The
/// daemon now exports a whole partitioned disk (ADR-0023), not a bare
/// volume, so the wire size is larger than `volume_size_gb * 2^30`.
fn expected_disk_size(volume_size_gb: u32) -> u64 {
    u64::from(volume_size_gb) * BYTES_PER_GIB + MBR_GAP_BYTES
}

/// Polling interval for waiting on the daemon's socket file to
/// appear after spawn. 50 ms is long enough to avoid CPU-burning
/// on a sub-second cold boot and short enough that a fast Linux
/// run sees < 100 ms of overhead.
const SOCKET_POLL_INTERVAL: Duration = Duration::from_millis(50);

/// How long to wait for the daemon's socket file to appear before
/// giving up. The cold-start path is `Config::load` + `mkdir -p` +
/// `unlink` + `bind` — sub-second on every box we test. 10 s is a
/// big multiplier against that to absorb CI noise.
const SOCKET_WAIT_TIMEOUT: Duration = Duration::from_secs(10);

/// How long to wait for a SIGTERM'd daemon to exit before
/// escalating to SIGKILL in `DaemonHandle::drop`. The clean path
/// is `signal recv` -> `select! exits` -> `runtime.block_on`
/// returns -> `main` returns `ExitCode::SUCCESS`, which is
/// microseconds on every host.
const SIGTERM_WAIT_TIMEOUT: Duration = Duration::from_secs(5);

/// Maximum time to allow any single async NBD operation in a
/// smoke test to block. If we hit this the daemon is wedged and
/// the test should fail loudly rather than hang the suite.
const NBD_IO_TIMEOUT: Duration = Duration::from_secs(5);

const TRANSMISSION_FLAGS_LEN: usize = 2;
const EXPORT_REPLY_NO_ZEROES_LEN: usize = 8 + TRANSMISSION_FLAGS_LEN;

/// RAII guard for a spawned `teslafat` subprocess + its config /
/// socket scratch dir + its captured stderr.
///
/// `Drop` SIGTERMs the child (if still alive), waits briefly,
/// escalates to a kill on `Child::kill`, and dumps the captured
/// stderr to test output via `println!`. The dump is the
/// difference between "smoke test failed for inscrutable reason"
/// and "smoke test failed because the daemon's tracing line said
/// `bind: EACCES`" — never skip it.
struct DaemonHandle {
    child: Option<Child>,
    socket_path: PathBuf,
    // Captured stderr lines from the daemon. The pump thread
    // appends; the test reads in `Drop` to surface failure context.
    stderr_lines: Arc<Mutex<Vec<String>>>,
    // The tempdir hosts both the config file and the socket. Kept
    // alive for the lifetime of the daemon so neither vanishes
    // while it's still in use.
    _tempdir: TempDir,
}

impl DaemonHandle {
    fn socket_path(&self) -> &Path {
        &self.socket_path
    }

    fn pid(&self) -> u32 {
        self.child
            .as_ref()
            .expect("child already taken by drop")
            .id()
    }

    /// Send SIGTERM to the running daemon. Returns the daemon's
    /// exit status (waiting up to `SIGTERM_WAIT_TIMEOUT`).
    /// Subsequent `Drop` becomes a no-op.
    fn sigterm_and_wait(mut self) -> std::process::ExitStatus {
        let pid = self.pid();
        send_sigterm(pid).expect("kill -TERM <pid>");
        let mut child = self.child.take().expect("child");
        let deadline = Instant::now() + SIGTERM_WAIT_TIMEOUT;
        loop {
            match child.try_wait() {
                Ok(Some(status)) => return status,
                Ok(None) => {
                    if Instant::now() >= deadline {
                        let _ = child.kill();
                        let _ = child.wait();
                        self.dump_stderr_on_failure();
                        panic!("daemon did not exit within {SIGTERM_WAIT_TIMEOUT:?} of SIGTERM");
                    }
                    thread::sleep(SOCKET_POLL_INTERVAL);
                }
                Err(e) => {
                    self.dump_stderr_on_failure();
                    panic!("waitpid failed: {e}");
                }
            }
        }
    }

    /// Snapshot the captured stderr lines so far. Useful in
    /// asserts that want to verify the daemon emitted a specific
    /// tracing line (e.g. the "started" sentinel).
    fn stderr_snapshot(&self) -> Vec<String> {
        self.stderr_lines.lock().unwrap().clone()
    }

    fn dump_stderr_on_failure(&self) {
        let lines = self.stderr_lines.lock().unwrap();
        eprintln!("---- captured daemon stderr ({} lines) ----", lines.len());
        for line in lines.iter() {
            eprintln!("{line}");
        }
        eprintln!("---- end daemon stderr ----");
    }
}

impl Drop for DaemonHandle {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            // Best-effort cleanup: SIGTERM, brief grace, then
            // SIGKILL. We're in a test teardown; nothing here can
            // fail the test, but we should still surface anything
            // weird in the captured stderr.
            let pid = child.id();
            let _ = send_sigterm(pid);
            let deadline = Instant::now() + SIGTERM_WAIT_TIMEOUT;
            loop {
                match child.try_wait() {
                    Ok(Some(_)) | Err(_) => break,
                    Ok(None) => {
                        if Instant::now() >= deadline {
                            let _ = child.kill();
                            let _ = child.wait();
                            break;
                        }
                        thread::sleep(SOCKET_POLL_INTERVAL);
                    }
                }
            }
            if thread::panicking() {
                self.dump_stderr_on_failure();
            }
        }
    }
}

/// Build the spawn config and start the daemon. Polls until the
/// socket file exists; panics on timeout with full stderr dump.
fn start_daemon(volume_size_gb: u32) -> DaemonHandle {
    start_daemon_with_handshake_timeout(volume_size_gb, 30)
}

fn start_daemon_with_handshake_timeout(
    volume_size_gb: u32,
    handshake_timeout_s: u64,
) -> DaemonHandle {
    let tempdir = TempDir::new().expect("tempdir");
    let config_path = tempdir.path().join("teslafat.toml");
    let socket_path = tempdir.path().join("teslafat.sock");
    // The daemon walks `backing_root` at startup; point it at a real
    // empty directory inside the tempdir so `SynthBackend::open`
    // succeeds (an empty TeslaCAM volume) instead of erroring on a
    // missing path.
    let backing_root = tempdir.path().join("backing");
    std::fs::create_dir_all(&backing_root).expect("create backing dir");
    let toml = fixture_toml_with_backing(
        &socket_path,
        &backing_root,
        volume_size_gb,
        handshake_timeout_s,
    );
    std::fs::write(&config_path, toml).expect("write config");

    // Cargo sets `CARGO_BIN_EXE_<bin-name>` for integration tests
    // so we can spawn the just-built binary without going through
    // `cargo run` (which would lock the build cache).
    let binary = env!("CARGO_BIN_EXE_teslafat");

    let mut child = Command::new(binary)
        .arg("--config")
        .arg(&config_path)
        // Be explicit so a developer's exported RUST_LOG doesn't
        // turn off the "started" sentinel that we sometimes assert
        // on, and turn on debug for connection lifecycle traces
        // that are invaluable in failure dumps.
        .env("RUST_LOG", "teslafat=debug,info")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn teslafat");

    let stderr_lines = Arc::new(Mutex::new(Vec::<String>::new()));
    // Move stderr into a blocking pump thread. Tokio doesn't own
    // the daemon (it's a subprocess), so we don't need
    // tokio::process — a vanilla OS thread reading the synchronous
    // pipe is the simplest correct shape.
    if let Some(stderr) = child.stderr.take() {
        let sink = Arc::clone(&stderr_lines);
        thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines().map_while(Result::ok) {
                sink.lock().unwrap().push(line);
            }
        });
    }

    let handle = DaemonHandle {
        child: Some(child),
        socket_path: socket_path.clone(),
        stderr_lines,
        _tempdir: tempdir,
    };

    if let Err(e) = wait_for_socket(&handle.socket_path, SOCKET_WAIT_TIMEOUT) {
        handle.dump_stderr_on_failure();
        panic!("daemon did not bind socket: {e}");
    }
    handle
}

/// Poll for the socket file's existence with `SOCKET_POLL_INTERVAL`.
/// Returns `Ok(())` when the file shows up, `Err(string)` on
/// deadline. Does NOT verify it's actually accepting connections —
/// that's the caller's job (via `UnixStream::connect`).
fn wait_for_socket(path: &Path, timeout_dur: Duration) -> Result<(), String> {
    let deadline = Instant::now() + timeout_dur;
    while Instant::now() < deadline {
        if path.exists() {
            return Ok(());
        }
        thread::sleep(SOCKET_POLL_INTERVAL);
    }
    Err(format!(
        "socket {} did not appear within {:?}",
        path.display(),
        timeout_dur
    ))
}

/// Send SIGHUP to a PID via the POSIX `kill(1)` command. This is the
/// live-reload trigger: the daemon re-walks its backing tree and swaps
/// in a fresh synth view once the LUN is quiescent.
fn send_sighup(pid: u32) -> std::io::Result<()> {
    let status = Command::new("kill")
        .arg("-HUP")
        .arg(pid.to_string())
        .status()?;
    if !status.success() {
        return Err(std::io::Error::other(format!(
            "kill -HUP {pid} returned {status:?}"
        )));
    }
    Ok(())
}

/// Poll the daemon's captured stderr until a line contains `needle`
/// or the deadline elapses. Returns the matching line on success.
fn wait_for_stderr_line(
    handle: &DaemonHandle,
    needle: &str,
    timeout_dur: Duration,
) -> Option<String> {
    let deadline = Instant::now() + timeout_dur;
    loop {
        if let Some(line) = handle
            .stderr_snapshot()
            .into_iter()
            .find(|l| l.contains(needle))
        {
            return Some(line);
        }
        if Instant::now() >= deadline {
            return None;
        }
        thread::sleep(SOCKET_POLL_INTERVAL);
    }
}

/// Build a fixture config TOML pointing `backing_root` at a caller-
/// supplied path so a test can mutate the backing tree and trigger a
/// SIGHUP reload.
fn fixture_toml_with_backing(
    socket_path: &Path,
    backing_root: &Path,
    volume_size_gb: u32,
    handshake_timeout_s: u64,
) -> String {
    let socket = socket_path.display();
    let backing = backing_root.display();
    format!(
        "disk_signature = 0x12345678\n\
         \n\
         [nbd]\n\
         socket_path = \"{socket}\"\n\
         handshake_timeout_seconds = {handshake_timeout_s}\n\
         \n\
         [[partition]]\n\
         backing_root = \"{backing}\"\n\
         volume_size_gb = {volume_size_gb}\n\
         volume_label = \"TESLACAM\"\n\
         \n\
         [partition.retention]\n\
         recentclips_hide_after_seconds = 1800\n"
    )
}

/// Start the daemon against a caller-owned `backing_root` (kept alive
/// by the test) so the test can add files and SIGHUP-reload the view.
fn start_daemon_with_backing_root(volume_size_gb: u32, backing_root: &Path) -> DaemonHandle {
    let tempdir = TempDir::new().expect("tempdir");
    let config_path = tempdir.path().join("teslafat.toml");
    let socket_path = tempdir.path().join("teslafat.sock");
    let toml = fixture_toml_with_backing(&socket_path, backing_root, volume_size_gb, 30);
    std::fs::write(&config_path, toml).expect("write config");

    let binary = env!("CARGO_BIN_EXE_teslafat");
    let mut child = Command::new(binary)
        .arg("--config")
        .arg(&config_path)
        .env("RUST_LOG", "teslafat=debug,info")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn teslafat");

    let stderr_lines = Arc::new(Mutex::new(Vec::<String>::new()));
    if let Some(stderr) = child.stderr.take() {
        let sink = Arc::clone(&stderr_lines);
        thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines().map_while(Result::ok) {
                sink.lock().unwrap().push(line);
            }
        });
    }

    let handle = DaemonHandle {
        child: Some(child),
        socket_path: socket_path.clone(),
        stderr_lines,
        _tempdir: tempdir,
    };

    if let Err(e) = wait_for_socket(&handle.socket_path, SOCKET_WAIT_TIMEOUT) {
        handle.dump_stderr_on_failure();
        panic!("daemon did not bind socket: {e}");
    }
    handle
}

/// Send SIGTERM to a PID via the POSIX `kill(1)` command. Zero
/// new dependencies; works on every Unix host we support
/// (Linux + macOS dev box).
fn send_sigterm(pid: u32) -> std::io::Result<()> {
    let status = Command::new("kill")
        .arg("-TERM")
        .arg(pid.to_string())
        .status()?;
    if !status.success() {
        return Err(std::io::Error::other(format!(
            "kill -TERM {pid} returned {status:?}"
        )));
    }
    Ok(())
}

/// Build a current-thread tokio runtime for one smoke test's
/// async body. Matches the daemon's own runtime shape so the
/// client side does not pull in `rt-multi-thread`.
fn smoke_runtime() -> tokio::runtime::Runtime {
    Builder::new_current_thread()
        .enable_io()
        .enable_time()
        .build()
        .expect("build tokio current-thread runtime")
}

/// Drive the legacy `NBD_OPT_EXPORT_NAME` handshake from the
/// client side. Sends `CF_FIXED_NEWSTYLE | CF_NO_ZEROES` so the
/// server replies with the compact 10-byte export reply (no
/// 124-byte legacy zero pad). Returns the advertised export size
/// the server announced — the smoke test uses this to assert the
/// `cfg.volume_size_gb * GiB` plumbing is correct end-to-end.
///
/// Mirrors the helper in `crate::server::tests::drive_client_handshake`
/// but adds `CF_NO_ZEROES` (avoids the 124-byte pad) and parses the
/// 10-byte reply into the advertised size. Re-implementing it
/// here (rather than re-exporting) keeps the cross-platform test
/// helper isolated to its single user inside the production
/// crate.
async fn client_handshake_export_name(stream: &mut UnixStream) -> u64 {
    let mut greeting = [0u8; GREETING_LEN];
    timeout(NBD_IO_TIMEOUT, stream.read_exact(&mut greeting))
        .await
        .expect("read greeting (timeout)")
        .expect("read greeting (i/o)");

    let client_flags = CF_FIXED_NEWSTYLE | CF_NO_ZEROES;
    timeout(
        NBD_IO_TIMEOUT,
        stream.write_all(&client_flags.to_be_bytes()),
    )
    .await
    .expect("write client_flags (timeout)")
    .expect("write client_flags (i/o)");
    timeout(NBD_IO_TIMEOUT, stream.write_all(&IHAVEOPT.to_be_bytes()))
        .await
        .expect("write IHAVEOPT (timeout)")
        .expect("write IHAVEOPT (i/o)");
    timeout(
        NBD_IO_TIMEOUT,
        stream.write_all(&NBD_OPT_EXPORT_NAME.to_be_bytes()),
    )
    .await
    .expect("write NBD_OPT_EXPORT_NAME (timeout)")
    .expect("write NBD_OPT_EXPORT_NAME (i/o)");
    // 0-byte export name -> default export.
    timeout(NBD_IO_TIMEOUT, stream.write_all(&0u32.to_be_bytes()))
        .await
        .expect("write name_len (timeout)")
        .expect("write name_len (i/o)");
    timeout(NBD_IO_TIMEOUT, stream.flush())
        .await
        .expect("flush (timeout)")
        .expect("flush (i/o)");

    // 10-byte reply: u64 export size + u16 transmission flags.
    // We chose CF_NO_ZEROES above so the legacy 124-byte pad is
    // not sent.
    let mut reply = [0u8; EXPORT_REPLY_NO_ZEROES_LEN];
    timeout(NBD_IO_TIMEOUT, stream.read_exact(&mut reply))
        .await
        .expect("read export reply (timeout)")
        .expect("read export reply (i/o)");

    let mut size_bytes = [0u8; 8];
    size_bytes.copy_from_slice(&reply[0..8]);
    u64::from_be_bytes(size_bytes)
}

/// Issue an `NBD_CMD_READ` for `length` bytes starting at
/// `offset`, return the payload bytes the server replies with.
/// Asserts the simple-reply header has the right magic, the
/// expected `handle`, and `error == NBD_EOK`. The test calls this
/// as the primary "did the transmission loop and `SynthBackend`
/// agree?" assertion.
async fn client_read(stream: &mut UnixStream, offset: u64, length: u32, handle: u64) -> Vec<u8> {
    let req = encode_request_header(&RequestHeader {
        flags: 0,
        kind: NBD_CMD_READ,
        handle,
        offset,
        length,
    });
    timeout(NBD_IO_TIMEOUT, stream.write_all(&req))
        .await
        .expect("write READ header (timeout)")
        .expect("write READ header (i/o)");
    timeout(NBD_IO_TIMEOUT, stream.flush())
        .await
        .expect("flush READ (timeout)")
        .expect("flush READ (i/o)");

    let mut reply_header = [0u8; SIMPLE_REPLY_HEADER_LEN];
    timeout(NBD_IO_TIMEOUT, stream.read_exact(&mut reply_header))
        .await
        .expect("read simple-reply header (timeout)")
        .expect("read simple-reply header (i/o)");
    let magic = u32::from_be_bytes([
        reply_header[0],
        reply_header[1],
        reply_header[2],
        reply_header[3],
    ]);
    let error = u32::from_be_bytes([
        reply_header[4],
        reply_header[5],
        reply_header[6],
        reply_header[7],
    ]);
    let got_handle = u64::from_be_bytes([
        reply_header[8],
        reply_header[9],
        reply_header[10],
        reply_header[11],
        reply_header[12],
        reply_header[13],
        reply_header[14],
        reply_header[15],
    ]);
    assert_eq!(
        magic, NBD_SIMPLE_REPLY_MAGIC,
        "simple-reply magic mismatch (got 0x{magic:08x})"
    );
    assert_eq!(error, NBD_EOK, "READ returned NBD error 0x{error:08x}");
    assert_eq!(
        got_handle, handle,
        "reply handle 0x{got_handle:x} != request handle 0x{handle:x}"
    );

    let mut payload = vec![0u8; length as usize];
    timeout(NBD_IO_TIMEOUT, stream.read_exact(&mut payload))
        .await
        .expect("read READ payload (timeout)")
        .expect("read READ payload (i/o)");
    payload
}

/// Issue an `NBD_CMD_DISC`. Per spec the server does NOT reply;
/// the function returns as soon as the request header is flushed.
async fn client_disc(stream: &mut UnixStream, handle: u64) {
    let req = encode_request_header(&RequestHeader {
        flags: 0,
        kind: NBD_CMD_DISC,
        handle,
        offset: 0,
        length: 0,
    });
    timeout(NBD_IO_TIMEOUT, stream.write_all(&req))
        .await
        .expect("write DISC header (timeout)")
        .expect("write DISC header (i/o)");
    timeout(NBD_IO_TIMEOUT, stream.flush())
        .await
        .expect("flush DISC (timeout)")
        .expect("flush DISC (i/o)");
}

/// Connect to the daemon's Unix socket with bounded retries. The
/// socket file existing (per `wait_for_socket`) is necessary but
/// not sufficient — there's a microsecond window between
/// `UnixListener::bind` returning and `accept()` actually being
/// reachable. The retry loop closes that race without polling
/// indefinitely.
async fn connect_with_retry(path: &Path) -> UnixStream {
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut last_err = None;
    while Instant::now() < deadline {
        match UnixStream::connect(path).await {
            Ok(stream) => return stream,
            Err(e) => {
                last_err = Some(e);
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        }
    }
    panic!(
        "UnixStream::connect({}) never succeeded: {:?}",
        path.display(),
        last_err
    );
}

// ============================================================
// Tests
// ============================================================

/// Happy path. The daemon serves a single client through the full
/// pipeline: handshake -> READ 4 KiB -> DISC -> SIGTERM ->
/// clean exit. Verifies the `SynthBackend` wires through the
/// transmission loop and the `server::serve` accept loop, and that
/// the daemon's signal handler closes the run cleanly.
///
/// The READ targets the first byte of partition 1 (one 1 MiB
/// alignment unit in, past the MBR/gap), which for the synthesized
/// FAT32 volume is the boot sector: we assert the fixed `EB 58 90`
/// jump, the `MSWIN4.1` OEM tag, the `FAT32   ` filesystem tag, and
/// the `55 AA` end signature all arrive byte-exact over the NBD wire
/// — proving the partitioned synth view (not a zero placeholder) is
/// being served.
#[test]
fn daemon_serves_fat_boot_sector_via_nbd_handshake_and_read() {
    let handle = start_daemon(4);

    let rt = smoke_runtime();
    let payload = rt.block_on(async {
        let mut stream = connect_with_retry(handle.socket_path()).await;
        let advertised = client_handshake_export_name(&mut stream).await;
        let expected = expected_disk_size(4);
        assert_eq!(
            advertised, expected,
            "advertised export size {advertised} != expected {expected}",
        );
        let payload = client_read(&mut stream, MBR_GAP_BYTES, 4096, 0xC0FF_EE00_DEAD_BEEFu64).await;
        client_disc(&mut stream, 0xC0FF_EE00_DEAD_BEF0u64).await;
        // Give the server a moment to recognise DISC and drop the
        // connection before we SIGTERM. Not strictly required —
        // SIGTERM is honoured even mid-transmission — but it makes
        // the failure dump easier to read on regression.
        tokio::time::sleep(Duration::from_millis(50)).await;
        drop(stream);
        payload
    });

    assert_eq!(payload.len(), 4096, "READ payload length");
    assert_eq!(
        &payload[0x00..0x03],
        &[0xEB, 0x58, 0x90],
        "boot sector jump instruction mismatch",
    );
    assert_eq!(
        &payload[0x03..0x0B],
        b"MSWIN4.1",
        "boot sector OEM name mismatch",
    );
    assert_eq!(
        &payload[0x52..0x5A],
        b"FAT32   ",
        "boot sector filesystem-type tag mismatch",
    );
    assert_eq!(
        &payload[0x1FE..0x200],
        &[0x55, 0xAA],
        "boot sector end signature mismatch",
    );

    let status = handle.sigterm_and_wait();
    assert!(
        status.success(),
        "daemon exited with non-success status {status:?}",
    );
}

/// SIGTERM exits cleanly even when no client has ever connected.
/// Verifies the signal-handling future is wired into the accept
/// loop and not gated on at-least-one-connection.
#[test]
fn daemon_exits_cleanly_on_sigterm_with_no_clients() {
    let handle = start_daemon(4);
    let socket_path = handle.socket_path().to_path_buf();

    let status = handle.sigterm_and_wait();
    assert!(
        status.success(),
        "daemon exited with non-success status {status:?}",
    );

    // Best-effort socket cleanup is part of the clean-shutdown
    // contract in `main::unix_serve::serve_until_signal`. The path
    // should be gone after a SIGTERM exit. (We accept a small
    // grace because the cleanup happens after the runtime
    // returns; the OS may not flush the directory entry before
    // our test reads the dir.)
    let deadline = Instant::now() + Duration::from_secs(2);
    while socket_path.exists() && Instant::now() < deadline {
        thread::sleep(SOCKET_POLL_INTERVAL);
    }
    assert!(
        !socket_path.exists(),
        "socket file {} still exists after clean shutdown",
        socket_path.display(),
    );
}

/// ADR-0006 §B enforcement in the binary, not just the unit test.
/// Connect, send a handshake reply that omits `CF_FIXED_NEWSTYLE` so
/// the server's handshake returns `Err`; the daemon must log a
/// `warn!` and stay alive. A second client then connects, completes
/// a real handshake + READ + DISC, and the daemon SIGTERMs out
/// cleanly. If the daemon-exit-on-client-error regression returns,
/// either the second connection's `connect_with_retry` or the
/// `sigterm_and_wait` call will fail.
#[test]
fn daemon_recovers_from_bad_handshake_and_accepts_next_client() {
    let handle = start_daemon(4);

    let rt = smoke_runtime();
    rt.block_on(async {
        // First client: deliberately bad handshake (0 client flags).
        // After the server rejects the handshake the daemon must
        // keep the accept loop running.
        {
            let mut bad = connect_with_retry(handle.socket_path()).await;
            let mut greeting = [0u8; GREETING_LEN];
            timeout(NBD_IO_TIMEOUT, bad.read_exact(&mut greeting))
                .await
                .expect("bad client: greeting read (timeout)")
                .expect("bad client: greeting read");
            // CF_FIXED_NEWSTYLE missing -> handshake bails.
            timeout(NBD_IO_TIMEOUT, bad.write_all(&0u32.to_be_bytes()))
                .await
                .expect("bad client: client_flags write (timeout)")
                .expect("bad client: client_flags write");
            timeout(NBD_IO_TIMEOUT, bad.flush())
                .await
                .expect("bad client: flush (timeout)")
                .expect("bad client: flush");
            // Wait for the server to drop us; an EOF on read is the
            // signal that the server closed the connection.
            let mut probe = [0u8; 16];
            let _ = timeout(NBD_IO_TIMEOUT, bad.read(&mut probe)).await;
            drop(bad);
        }

        // Second client: real handshake, real READ, real DISC.
        // This is the assertion that the daemon survived.
        {
            let mut good = connect_with_retry(handle.socket_path()).await;
            let advertised = client_handshake_export_name(&mut good).await;
            assert_eq!(advertised, expected_disk_size(4));
            let payload = client_read(&mut good, 0, 512, 0xBADD_F00D_BADD_F00Du64).await;
            assert_eq!(
                &payload[0x1FE..0x200],
                &[0x55, 0xAA],
                "second-client READ: MBR boot signature",
            );
            client_disc(&mut good, 0xBADD_F00D_BADD_F00Eu64).await;
        }
    });

    let status = handle.sigterm_and_wait();
    assert!(
        status.success(),
        "daemon exited with non-success status {status:?}",
    );
}

/// `cfg.volume_size_gb` actually flows through main -> `SynthBackend`
/// -> handshake export-name reply -> wire. Uses a non-default size
/// to rule out "the daemon ignored config and used a constant".
#[test]
fn daemon_advertises_configured_volume_size_in_handshake() {
    const VOL_GIB: u32 = 17;
    let handle = start_daemon(VOL_GIB);

    let rt = smoke_runtime();
    let advertised = rt.block_on(async {
        let mut stream = connect_with_retry(handle.socket_path()).await;
        let size = client_handshake_export_name(&mut stream).await;
        client_disc(&mut stream, 0).await;
        size
    });

    assert_eq!(
        advertised,
        expected_disk_size(VOL_GIB),
        "wire-advertised size != disk (MBR gap + volume_size_gb * 2^30)",
    );

    let status = handle.sigterm_and_wait();
    assert!(status.success(), "daemon exit status {status:?}");
}

/// The "started" sentinel tracing line that Phase 1.1 promises and
/// that `tests/sentinel.rs` covers in --check-config mode is also
/// emitted in the live-serve path. The smoke harness uses
/// socket-presence polling for liveness (more reliable than line
/// matching), but we still want to know the sentinel is on the
/// wire in serve mode so operators have a single grep-target for
/// "did the daemon start?".
#[test]
fn daemon_emits_started_sentinel_in_serve_mode() {
    let handle = start_daemon(4);
    // Give the pump thread a chance to flush the startup lines.
    // The "started" message is emitted before `serve` blocks, but
    // `BufReader::lines()` only returns complete lines so we may
    // race with the newline byte.
    thread::sleep(Duration::from_millis(200));

    let lines = handle.stderr_snapshot();
    let saw_sentinel = lines.iter().any(|l| l.contains(r#""message":"started""#));
    if !saw_sentinel {
        handle.dump_stderr_on_failure();
    }
    assert!(
        saw_sentinel,
        "did not see the \"started\" sentinel in daemon stderr",
    );

    let status = handle.sigterm_and_wait();
    assert!(status.success(), "daemon exit status {status:?}");
}

/// Cleanly exercising the `SOCKET_WAIT_TIMEOUT` path is hard without
/// either flakiness or a fake daemon, so we accept that the
/// "happy" tests above already exercise the polling logic via
/// their own setup. This guard tests instead that
/// `start_daemon_with_handshake_timeout`'s plumbing of the config
/// override actually reaches the daemon: pass a recognisable
/// handshake timeout value and verify it shows up in the
/// `"started"` log line's `nbd_handshake_timeout_s` field. If a
/// future refactor stops propagating the config to the sentinel,
/// this catches it.
#[test]
fn daemon_handshake_timeout_config_value_reaches_sentinel() {
    let handle = start_daemon_with_handshake_timeout(4, 47);
    thread::sleep(Duration::from_millis(200));

    let lines = handle.stderr_snapshot();
    let saw_field = lines
        .iter()
        .any(|l| l.contains(r#""nbd_handshake_timeout_s":47"#));
    if !saw_field {
        handle.dump_stderr_on_failure();
    }
    assert!(
        saw_field,
        "did not see nbd_handshake_timeout_s=47 in the sentinel line",
    );

    let status = handle.sigterm_and_wait();
    assert!(status.success(), "daemon exit status {status:?}");
}

/// SIGHUP triggers a live re-walk of the backing tree and a
/// quiescence-gated swap of the served synth view, without restarting
/// the daemon and without changing the advertised export size (the NBD
/// size contract is fixed for the life of the connection/device).
///
/// This proves the Phase-1 live-reload wiring end-to-end through the
/// real binary: the SIGHUP handler is installed alongside SIGTERM/SIGINT,
/// the rebuild runs off the request path, and the quiescent swap
/// applies. A file added to the backing tree between start and SIGHUP
/// is picked up by the re-walk (observable via the rebuilt
/// `file_count` in the log), and the daemon keeps serving + still
/// exits cleanly on SIGTERM afterwards.
#[test]
fn daemon_reloads_backing_tree_on_sighup() {
    const VOL_GIB: u32 = 4;

    let backing = TempDir::new().expect("backing tempdir");
    std::fs::write(backing.path().join("first.txt"), b"hello").expect("seed file");

    let handle = start_daemon_with_backing_root(VOL_GIB, backing.path());

    // Record the export size before the reload.
    let rt = smoke_runtime();
    let size_before = rt.block_on(async {
        let mut stream = connect_with_retry(handle.socket_path()).await;
        let size = client_handshake_export_name(&mut stream).await;
        client_disc(&mut stream, 0).await;
        size
    });
    assert_eq!(
        size_before,
        expected_disk_size(VOL_GIB),
        "pre-reload export size != disk (MBR gap + volume_size_gb * 2^30)",
    );

    // Mutate the backing tree, then trigger the live reload.
    std::fs::write(backing.path().join("second.txt"), b"world").expect("add file");
    send_sighup(handle.pid()).expect("kill -HUP <pid>");

    // The daemon should re-walk, rebuild, and swap in the new view.
    let rebuilt = wait_for_stderr_line(
        &handle,
        "rebuilt synth view from backing tree",
        Duration::from_secs(10),
    );
    if rebuilt.is_none() {
        handle.dump_stderr_on_failure();
    }
    assert!(
        rebuilt.is_some(),
        "daemon did not log a rebuild after SIGHUP"
    );

    let swapped = wait_for_stderr_line(
        &handle,
        "reload swap applied; new synth view is live",
        Duration::from_secs(10),
    );
    if swapped.is_none() {
        handle.dump_stderr_on_failure();
    }
    assert!(
        swapped.is_some(),
        "daemon did not apply the quiescent reload swap after SIGHUP",
    );

    // The export size is a fixed contract: it must be identical after
    // the reload (it is derived from the config geometry, not the file
    // set), or the kernel nbd-client / USB host would see the device
    // size change underneath it.
    let size_after = rt.block_on(async {
        let mut stream = connect_with_retry(handle.socket_path()).await;
        let size = client_handshake_export_name(&mut stream).await;
        client_disc(&mut stream, 0).await;
        size
    });
    assert_eq!(
        size_after, size_before,
        "export size changed across a SIGHUP reload",
    );

    let status = handle.sigterm_and_wait();
    assert!(status.success(), "daemon exit status {status:?}");
}
