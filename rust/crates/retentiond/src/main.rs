//! `retentiond` binary entrypoint.
//!
//! Policy decisions live in the library crate; this binary wires CLI parsing and
//! the unix-only live archive-recent driver loop.

#![allow(clippy::print_stdout, clippy::print_stderr)]

#[cfg(unix)]
mod live;

use std::process::ExitCode;

#[cfg(unix)]
use std::path::{Path, PathBuf};
#[cfg(unix)]
use std::sync::atomic::{AtomicBool, Ordering};
#[cfg(unix)]
use std::thread;
#[cfg(unix)]
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[cfg(unix)]
use live::LiveArchiveStore;
#[cfg(unix)]
use serde::Serialize;
#[cfg(unix)]
use retentiond::archive_driver::{DriverState, archive_recent_once};
#[cfg(unix)]
use retentiond::candidates::SqliteCandidateReader;
#[cfg(unix)]
use retentiond::read_client::{SCANNERD_READ_SOCKET_PATH, UnixReadFileClient};
#[cfg(unix)]
use retentiond::register_client::{INDEXD_SOCKET_PATH, UnixRegisterClient};

#[cfg(unix)]
const DEFAULT_SLOT: u8 = 0;
#[cfg(unix)]
const DEFAULT_INTERVAL_SECS: u64 = 20;
#[cfg(unix)]
const DEFAULT_INDEXD_DB_PATH: &str = "/var/lib/teslausb/index.sqlite3";
#[cfg(unix)]
const DEFAULT_HEALTH_FILE: &str = "/run/teslausb/retentiond.health.json";

#[cfg(unix)]
static SHUTDOWN: AtomicBool = AtomicBool::new(false);

#[cfg(unix)]
#[derive(Debug, Serialize)]
struct HealthHeartbeat {
    schema: u32,
    updated_at: i64,
    running: bool,
    pending: u64,
    last_progress_at: i64,
}

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
        Some("serve") => run_serve(args.get(1..).unwrap_or(&[])),
        Some(other) => {
            eprintln!("retentiond: unknown command `{other}`\n{}", usage());
            ExitCode::FAILURE
        }
    }
}

fn usage() -> String {
    "usage: retentiond <version|serve|help>\n\
     serve mode (phase-1): retentiond serve --archive-recent-only --no-delete \\\n\
       --archive-root <path> [--indexd-db <path>] [--scannerd-read-socket <path>] \\\n\
       [--indexd-socket <path>] [--slot <u8>] [--interval-secs <u64>]\n\
     note: this build only supports non-destructive archive-recent-only serve mode."
        .to_owned()
}

#[cfg(not(unix))]
fn run_serve(_args: &[String]) -> ExitCode {
    eprintln!("retentiond serve: live archive-recent-only mode is only supported on unix.");
    ExitCode::FAILURE
}

