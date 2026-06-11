//! Control-plane IPC for `gadgetd serve`: a length-prefixed JSON protocol over a
//! Unix domain socket. The daemon brings nothing up itself — gadget bring-up is
//! owned by a separate, earlier systemd unit — so a socket-bind or serve-loop
//! failure here can never disturb the car-facing LUN. On start it performs
//! **handoff crash-recovery** (clean stale loops/mounts, then re-present an
//! interrupted eject) and then serves:
//!
//! - `gadget_status` — read-only; answerable concurrently with an in-flight
//!   handoff (each connection is its own thread).
//! - `request_mutation` — serialized via a `try_lock`; a second concurrent
//!   request is refused, never queued.
//! - `handoff_status` — last/current handoff record.

use std::io::{self, Read, Write};
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::json;

use crate::config::{self, GadgetConfig};
use crate::exec::{self, LiveLun, MtimeSaveGuard};
use crate::handoff::{self, HandoffOutcome, ImageMutator, LunControl, Mutation, Partition};
use crate::mutate::LoopMutator;

/// Maximum accepted frame size (a handful of small JSON fields).
const MAX_FRAME: u32 = 1 << 20;
/// Per-connection I/O timeout so a slow/stuck client can't pin a thread.
const CONN_TIMEOUT: Duration = Duration::from_secs(15);
/// mtime sampling interval for the save-active guard.
const SAVE_SAMPLE: Duration = Duration::from_millis(500);
/// How often the serve loop re-checks that its control socket path still exists.
/// A peer unit sharing the runtime dir can unlink it (the reason both units now
/// set `RuntimeDirectoryPreserve=yes`); if it ever vanishes anyway we re-bind so
/// the write path heals itself instead of "listening into the void".
const SOCKET_HEALTH_INTERVAL: Duration = Duration::from_secs(2);
/// Backoff between non-blocking accept polls when no connection is pending. Adds
/// at most this much latency to picking up a new handoff request — negligible
/// against a multi-second handoff, and the cost of self-healing the socket.
const ACCEPT_POLL: Duration = Duration::from_millis(100);

/// A wire request (`cmd`-tagged JSON).
#[derive(Debug, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum Request {
    /// Read-only gadget + handoff status.
    GadgetStatus,
    /// Request a serialized mutation handoff.
    #[serde(rename = "request_mutation")]
    Mutate {
        /// Target partition (1 = `TeslaCam`, 2 = media).
        partition: u8,
        /// The validated mutation to apply.
        mutation: Mutation,
    },
    /// Poll the record for a prior handoff id.
    HandoffStatus {
        /// The id returned by a previous `request_mutation`.
        handoff_id: String,
    },
}

/// The last (or in-flight) handoff, for status reporting.
#[derive(Debug, Clone, Default)]
struct HandoffRecord {
    id: String,
    phase: String,
    result: Option<String>,
    detail: Option<String>,
}

/// Shared daemon state.
struct ServeState {
    cfg: GadgetConfig,
    runtime_root: PathBuf,
    allow_hot: bool,
    record: Mutex<Option<HandoffRecord>>,
    handoff_lock: Mutex<()>,
    next_id: AtomicU64,
}

/// Read a length-prefixed frame (4-byte LE length, then the payload).
fn read_frame(stream: &mut impl Read, cap: u32) -> io::Result<Vec<u8>> {
    let mut len_buf = [0u8; 4];
    stream.read_exact(&mut len_buf)?;
    let len = u32::from_le_bytes(len_buf);
    if len > cap {
        return Err(io::Error::other(format!("frame too large: {len} > {cap}")));
    }
    let mut payload = vec![0u8; len as usize];
    stream.read_exact(&mut payload)?;
    Ok(payload)
}

/// Write a length-prefixed frame.
fn write_frame(stream: &mut impl Write, payload: &[u8]) -> io::Result<()> {
    let len = u32::try_from(payload.len())
        .map_err(|_| io::Error::other("response exceeds u32 length"))?;
    stream.write_all(&len.to_le_bytes())?;
    stream.write_all(payload)?;
    stream.flush()
}

/// Bind (or re-bind) the control socket: clear any stale file, bind, tighten
/// perms to 0o660, log. Factored out so the serve loop can re-bind if the socket
/// path is later unlinked out from under us.
fn bind_listener(socket_path: &Path) -> io::Result<UnixListener> {
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    // A stale socket file from a prior run would make bind fail with EADDRINUSE.
    match std::fs::remove_file(socket_path) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    let listener = UnixListener::bind(socket_path)?;
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o660))?;
    println!("gadgetd serve: listening on {}", socket_path.display());
    Ok(listener)
}

