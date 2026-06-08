//! `retentiond` — archiving, retention policy, and the SD-card space governor.
//!
//! This binary is a thin entrypoint. The **deliverable of Task 6.1a–f is the
//! library** (`retentiond::*`): the per-folder archiving policy, manifest
//! verification, the `RecentClips` rotation estimate, the value-scoring eviction
//! model, the space-governor tier state machine, lease honoring, and the
//! crash-safe single-deleter protocol — all pure and host-unit-tested behind
//! traits ([`retentiond::io`]).
//!
//! The **live wiring** — real `statfs`/`fsync`/`rename`/unlink, the `gadgetd`
//! eject-handoff IPC client, and the `indexd` lease/delete-state RPC client — is
//! hardware-gated (`#[cfg(unix)]`) and depends on the governor-defaults
//! calibration gate (Task 2.7 / `storage.md` §7), so it is intentionally **not**
//! implemented in this host-core lane. This entrypoint exists so the crate
//! produces its declared binary and can report its build identity.

// systemd captures this binary's stdout/stderr into the journal, so direct
// console output is the intended logging path for the daemon entrypoint only.
#![allow(clippy::print_stdout, clippy::print_stderr)]

use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("version" | "--version" | "-V") => {
            println!("retentiond {}", env!("CARGO_PKG_VERSION"));
            ExitCode::SUCCESS
        }
        Some("--help" | "-h" | "help") | None => {
            println!("{}", usage());
            ExitCode::SUCCESS
        }
        Some("serve") => {
            // The live daemon loop (statfs cadence, archive pipeline, governor,
            // single-deleter) is hardware-gated and calibration-gated; it is not
            // part of the host-testable core this lane delivers.
            eprintln!(
                "retentiond serve: live wiring is hardware-gated (Task 2.7 governor \
                 calibration / storage.md §7) and not built in the host-core lane."
            );
            ExitCode::FAILURE
        }
        Some(other) => {
            eprintln!("retentiond: unknown command `{other}`\n{}", usage());
            ExitCode::FAILURE
        }
    }
}

fn usage() -> String {
    "usage: retentiond <version|serve|help>\n\
     note: the Task 6.1a-f deliverable is the host-tested library core; \
     `serve` (live wiring) is hardware/calibration-gated."
        .to_owned()
}