#[cfg(unix)]
fn run_serve(args: &[String]) -> ExitCode {
    let parsed = match parse_serve_args(args) {
        Ok(parsed) => parsed,
        Err(message) => {
            eprintln!("{message}");
            return ExitCode::FAILURE;
        }
    };

    if !parsed.archive_recent_only {
        eprintln!(
            "retentiond serve: only --archive-recent-only mode is supported in this build \
             (phase-1 non-destructive)."
        );
        return ExitCode::FAILURE;
    }
    if !parsed.no_delete {
        eprintln!("retentiond serve: phase-1 requires --no-delete.");
        return ExitCode::FAILURE;
    }
    let Some(archive_root) = parsed.archive_root.clone() else {
        eprintln!("retentiond serve: missing required --archive-root <path>.");
        return ExitCode::FAILURE;
    };

    let candidates = match SqliteCandidateReader::open(&parsed.indexd_db) {
        Ok(reader) => reader,
        Err(err) => {
            eprintln!(
                "retentiond serve: cannot open indexd DB read-only at {}: {err}",
                parsed.indexd_db.display()
            );
            return ExitCode::FAILURE;
        }
    };

    install_shutdown_handlers();

    let store = LiveArchiveStore::new(
        Box::new(UnixReadFileClient::new(&parsed.scannerd_read_socket)),
        &archive_root,
    );
    let register = UnixRegisterClient::new(&parsed.indexd_socket);
    let mut state = DriverState::new();
    let health_file = std::env::var_os("RETENTIOND_HEALTH_FILE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(DEFAULT_HEALTH_FILE));
    let startup_now = now_epoch_s_saturating();
    let mut last_progress_at = startup_now;
    let mut last_pending: u64 = 0;
    let mut health_write_error_logged = false;
    write_health_heartbeat_best_effort(
        &health_file,
        startup_now,
        last_pending,
        last_progress_at,
        &mut health_write_error_logged,
    );

    while !SHUTDOWN.load(Ordering::Relaxed) {
        let now_epoch_s = now_epoch_s_saturating();
        match archive_recent_once(&candidates, &store, &register, &mut state, now_epoch_s) {
            Ok(report) => {
                let has_activity = report.observed > 0
                    || report.registered > 0
                    || report.registered_from_pending > 0
                    || report.copy_failed > 0
                    || report.register_deferred > 0
                    || report.register_rejected > 0
                    || report.quarantined_undecodable > 0
                    || report.skipped_already_pending > 0
                    || report.skipped_rejected > 0
                    || report.dropped_poison > 0
                    || report.pending_len > 0;
                if has_activity {
                    println!(
                        "retentiond archive_recent_only slot={} observed={} registered={} \
                         registered_from_pending={} copy_failed={} register_deferred={} \
                         register_rejected={} quarantined_undecodable={} \
                         skipped_already_pending={} skipped_rejected={} dropped_poison={} \
                         pending={}",
                        parsed.slot,
                        report.observed,
                        report.registered,
                        report.registered_from_pending,
                        report.copy_failed,
                        report.register_deferred,
                        report.register_rejected,
                        report.quarantined_undecodable,
                        report.skipped_already_pending,
                        report.skipped_rejected,
                        report.dropped_poison,
                        report.pending_len
                    );
                }
                if report.observed > 0 || report.registered > 0 || report.registered_from_pending > 0
                {
                    last_progress_at = now_epoch_s;
                }
                last_pending = u64::try_from(report.pending_len).unwrap_or(u64::MAX);
                write_health_heartbeat_best_effort(
                    &health_file,
                    now_epoch_s,
                    last_pending,
                    last_progress_at,
                    &mut health_write_error_logged,
                );
            }
            Err(err) => {
                // Intentionally do NOT refresh the heartbeat on a failed cycle: a
                // worker that errors every loop must not keep reporting a fresh
                // "Idle, queue empty" / "{n} pending" status. Leaving updated_at
                // frozen lets webd age it into "stale" then "Worker not running",
                // which is the whole point of this health signal.
                eprintln!("retentiond archive_recent_only: cycle error: {err}");
            }
        }
        sleep_interruptible(parsed.interval_secs);
    }

    ExitCode::SUCCESS
}

#[cfg(unix)]
fn sleep_interruptible(interval_secs: u64) {
    for _ in 0..interval_secs {
        if SHUTDOWN.load(Ordering::Relaxed) {
            break;
        }
        thread::sleep(Duration::from_secs(1));
    }
}

#[cfg(unix)]
fn now_epoch_s_saturating() -> i64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(duration) => i64::try_from(duration.as_secs()).unwrap_or(i64::MAX),
        Err(_) => 0,
    }
}

#[cfg(unix)]
fn render_health(now: i64, pending: u64, last_progress_at: i64) -> String {
    let heartbeat = HealthHeartbeat {
        schema: 1,
        updated_at: now,
        running: true,
        pending,
        last_progress_at,
    };
    serde_json::to_string(&heartbeat).unwrap_or_else(|_| {
        format!(
            "{{\"schema\":1,\"updated_at\":{now},\"running\":true,\"pending\":{pending},\"last_progress_at\":{last_progress_at}}}"
        )
    })
}

#[cfg(unix)]
fn write_health_heartbeat_atomic(path: &Path, body: &str) -> std::io::Result<()> {
    let mut tmp = path.as_os_str().to_os_string();
    tmp.push(".tmp");
    let tmp_path = PathBuf::from(tmp);
    std::fs::write(&tmp_path, body)?;
    std::fs::rename(&tmp_path, path)?;
    Ok(())
}

#[cfg(unix)]
fn write_health_heartbeat_best_effort(
    path: &Path,
    now: i64,
    pending: u64,
    last_progress_at: i64,
    write_error_logged: &mut bool,
) {
    let body = render_health(now, pending, last_progress_at);
    if let Err(err) = write_health_heartbeat_atomic(path, &body) {
        if !*write_error_logged {
            eprintln!(
                "retentiond archive_recent_only: health heartbeat write failed at {}: {err}",
                path.display()
            );
            *write_error_logged = true;
        }
    }
}

#[cfg(unix)]
#[derive(Debug, Clone)]
struct ServeArgs {
    archive_recent_only: bool,
    no_delete: bool,
    archive_root: Option<PathBuf>,
    indexd_db: PathBuf,
    scannerd_read_socket: PathBuf,
    indexd_socket: PathBuf,
    slot: u8,
    interval_secs: u64,
}

#[cfg(unix)]
impl Default for ServeArgs {
    fn default() -> Self {
        Self {
            archive_recent_only: false,
            no_delete: false,
            archive_root: None,
            indexd_db: PathBuf::from(DEFAULT_INDEXD_DB_PATH),
            scannerd_read_socket: PathBuf::from(SCANNERD_READ_SOCKET_PATH),
            indexd_socket: PathBuf::from(INDEXD_SOCKET_PATH),
            slot: DEFAULT_SLOT,
            interval_secs: DEFAULT_INTERVAL_SECS,
        }
    }
}

