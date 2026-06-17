//! Control-plane IPC for `schedulerd serve`: a length-prefixed JSON protocol
//! over a Unix domain socket, mirroring `gadgetd`'s proven framing.
//!
//! `schedulerd` owns the schedule state and the chime library; `webd` is a pure
//! proxy that forwards REST requests here. Each connection is handled on its own
//! thread; all state mutation is serialized behind a single `Mutex<Store>` so
//! the single-writer contract holds. Authorization is by filesystem permission
//! on the socket (mode `0o660`, group-owned), the same posture `gadgetd` uses —
//! there is no in-band auth.
//!
//! This module is Unix-only (it uses `std::os::unix`); the daemon only ever runs
//! on the Pi. The platform-agnostic core (`model`, `store`, `library`) compiles
//! everywhere so the host can unit-test it.

// systemd captures the daemon's stdout/stderr into the journal, so direct
// console output is the intended logging path for the serve loop.
#![allow(clippy::print_stdout, clippy::print_stderr)]

use std::io::{self, Read, Write};
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::{Value, json};
use teslausb_core::chime::{Pick, civil_from_unix};

use crate::library;
use crate::model::{GroupInput, RandomMode, ScheduleInput, SchedulerMenus};
use crate::store::Store;

/// Maximum accepted frame size. Schedule/group payloads are tiny; library
/// uploads are passed by *staged path*, never inline, so 1 MiB is ample.
const MAX_FRAME: u32 = 1 << 20;
/// Per-connection I/O timeout so a slow/stuck client can't pin a thread.
const CONN_TIMEOUT: Duration = Duration::from_secs(15);
/// How often the serve loop re-checks that its control socket still exists, so a
/// peer unit that unlinks it (the shared-`RuntimeDirectory` hazard) heals.
const SOCKET_HEALTH_INTERVAL: Duration = Duration::from_secs(2);
/// Backoff between non-blocking accept polls when no connection is pending.
const ACCEPT_POLL: Duration = Duration::from_millis(100);

/// A wire request (`cmd`-tagged JSON, `snake_case`).
#[derive(Debug, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum Request {
    /// Full read: schedules + groups + random mode + library + menus.
    Snapshot,
    /// Just the chime library listing.
    ListLibrary,
    /// Create a schedule.
    AddSchedule {
        /// The schedule definition.
        input: ScheduleInput,
    },
    /// Replace a schedule by id.
    UpdateSchedule {
        /// Target schedule id.
        id: String,
        /// The replacement definition.
        input: ScheduleInput,
    },
    /// Delete a schedule by id.
    DeleteSchedule {
        /// Target schedule id.
        id: String,
    },
    /// Create a group.
    AddGroup {
        /// The group definition.
        input: GroupInput,
    },
    /// Replace a group by id.
    UpdateGroup {
        /// Target group id.
        id: String,
        /// The replacement definition.
        input: GroupInput,
    },
    /// Delete a group by id.
    DeleteGroup {
        /// Target group id.
        id: String,
    },
    /// Set the random-on-boot mode.
    SetRandomMode {
        /// The new random-mode configuration.
        mode: RandomMode,
    },
    /// Adopt a staged temp file into the library under `filename`.
    AddLibraryFile {
        /// Absolute path of the staged upload (root-owned, on the same fs).
        staged_path: String,
        /// Destination single-segment `*.wav` filename.
        filename: String,
    },
    /// Remove a file from the library.
    DeleteLibraryFile {
        /// The library filename to remove.
        filename: String,
    },
    /// Evaluate which chime should be active at the given instant.
    Evaluate {
        /// Unix timestamp (seconds) of "now".
        unix_secs: i64,
        /// Local UTC offset in seconds (the caller resolves DST).
        tz_offset_secs: i32,
        /// The chime currently active, if known (to avoid re-picking it).
        #[serde(default)]
        active_chime: Option<String>,
        /// Optional library override (used by webd to pass its real catalog).
        #[serde(default)]
        library: Option<Vec<String>>,
    },
    /// Evaluate the boot-time chime (`OnBoot` schedules + random-on-boot).
    EvaluateBoot {
        unix_secs: i64,
        tz_offset_secs: i32,
        #[serde(default)]
        active_chime: Option<String>,
        #[serde(default)]
        library: Option<Vec<String>>,
        #[serde(default)]
        boot_seed: u64,
    },
}

/// Shared daemon state: the single-writer store plus the library directory.
struct ServeState {
    store: Mutex<Store>,
    library_dir: PathBuf,
}

