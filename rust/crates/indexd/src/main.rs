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
//! The client owns the **30 s cadence** and a **monotonic generation**. It
//! requests a census-only pass after connect/apply-failure, then sends a
//! durable front-shape worklist selected from `front_parse_attempts` +
//! `front_census`.
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
    use std::collections::{HashMap, HashSet};
    use std::os::unix::net::UnixStream;
    use std::path::{Path, PathBuf};
    use std::sync::{Arc, Mutex};
    use std::thread::sleep;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use indexd::apply::apply;
    use indexd::db::ingest::{FrontParseAttemptRow, load_front_parse_attempts};
    use indexd::db::mutations::BootContext;
    use indexd::db::{DbError, open};
    use indexd::derive::{self, DeriveConfig};
    use indexd::server;
    use scannerd::produce::MAX_FRONT_SHAPES_PER_BATCH;
    use scannerd::proto::{Request, read_batch, write_request};
    use scannerd::record::{FrontCensusRecord, PARSER_VERSION};
    use scannerd::timestamp::epoch_from_tesla_timestamp;
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
    const SCANNER_IO_TIMEOUT: Duration = Duration::from_secs(60);
    const CONTROL_IO_TIMEOUT: Duration = Duration::from_secs(15);
    const OLDEST_BACKLOG_RESERVE: usize = 2;

    #[derive(Debug, Clone)]
    struct ShapeCandidate {
        key: String,
        ts: i64,
        nonterminal_backlog: bool,
    }

    fn parse_fingerprint_hex(value: Option<&str>) -> Option<u64> {
        value.and_then(|v| u64::from_str_radix(v, 16).ok())
    }

    fn canonical_key_timestamp(key: &str) -> i64 {
        key.rsplit('/')
            .next()
            .and_then(epoch_from_tesla_timestamp)
            .unwrap_or(0)
    }

    fn is_terminal_state(parse_state: &str) -> bool {
        matches!(
            parse_state,
            "parsed_with_waypoints" | "no_waypoints" | "encrypted"
        )
    }

    fn is_nonterminal_state(parse_state: &str) -> bool {
        !is_terminal_state(parse_state)
    }

    fn terminal_refresh_needed(
        parser_version: Option<i64>,
        parse_fingerprint: Option<&str>,
        front_fingerprint: u64,
    ) -> bool {
        parser_version.is_some_and(|version| version < PARSER_VERSION)
            || parse_fingerprint_hex(parse_fingerprint)
                .is_some_and(|stored| stored != front_fingerprint)
    }

    fn should_shape_front(
        census: &FrontCensusRecord,
        row: Option<&FrontParseAttemptRow>,
        now: i64,
    ) -> Option<bool> {
        if !census.front_stable {
            return None;
        }
        let Some((parse_state, parse_fingerprint, parser_version, _attempt_count, next_retry_at)) =
            row
        else {
            return Some(false);
        };
        if matches!(
            parse_state.as_str(),
            "parsed_with_waypoints" | "no_waypoints" | "encrypted"
        ) {
            if terminal_refresh_needed(
                *parser_version,
                parse_fingerprint.as_deref(),
                census.front_fingerprint,
            ) {
                return Some(false);
            }
            return None;
        }
        let fingerprint_changed = parse_fingerprint
            .as_deref()
            .and_then(|value| u64::from_str_radix(value, 16).ok())
            .is_some_and(|stored| stored != census.front_fingerprint);
        if fingerprint_changed {
            return Some(true);
        }
        if (*next_retry_at).is_none_or(|retry_at| now >= retry_at) {
            return Some(is_nonterminal_state(parse_state));
        }
        None
    }

    pub(crate) fn select_shape_keys(
        census: &[FrontCensusRecord],
        attempts: &HashMap<String, FrontParseAttemptRow>,
        now: i64,
    ) -> Vec<String> {
        let mut candidates: Vec<ShapeCandidate> = Vec::new();
        for front in census {
            let Some(nonterminal_backlog) =
                should_shape_front(front, attempts.get(front.canonical_key.as_str()), now)
            else {
                continue;
            };
            candidates.push(ShapeCandidate {
                key: front.canonical_key.clone(),
                ts: canonical_key_timestamp(&front.canonical_key),
                nonterminal_backlog,
            });
        }
        candidates.sort_by(|a, b| b.ts.cmp(&a.ts).then_with(|| a.key.cmp(&b.key)));
        let cap = MAX_FRONT_SHAPES_PER_BATCH;
        if candidates.len() <= cap {
            return candidates
                .into_iter()
                .map(|candidate| candidate.key)
                .collect();
        }

        let mut selected = Vec::with_capacity(cap);
        let mut selected_set: HashSet<String> = HashSet::new();
        let mut backlog_oldest: Vec<&ShapeCandidate> = candidates
            .iter()
            .filter(|candidate| candidate.nonterminal_backlog)
            .collect();
        backlog_oldest.sort_by(|a, b| a.ts.cmp(&b.ts).then_with(|| a.key.cmp(&b.key)));
        for candidate in backlog_oldest
            .into_iter()
            .take(OLDEST_BACKLOG_RESERVE.min(cap))
        {
            if selected_set.insert(candidate.key.clone()) {
                selected.push(candidate.key.clone());
            }
        }
        for candidate in candidates {
            if selected.len() >= cap {
                break;
            }
            if selected_set.insert(candidate.key.clone()) {
                selected.push(candidate.key);
            }
        }
        selected
    }

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
            .map_or_else(|| PathBuf::from(DEFAULT_HEALTH_FILE), PathBuf::from);
        let db_display = db_path.display().to_string();
        let conn = open(&db_path).map_err(|e: DbError| format!("opening {db_display}: {e}"))?;
        let conn = Arc::new(Mutex::new(conn));

        // Single-writer hygiene: reap leases stranded by a previous boot.
        let boot = Arc::new(BootContext::new());
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

        let _server_thread =
            server::spawn(&conn, &boot, &indexd_socket_path, CONTROL_IO_TIMEOUT)
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
                    // A fresh connection starts with a census-only pass.
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
        if let Err(e) = stream.set_read_timeout(Some(SCANNER_IO_TIMEOUT)) {
            eprintln!("indexd: set read timeout failed: {e}");
            return;
        }
        if let Err(e) = stream.set_write_timeout(Some(SCANNER_IO_TIMEOUT)) {
            eprintln!("indexd: set write timeout failed: {e}");
            return;
        }

        // First pass after connect is census-only.
        let mut next_shape: Vec<String> = Vec::new();
        loop {
            *generation += 1;
            let want_generation = *generation;
            let request = Request::Scan {
                generation: want_generation,
                shape: next_shape.clone(),
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
            // connection and reconnect.
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
            let (apply_result, selected_shape) = {
                let Ok(mut locked) = conn.lock() else {
                    eprintln!("indexd: database mutex poisoned");
                    return;
                };
                let effective_cfg = derive::load_derive_config(&locked, derive_cfg);
                let result = apply(&mut locked, &batch, effective_cfg);
                let next = if result.is_ok() {
                    match load_front_parse_attempts(&locked) {
                        Ok(attempts) => {
                            let now = now_epoch_s_saturating();
                            Some(select_shape_keys(&batch.front_census, &attempts, now))
                        }
                        Err(err) => {
                            eprintln!("indexd: loading front_parse_attempts failed: {err}");
                            Some(Vec::new())
                        }
                    }
                } else {
                    None
                };
                (result, next)
            };
            match apply_result {
                Ok(report) => {
                    next_shape = selected_shape.unwrap_or_default();
                    println!(
                        "indexd: pass gen {want_generation} — {} clips, {} front, {} waypoints, \
                         {} trips, {} events, {} pruned, {} errors, {} front-parse-errors, \
                         {} front-read-errors, {} front-no-waypoints",
                        report.clips_written,
                        report.front_walked,
                        report.waypoints,
                        report.trips,
                        report.events,
                        report.pruned,
                        report.record_errors,
                        report.front_parse_errors,
                        report.front_read_errors,
                        report.front_no_waypoints,
                    );
                    write_health_heartbeat_best_effort(
                        health_path,
                        now_epoch_s_saturating(),
                        health_write_error_logged,
                    );
                }
                Err(e) => {
                    eprintln!("indexd: apply failed (gen {want_generation}): {e}");
                    next_shape.clear();
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
    use std::collections::HashMap;

    use scannerd::record::{FrontCensusRecord, PARSER_VERSION};

    use super::unix_app::{render_indexd_health, select_shape_keys};

    #[test]
    fn render_indexd_health_serializes_expected_fields() {
        let raw = render_indexd_health(1234);
        let value: serde_json::Value = match serde_json::from_str(&raw) {
            Ok(value) => value,
            Err(err) => panic!("render_indexd_health should produce valid json: {err}"),
        };
        assert_eq!(
            value.get("schema").and_then(serde_json::Value::as_i64),
            Some(1)
        );
        assert_eq!(
            value.get("updated_at").and_then(serde_json::Value::as_i64),
            Some(1234)
        );
        assert_eq!(
            value.get("running").and_then(serde_json::Value::as_bool),
            Some(true)
        );
    }

    #[test]
    fn day1_terminal_row_with_null_version_and_fingerprint_is_skipped() {
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let census = vec![FrontCensusRecord {
            canonical_key: key.to_owned(),
            front_fingerprint: 0x1234,
            front_stable: true,
        }];
        let mut attempts = HashMap::new();
        attempts.insert(
            key.to_owned(),
            ("parsed_with_waypoints".to_owned(), None, None, 0, None),
        );
        let shape = select_shape_keys(&census, &attempts, 0);
        assert!(shape.is_empty());
    }

    #[test]
    fn frontless_clip_is_never_selected_until_front_census_exists() {
        // No front census entry => clip has no front angle this pass.
        let shape = select_shape_keys(&[], &HashMap::new(), 0);
        assert!(shape.is_empty());

        // Front appears and is stable on next pass => selected.
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let census = vec![FrontCensusRecord {
            canonical_key: key.to_owned(),
            front_fingerprint: 0x1234,
            front_stable: true,
        }];
        let shape = select_shape_keys(&census, &HashMap::new(), 0);
        assert_eq!(shape, vec![key.to_owned()]);
    }

    #[test]
    fn parse_error_selection_respects_backoff_and_fingerprint_reset() {
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let census = vec![FrontCensusRecord {
            canonical_key: key.to_owned(),
            front_fingerprint: 0xaaa,
            front_stable: true,
        }];
        let mut attempts = HashMap::new();
        attempts.insert(
            key.to_owned(),
            (
                "read_error".to_owned(),
                Some("aaa".to_owned()),
                Some(PARSER_VERSION),
                3,
                Some(500),
            ),
        );
        assert!(
            select_shape_keys(&census, &attempts, 400).is_empty(),
            "must skip while backoff is active"
        );
        assert_eq!(
            select_shape_keys(&census, &attempts, 500),
            vec![key.to_owned()],
            "must select once retry window opens"
        );

        // Fingerprint changed while still backed off => select immediately.
        let changed = vec![FrontCensusRecord {
            canonical_key: key.to_owned(),
            front_fingerprint: 0xbbb,
            front_stable: true,
        }];
        assert_eq!(
            select_shape_keys(&changed, &attempts, 400),
            vec![key.to_owned()]
        );
    }

    #[test]
    fn unplaceable_front_with_future_retry_is_not_selected() {
        let key = "0:TeslaCam/SavedClips/2026-13-99_00-00-00/2026-13-99_00-00-00";
        let census = vec![FrontCensusRecord {
            canonical_key: key.to_owned(),
            front_fingerprint: 0xabc,
            front_stable: true,
        }];
        let mut attempts = HashMap::new();
        attempts.insert(
            key.to_owned(),
            (
                "parse_error".to_owned(),
                Some("abc".to_owned()),
                None,
                1,
                Some(500),
            ),
        );
        assert!(
            !select_shape_keys(&census, &attempts, 400).contains(&key.to_owned()),
            "retry backoff must suppress re-selection until next_retry_at"
        );
    }

    #[test]
    fn encrypted_front_state_is_terminal_and_not_retried() {
        let key = "0:TeslaCam/EncryptedClips/RecentClips/2026-06-01_20-10-35/2026-06-01_20-10-35";
        let census = vec![FrontCensusRecord {
            canonical_key: key.to_owned(),
            front_fingerprint: 0xabc,
            front_stable: true,
        }];
        let mut attempts = HashMap::new();
        attempts.insert(
            key.to_owned(),
            (
                "encrypted".to_owned(),
                Some("abc".to_owned()),
                Some(PARSER_VERSION),
                0,
                None,
            ),
        );
        assert!(
            !select_shape_keys(&census, &attempts, 400).contains(&key.to_owned()),
            "terminal encrypted state must not be selected for retry"
        );
    }

    #[test]
    fn newest_first_selection_reserves_oldest_nonterminal_backlog_slots() {
        let mut census = Vec::new();
        let mut attempts = HashMap::new();
        for i in 0..10 {
            let ts = format!("2026-06-01_20-10-{i:02}");
            let key = format!("0:TeslaCam/SavedClips/{ts}/{ts}");
            census.push(FrontCensusRecord {
                canonical_key: key.clone(),
                front_fingerprint: i,
                front_stable: true,
            });
            attempts.insert(
                key,
                (
                    "parse_error".to_owned(),
                    Some(format!("{i:x}")),
                    Some(PARSER_VERSION),
                    1,
                    None,
                ),
            );
        }
        let shape = select_shape_keys(&census, &attempts, 0);
        assert_eq!(shape.len(), scannerd::produce::MAX_FRONT_SHAPES_PER_BATCH);
        assert!(shape.iter().any(|k| k.contains("2026-06-01_20-10-00")));
        assert!(shape.iter().any(|k| k.contains("2026-06-01_20-10-01")));
    }
}
