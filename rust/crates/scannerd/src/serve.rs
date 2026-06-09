//! `scannerd serve` — the producer daemon endpoint of the
//! `scannerd → indexd` seam.
//!
//! Binds a Unix domain socket and answers [`Request::Scan`] requests by
//! running one [`produce`](scannerd::produce::produce) pass over the
//! read-only backing image and streaming back a [`ScanBatch`] of facts.
//! `scannerd` is the **least-privilege** process: it holds only the
//! read-only image fd and an ephemeral in-memory [`StabilityTracker`]; it
//! owns no database. A weaponized clip can at worst crash/OOM this
//! disposable daemon (systemd restarts it) — it can never reach the
//! DB-owning `indexd` process, which only ever sees typed, capped,
//! validated JSON.
//!
//! ## Connection model
//!
//! `indexd` is the sole client and **drives** the cadence: it holds one
//! persistent connection and sends a `Scan` request per pass. The
//! `StabilityTracker` lives for the **process lifetime** and is **never
//! reset on connect**, so `stable_scans` / `held_secs` accumulate across
//! requests (a reset would zero the quiescence window and nothing would
//! ever emit). Connections are served one at a time — there is only ever
//! one legitimate peer — which also makes the shared tracker borrow
//! trivially safe without a lock.
//!
//! ## Security
//!
//! Access is gated by **filesystem permissions**, matching the `gadgetd`
//! control-socket precedent in this workspace: the socket is created
//! `0o660` inside a `0o750` runtime directory, both owned by the
//! `teslausb` user/group, so only that user (the `indexd` peer) and group
//! can connect. Tighter exact-UID `SO_PEERCRED` authorization is desirable
//! but is **not reachable in safe stable Rust on this toolchain**
//! (`std::os::unix::net::UnixStream::peer_cred` is unstable, and the
//! workspace denies `unsafe_code`, so a raw `getsockopt(SO_PEERCRED)` /
//! `geteuid` is off-limits without pulling in an `unsafe` wrapper crate).
//! The filesystem posture matches every other local socket in the system
//! (including `gadgetd`'s far more dangerous mutate socket); exact-UID
//! peer-cred is recorded as a follow-up in the ADR. The reader is opened
//! read-only and never mounts, so serving can never disturb the car's
//! write path (the #1 invariant).

use std::io;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::process::ExitCode;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use scannerd::produce::{DEFAULT_SEI_SAMPLE_RATE, produce};
use scannerd::proto::{Request, read_request, write_batch};
use scannerd::stability::{StabilityConfig, StabilityTracker};

use crate::io::PreadReader;

/// Default control-socket path (matches the `gadgetd` runtime layout).
const DEFAULT_SOCKET: &str = "/run/teslausb/scannerd.sock";
/// Write timeout so a stuck client cannot pin the serve loop forever.
/// There is deliberately **no read timeout**: between passes the client
/// legitimately idles (the 30 s cadence is client-owned), and the server
/// blocks waiting for the next request.
const WRITE_TIMEOUT: Duration = Duration::from_secs(30);

/// Parse `scannerd serve <image> [--socket <path>] [--sample-rate <n>]`
/// and run the daemon.
pub fn run_serve(args: &[String]) -> ExitCode {
    let Some(image) = args.get(2) else {
        eprintln!("usage: scannerd serve <image-path> [--socket <path>] [--sample-rate <n>]");
        return ExitCode::FAILURE;
    };
    let socket = arg_value(args, "--socket").unwrap_or_else(|| DEFAULT_SOCKET.to_owned());
    let sample_rate = arg_value(args, "--sample-rate")
        .and_then(|s| s.parse::<u32>().ok())
        .unwrap_or(DEFAULT_SEI_SAMPLE_RATE);

    let reader = match PreadReader::open(Path::new(image)) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("scannerd serve: cannot open {image}: {e}");
            return ExitCode::FAILURE;
        }
    };

    match serve(&reader, Path::new(&socket), sample_rate) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("scannerd serve: fatal: {e}");
            ExitCode::FAILURE
        }
    }
}

/// Find the value following a `--flag` in the argument list.
fn arg_value(args: &[String], flag: &str) -> Option<String> {
    args.iter()
        .position(|a| a == flag)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

/// Wall-clock epoch seconds for the quiescence window.
fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Bind the socket and serve until the listener errors. The tracker is
/// created once here so it persists across every connection for the
/// daemon's lifetime.
fn serve(reader: &PreadReader, socket_path: &Path, sample_rate: u32) -> io::Result<()> {
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
        // Restrict the runtime dir so only the owner/group can traverse to
        // the socket (defense in depth alongside the socket's own mode).
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o750))?;
    }
    // A stale socket from a prior run makes bind fail with EADDRINUSE.
    match std::fs::remove_file(socket_path) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    let listener = UnixListener::bind(socket_path)?;
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o660))?;

    let mut tracker = StabilityTracker::new(StabilityConfig::default());

    println!(
        "scannerd serve: listening on {} (sample_rate {sample_rate})",
        socket_path.display()
    );

    for conn in listener.incoming() {
        match conn {
            Ok(stream) => {
                if let Err(e) = handle_conn(stream, reader, &mut tracker, sample_rate) {
                    eprintln!("scannerd serve: connection ended: {e}");
                }
            }
            Err(e) => eprintln!("scannerd serve: accept error: {e}"),
        }
    }
    Ok(())
}

/// Serve one persistent connection: answer every request on it until the
/// client disconnects. Authorization is by filesystem permission on the
/// socket (see the module docs); there is no in-band auth handshake.
fn handle_conn(
    mut stream: UnixStream,
    reader: &PreadReader,
    tracker: &mut StabilityTracker,
    sample_rate: u32,
) -> io::Result<()> {
    stream.set_write_timeout(Some(WRITE_TIMEOUT))?;

    loop {
        let request = match read_request(&mut stream) {
            Ok(r) => r,
            // A clean client disconnect surfaces as EOF on the next read.
            Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Ok(()),
            Err(e) => return Err(e),
        };
        match request {
            Request::Scan { generation, resync } => {
                if resync {
                    tracker.arm_resync();
                }
                let mut batch = produce(reader, tracker, now_secs(), sample_rate)
                    .map_err(|e| io::Error::other(format!("produce failed: {e}")))?;
                batch.generation = generation;
                write_batch(&mut stream, &batch)?;
            }
        }
    }
}