/// Read a length-prefixed frame (4-byte LE length, then payload).
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
/// perms to `0o660`. Mirrors `gadgetd`'s `bind_listener`.
fn bind_listener(socket_path: &Path) -> io::Result<UnixListener> {
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    match std::fs::remove_file(socket_path) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    let listener = UnixListener::bind(socket_path)?;
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o660))?;
    println!("schedulerd serve: listening on {}", socket_path.display());
    Ok(listener)
}

/// True iff the control socket path still exists AND is a socket.
fn socket_path_healthy(socket_path: &Path) -> bool {
    std::fs::symlink_metadata(socket_path)
        .map(|m| m.file_type().is_socket())
        .unwrap_or(false)
}

/// Bring up the control socket and serve until the listener errors.
///
/// # Errors
/// Returns an error only for fatal listener/bind problems; per-connection
/// errors are logged and never stop the daemon.
pub fn serve(state_path: PathBuf, library_dir: PathBuf, socket_path: &Path) -> io::Result<()> {
    let store = Store::load(state_path);
    let state = Arc::new(ServeState {
        store: Mutex::new(store),
        library_dir,
    });

    let mut listener = bind_listener(socket_path)?;
    listener.set_nonblocking(true)?;

    let mut last_health = Instant::now();
    loop {
        match listener.accept() {
            Ok((stream, _addr)) => {
                stream.set_nonblocking(false)?;
                let st = Arc::clone(&state);
                std::thread::spawn(move || {
                    if let Err(e) = handle_conn(&stream, &st) {
                        eprintln!("schedulerd serve: connection error: {e}");
                    }
                });
            }
            Err(ref e) if e.kind() == io::ErrorKind::WouldBlock => {
                std::thread::sleep(ACCEPT_POLL);
            }
            Err(e) => eprintln!("schedulerd serve: accept error: {e}"),
        }

        if last_health.elapsed() >= SOCKET_HEALTH_INTERVAL {
            last_health = Instant::now();
            if !socket_path_healthy(socket_path) {
                eprintln!(
                    "schedulerd serve: control socket {} vanished; re-binding",
                    socket_path.display()
                );
                match bind_listener(socket_path) {
                    Ok(l) => {
                        l.set_nonblocking(true)?;
                        listener = l;
                    }
                    Err(e) => eprintln!("schedulerd serve: re-bind failed: {e}"),
                }
            }
        }
    }
}

fn handle_conn(stream: &UnixStream, state: &ServeState) -> io::Result<()> {
    stream.set_read_timeout(Some(CONN_TIMEOUT))?;
    stream.set_write_timeout(Some(CONN_TIMEOUT))?;
    let mut reader = stream;
    let payload = read_frame(&mut reader, MAX_FRAME)?;

    let response = match serde_json::from_slice::<Request>(&payload) {
        Ok(req) => dispatch(req, state),
        Err(e) => err_envelope("bad_request", &format!("bad request: {e}")),
    };
    let bytes = serde_json::to_vec(&response).map_err(io::Error::other)?;
    let mut writer = stream;
    write_frame(&mut writer, &bytes)
}

/// The shared `{error:{code,message}}` envelope (matches the webd convention).
fn err_envelope(code: &str, message: &str) -> Value {
    json!({ "error": { "code": code, "message": message } })
}

/// Map a [`crate::store::StoreError`] onto the error envelope.
fn store_err(e: &crate::store::StoreError) -> Value {
    use crate::store::StoreError;
    match e {
        StoreError::Validation(v) => err_envelope(v.code, &v.message),
        StoreError::NotFound => err_envelope("not_found", "no record with that id"),
        StoreError::Io(io) => err_envelope("io_error", &io.to_string()),
    }
}

/// Map a [`crate::library::LibraryError`] onto the error envelope.
fn library_err(e: &crate::library::LibraryError) -> Value {
    use crate::library::LibraryError;
    match e {
        LibraryError::Validation(v) => err_envelope(v.code, &v.message),
        LibraryError::NotFound => err_envelope("not_found", "no such library file"),
        LibraryError::Io(io) => err_envelope("io_error", &io.to_string()),
    }
}

