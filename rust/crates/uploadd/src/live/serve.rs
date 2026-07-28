use std::io::Write as _;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};

use libc::c_int;

use crate::config::UploaddConfig;
use crate::indexd_client::{INDEXD_SOCKET_PATH, UnixIndexdClient};
use crate::live::enqueue::{ChildSource, DiscoverEnqueuer, EnqueueReport, LiveChildSource};
use crate::live::indexd::{LiveLeaseClient, LiveQueueStore};
use crate::live::system::{LiveCommandRunner, LiveThrottleSource, LiveWaiter};
use crate::rclone::{RcloneRemote, RcloneUploadEngine};
use crate::serve::{DrainStop, Scheduler, SchedulerTimings};
use crate::source::ArchiveRoot;
use crate::time::Waiter;

const DEFAULT_WIFID_SOCKET: &str = "/run/teslausb/wifid.sock";
const DEFAULT_GOVERNOR_FILE: &str = "/run/teslausb/retentiond.governor.json";
const DEFAULT_ARCHIVE_ROOT: &str = "/srv/teslausb/archive";
const DEFAULT_RCLONE_BINARY: &str = "rclone";
const DEFAULT_RCLONE_REMOTE: &str = "teslausb-cloud";
const DEFAULT_INTERVAL_SECS: u64 = 5;
const MAX_DRAIN_STEPS: u32 = 4_096;
const WAIT_SLICE_MS: u64 = 250;

static SHUTDOWN_REQUESTED: AtomicBool = AtomicBool::new(false);

#[derive(Debug, Clone)]
struct ServeArgs {
    indexd_socket: PathBuf,
    wifid_socket: PathBuf,
    archive_root: String,
    destination_id: String,
    remote_prefix: String,
    rclone_remote: String,
    rclone_binary: String,
    rclone_config: Option<String>,
    interval_secs: u64,
    /// `None` leaves the producer's own default in place.
    max_parents_per_pass: Option<u32>,
    once: bool,
}

