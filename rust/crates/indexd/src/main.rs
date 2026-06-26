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
    use std::path::{Path, PathBuf};
    use std::sync::{Arc, Mutex};
    use std::thread::sleep;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use indexd::apply::apply;
    use indexd::db::mutations::BootContext;
    use indexd::db::{DbError, open};
    use indexd::derive::DeriveConfig;
    use indexd::server;
    use scannerd::proto::{Request, read_batch, write_request};
    use serde::Serialize;

    /// Default on-Pi DB path. ext4, Pi-side — NEVER inside `disk.img` / the
    /// Tesla volume (SPEC §6.1 #1 invariant).
    const DEFAULT_DB_PATH: &str = "/var/lib/teslausb/index.sqlite3";

    /// Default `scannerd` control-socket path (matches `scannerd serve`).
    const DEFAULT_SCANNERD_SOCKET: &str = "/run/teslausb/scannerd.sock";

    /// Default `indexd` control-socket path (`retentiond` registration RPC).
    const DEFAULT_INDEXD_SOCKET: &str = "/run/teslausb/indexd.sock";
    const DEFAULT_HEALTH_FILE: &str = "/run/teslausb/indexd.health.json";

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

    #[derive(Debug, Serialize)]
    struct HealthHeartbeat {
        schema: u32,
        updated_at: i64,
        running: bool,
    }

    fn now_epoch_s_saturating() -> i64 {
        match SystemTime::now().duration_since(UNIX_EPOCH) {
            Ok(duration) => i64::try_from(duration.as_secs()).unwrap_or(i64::MAX),
            Err(_) => 0,
        }
    }

    pub(crate) fn render_indexd_health(now: i64) -> String {
        let heartbeat = HealthHeartbeat {
            schema: 1,
            updated_at: now,
            running: true,
        };
        serde_json::to_string(&heartbeat)
            .unwrap_or_else(|_| format!("{{\"schema\":1,\"updated_at\":{now},\"running\":true}}"))
    }

    fn write_health_heartbeat_atomic(path: &Path, body: &str) -> std::io::Result<()> {
        let mut tmp = path.as_os_str().to_os_string();
        tmp.push(".tmp");
        let tmp_path = PathBuf::from(tmp);
        std::fs::write(&tmp_path, body)?;
        std::fs::rename(&tmp_path, path)?;
        Ok(())
    }

    fn write_health_heartbeat_best_effort(path: &Path, now: i64, write_error_logged: &mut bool) {
        let body = render_indexd_health(now);
        if let Err(err) = write_health_heartbeat_atomic(path, &body) {
            if !*write_error_logged {
                eprintln!(
                    "indexd: health heartbeat write failed at {}: {err}",
                    path.display()
                );
                *write_error_logged = true;
            }
        }
    }

    /// Resolve config from args/env: `argv[1]` (or `INDEXD_DB`) = DB path;
    /// `argv[2]` (or `INDEXD_SCANNERD_SOCKET`) = `scannerd` socket path;
    /// `argv[3]` (or `INDEXD_SOCKET`) = `indexd` socket path.
    fn resolve_paths() -> (PathBuf, PathBuf, PathBuf) {
        let mut args = std::env::args().skip(1);
        let db = args
            .next()
            .or_else(|| std::env::var("INDEXD_DB").ok())
            .unwrap_or_else(|| DEFAULT_DB_PATH.to_owned());
        let scannerd_socket = args
            .next()
            .or_else(|| std::env::var("INDEXD_SCANNERD_SOCKET").ok())
            .unwrap_or_else(|| DEFAULT_SCANNERD_SOCKET.to_owned());
        let indexd_socket = args
            .next()
            .or_else(|| std::env::var("INDEXD_SOCKET").ok())
            .unwrap_or_else(|| DEFAULT_INDEXD_SOCKET.to_owned());
        (
            PathBuf::from(db),
            PathBuf::from(scannerd_socket),
            PathBuf::from(indexd_socket),
        )
    }

    /// Open the DB, reap stale leases, then run the connect/scan loop
    /// forever (the loop only returns on a fatal, non-recoverable error).
    pub fn run() -> Result<(), String> {
        let (db_path, scannerd_socket_path, indexd_socket_path) = resolve_paths();
        let health_file = std::env::var_os("INDEXD_HEALTH_FILE")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(DEFAULT_HEALTH_FILE));
        let db_display = db_path.display().to_string();
        let conn = open(&db_path).map_err(|e: DbError| format!("opening {db_display}: {e}"))?;
        let conn = Arc::new(Mutex::new(conn));

        // Single-writer hygiene: reap leases stranded by a previous boot.
        let boot = BootContext::new();
        let reaped = {
            let locked = conn
                .lock()
                .map_err(|_| "index database mutex is poisoned".to_owned())?;
            boot.reap(&locked)
                .map_err(|e| format!("reaping stale leases: {e}"))?
        };
        println!(
            "indexd: boot {} ; reaped {reaped} stale lease(s)",
            boot.boot_id()
        );

        let _server_thread = server::spawn(&conn, &indexd_socket_path, IO_TIMEOUT)
            .map_err(|e| format!("binding {}: {e}", indexd_socket_path.display()))?;

        let socket_display = scannerd_socket_path.display().to_string();
        let derive_cfg = DeriveConfig::default();
        let mut generation: u64 = 0;
        let mut health_write_error_logged = false;
        write_health_heartbeat_best_effort(
            &health_file,
            now_epoch_s_saturating(),
            &mut health_write_error_logged,
        );

        println!("indexd: consuming scannerd at {socket_display} → {db_display}");
        loop {
            write_health_heartbeat_best_effort(
                &health_file,
                now_epoch_s_saturating(),
                &mut health_write_error_logged,
            );
            match UnixStream::connect(&scannerd_socket_path) {
                Ok(stream) => {
                    // A fresh connection: resync to recover any batch the
                    // previous connection produced but never committed.
                    serve_connection(
                        stream,
                        &conn,
                        derive_cfg,
                        &mut generation,
                        &health_file,
                        &mut health_write_error_logged,
                    );
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
        conn: &Arc<Mutex<rusqlite::Connection>>,
        derive_cfg: DeriveConfig,
        generation: &mut u64,
        health_path: &Path,
        health_write_error_logged: &mut bool,
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
            // Refresh liveness immediately before apply() too: this bounds the
            // no-heartbeat window to a single apply() call rather than
            // apply()+sleep+scan-read. (At this device's scale apply() is sub-
            // second; we deliberately do NOT run a separate timer thread, which
            // would keep the heartbeat fresh even through a genuinely hung apply
            // and so mask the very liveness failure this signal exists to catch.)
            write_health_heartbeat_best_effort(
                health_path,
                now_epoch_s_saturating(),
                health_write_error_logged,
            );
            let apply_result = {
                let Ok(mut locked) = conn.lock() else {
                    eprintln!("indexd: database mutex poisoned");
                    return;
                };
                apply(&mut locked, &batch, derive_cfg)
            };
            match apply_result {
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
                    write_health_heartbeat_best_effort(
                        health_path,
                        now_epoch_s_saturating(),
                        health_write_error_logged,
                    );
                }
                Err(e) => {
                    eprintln!("indexd: apply failed (gen {want_generation}): {e}");
                    // The batch advanced scannerd's tracker but did not commit
                    // here; replay the full stable set next pass to recover it.
                    resync = true;
                    write_health_heartbeat_best_effort(
                        health_path,
                        now_epoch_s_saturating(),
                        health_write_error_logged,
                    );
                }
            }
            sleep(Duration::from_secs(SCAN_INTERVAL_SECS));
        }
    }
}

#[cfg(all(test, unix))]
#[allow(clippy::unwrap_used, clippy::panic)]
mod tests {
    use super::unix_app::render_indexd_health;

    #[test]
    fn render_indexd_health_serializes_expected_fields() {
        let raw = render_indexd_health(1234);
        let value: serde_json::Value = match serde_json::from_str(&raw) {
            Ok(value) => value,
            Err(err) => panic!("render_indexd_health should produce valid json: {err}"),
        };
        assert_eq!(value["schema"], 1);
        assert_eq!(value["updated_at"], 1234);
        assert_eq!(value["running"], true);
    }
}