#[allow(clippy::too_many_lines)]
fn dispatch(req: Request, state: &ServeState) -> Value {
    match req {
        Request::Snapshot => match state.store.lock() {
            Ok(store) => {
                let library = library::scan(&state.library_dir).unwrap_or_default();
                json!({
                    "schedules": store.schedules(),
                    "groups": store.groups(),
                    "randomMode": store.random_mode(),
                    "library": library,
                    "menus": SchedulerMenus::build(),
                })
            }
            Err(_) => err_envelope("locked", "state lock poisoned"),
        },
        Request::ListLibrary => match library::scan(&state.library_dir) {
            Ok(l) => json!({ "library": l }),
            Err(e) => library_err(&e),
        },
        Request::AddSchedule { input } => with_store(state, |store| {
            store
                .add_schedule(input)
                .map_or_else(|e| store_err(&e), |rec| json!({ "schedule": rec }))
        }),
        Request::UpdateSchedule { id, input } => with_store(state, |store| {
            store
                .update_schedule(&id, input)
                .map_or_else(|e| store_err(&e), |rec| json!({ "schedule": rec }))
        }),
        Request::DeleteSchedule { id } => with_store(state, |store| {
            store
                .delete_schedule(&id)
                .map_or_else(|e| store_err(&e), |removed| json!({ "removed": removed }))
        }),
        Request::AddGroup { input } => with_store(state, |store| {
            store
                .add_group(input)
                .map_or_else(|e| store_err(&e), |g| json!({ "group": g }))
        }),
        Request::UpdateGroup { id, input } => with_store(state, |store| {
            store
                .update_group(&id, input)
                .map_or_else(|e| store_err(&e), |g| json!({ "group": g }))
        }),
        Request::DeleteGroup { id } => with_store(state, |store| {
            store
                .delete_group(&id)
                .map_or_else(|e| store_err(&e), |removed| json!({ "removed": removed }))
        }),
        Request::SetRandomMode { mode } => with_store(state, |store| {
            store
                .set_random_mode(mode)
                .map_or_else(|e| store_err(&e), |()| json!({ "ok": true }))
        }),
        Request::AddLibraryFile {
            staged_path,
            filename,
        } => match library::adopt(&state.library_dir, Path::new(&staged_path), &filename) {
            Ok(entry) => json!({ "file": entry }),
            Err(e) => library_err(&e),
        },
        Request::DeleteLibraryFile { filename } => {
            match library::remove(&state.library_dir, &filename) {
                Ok(removed) => json!({ "removed": removed }),
                Err(e) => library_err(&e),
            }
        }
        Request::Evaluate {
            unix_secs,
            tz_offset_secs,
            active_chime,
            library,
        } => match state.store.lock() {
            Ok(store) => {
                let now = civil_from_unix(unix_secs, tz_offset_secs);
                let library = resolve_eval_library(library, &state.library_dir);
                let pick = store.evaluate(now, active_chime.as_deref(), &library);
                json!({ "pick": pick_json(pick) })
            }
            Err(_) => err_envelope("locked", "state lock poisoned"),
        },
        Request::EvaluateBoot {
            unix_secs,
            tz_offset_secs,
            active_chime,
            library,
            boot_seed,
        } => match state.store.lock() {
            Ok(store) => {
                let now = civil_from_unix(unix_secs, tz_offset_secs);
                let library = resolve_eval_library(library, &state.library_dir);
                let pick = store.evaluate_boot(
                    now,
                    active_chime.as_deref(),
                    &library,
                    boot_seed,
                );
                json!({ "pick": pick_json(pick) })
            }
            Err(_) => err_envelope("locked", "state lock poisoned"),
        },
    }
}

/// Resolve the library list for an evaluate request. A **supplied** `library`
/// (from webd's authoritative media catalog) is used verbatim — INCLUDING an
/// empty list, which legitimately means "no installable candidates" and must
/// NOT silently fall back to the stale local scan. Only an **omitted** field
/// (legacy callers) triggers the local `library_dir` scan.
fn resolve_eval_library(supplied: Option<Vec<String>>, library_dir: &std::path::Path) -> Vec<String> {
    match supplied {
        Some(v) => v,
        None => library::scan(library_dir)
            .unwrap_or_default()
            .into_iter()
            .map(|e| e.filename)
            .collect(),
    }
}

/// Build the `{scheduleId, scheduleName, chimeFilename}` JSON for a resolved
/// pick (camelCase wire shape shared by `Evaluate` and `EvaluateBoot`).
fn pick_json(pick: Option<Pick>) -> Option<Value> {
    pick.map(|p| {
        json!({
            "scheduleId": p.schedule_id,
            "scheduleName": p.schedule_name,
            "chimeFilename": p.chime_filename,
        })
    })
}