impl Default for ServeArgs {
    fn default() -> Self {
        Self {
            indexd_socket: PathBuf::from(
                std::env::var("UPLOADD_INDEXD_SOCKET").unwrap_or_else(|_| INDEXD_SOCKET_PATH.to_owned()),
            ),
            wifid_socket: PathBuf::from(
                std::env::var("UPLOADD_WIFID_SOCKET").unwrap_or_else(|_| DEFAULT_WIFID_SOCKET.to_owned()),
            ),
            archive_root: std::env::var("UPLOADD_ARCHIVE_ROOT")
                .unwrap_or_else(|_| DEFAULT_ARCHIVE_ROOT.to_owned()),
            destination_id: std::env::var("UPLOADD_DESTINATION_ID").unwrap_or_default(),
            remote_prefix: std::env::var("UPLOADD_REMOTE_PREFIX").unwrap_or_default(),
            rclone_remote: std::env::var("UPLOADD_RCLONE_REMOTE")
                .unwrap_or_else(|_| DEFAULT_RCLONE_REMOTE.to_owned()),
            rclone_binary: std::env::var("UPLOADD_RCLONE_BINARY")
                .unwrap_or_else(|_| DEFAULT_RCLONE_BINARY.to_owned()),
            rclone_config: std::env::var("UPLOADD_RCLONE_CONFIG").ok().filter(|v| !v.is_empty()),
            interval_secs: std::env::var("UPLOADD_INTERVAL_SECS")
                .ok()
                .and_then(|v| v.parse::<u64>().ok())
                .unwrap_or(DEFAULT_INTERVAL_SECS),
            max_parents_per_pass: std::env::var("UPLOADD_MAX_PARENTS_PER_PASS")
                .ok()
                .and_then(|v| v.parse::<u32>().ok()),
            once: false,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
struct CycleDrainReport {
    hydrate_error: Option<String>,
    drain_error: Option<String>,
    exhausted: u32,
    skipped_missing_source_rel: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[allow(clippy::struct_field_names)]
struct CycleReport {
    producer_failure: Option<String>,
    hydrate_failure: Option<String>,
    drain_failure: Option<String>,
    exhausted: u32,
    skipped_missing_source_rel: u32,
}

trait ProducerPass {
    fn run_pass(&self) -> Result<EnqueueReport, String>;
}

trait DrainPass {
    fn run_pass(&mut self) -> CycleDrainReport;
}

trait CycleExecutor {
    fn run_cycle(&mut self) -> CycleReport;
}

trait ShutdownSignal {
    fn is_shutdown_requested(&self) -> bool;
}

struct ProcessShutdown;

impl ShutdownSignal for ProcessShutdown {
    fn is_shutdown_requested(&self) -> bool {
        SHUTDOWN_REQUESTED.load(Ordering::Relaxed)
    }
}

struct LiveProducer<'a, C: crate::indexd_client::IndexdCloudClient, S: ChildSource> {
    enqueuer: DiscoverEnqueuer<'a, C, S>,
}

impl<C: crate::indexd_client::IndexdCloudClient, S: ChildSource> ProducerPass for LiveProducer<'_, C, S> {
    fn run_pass(&self) -> Result<EnqueueReport, String> {
        self.enqueuer.run().map_err(|err| err.to_string())
    }
}

struct LiveDrainer<'a, C: crate::indexd_client::IndexdCloudClient> {
    scheduler: Scheduler<RcloneUploadEngine<'a>>,
    queue_store: &'a LiveQueueStore<C>,
    waiter: &'a dyn Waiter,
    timings: SchedulerTimings,
    max_steps: u32,
}

impl<C: crate::indexd_client::IndexdCloudClient> DrainPass for LiveDrainer<'_, C> {
    fn run_pass(&mut self) -> CycleDrainReport {
        let hydrate_error = self
            .scheduler
            .hydrate(self.queue_store)
            .err()
            .map(|err| err.to_string());
        let drain_report = self
            .scheduler
            .drain_ready(self.waiter, &self.timings, self.max_steps);
        let drain_error = match drain_report.stopped {
            DrainStop::Infra(reason) => Some(reason),
            DrainStop::Idle | DrainStop::Paused { .. } | DrainStop::Budget => None,
        };
        CycleDrainReport {
            hydrate_error,
            drain_error,
            exhausted: drain_report.exhausted,
            skipped_missing_source_rel: self.queue_store.take_skipped_missing_source_rel(),
        }
    }
}

struct LoopExecutor<P: ProducerPass, D: DrainPass> {
    producer: P,
    drainer: D,
}

impl<P: ProducerPass, D: DrainPass> CycleExecutor for LoopExecutor<P, D> {
    fn run_cycle(&mut self) -> CycleReport {
        orchestrate_cycle(&self.producer, &mut self.drainer)
    }
}

fn orchestrate_cycle(producer: &dyn ProducerPass, drainer: &mut dyn DrainPass) -> CycleReport {
    let producer_failure = producer.run_pass().err();
    let drain_report = drainer.run_pass();
    CycleReport {
        producer_failure,
        hydrate_failure: drain_report.hydrate_error,
        drain_failure: drain_report.drain_error,
        exhausted: drain_report.exhausted,
        skipped_missing_source_rel: drain_report.skipped_missing_source_rel,
    }
}

fn run_loop(
    executor: &mut dyn CycleExecutor,
    shutdown: &dyn ShutdownSignal,
    waiter: &dyn Waiter,
    interval_secs: u64,
    once: bool,
) -> u64 {
    let mut cycles: u64 = 0;
    loop {
        if shutdown.is_shutdown_requested() {
            break;
        }
        let report = executor.run_cycle();
        log_cycle_report(cycles.saturating_add(1), &report);
        cycles = cycles.saturating_add(1);
        if once || shutdown.is_shutdown_requested() {
            break;
        }
        wait_between_cycles(waiter, shutdown, interval_secs.saturating_mul(1_000));
    }
    cycles
}

