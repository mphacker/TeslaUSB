//! `schedulerd` — the lock-chime scheduler daemon entrypoint.
//!
//! This binary is a thin entrypoint. The **deliverable of this lane is the
//! library** (`schedulerd::model` + `schedulerd::store`): the validated
//! schedule/group/library state owner, its atomic JSON persistence, and the
//! rule-evaluation driver over [`teslausb_core::chime`] — all host-unit-tested.
//!
//! The **live wiring** — a UDS control socket (mirroring `gadgetd`'s framing
//! and `SO_PEERCRED` authz), the per-minute tick that evaluates the active
//! chime, and the `gadgetd` activation enqueue that swaps `LockChime.wav` via a
//! staged library copy — is hardware/IPC-gated and is built in a later slice,
//! the same posture as `uploadd`/`retentiond`. This entrypoint exists so the
//! crate produces its declared binary and can report its build identity.

// systemd captures this binary's stdout/stderr into the journal, so direct
// console output is the intended logging path for the daemon entrypoint only.
#![allow(clippy::print_stdout, clippy::print_stderr)]

use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("version" | "--version" | "-V") => {
            println!("schedulerd {}", env!("CARGO_PKG_VERSION"));
            ExitCode::SUCCESS
        }
        Some("--help" | "-h" | "help") | None => {
            println!("{}", usage());
            ExitCode::SUCCESS
        }
        Some("serve") => serve(),
        Some(other) => {
            eprintln!("schedulerd: unknown command `{other}`\n{}", usage());
            ExitCode::FAILURE
        }
    }
}

fn usage() -> String {
    "usage: schedulerd <version|serve|help>\n\
     note: the deliverable in this lane is the host-tested library core; \
     `serve` (live UDS + tick + gadgetd activation) is hardware/IPC-gated."
        .to_owned()
}

/// Default state-document path; override with `SCHEDULERD_STATE`.
const DEFAULT_STATE_PATH: &str = "/data/teslausb/chime-schedules.json";
/// Default chime-library directory; override with `SCHEDULERD_LIBRARY_DIR`.
const DEFAULT_LIBRARY_DIR: &str = "/data/teslausb/chimes";
/// Default control-socket path; override with `SCHEDULERD_SOCKET`.
const DEFAULT_SOCKET_PATH: &str = "/run/teslausb/schedulerd.sock";

/// Run the control-socket server: serves schedule/group/library state to `webd`
/// over a Unix domain socket. The per-minute enforcement tick and the `gadgetd`
/// activation enqueue (the live `LockChime.wav` swap) are a later, hardware-gated
/// slice — this server only owns and serves the state.
#[cfg(unix)]
fn serve() -> ExitCode {
    use std::path::PathBuf;

    let state_path = PathBuf::from(
        std::env::var("SCHEDULERD_STATE").unwrap_or_else(|_| DEFAULT_STATE_PATH.to_owned()),
    );
    let library_dir = PathBuf::from(
        std::env::var("SCHEDULERD_LIBRARY_DIR").unwrap_or_else(|_| DEFAULT_LIBRARY_DIR.to_owned()),
    );
    let socket_path = PathBuf::from(
        std::env::var("SCHEDULERD_SOCKET").unwrap_or_else(|_| DEFAULT_SOCKET_PATH.to_owned()),
    );

    match schedulerd::ipc::serve(state_path, library_dir, &socket_path) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("schedulerd serve: fatal: {e}");
            ExitCode::FAILURE
        }
    }
}

/// On non-Unix hosts the control socket cannot exist; report it cleanly so the
/// dev host keeps compiling (the daemon only ever runs on the Pi).
#[cfg(not(unix))]
fn serve() -> ExitCode {
    eprintln!("schedulerd serve: the UDS control socket is only available on Unix targets");
    ExitCode::FAILURE
}