/// Run `f` with the locked store, answering a poisoned lock with an envelope.
fn with_store<F>(state: &ServeState, f: F) -> Value
where
    F: FnOnce(&mut Store) -> Value,
{
    match state.store.lock() {
        Ok(mut store) => f(&mut store),
        Err(_) => err_envelope("locked", "state lock poisoned"),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::panic, clippy::indexing_slicing)]
mod tests {
    use super::*;

    fn tmp(tag: &str) -> PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        std::env::temp_dir().join(format!("schedipc-{tag}-{nanos}"))
    }

    /// Round-trip one request through a live serve loop on a temp socket.
    fn call(socket: &Path, req: &Value) -> Value {
        let mut stream = UnixStream::connect(socket).unwrap();
        let payload = serde_json::to_vec(req).unwrap();
        write_frame(&mut stream, &payload).unwrap();
        let resp = read_frame(&mut stream, MAX_FRAME).unwrap();
        serde_json::from_slice(&resp).unwrap()
    }

    fn spawn_server(tag: &str) -> (PathBuf, PathBuf) {
        let dir = tmp(tag);
        std::fs::create_dir_all(&dir).unwrap();
        let state_path = dir.join("state.json");
        let library_dir = dir.join("chimes");
        let socket = dir.join("schedulerd.sock");
        let sp = state_path.clone();
        let ld = library_dir.clone();
        let sock = socket.clone();
        std::thread::spawn(move || {
            let _ = serve(sp, ld, &sock);
        });
        // Wait for the socket to appear.
        for _ in 0..100 {
            if socket_path_healthy(&socket) {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        (socket, dir)
    }

    #[test]
    fn snapshot_add_and_persist_over_socket() {
        let (socket, dir) = spawn_server("snap");

        // Initially empty.
        let snap = call(&socket, &json!({ "cmd": "snapshot" }));
        assert_eq!(snap["schedules"].as_array().unwrap().len(), 0);
        assert_eq!(snap["menus"]["holidays"].as_array().unwrap().len(), 18);

        // Add a weekly schedule.
        let add = call(
            &socket,
            &json!({
                "cmd": "add_schedule",
                "input": {
                    "name": "Morning",
                    "chimeFilename": "Classic.wav",
                    "scheduleType": "weekly",
                    "days": ["Monday"],
                    "hour": 8,
                    "minute": 0,
                    "enabled": true
                }
            }),
        );
        assert_eq!(add["schedule"]["id"], "sched-1");

        // It shows up in a fresh snapshot.
        let snap2 = call(&socket, &json!({ "cmd": "snapshot" }));
        assert_eq!(snap2["schedules"].as_array().unwrap().len(), 1);

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn validation_error_returns_envelope() {
        let (socket, dir) = spawn_server("valid");
        let resp = call(
            &socket,
            &json!({
                "cmd": "add_schedule",
                "input": {
                    "name": "",
                    "chimeFilename": "X.wav",
                    "scheduleType": "weekly",
                    "days": ["Monday"],
                    "hour": 8,
                    "minute": 0
                }
            }),
        );
        assert_eq!(resp["error"]["code"], "empty_name");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn bad_request_returns_envelope() {
        let (socket, dir) = spawn_server("bad");
        let resp = call(&socket, &json!({ "cmd": "no_such_command" }));
        assert_eq!(resp["error"]["code"], "bad_request");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn evaluate_resolves_active_chime() {
        let (socket, dir) = spawn_server("eval");
        // Add a recurring-free weekly schedule on Thursday at 09:00.
        call(
            &socket,
            &json!({
                "cmd": "add_schedule",
                "input": {
                    "name": "Morn",
                    "chimeFilename": "Classic.wav",
                    "scheduleType": "weekly",
                    "days": ["Thursday"],
                    "hour": 9,
                    "minute": 0,
                    "enabled": true
                }
            }),
        );
        // 2026-01-01T09:30:00Z is a Thursday; UTC offset 0.
        let resp = call(
            &socket,
            &json!({
                "cmd": "evaluate",
                "unix_secs": 1_767_260_400_i64 + 1800,
                "tz_offset_secs": 0
            }),
        );
        assert_eq!(resp["pick"]["chimeFilename"], "Classic.wav");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn evaluate_boot_round_trips_pick() {
        let (socket, dir) = spawn_server("boot_eval");
        let resp = call(
            &socket,
            &json!({
                "cmd": "evaluate_boot",
                "unix_secs": 1_767_260_400_i64,
                "tz_offset_secs": 0,
                "library": ["A.wav"],
                "boot_seed": 5
            }),
        );
        assert!(resp["pick"].is_null() || resp["pick"]["chimeFilename"].is_string());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
