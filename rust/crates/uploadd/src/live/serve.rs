use std::fs::OpenOptions;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};

use libc::c_int;
use teslausb_creds::{
    BlobKeyMaterial, CLOUD_PROVIDER_CREDS_FILENAME, CredentialDocument, HardwareRoot, ProcHardwareRoot,
    TESLA_SALT_FILENAME, decrypt, derive_key, read_blob, read_salt, render_rclone_conf, validate_document,
};

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
const DEFAULT_CLOUD_STATE_DIR: &str = "/var/lib/teslausb";
const DEFAULT_RUNTIME_DIR: &str = "/run/teslausb";
const DEFAULT_RCLONE_BINARY: &str = "rclone";
const DEFAULT_RCLONE_REMOTE: &str = "teslausb-cloud";
const RENDERED_REMOTE_NAME: &str = "teslausb";
const RENDERED_CONFIG_FILENAME: &str = "rclone.conf";
const DEFAULT_INTERVAL_SECS: u64 = 5;
const MAX_DRAIN_STEPS: u32 = 4_096;
const WAIT_SLICE_MS: u64 = 250;

static SHUTDOWN_REQUESTED: AtomicBool = AtomicBool::new(false);

#[derive(Debug, Clone)]
struct ServeArgs {
    indexd_socket: PathBuf,
    wifid_socket: PathBuf,
    archive_root: String,
    cloud_state_dir: String,
    runtime_dir: String,
    destination_id: String,
    remote_prefix: String,
    rclone_remote: String,
    rclone_remote_is_default: bool,
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
            cloud_state_dir: std::env::var("UPLOADD_CLOUD_STATE_DIR")
                .unwrap_or_else(|_| DEFAULT_CLOUD_STATE_DIR.to_owned()),
            runtime_dir: std::env::var("UPLOADD_RUNTIME_DIR")
                .unwrap_or_else(|_| DEFAULT_RUNTIME_DIR.to_owned()),
            destination_id: std::env::var("UPLOADD_DESTINATION_ID").unwrap_or_default(),
            remote_prefix: std::env::var("UPLOADD_REMOTE_PREFIX").unwrap_or_default(),
            rclone_remote: std::env::var("UPLOADD_RCLONE_REMOTE")
                .ok()
                .filter(|v| !v.trim().is_empty())
                .unwrap_or_else(|| DEFAULT_RCLONE_REMOTE.to_owned()),
            rclone_remote_is_default: std::env::var("UPLOADD_RCLONE_REMOTE")
                .ok()
                .filter(|v| !v.trim().is_empty())
                .is_none(),
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

fn resolve_runtime_rclone_config(
    parsed: &ServeArgs,
    hardware_root: &dyn HardwareRoot,
    mut log: impl FnMut(&str),
) -> Option<PathBuf> {
    let runtime_config = Path::new(&parsed.runtime_dir).join(RENDERED_CONFIG_FILENAME);
    match std::fs::remove_file(&runtime_config) {
        Ok(()) => {}
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
        Err(err) => log(&format!(
            "uploadd serve: failed to remove stale runtime rclone config `{}`: {err}; continuing",
            runtime_config.display()
        )),
    }
    if parsed.rclone_config.is_some() {
        return None;
    }
    render_runtime_rclone_config(parsed, &runtime_config, hardware_root, &mut log)
}

fn render_runtime_rclone_config(
    parsed: &ServeArgs,
    runtime_config: &Path,
    hardware_root: &dyn HardwareRoot,
    mut log: impl FnMut(&str),
) -> Option<PathBuf> {
    let blob_path = Path::new(&parsed.cloud_state_dir).join(CLOUD_PROVIDER_CREDS_FILENAME);
    if !blob_path.exists() {
        return None;
    }

    let rendered = (|| -> Result<String, teslausb_creds::CredsError> {
        let blob = read_blob(&blob_path)?;
        let salt = read_salt(&Path::new(&parsed.cloud_state_dir).join(TESLA_SALT_FILENAME))?;
        let key = derive_key(hardware_root, &salt, teslausb_creds::DEFAULT_KDF_ITERS)?;
        let material = BlobKeyMaterial {
            key,
            salt,
            kdf_iters: teslausb_creds::DEFAULT_KDF_ITERS,
        };
        let plaintext = decrypt(&blob, &material)?;
        let document = CredentialDocument::from_bytes(&plaintext)?;
        let validated = validate_document(&document)?;
        render_rclone_conf(RENDERED_REMOTE_NAME, &validated)
    })();

    match rendered {
        Ok(contents) => match write_runtime_rclone_config(runtime_config, &contents) {
            Ok(()) => Some(runtime_config.to_path_buf()),
            Err(err) => {
                log(&format!(
                    "uploadd serve: failed to write runtime rclone config `{}`: {err}; continuing without rendered cloud credentials",
                    runtime_config.display()
                ));
                None
            }
        },
        Err(err) => {
            log(&format!(
                "uploadd serve: cloud credentials unreadable ({err}); continuing without cloud upload config. Re-save cloud credentials in the web UI."
            ));
            None
        }
    }
}

fn write_runtime_rclone_config(path: &Path, contents: &str) -> Result<(), std::io::Error> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut opts = OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;