fn wait_between_cycles(waiter: &dyn Waiter, shutdown: &dyn ShutdownSignal, mut remaining_ms: u64) {
    while remaining_ms > 0 {
        if shutdown.is_shutdown_requested() {
            break;
        }
        let slice = remaining_ms.min(WAIT_SLICE_MS);
        waiter.wait_ms(slice);
        remaining_ms = remaining_ms.saturating_sub(slice);
    }
}

fn log_cycle_report(cycle: u64, report: &CycleReport) {
    if let Some(reason) = &report.producer_failure {
        write_stderr_line(&format!("uploadd serve: cycle {cycle} producer error: {reason}"));
    }
    if let Some(reason) = &report.hydrate_failure {
        write_stderr_line(&format!("uploadd serve: cycle {cycle} hydrate error: {reason}"));
    }
    if let Some(reason) = &report.drain_failure {
        write_stderr_line(&format!("uploadd serve: cycle {cycle} drain infra error: {reason}"));
    }
    if report.exhausted > 0 {
        write_stderr_line(&format!(
            "uploadd serve: cycle {cycle} parked {} item(s) after exhausting retries",
            report.exhausted
        ));
    }
    if report.skipped_missing_source_rel > 0 {
        write_stderr_line(&format!(
            "uploadd serve: cycle {cycle} skipped {} queue row(s) with no source path",
            report.skipped_missing_source_rel
        ));
    }
}

fn write_stderr_line(line: &str) {
    let mut stderr = std::io::stderr().lock();
    let _ = writeln!(stderr, "{line}");
}

fn parse_serve_args(args: &[String]) -> Result<ServeArgs, String> {
    let mut parsed = ServeArgs::default();
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--indexd-socket" => {
                let value = next_arg_value(&mut iter, "--indexd-socket")?;
                parsed.indexd_socket = PathBuf::from(value);
            }
            "--wifid-socket" => {
                let value = next_arg_value(&mut iter, "--wifid-socket")?;
                parsed.wifid_socket = PathBuf::from(value);
            }
            "--archive-root" => {
                parsed.archive_root = next_arg_value(&mut iter, "--archive-root")?;
            }
            "--destination-id" => {
                parsed.destination_id = next_arg_value(&mut iter, "--destination-id")?;
            }
            "--remote-prefix" => {
                parsed.remote_prefix = next_arg_value(&mut iter, "--remote-prefix")?;
            }
            "--rclone-remote" => {
                parsed.rclone_remote = next_arg_value(&mut iter, "--rclone-remote")?;
            }
            "--rclone-binary" => {
                parsed.rclone_binary = next_arg_value(&mut iter, "--rclone-binary")?;
            }
            "--rclone-config" => {
                parsed.rclone_config = Some(next_arg_value(&mut iter, "--rclone-config")?);
            }
            "--interval-secs" => {
                let value = next_arg_value(&mut iter, "--interval-secs")?;
                parsed.interval_secs = parse_arg::<u64>("--interval-secs", &value)?;
            }
            "--max-parents-per-pass" => {
                let value = next_arg_value(&mut iter, "--max-parents-per-pass")?;
                parsed.max_parents_per_pass =
                    Some(parse_arg::<u32>("--max-parents-per-pass", &value)?);
            }
            "--once" => parsed.once = true,
            other => return Err(format!("uploadd serve: unknown option `{other}`.\n{}", serve_usage())),
        }
    }

    if parsed.archive_root.trim().is_empty() {
        return Err("uploadd serve: --archive-root must be non-empty.".to_owned());
    }
    if parsed.destination_id.is_empty() || parsed.destination_id.len() > 128 {
        return Err("uploadd serve: --destination-id must be 1..=128 bytes.".to_owned());
    }
    if parsed.rclone_remote.trim().is_empty() {
        return Err("uploadd serve: --rclone-remote must be non-empty.".to_owned());
    }
    if parsed.rclone_binary.trim().is_empty() {
        return Err("uploadd serve: --rclone-binary must be non-empty.".to_owned());
    }
    if parsed.interval_secs == 0 {
        return Err("uploadd serve: --interval-secs must be greater than 0.".to_owned());
    }
    Ok(parsed)
}

