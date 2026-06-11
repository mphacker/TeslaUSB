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
        Some("serve") => {
            eprintln!(
                "schedulerd serve: the state owner + rule-evaluation core is \
                 implemented + host-tested (schedulerd::store::Store over \
                 teslausb-core::chime); the UDS control socket, per-minute tick, \
                 and gadgetd activation enqueue are hardware/IPC-gated and not \
                 built in this lane."
            );
            ExitCode::FAILURE
        }
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