/// True iff the control socket path still exists AND is a socket. A peer service
/// sharing the runtime dir can unlink it on stop; the kernel keeps our existing
/// listener fd alive (so `ss` still shows us `LISTENing`) but new clients get
/// ENOENT on connect. Detecting the vanished path lets us re-bind.
fn socket_path_healthy(socket_path: &Path) -> bool {
    std::fs::symlink_metadata(socket_path)
        .map(|m| m.file_type().is_socket())
        .unwrap_or(false)
}

/// Bring up the control socket and serve until the listener errors.
///
/// # Errors
/// Returns an error only for fatal listener/bind problems; per-connection errors
/// are logged and do not stop the daemon.
pub(crate) fn serve(
    cfg: GadgetConfig,
    runtime_root: PathBuf,
    socket_path: &Path,
    allow_hot: bool,
) -> io::Result<()> {
    // Handoff crash-recovery BEFORE serving (never touches gadget bring-up).
    recover_interrupted_handoff(&cfg, &runtime_root);

    let mut listener = bind_listener(socket_path)?;
    listener.set_nonblocking(true)?;

    let state = Arc::new(ServeState {
        cfg,
        runtime_root,
        allow_hot,
        record: Mutex::new(None),
        handoff_lock: Mutex::new(()),
        next_id: AtomicU64::new(1),
    });

    let mut last_health = Instant::now();
    loop {
        match listener.accept() {
            Ok((stream, _addr)) => {
                // handle_conn relies on SO_RCVTIMEO/SO_SNDTIMEO, not non-blocking
                // semantics, so the accept'd stream must be blocking.
                stream.set_nonblocking(false)?;
                let st = Arc::clone(&state);
                std::thread::spawn(move || {
                    if let Err(e) = handle_conn(&stream, &st) {
                        eprintln!("gadgetd serve: connection error: {e}");
                    }
                });
            }
            Err(ref e) if e.kind() == io::ErrorKind::WouldBlock => {
                std::thread::sleep(ACCEPT_POLL);
            }
            Err(e) => eprintln!("gadgetd serve: accept error: {e}"),
        }

        // Self-heal: if our socket path was unlinked (e.g. a peer unit sharing
        // the runtime dir tore it down), re-bind so the write path recovers
        // instead of silently listening on an orphaned inode.
        if last_health.elapsed() >= SOCKET_HEALTH_INTERVAL {
            last_health = Instant::now();
            if !socket_path_healthy(socket_path) {
                eprintln!(
                    "gadgetd serve: control socket {} vanished; re-binding",
                    socket_path.display()
                );
                match bind_listener(socket_path) {
                    Ok(l) => {
                        l.set_nonblocking(true)?;
                        listener = l;
                    }
                    Err(e) => eprintln!("gadgetd serve: re-bind failed: {e}"),
                }
            }
        }
    }
}

/// On startup, reconcile an interrupted handoff on BOTH LUNs independently. Each
/// LUN backs its own single-partition image, so recovery is per-LUN: clear any
/// stale loop device / mount left on THAT LUN's image, and only then — if the
/// gadget is bound but that LUN is empty — re-present THAT LUN's own image. The
/// `TeslaCam` LUN (`lun.0`) is recovered first (it is sacred — the car-facing
/// dashcam drive must never stay ejected) and an image is only ever re-presented
/// onto its own LUN (never cross-wired). Never re-present while the image might
/// still be held locally (the never-double-writer precedence).
fn recover_interrupted_handoff(cfg: &GadgetConfig, runtime_root: &Path) {
    for lun in [config::TESLACAM_LUN, config::MEDIA_LUN] {
        recover_lun(cfg, runtime_root, lun);
    }
}

fn recover_lun(cfg: &GadgetConfig, runtime_root: &Path, lun_index: u8) {
    let image = cfg.image_for_lun(lun_index);
    let lun = LiveLun::for_lun(cfg.clone(), lun_index);
    let mutator = LoopMutator::new(image.to_path_buf(), runtime_root.to_path_buf());

    let cleanup = mutator.cleanup_stale();
    if let Err(e) = &cleanup {
        eprintln!(
            "gadgetd serve: stale-mount cleanup failed during recovery (lun.{lun_index}): {e}"
        );
    }

    match (lun.is_bound(), lun.lun_is_empty()) {
        (Ok(true), Ok(true)) => {
            if cleanup.is_ok() {
                match lun.represent() {
                    Ok(()) => {
                        println!(
                            "gadgetd serve: recovered interrupted handoff (re-presented lun.{lun_index})"
                        );
                    }
                    Err(e) => eprintln!(
                        "gadgetd serve: CRITICAL recovery re-present failed (lun.{lun_index}): {e}"
                    ),
                }
            } else {
                eprintln!(
                    "gadgetd serve: CRITICAL: lun.{lun_index} ejected but stale mounts remain; \
                     NOT re-presenting to avoid a double-writer — manual recovery needed"
                );
            }
        }
        (Ok(_), Ok(_)) => {}
        (b, f) => eprintln!(
            "gadgetd serve: recovery state read failed (lun.{lun_index}, bound={b:?}, empty={f:?})"
        ),
    }
}

