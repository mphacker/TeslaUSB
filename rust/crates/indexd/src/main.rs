//! `indexd` binary entry point.
//!
//! `indexd` is the **sole DB writer** and a pure *consumer* of `scannerd`'s
//! facts: it never opens the raw backing image and never parses car-written
//! bytes. It connects to `scannerd`'s Unix socket, drives the scan cadence,
//! receives a [`ScanBatch`](scannerd::record::ScanBatch) of validated facts
//! per pass, and applies it to `SQLite` (`indexd::apply`). This is the
//! consumer half of the `scannerd → indexd` privilege/fault-isolation seam:
//! a weaponized clip can only ever crash the disposable `scannerd`
//! producer; it can never reach this DB-owning process.
//!
//! The client owns the **30 s cadence** and a **monotonic generation**, and
//! requests a **resync** (full replay of the currently-stable set) on first
//! connect, on every reconnect, and after any apply failure — recovering a
//! batch that was produced but not durably committed, leaning on the
//! rebuildable, idempotent DB rather than any persisted queue.
//!
//! A binary may relax the `print_*` lints (like `gadgetd`/`scannerd`) but
//! NOT `unwrap_used`. The whole client is Unix-only (it speaks over a
//! `UnixStream`); the non-Unix build is a stub, mirroring `scannerd`.

#![allow(clippy::print_stdout, clippy::print_stderr)]

use std::process::ExitCode;

#[cfg(not(unix))]
fn main() -> ExitCode {
    eprintln!("indexd: this binary runs on Linux (the Pi) only");
    ExitCode::FAILURE
}