fn next_arg_value(iter: &mut std::slice::Iter<'_, String>, flag: &str) -> Result<String, String> {
    iter.next()
        .cloned()
        .ok_or_else(|| format!("uploadd serve: missing value for {flag}."))
}

fn parse_arg<T>(flag: &str, value: &str) -> Result<T, String>
where
    T: std::str::FromStr,
    <T as std::str::FromStr>::Err: std::fmt::Display,
{
    value
        .parse::<T>()
        .map_err(|err| format!("uploadd serve: invalid {flag} `{value}`: {err}"))
}

extern "C" fn shutdown_signal_handler(_signal: c_int) {
    SHUTDOWN_REQUESTED.store(true, Ordering::Relaxed);
}

#[allow(unsafe_code)]
fn install_shutdown_handlers() {
    SHUTDOWN_REQUESTED.store(false, Ordering::Relaxed);
    unsafe {
        let handler = shutdown_signal_handler as libc::sighandler_t;
        let _ = libc::signal(libc::SIGTERM, handler);
        let _ = libc::signal(libc::SIGINT, handler);
    }
}

/// Parse `uploadd serve` args, wire live adapters, and run the serve loop.
#[must_use]
pub fn run_serve(args: &[String]) -> ExitCode {
    let parsed = match parse_serve_args(args) {
        Ok(value) => value,
        Err(err) => {
            write_stderr_line(&err);
            return ExitCode::FAILURE;
        }
    };
    let cfg = UploaddConfig::default();
    if let Err(reason) = cfg.validate() {
        write_stderr_line(&format!("uploadd serve: invalid config: {reason}"));
        return ExitCode::FAILURE;
    }

    install_shutdown_handlers();

    let indexd_client = UnixIndexdClient::new(parsed.indexd_socket.clone());
    let queue_store = LiveQueueStore::new(indexd_client.clone(), cfg.retry.max_attempts);
    let lease_client = LiveLeaseClient::new(indexd_client.clone());
    let archive_root = ArchiveRoot::new(parsed.archive_root.clone());
    let throttle_source = LiveThrottleSource::with_paths(parsed.wifid_socket, DEFAULT_GOVERNOR_FILE);
    let child_source = LiveChildSource::new(archive_root.clone());
    let enqueuer = match DiscoverEnqueuer::new(
        &indexd_client,
        &child_source,
        parsed.destination_id.clone(),
        parsed.remote_prefix.clone(),
    ) {
        Ok(value) => match parsed.max_parents_per_pass {
            Some(budget) => value.with_max_parents_per_pass(budget),
            None => value,
        },
        Err(err) => {
            write_stderr_line(&format!("uploadd serve: startup config error: {err}"));
            return ExitCode::FAILURE;
        }
    };
    let remote = RcloneRemote {
        binary: parsed.rclone_binary,
        name: parsed.rclone_remote,
        config_path: parsed.rclone_config,
    };
    let runner = LiveCommandRunner;
    let waiter = LiveWaiter;
    let engine = RcloneUploadEngine {
        cfg: &cfg,
        archive_root: &archive_root,
        remote: &remote,
        runner: &runner,
        lease: &lease_client,
        queue_store: &queue_store,
        throttle: &throttle_source,
    };
    let scheduler = Scheduler::new(engine, &cfg);
    let producer = LiveProducer { enqueuer };
    let drainer = LiveDrainer {
        scheduler,
        queue_store: &queue_store,
        waiter: &waiter,
        timings: SchedulerTimings::default(),
        max_steps: MAX_DRAIN_STEPS,
    };
    let mut executor = LoopExecutor { producer, drainer };
    let shutdown = ProcessShutdown;
    let cycles = run_loop(
        &mut executor,
        &shutdown,
        &waiter,
        parsed.interval_secs,
        parsed.once,
    );
    write_stderr_line(&format!("uploadd serve: clean shutdown after {cycles} cycle(s)"));
    ExitCode::SUCCESS
}