fn handle_conn(stream: &UnixStream, state: &ServeState) -> io::Result<()> {
    stream.set_read_timeout(Some(CONN_TIMEOUT))?;
    stream.set_write_timeout(Some(CONN_TIMEOUT))?;
    let mut reader = stream;
    let payload = read_frame(&mut reader, MAX_FRAME)?;

    let response = match serde_json::from_slice::<Request>(&payload) {
        Ok(req) => dispatch(req, state),
        Err(e) => json!({ "error": format!("bad request: {e}") }),
    };
    let bytes = serde_json::to_vec(&response).map_err(io::Error::other)?;
    let mut writer = stream;
    write_frame(&mut writer, &bytes)
}

fn dispatch(req: Request, state: &ServeState) -> serde_json::Value {
    match req {
        Request::GadgetStatus => gadget_status(state),
        Request::Mutate {
            partition,
            mutation,
        } => request_mutation(state, partition, &mutation),
        Request::HandoffStatus { handoff_id } => handoff_status(state, &handoff_id),
    }
}

fn gadget_status(state: &ServeState) -> serde_json::Value {
    let status = exec::read_status(&state.cfg);
    let record = state.record.lock().ok().and_then(|r| r.clone());
    let handoff_active = state.handoff_lock.try_lock().is_err();
    json!({
        "present": status.present,
        "bound": status.bound_udc.is_some(),
        "bound_udc": status.bound_udc,
        "udc_state": status.udc_state,
        "lun_file": status.lun_file,
        "media_lun_file": status.media_lun_file,
        "handoff_active": handoff_active,
        "last_result": record.as_ref().and_then(|r| r.result.clone()),
        "last_handoff_id": record.as_ref().map(|r| r.id.clone()),
    })
}

fn handoff_status(state: &ServeState, handoff_id: &str) -> serde_json::Value {
    match state.record.lock().ok().and_then(|r| r.clone()) {
        Some(rec) if rec.id == handoff_id => json!({
            "handoff_id": rec.id,
            "phase": rec.phase,
            "result": rec.result,
            "detail": rec.detail,
        }),
        _ => json!({ "error": format!("unknown handoff_id: {handoff_id}") }),
    }
}

fn request_mutation(state: &ServeState, partition: u8, mutation: &Mutation) -> serde_json::Value {
    let partition = match Partition::from_u8(partition) {
        Ok(p) => p,
        Err(e) => return json!({ "refused": e }),
    };
    if let Err(e) = mutation.validate() {
        return json!({ "refused": format!("invalid mutation: {e}") });
    }

    // Serialize: a second handoff is refused, never queued (spec §4).
    let Ok(_guard) = state.handoff_lock.try_lock() else {
        return json!({ "refused": "handoff_active" });
    };

    let id = format!("h-{}", state.next_id.fetch_add(1, Ordering::SeqCst));
    set_record(
        state,
        HandoffRecord {
            id: id.clone(),
            phase: "queued".to_owned(),
            ..Default::default()
        },
    );

    let lun_index = partition.lun_index();
    let image = state.cfg.image_for_lun(lun_index).to_path_buf();
    let lun = LiveLun::for_lun(state.cfg.clone(), lun_index);
    let guard = MtimeSaveGuard::new(image.clone(), SAVE_SAMPLE);
    let mutator = LoopMutator::new(image, state.runtime_root.clone());

    let outcome = handoff::run_handoff(
        &lun,
        &guard,
        &mutator,
        partition,
        mutation,
        state.allow_hot,
        |phase| update_phase(state, phase.as_str()),
    );

    finalize_record(state, &id, &outcome);
    match &outcome {
        HandoffOutcome::Done => json!({ "handoff_id": id, "result": "done" }),
        HandoffOutcome::Refused(r) => json!({ "handoff_id": id, "refused": r }),
        HandoffOutcome::Failed(d) => json!({ "handoff_id": id, "result": "failed", "detail": d }),
        HandoffOutcome::CriticalFault(d) => {
            json!({ "handoff_id": id, "result": "critical_fault", "detail": d })
        }
    }
}

fn set_record(state: &ServeState, rec: HandoffRecord) {
    if let Ok(mut r) = state.record.lock() {
        *r = Some(rec);
    }
}

fn update_phase(state: &ServeState, phase: &str) {
    if let Ok(mut r) = state.record.lock() {
        if let Some(rec) = r.as_mut() {
            phase.clone_into(&mut rec.phase);
        }
    }
}

fn finalize_record(state: &ServeState, id: &str, outcome: &HandoffOutcome) {
    if let Ok(mut r) = state.record.lock() {
        if let Some(rec) = r.as_mut() {
            if rec.id == id {
                rec.result = Some(outcome.kind().to_owned());
                rec.detail = outcome.detail().map(ToOwned::to_owned);
            }
        }
    }
}