#[cfg(unix)]
fn parse_serve_args(args: &[String]) -> Result<ServeArgs, String> {
    let mut parsed = ServeArgs::default();
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--archive-recent-only" => parsed.archive_recent_only = true,
            "--no-delete" => parsed.no_delete = true,
            "--archive-root" => {
                let value = next_arg_value(&mut iter, "--archive-root")?;
                parsed.archive_root = Some(PathBuf::from(value));
            }
            "--indexd-db" => {
                let value = next_arg_value(&mut iter, "--indexd-db")?;
                parsed.indexd_db = PathBuf::from(value);
            }
            "--scannerd-read-socket" => {
                let value = next_arg_value(&mut iter, "--scannerd-read-socket")?;
                parsed.scannerd_read_socket = PathBuf::from(value);
            }
            "--indexd-socket" => {
                let value = next_arg_value(&mut iter, "--indexd-socket")?;
                parsed.indexd_socket = PathBuf::from(value);
            }
            "--slot" => {
                let value = next_arg_value(&mut iter, "--slot")?;
                parsed.slot = parse_arg::<u8>("--slot", &value)?;
            }
            "--interval-secs" => {
                let value = next_arg_value(&mut iter, "--interval-secs")?;
                parsed.interval_secs = parse_arg::<u64>("--interval-secs", &value)?;
            }
            other => return Err(format!("retentiond serve: unknown option `{other}`.\n{}", usage())),
        }
    }
    if parsed.interval_secs == 0 {
        return Err("retentiond serve: --interval-secs must be greater than 0.".to_owned());
    }
    Ok(parsed)
}

#[cfg(unix)]
fn next_arg_value(iter: &mut std::slice::Iter<'_, String>, flag: &str) -> Result<String, String> {
    iter.next()
        .cloned()
        .ok_or_else(|| format!("retentiond serve: missing value for {flag}."))
}

#[cfg(unix)]
fn parse_arg<T>(flag: &str, value: &str) -> Result<T, String>
where
    T: std::str::FromStr,
    <T as std::str::FromStr>::Err: std::fmt::Display,
{
    value
        .parse::<T>()
        .map_err(|err| format!("retentiond serve: invalid {flag} `{value}`: {err}"))
}

#[cfg(unix)]
extern "C" fn shutdown_signal_handler(_signal: libc::c_int) {
    SHUTDOWN.store(true, Ordering::Relaxed);
}

#[cfg(unix)]
#[allow(unsafe_code)]
fn install_shutdown_handlers() {
    SHUTDOWN.store(false, Ordering::Relaxed);
    unsafe {
        let handler = shutdown_signal_handler as libc::sighandler_t;
        let _ = libc::signal(libc::SIGTERM, handler);
        let _ = libc::signal(libc::SIGINT, handler);
    }
}

#[cfg(all(test, unix))]
#[allow(clippy::unwrap_used, clippy::panic)]
mod tests {
    use super::{parse_serve_args, render_health};

    #[test]
    fn parse_serve_args_rejects_zero_interval_secs() {
        let args = vec!["--interval-secs".to_owned(), "0".to_owned()];
        let err = parse_serve_args(&args).err();
        assert!(err.is_some());
        assert!(
            err.as_deref()
                .is_some_and(|message| message.contains("--interval-secs"))
        );
    }

    #[test]
    fn parse_serve_args_parses_new_phase1_flags() {
        let args = vec![
            "--archive-recent-only".to_owned(),
            "--no-delete".to_owned(),
            "--archive-root".to_owned(),
            "/data/teslausb/archive".to_owned(),
            "--indexd-db".to_owned(),
            "/var/lib/teslausb/index.sqlite3".to_owned(),
            "--scannerd-read-socket".to_owned(),
            "/run/teslausb/scannerd-read.sock".to_owned(),
        ];
        let parsed = match parse_serve_args(&args) {
            Ok(parsed) => parsed,
            Err(err) => panic!("parse args: {err}"),
        };
        assert!(parsed.archive_recent_only);
        assert!(parsed.no_delete);
        assert_eq!(
            parsed.archive_root.as_deref().and_then(std::path::Path::to_str),
            Some("/data/teslausb/archive")
        );
        assert_eq!(
            parsed.indexd_db.to_str(),
            Some("/var/lib/teslausb/index.sqlite3")
        );
        assert_eq!(
            parsed.scannerd_read_socket.to_str(),
            Some("/run/teslausb/scannerd-read.sock")
        );
    }

    #[test]
    fn render_health_serializes_expected_fields() {
        let raw = render_health(1234, 42, 1200);
        let value: serde_json::Value = match serde_json::from_str(&raw) {
            Ok(value) => value,
            Err(err) => panic!("render_health should produce valid json: {err}"),
        };
        assert_eq!(value["schema"], 1);
        assert_eq!(value["updated_at"], 1234);
        assert_eq!(value["running"], true);
        assert_eq!(value["pending"], 42);
        assert_eq!(value["last_progress_at"], 1200);
    }
}