/// Usage text for `uploadd serve`.
#[must_use]
pub fn serve_usage() -> String {
    "uploadd serve [--indexd-socket <path>] [--wifid-socket <path>] \
--archive-root <path> --destination-id <id> [--remote-prefix <prefix>] \
[--rclone-remote <name>] [--rclone-binary <path>] [--rclone-config <path>] \
[--interval-secs <u64>] [--max-parents-per-pass <u32>] [--once]\n\
env fallback: UPLOADD_INDEXD_SOCKET, UPLOADD_WIFID_SOCKET, UPLOADD_ARCHIVE_ROOT, \
UPLOADD_DESTINATION_ID, UPLOADD_REMOTE_PREFIX, UPLOADD_RCLONE_REMOTE, \
UPLOADD_RCLONE_BINARY, UPLOADD_RCLONE_CONFIG, UPLOADD_INTERVAL_SECS, \
UPLOADD_MAX_PARENTS_PER_PASS"
        .to_owned()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use std::cell::{Cell, RefCell};
    use std::rc::Rc;

    use super::*;

    #[derive(Default)]
    struct FakeProducer {
        calls: Cell<u32>,
        outcomes: RefCell<Vec<Result<EnqueueReport, String>>>,
    }

    impl ProducerPass for FakeProducer {
        fn run_pass(&self) -> Result<EnqueueReport, String> {
            self.calls.set(self.calls.get().saturating_add(1));
            if self.outcomes.borrow().is_empty() {
                return Ok(EnqueueReport::default());
            }
            self.outcomes.borrow_mut().remove(0)
        }
    }

    #[derive(Default, Clone)]
    struct FakeDrainerState {
        calls: u32,
    }

    struct FakeDrainer {
        state: Rc<RefCell<FakeDrainerState>>,
        outcomes: RefCell<Vec<CycleDrainReport>>,
    }

    impl DrainPass for FakeDrainer {
        fn run_pass(&mut self) -> CycleDrainReport {
            let mut state = self.state.borrow_mut();
            state.calls = state.calls.saturating_add(1);
            if self.outcomes.borrow().is_empty() {
                return CycleDrainReport::default();
            }
            self.outcomes.borrow_mut().remove(0)
        }
    }

    struct FakeShutdown {
        flag: Rc<Cell<bool>>,
    }

    impl ShutdownSignal for FakeShutdown {
        fn is_shutdown_requested(&self) -> bool {
            self.flag.get()
        }
    }

    struct FakeWaiter {
        waits: Rc<RefCell<Vec<u64>>>,
        shutdown_flag: Option<Rc<Cell<bool>>>,
        set_shutdown_after_wait_count: usize,
    }

    impl Waiter for FakeWaiter {
        fn wait_ms(&self, ms: u64) {
            self.waits.borrow_mut().push(ms);
            if let Some(flag) = &self.shutdown_flag {
                if self.waits.borrow().len() >= self.set_shutdown_after_wait_count {
                    flag.set(true);
                }
            }
        }
    }

    #[test]
    fn producer_error_does_not_prevent_drain_this_cycle() {
        let producer = FakeProducer {
            calls: Cell::new(0),
            outcomes: RefCell::new(vec![Err("discover failed".to_owned())]),
        };
        let drain_state = Rc::new(RefCell::new(FakeDrainerState::default()));
        let mut drainer = FakeDrainer {
            state: drain_state.clone(),
            outcomes: RefCell::new(vec![CycleDrainReport::default()]),
        };
        let report = orchestrate_cycle(&producer, &mut drainer);
        assert_eq!(producer.calls.get(), 1);
        assert_eq!(drain_state.borrow().calls, 1);
        assert_eq!(report.producer_failure.as_deref(), Some("discover failed"));
    }

    #[test]
    fn drain_infra_error_does_not_prevent_next_cycle_producer() {
        let producer = FakeProducer {
            calls: Cell::new(0),
            outcomes: RefCell::new(vec![Ok(EnqueueReport::default()), Ok(EnqueueReport::default())]),
        };
        let drain_state = Rc::new(RefCell::new(FakeDrainerState::default()));
        let drainer = FakeDrainer {
            state: drain_state.clone(),
            outcomes: RefCell::new(vec![
                CycleDrainReport {
                    hydrate_error: None,
                    drain_error: Some("index hiccup".to_owned()),
                    exhausted: 0,
                    skipped_missing_source_rel: 0,
                },
                CycleDrainReport::default(),
            ]),
        };
        let mut executor = LoopExecutor { producer, drainer };
        let shutdown_flag = Rc::new(Cell::new(false));
        let shutdown = FakeShutdown {
            flag: shutdown_flag.clone(),
        };
        let waits = Rc::new(RefCell::new(Vec::new()));
        let waiter = FakeWaiter {
            waits: waits.clone(),
            shutdown_flag: Some(shutdown_flag),
            set_shutdown_after_wait_count: 5,
        };
        let cycles = run_loop(&mut executor, &shutdown, &waiter, 1, false);
        assert_eq!(cycles, 2);
        assert_eq!(executor.producer.calls.get(), 2);
        assert_eq!(drain_state.borrow().calls, 2);
        assert_eq!(waits.borrow().len(), 5);
    }

    #[test]
    fn once_mode_runs_exactly_one_cycle() {
        let producer = FakeProducer::default();
        let drain_state = Rc::new(RefCell::new(FakeDrainerState::default()));
        let drainer = FakeDrainer {
            state: drain_state.clone(),
            outcomes: RefCell::new(Vec::new()),
        };
        let mut executor = LoopExecutor { producer, drainer };
        let shutdown = FakeShutdown {
            flag: Rc::new(Cell::new(false)),
        };
        let waits = Rc::new(RefCell::new(Vec::new()));
        let waiter = FakeWaiter {
            waits: waits.clone(),
            shutdown_flag: None,
            set_shutdown_after_wait_count: 0,
        };
        let cycles = run_loop(&mut executor, &shutdown, &waiter, 30, true);
        assert_eq!(cycles, 1);
        assert_eq!(executor.producer.calls.get(), 1);
        assert_eq!(drain_state.borrow().calls, 1);
        assert!(waits.borrow().is_empty());
    }

    #[test]
    fn shutdown_signal_stops_loop_between_cycles() {
        let producer = FakeProducer::default();
        let drain_state = Rc::new(RefCell::new(FakeDrainerState::default()));
        let drainer = FakeDrainer {
            state: drain_state.clone(),
            outcomes: RefCell::new(Vec::new()),
        };
        let mut executor = LoopExecutor { producer, drainer };
        let shutdown_flag = Rc::new(Cell::new(false));
        let shutdown = FakeShutdown {
            flag: shutdown_flag.clone(),
        };
        let waits = Rc::new(RefCell::new(Vec::new()));
        let waiter = FakeWaiter {
            waits: waits.clone(),
            shutdown_flag: Some(shutdown_flag),
            set_shutdown_after_wait_count: 1,
        };
        let cycles = run_loop(&mut executor, &shutdown, &waiter, 1, false);
        assert_eq!(cycles, 1);
        assert_eq!(executor.producer.calls.get(), 1);
        assert_eq!(drain_state.borrow().calls, 1);
        assert_eq!(waits.borrow().len(), 1);
    }

    #[test]
    fn parse_serve_args_rejects_missing_or_invalid_destination_id() {
        let parsed_missing = parse_serve_args(&[
            "--destination-id".to_owned(),
            String::new(),
        ]);
        assert!(parsed_missing.is_err());

        let parsed_invalid = parse_serve_args(&[
            "--destination-id".to_owned(),
            "x".repeat(129),
        ]);
        assert!(parsed_invalid.is_err());
    }
}
