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
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Deserialize;
use serde_json::json;

use crate::config::GadgetConfig;
use crate::exec::{self, LiveLun, MtimeSaveGuard};
use crate::handoff::{self, HandoffOutcome, ImageMutator, LunControl, Mutation, Partition};
use crate::mutate::LoopMutator;

/// Maximum accepted frame size (a handful of small JSON fields).
const MAX_FRAME: u32 = 1 << 20;
/// Per-connection I/O timeout so a slow/stuck client can't pin a thread.
const CONN_TIMEOUT: Duration = Duration::from_secs(15);
/// mtime sampling interval for the save-active guard.
const SAVE_SAMPLE: Duration = Duration::from_millis(500);

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
    image: PathBuf,
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

/// Bring up the control socket and serve until the listener errors.
///
/// # Errors
/// Returns an error only for fatal listener/bind problems; per-connection errors
/// are logged and do not stop the daemon.
pub(crate) fn serve(
    cfg: GadgetConfig,
    image: PathBuf,
    runtime_root: PathBuf,
    socket_path: &Path,
    allow_hot: bool,
) -> io::Result<()> {
    // Handoff crash-recovery BEFORE serving (never touches gadget bring-up).
    recover_interrupted_handoff(&cfg, &image, &runtime_root);

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

    let state = Arc::new(ServeState {
        cfg,
        image,
        runtime_root,
        allow_hot,
        record: Mutex::new(None),
        handoff_lock: Mutex::new(()),
        next_id: AtomicU64::new(1),
    });

    for conn in listener.incoming() {
        match conn {
            Ok(stream) => {
                let st = Arc::clone(&state);
                std::thread::spawn(move || {
                    if let Err(e) = handle_conn(&stream, &st) {
                        eprintln!("gadgetd serve: connection error: {e}");
                    }
                });
            }
            Err(e) => eprintln!("gadgetd serve: accept error: {e}"),
        }
    }
    Ok(())
}

/// On startup, reconcile an interrupted handoff: first clear any stale loop
/// device / mount left on the image, and only then — if the gadget is bound but
/// the LUN is empty — re-present. Never re-present while the image might still
/// be held locally (the never-double-writer precedence).
fn recover_interrupted_handoff(cfg: &GadgetConfig, image: &Path, runtime_root: &Path) {
    let lun = LiveLun::new(cfg.clone());
    let mutator = LoopMutator::new(image.to_path_buf(), runtime_root.to_path_buf());

    let cleanup = mutator.cleanup_stale();
    if let Err(e) = &cleanup {
        eprintln!("gadgetd serve: stale-mount cleanup failed during recovery: {e}");
    }

    match (lun.is_bound(), lun.lun_is_empty()) {
        (Ok(true), Ok(true)) => {
            if cleanup.is_ok() {
                match lun.represent() {
                    Ok(()) => {
                        println!("gadgetd serve: recovered interrupted handoff (re-presented LUN)");
                    }
                    Err(e) => eprintln!("gadgetd serve: CRITICAL recovery re-present failed: {e}"),
                }
            } else {
                eprintln!(
                    "gadgetd serve: CRITICAL: LUN ejected but stale mounts remain; \
                     NOT re-presenting to avoid a double-writer — manual recovery needed"
                );
            }
        }
        (Ok(_), Ok(_)) => {}
        (b, f) => eprintln!("gadgetd serve: recovery state read failed (bound={b:?}, empty={f:?})"),
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

    let lun = LiveLun::new(state.cfg.clone());
    let guard = MtimeSaveGuard::new(state.image.clone(), SAVE_SAMPLE);
    let mutator = LoopMutator::new(state.image.clone(), state.runtime_root.clone());

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
}
