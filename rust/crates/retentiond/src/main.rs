//! `retentiond` binary entrypoint.
//!
//! Policy decisions live in the library crate; this binary wires CLI parsing and
//! the unix-only live archive-recent driver loop.

// systemd captures this binary's stdout/stderr into the journal, so direct
// console output is the intended logging path for the daemon entrypoint only.
#![allow(clippy::print_stdout, clippy::print_stderr)]

#[cfg(unix)]
mod live;

use std::process::ExitCode;

#[cfg(unix)]
use std::path::PathBuf;
#[cfg(unix)]
use std::sync::atomic::{AtomicBool, Ordering};
#[cfg(unix)]
use std::thread;
#[cfg(unix)]
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[cfg(unix)]
use live::{LiveArchiveStore, LiveRecentDirReader};
#[cfg(unix)]
use retentiond::archive_driver::{DriverState, archive_recent_once};
#[cfg(unix)]
use retentiond::recent_facts::RecentFactsGatherer;
#[cfg(unix)]
use retentiond::register_client::{INDEXD_SOCKET_PATH, UnixRegisterClient};

#[cfg(unix)]
const DEFAULT_SLOT: u8 = 0;
#[cfg(unix)]
const DEFAULT_RECENTCLIPS_DIR: &str = "TeslaCam/RecentClips";
#[cfg(unix)]
const DEFAULT_INTERVAL_SECS: u64 = 20;
#[cfg(unix)]
const DEFAULT_REQUIRED_STABLE_PASSES: u32 = 2;

#[cfg(unix)]
static SHUTDOWN: AtomicBool = AtomicBool::new(false);

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
       --source-root <path> --archive-root <path> [--indexd-socket <path>] \\\n\
       [--slot <u8>] [--recentclips-dir <path>] [--interval-secs <u64>] \\\n\
       [--required-stable-passes <u32>]\n\
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
    let Some(source_root) = parsed.source_root.clone() else {
        eprintln!("retentiond serve: missing required --source-root <path>.");
        return ExitCode::FAILURE;
    };
    let Some(archive_root) = parsed.archive_root.clone() else {
        eprintln!("retentiond serve: missing required --archive-root <path>.");
        return ExitCode::FAILURE;
    };

    install_shutdown_handlers();

    let mut gatherer = RecentFactsGatherer::new(parsed.required_stable_passes);
    let store = LiveArchiveStore::new(&source_root, &archive_root);
    let register = UnixRegisterClient::new(&parsed.indexd_socket);
    let recentclips_dir = parsed.recentclips_dir.clone();
    let reader = LiveRecentDirReader::new(&source_root, &recentclips_dir, parsed.slot);
    let mut state = DriverState::new();

    while !SHUTDOWN.load(Ordering::Relaxed) {
        let now_epoch_s = now_epoch_s_saturating();
        match archive_recent_once(
            &mut gatherer,
            parsed.slot,
            &recentclips_dir,
            &reader,
            &store,
            &register,
            &mut state,
            now_epoch_s,
        ) {
            Ok(report) => {
                let has_activity = report.observed > 0
                    || report.registered > 0
                    || report.registered_from_pending > 0
                    || report.copy_failed > 0
                    || report.register_deferred > 0
                    || report.skipped_already_pending > 0
                    || report.dropped_poison > 0
                    || report.pending_len > 0;
                if has_activity {
                    println!(
                        "retentiond archive_recent_only slot={} observed={} registered={} \
                         registered_from_pending={} copy_failed={} register_deferred={} \
                         skipped_already_pending={} dropped_poison={} pending={}",
                        parsed.slot,
                        report.observed,
                        report.registered,
                        report.registered_from_pending,
                        report.copy_failed,
                        report.register_deferred,
                        report.skipped_already_pending,
                        report.dropped_poison,
                        report.pending_len
                    );
                }
            }
            Err(err) => eprintln!("retentiond archive_recent_only: cycle error: {err}"),
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
#[derive(Debug, Clone)]
struct ServeArgs {
    archive_recent_only: bool,
    no_delete: bool,
    source_root: Option<PathBuf>,
    archive_root: Option<PathBuf>,
    indexd_socket: PathBuf,
    slot: u8,
    recentclips_dir: String,
    interval_secs: u64,
    required_stable_passes: u32,
}

#[cfg(unix)]
impl Default for ServeArgs {
    fn default() -> Self {
        Self {
            archive_recent_only: false,
            no_delete: false,
            source_root: None,
            archive_root: None,
            indexd_socket: PathBuf::from(INDEXD_SOCKET_PATH),
            slot: DEFAULT_SLOT,
            recentclips_dir: DEFAULT_RECENTCLIPS_DIR.to_owned(),
            interval_secs: DEFAULT_INTERVAL_SECS,
            required_stable_passes: DEFAULT_REQUIRED_STABLE_PASSES,
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
            "--source-root" => {
                let value = next_arg_value(&mut iter, "--source-root")?;
                parsed.source_root = Some(PathBuf::from(value));
            }
            "--archive-root" => {
                let value = next_arg_value(&mut iter, "--archive-root")?;
                parsed.archive_root = Some(PathBuf::from(value));
            }
            "--indexd-socket" => {
                let value = next_arg_value(&mut iter, "--indexd-socket")?;
                parsed.indexd_socket = PathBuf::from(value);
            }
            "--slot" => {
                let value = next_arg_value(&mut iter, "--slot")?;
                parsed.slot = parse_arg::<u8>("--slot", &value)?;
            }
            "--recentclips-dir" => {
                parsed.recentclips_dir = next_arg_value(&mut iter, "--recentclips-dir")?;
            }
            "--interval-secs" => {
                let value = next_arg_value(&mut iter, "--interval-secs")?;
                parsed.interval_secs = parse_arg::<u64>("--interval-secs", &value)?;
            }
            "--required-stable-passes" => {
                let value = next_arg_value(&mut iter, "--required-stable-passes")?;
                parsed.required_stable_passes =
                    parse_arg::<u32>("--required-stable-passes", &value)?;
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
    // SAFETY: libc expects a C ABI function pointer. The handler only performs a
    // single atomic store, which is signal-safe.
    unsafe {
        let handler = shutdown_signal_handler as libc::sighandler_t;
        let _ = libc::signal(libc::SIGTERM, handler);
        let _ = libc::signal(libc::SIGINT, handler);
    }
}

#[cfg(all(test, unix))]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::parse_serve_args;

    #[test]
    fn parse_serve_args_rejects_zero_interval_secs() {
        let args = vec!["--interval-secs".to_owned(), "0".to_owned()];
        let err = parse_serve_args(&args).err();
        assert!(err.is_some(), "zero interval should be rejected");
        assert!(
            err.as_deref().is_some_and(|message| message.contains("--interval-secs")),
            "error should mention the flag: {err:?}"
        );
    }
}