#[cfg(test)]
#[allow(clippy::panic, clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::{MAX_FRAME, Request, read_frame, write_frame};
    use crate::handoff::Mutation;
    use std::io::Cursor;

    #[test]
    fn frame_roundtrips() {
        let mut buf = Vec::new();
        write_frame(&mut buf, b"hello").expect("write");
        let mut cur = Cursor::new(buf);
        let got = read_frame(&mut cur, MAX_FRAME).expect("read");
        assert_eq!(got, b"hello");
    }

    #[test]
    fn read_frame_rejects_oversize() {
        // Length prefix claims 2 MiB, over the 1 MiB cap.
        let mut buf = Vec::new();
        buf.extend_from_slice(&(2u32 << 20).to_le_bytes());
        let mut cur = Cursor::new(buf);
        assert!(read_frame(&mut cur, MAX_FRAME).is_err());
    }

    #[test]
    fn read_frame_errors_on_truncated_length() {
        let mut cur = Cursor::new(vec![1u8, 2u8]); // < 4 bytes
        assert!(read_frame(&mut cur, MAX_FRAME).is_err());
    }

    #[test]
    fn parses_request_mutation_delete() {
        let raw = br#"{"cmd":"request_mutation","partition":1,"mutation":{"op":"delete_path","rel_path":"TeslaCam/x"}}"#;
        let req: Request = serde_json::from_slice(raw).expect("parse");
        match req {
            Request::Mutate {
                partition,
                mutation,
            } => {
                assert_eq!(partition, 1);
                assert_eq!(
                    mutation,
                    Mutation::DeletePath {
                        rel_path: "TeslaCam/x".to_owned()
                    }
                );
            }
            other => panic!("expected request_mutation, got {other:?}"),
        }
    }

    #[test]
    fn parses_gadget_status() {
        let req: Request = serde_json::from_slice(br#"{"cmd":"gadget_status"}"#).expect("parse");
        assert!(matches!(req, Request::GadgetStatus));
    }

    use super::{bind_listener, socket_path_healthy};
    use std::os::unix::fs::{FileTypeExt, PermissionsExt};

    /// Unique scratch dir under the system temp dir (no tempfile dev-dep).
    fn scratch_dir(tag: &str) -> std::path::PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let dir =
            std::env::temp_dir().join(format!("gadgetd-test-{tag}-{}-{nanos}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("mkdir");
        dir
    }

    #[test]
    fn bind_listener_creates_socket_with_0660() {
        let dir = scratch_dir("bind");
        let sock = dir.join("gadgetd.sock");
        let listener = bind_listener(&sock).expect("bind");
        let meta = std::fs::symlink_metadata(&sock).expect("stat");
        assert!(meta.file_type().is_socket(), "path should be a socket");
        assert_eq!(meta.permissions().mode() & 0o777, 0o660);
        assert!(socket_path_healthy(&sock));
        drop(listener);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn bind_listener_replaces_stale_socket() {
        let dir = scratch_dir("stale");
        let sock = dir.join("gadgetd.sock");
        let first = bind_listener(&sock).expect("first bind");
        // A second bind over the live path must succeed by clearing the stale file
        // (the EADDRINUSE-avoidance path), proving re-bind is idempotent.
        let second = bind_listener(&sock).expect("re-bind over existing");
        assert!(socket_path_healthy(&sock));
        drop(first);
        drop(second);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn socket_path_healthy_false_when_missing_or_wrong_type() {
        let dir = scratch_dir("healthy");
        let missing = dir.join("nope.sock");
        assert!(!socket_path_healthy(&missing), "missing path is unhealthy");
        // A regular file at the path is NOT a healthy socket.
        let regular = dir.join("regular");
        std::fs::write(&regular, b"x").expect("write");
        assert!(
            !socket_path_healthy(&regular),
            "regular file is not a socket"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn rebind_recovers_after_socket_unlinked() {
        // Reproduces the RuntimeDirectory bug: a peer unlinks our live socket. The
        // first listener keeps its fd, but the path is gone (unhealthy). bind_listener
        // restores a fresh, healthy socket at the same path.
        let dir = scratch_dir("rebind");
        let sock = dir.join("gadgetd.sock");
        let orphaned = bind_listener(&sock).expect("initial bind");
        std::fs::remove_file(&sock).expect("simulate peer unlink");
        assert!(!socket_path_healthy(&sock), "socket path gone after unlink");
        let healed = bind_listener(&sock).expect("re-bind");
        assert!(socket_path_healthy(&sock), "socket restored after re-bind");
        drop(orphaned);
        drop(healed);
        std::fs::remove_dir_all(&dir).ok();
    }
}