        opts.mode(0o600);
    }
    let mut file = opts.open(path)?;
    file.write_all(contents.as_bytes())?;
    file.sync_all()?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

fn effective_rclone_remote_name(parsed: &ServeArgs, rendered_runtime_config: bool) -> String {
    if rendered_runtime_config && parsed.rclone_remote_is_default {
        RENDERED_REMOTE_NAME.to_owned()
    } else {
        parsed.rclone_remote.clone()
    }
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
            "--cloud-state-dir" => {
                parsed.cloud_state_dir = next_arg_value(&mut iter, "--cloud-state-dir")?;
            }
            "--runtime-dir" => {
                parsed.runtime_dir = next_arg_value(&mut iter, "--runtime-dir")?;
            }
            "--destination-id" => {
                parsed.destination_id = next_arg_value(&mut iter, "--destination-id")?;
            }
            "--remote-prefix" => {
                parsed.remote_prefix = next_arg_value(&mut iter, "--remote-prefix")?;
            }
            "--rclone-remote" => {
                parsed.rclone_remote = next_arg_value(&mut iter, "--rclone-remote")?;
                parsed.rclone_remote_is_default = false;
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
    if parsed.cloud_state_dir.trim().is_empty() {
        return Err("uploadd serve: --cloud-state-dir must be non-empty.".to_owned());
    }
    if parsed.runtime_dir.trim().is_empty() {
        return Err("uploadd serve: --runtime-dir must be non-empty.".to_owned());
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
    let rendered_runtime_config =
        resolve_runtime_rclone_config(&parsed, &ProcHardwareRoot, write_stderr_line);
    let rclone_config = parsed
        .rclone_config
        .clone()
        .or_else(|| rendered_runtime_config.as_ref().map(|path| path.to_string_lossy().into_owned()));
    let rclone_remote = effective_rclone_remote_name(&parsed, rendered_runtime_config.is_some());

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
        name: rclone_remote,
        config_path: rclone_config,
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
[--archive-root <path>] [--cloud-state-dir <path>] [--runtime-dir <path>] \
--destination-id <id> [--remote-prefix <prefix>] \
[--rclone-remote <name>] [--rclone-binary <path>] [--rclone-config <path>] \
[--interval-secs <u64>] [--max-parents-per-pass <u32>] [--once]\n\
env fallback: UPLOADD_INDEXD_SOCKET, UPLOADD_WIFID_SOCKET, UPLOADD_ARCHIVE_ROOT, \
UPLOADD_CLOUD_STATE_DIR, UPLOADD_RUNTIME_DIR, \
UPLOADD_DESTINATION_ID, UPLOADD_REMOTE_PREFIX, UPLOADD_RCLONE_REMOTE, \
UPLOADD_RCLONE_BINARY, UPLOADD_RCLONE_CONFIG, UPLOADD_INTERVAL_SECS, \
UPLOADD_MAX_PARENTS_PER_PASS"
        .to_owned()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use std::cell::{Cell, RefCell};
    use std::fs;
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;
    use std::path::{Path, PathBuf};
    use std::rc::Rc;
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;
    use teslausb_creds::{
        BlobKeyMaterial, CredentialDocument, CredentialFlow, OAuthProvider, StaticHardwareRoot, encrypt,
        read_or_create_salt, write_blob_atomic,
    };

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

    static NEXT_ID: AtomicU64 = AtomicU64::new(0);

    fn unique_path(prefix: &str) -> PathBuf {
        let unique = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!("{prefix}-{}-{unique}", std::process::id()))
    }

    fn write_valid_oauth_blob(cloud_state_dir: &Path, root: &StaticHardwareRoot) {
        fs::create_dir_all(cloud_state_dir).expect("create cloud dir");
        let salt_path = cloud_state_dir.join(TESLA_SALT_FILENAME);
        let blob_path = cloud_state_dir.join(CLOUD_PROVIDER_CREDS_FILENAME);
        let salt = read_or_create_salt(&salt_path).expect("create salt");
        let key = derive_key(root, &salt, teslausb_creds::DEFAULT_KDF_ITERS).expect("derive key");
        let material = BlobKeyMaterial {
            key,
            salt,
            kdf_iters: teslausb_creds::DEFAULT_KDF_ITERS,
        };
        let token = r#"{"access_token":"token-123","expiry":"2026-01-01T00:00:00Z"}"#.to_owned();
        let doc = CredentialDocument::new(CredentialFlow::OAuth {
            provider: OAuthProvider::Onedrive,
            token,
        });
        let plaintext = doc.to_canonical_bytes().expect("serialize doc");
        let blob = encrypt(&plaintext, &material).expect("encrypt blob");
        write_blob_atomic(&blob_path, &blob).expect("write blob");
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
    fn rendered_config_round_trips_and_has_secure_permissions() {
        let base = unique_path("uploadd-render-roundtrip");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        write_valid_oauth_blob(&cloud_state_dir, &root);
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            rclone_config: None,
            ..ServeArgs::default()
        };
        let mut logs = Vec::<String>::new();
        let rendered = resolve_runtime_rclone_config(&parsed, &root, |line| logs.push(line.to_owned()));
        let rendered_path = rendered.expect("rendered path");
        let text = fs::read_to_string(&rendered_path).expect("read rendered config");
        assert_eq!(text.matches("[teslausb]").count(), 1);
        assert!(text.contains("type = onedrive"));
        assert!(text.contains("token = {\"access_token\":\"token-123\",\"expiry\":\"2026-01-01T00:00:00Z\"}"));
        #[cfg(unix)]
        assert_eq!(fs::metadata(&rendered_path).unwrap().permissions().mode() & 0o777, 0o600);
        assert!(logs.is_empty(), "unexpected logs: {logs:?}");
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn stale_runtime_config_is_replaced_not_appended() {
        let base = unique_path("uploadd-render-stale");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        write_valid_oauth_blob(&cloud_state_dir, &root);
        fs::create_dir_all(&runtime_dir).expect("create runtime dir");
        fs::write(runtime_dir.join(RENDERED_CONFIG_FILENAME), "[stale]\nmarker = keep-me\n")
            .expect("write stale config");
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            rclone_config: None,
            ..ServeArgs::default()
        };
        let rendered = resolve_runtime_rclone_config(&parsed, &root, |_line| {});
        let rendered_path = rendered.expect("rendered path");
        let text = fs::read_to_string(&rendered_path).expect("read rendered config");
        assert_eq!(text.matches("[teslausb]").count(), 1);
        assert!(!text.contains("[stale]"));
        assert!(!text.contains("keep-me"));
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn corrupt_blob_keeps_running_and_preserves_blob_file() {
        let base = unique_path("uploadd-render-corrupt");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        fs::create_dir_all(&cloud_state_dir).expect("create cloud dir");
        let _ = read_or_create_salt(&cloud_state_dir.join(TESLA_SALT_FILENAME)).expect("create salt");
        let blob_path = cloud_state_dir.join(CLOUD_PROVIDER_CREDS_FILENAME);
        fs::write(&blob_path, b"corrupt").expect("write corrupt blob");
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            rclone_config: None,
            ..ServeArgs::default()
        };
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        let mut logs = Vec::<String>::new();
        let rendered = resolve_runtime_rclone_config(&parsed, &root, |line| logs.push(line.to_owned()));
        assert!(rendered.is_none());
        assert!(blob_path.exists());
        assert!(!runtime_dir.join(RENDERED_CONFIG_FILENAME).exists());
        assert!(logs.iter().any(|line| line.contains("cloud credentials unreadable")));
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn explicit_rclone_config_override_skips_rendering() {
        let base = unique_path("uploadd-render-override");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        write_valid_oauth_blob(&cloud_state_dir, &root);
        fs::create_dir_all(&runtime_dir).expect("create runtime dir");
        fs::write(runtime_dir.join(RENDERED_CONFIG_FILENAME), "[stale]\n").expect("write stale config");
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            rclone_config: Some("/explicit-rclone.conf".to_owned()),
            ..ServeArgs::default()
        };
        let rendered = resolve_runtime_rclone_config(&parsed, &root, |_line| {});
        assert!(rendered.is_none());
        assert!(!runtime_dir.join(RENDERED_CONFIG_FILENAME).exists());
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn missing_blob_skips_render_without_errors() {
        let base = unique_path("uploadd-render-missing");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            rclone_config: None,
            ..ServeArgs::default()
        };
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        let mut logs = Vec::<String>::new();
        let rendered = resolve_runtime_rclone_config(&parsed, &root, |line| logs.push(line.to_owned()));
        assert!(rendered.is_none());
        assert!(logs.is_empty(), "unexpected logs: {logs:?}");
        assert!(!runtime_dir.join(RENDERED_CONFIG_FILENAME).exists());
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn rendered_config_switches_default_remote_name_to_teslausb() {
        let parsed = ServeArgs {
            rclone_remote: DEFAULT_RCLONE_REMOTE.to_owned(),
            rclone_remote_is_default: true,
            ..ServeArgs::default()
        };
        let remote = effective_rclone_remote_name(&parsed, true);
        assert_eq!(remote, "teslausb");
    }

    #[test]
    fn explicit_remote_name_is_preserved_when_rendering() {
        let parsed = ServeArgs {
            rclone_remote: "custom-remote".to_owned(),
            rclone_remote_is_default: false,
            ..ServeArgs::default()
        };
        let remote = effective_rclone_remote_name(&parsed, true);
        assert_eq!(remote, "custom-remote");
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