#[cfg(unix)]
fn main() -> ExitCode {
    match unix_app::run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("indexd: fatal: {e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(unix)]
mod unix_app {
    use std::os::unix::net::UnixStream;
    use std::path::PathBuf;
    use std::thread::sleep;
    use std::time::Duration;

    use indexd::apply::apply;
    use indexd::db::mutations::BootContext;
    use indexd::db::{DbError, open};
    use indexd::derive::DeriveConfig;
    use scannerd::proto::{Request, read_batch, write_request};

    /// Default on-Pi DB path. ext4, Pi-side — NEVER inside `disk.img` / the
    /// Tesla volume (SPEC §6.1 #1 invariant).
    const DEFAULT_DB_PATH: &str = "/var/lib/teslausb/index.sqlite3";

    /// Default `scannerd` control-socket path (matches `scannerd serve`).
    const DEFAULT_SOCKET: &str = "/run/teslausb/scannerd.sock";

    /// Seconds between scan passes. Two stable observations spaced by the
    /// quiescence window gate a clip in.
    const SCAN_INTERVAL_SECS: u64 = 30;

    /// Backoff before reconnecting after a connect/stream failure, so a
    /// down or restarting `scannerd` doesn't spin the CPU.
    const RECONNECT_BACKOFF: Duration = Duration::from_secs(5);

    /// Read timeout for a response: `scannerd` answers a request promptly,
    /// so a stall here means a hung server — drop and reconnect. (Applies
    /// only while reading a reply; the idle gap between passes is a plain
    /// `sleep`, not a blocked read.)
    const IO_TIMEOUT: Duration = Duration::from_secs(60);

    /// Resolve config from args/env: `argv[1]` (or `INDEXD_DB`) = DB path;
    /// `argv[2]` (or `INDEXD_SCANNERD_SOCKET`) = `scannerd` socket path.
    fn resolve_paths() -> (PathBuf, PathBuf) {
        let mut args = std::env::args().skip(1);
        let db = args
            .next()
            .or_else(|| std::env::var("INDEXD_DB").ok())
            .unwrap_or_else(|| DEFAULT_DB_PATH.to_owned());
        let socket = args
            .next()
            .or_else(|| std::env::var("INDEXD_SCANNERD_SOCKET").ok())
            .unwrap_or_else(|| DEFAULT_SOCKET.to_owned());
        (PathBuf::from(db), PathBuf::from(socket))
    }

    /// Open the DB, reap stale leases, then run the connect/scan loop
    /// forever (the loop only returns on a fatal, non-recoverable error).
    pub fn run() -> Result<(), String> {
        let (db_path, socket_path) = resolve_paths();
        let db_display = db_path.display().to_string();
        let mut conn = open(&db_path).map_err(|e: DbError| format!("opening {db_display}: {e}"))?;

        // Single-writer hygiene: reap leases stranded by a previous boot.
        let boot = BootContext::new();
        let reaped = boot
            .reap(&conn)
            .map_err(|e| format!("reaping stale leases: {e}"))?;
        println!(
            "indexd: boot {} ; reaped {reaped} stale lease(s)",
            boot.boot_id()
        );

        let socket_display = socket_path.display().to_string();
        let derive_cfg = DeriveConfig::default();
        let mut generation: u64 = 0;

        println!("indexd: consuming scannerd at {socket_display} → {db_display}");
        loop {
            match UnixStream::connect(&socket_path) {
                Ok(stream) => {
                    // A fresh connection: resync to recover any batch the
                    // previous connection produced but never committed.
                    serve_connection(stream, &mut conn, derive_cfg, &mut generation);
                    eprintln!("indexd: scannerd connection closed; reconnecting");
                }
                Err(e) => {
                    eprintln!("indexd: connect {socket_display} failed: {e}; retrying");
                }
            }
            sleep(RECONNECT_BACKOFF);
        }
    }

    /// Drive scan passes over one connection until it errors (which returns
    /// to the caller to reconnect). `generation` is threaded through so it
    /// stays monotonic across reconnects for the whole process lifetime.
    fn serve_connection(
        mut stream: UnixStream,
        conn: &mut rusqlite::Connection,
        derive_cfg: DeriveConfig,
        generation: &mut u64,
    ) {
        if let Err(e) = stream.set_read_timeout(Some(IO_TIMEOUT)) {
            eprintln!("indexd: set read timeout failed: {e}");
            return;
        }
        if let Err(e) = stream.set_write_timeout(Some(IO_TIMEOUT)) {
            eprintln!("indexd: set write timeout failed: {e}");
            return;
        }

        // First request on a new connection always resyncs.
        let mut resync = true;
        loop {
            *generation += 1;
            let want_generation = *generation;
            let request = Request::Scan {
                generation: want_generation,
                resync,
            };
            if let Err(e) = write_request(&mut stream, &request) {
                eprintln!("indexd: send request failed: {e}");
                return;
            }
            let batch = match read_batch(&mut stream) {
                Ok(b) => b,
                Err(e) => {
                    eprintln!("indexd: read batch failed: {e}");
                    return;
                }
            };
            // The server echoes our generation; a mismatch means the stream
            // desynced. Don't apply a batch we can't trust — drop the
            // connection and reconnect (the next connection resyncs).
            if batch.generation != want_generation {
                eprintln!(
                    "indexd: batch generation {} != requested {want_generation}; reconnecting",
                    batch.generation
                );
                return;
            }
            match apply(conn, &batch, derive_cfg) {
                Ok(report) => {
                    println!(
                        "indexd: pass gen {want_generation} — {} clips, {} front, {} waypoints, \
                         {} trips, {} events, {} pruned, {} errors",
                        report.clips_written,
                        report.front_walked,
                        report.waypoints,
                        report.trips,
                        report.events,
                        report.pruned,
                        report.record_errors,
                    );
                    // Committed: the next pass only needs newly-eligible clips.
                    resync = false;
                }
                Err(e) => {
                    eprintln!("indexd: apply failed (gen {want_generation}): {e}");
                    // The batch advanced scannerd's tracker but did not commit
                    // here; replay the full stable set next pass to recover it.
                    resync = true;
                }
            }
            sleep(Duration::from_secs(SCAN_INTERVAL_SECS));
        }
    }
}
