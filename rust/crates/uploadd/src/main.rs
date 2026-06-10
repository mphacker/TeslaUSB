//! `uploadd` — the durable cloud-upload daemon entrypoint.
//!
//! This binary is a thin entrypoint. The **deliverable of Task 6.3 is the
//! library** (`uploadd::*`): the durable resumable queue state machine, the
//! priority policy, the upload-lease holder, the WiFi-throttle consumer + self
//! pacer, the integrity-checked transfer orchestration, and the durability
//! signal — all pure and host-unit-tested behind traits.
//!
//! The **live wiring** — the real transfer backend (rclone or a Rust uploader —
//! an unresolved ASK-FIRST "choose at build" decision, see
//! [`uploadd::transfer`]), the `indexd` lease/queue/durability RPC client, the
//! `wifid` throttle subscription, and the archive filesystem reads — is
//! hardware/IPC-gated and depends on the `WiFi` TX-cap calibration
//! (Task 2.6 / [`wifi-upload-throttle.md`]), so it is intentionally **not**
//! implemented in this host-core lane. This entrypoint exists so the crate
//! produces its declared binary and can report its build identity.
//!
//! [`wifi-upload-throttle.md`]: ../../../docs/specs/contracts/wifi-upload-throttle.md

// systemd captures this binary's stdout/stderr into the journal, so direct
// console output is the intended logging path for the daemon entrypoint only.
#![allow(clippy::print_stdout, clippy::print_stderr)]

use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("version" | "--version" | "-V") => {
            println!("uploadd {}", env!("CARGO_PKG_VERSION"));
            ExitCode::SUCCESS
        }
        Some("--help" | "-h" | "help") | None => {
            println!("{}", usage());
            ExitCode::SUCCESS
        }
        Some("serve") => {
            // The host-testable orchestration core is implemented and tested:
            // `uploadd::serve::Scheduler` (priority-ordered, lease-aware,
            // throttle-aware drain loop over the durable queue) drives an
            // `uploadd::serve::UploadProcessor`, and `uploadd::rclone`'s
            // `RcloneUploadEngine` is the chosen v1 backend (whole-file `rclone
            // copyto` + `hashsum` verify behind a `CommandRunner` subprocess
            // seam). The remaining *live wiring* — the `indexd` queue/lease/
            // durability RPC clients, the `wifid` throttle subscription, the
            // archive filesystem reads, and spawning the real `rclone` binary —
            // is hardware/IPC-gated and depends on the Task 2.6 `WiFi` TX-cap
            // calibration, so it is not constructed here.
            eprintln!(
                "uploadd serve: orchestration core is implemented + host-tested \
                 (uploadd::serve::Scheduler + uploadd::rclone::RcloneUploadEngine); \
                 live IPC/throttle/rclone wiring is hardware/calibration-gated and \
                 not built in this lane."
            );
            ExitCode::FAILURE
        }
        Some(other) => {
            eprintln!("uploadd: unknown command `{other}`\n{}", usage());
            ExitCode::FAILURE
        }
    }
}

fn usage() -> String {
    "usage: uploadd <version|serve|help>\n\
     note: the Task 6.3 deliverable is the host-tested library core; \
     `serve` (live wiring) is hardware/IPC/calibration-gated."
        .to_owned()
}
