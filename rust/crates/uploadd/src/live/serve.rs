use std::cell::RefCell;
use std::fs::OpenOptions;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};

use libc::c_int;
use serde_json::Value;
use teslausb_creds::{
    BlobKeyMaterial, CLOUD_PROVIDER_CREDS_FILENAME, CredentialDocument, HardwareRoot, ProcHardwareRoot,
    TESLA_SALT_FILENAME, decrypt, derive_key, encrypt, normalize_oauth_token, parse_single_remote_conf,
    read_blob, read_salt, render_rclone_conf, validate_document, with_creds_lock, write_blob_atomic,
};

use crate::config::UploaddConfig;
use crate::indexd_client::{INDEXD_SOCKET_PATH, UnixIndexdClient};
use crate::live::enqueue::{ChildSource, DiscoverEnqueuer, EnqueueReport, LiveChildSource};
use crate::live::indexd::{LiveLeaseClient, LiveQueueStore};
use crate::live::system::{LiveCommandRunner, LiveThrottleSource, LiveWaiter};
use crate::rclone::{RcloneRemote, RcloneUploadEngine};
use crate::serve::{DrainStop, Scheduler, SchedulerTimings};
use crate::source::ArchiveRoot;
use crate::throttle::{GateReason, PauseAction};
use crate::time::Waiter;

const DEFAULT_WIFID_SOCKET: &str = "/run/teslausb/wifid.sock";
const DEFAULT_GOVERNOR_FILE: &str = "/run/teslausb/retentiond.governor.json";
const DEFAULT_ARCHIVE_ROOT: &str = "/srv/teslausb/archive";
const DEFAULT_CLOUD_STATE_DIR: &str = "/var/lib/teslausb";
const DEFAULT_RUNTIME_DIR: &str = "/run/teslausb";
const DEFAULT_RCLONE_BINARY: &str = "rclone";
const DEFAULT_RCLONE_REMOTE: &str = "teslausb-cloud";
/// Remote-side folder every upload is nested under. Without this the composed
/// `remote_key` is relative to the account root, which scatters clip folders
/// across the user's personal drive instead of keeping them in one place.
const DEFAULT_REMOTE_PREFIX: &str = "TeslaUSB";
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
            remote_prefix: std::env::var("UPLOADD_REMOTE_PREFIX")
                .unwrap_or_else(|_| DEFAULT_REMOTE_PREFIX.to_owned()),
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

#[derive(Clone)]
struct OAuthReadbackBaseline {
    normalized_token: String,
    document: CredentialDocument,
}

#[derive(Clone)]
struct RenderedRuntimeConfig {
    path: PathBuf,
    oauth_baseline: Option<OAuthReadbackBaseline>,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
struct CycleDrainReport {
    hydrate_error: Option<String>,
    drain_error: Option<String>,
    pause: Option<CyclePause>,
    exhausted: u32,
    skipped_missing_source_rel: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct CyclePause {
    reason: GateReason,
    action: PauseAction,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
struct EnqueueSummary {
    discovered_parents: u64,
    skipped_existing_parents: u64,
    enqueued_children: u64,
    skipped_remote_key_too_long: u64,
    stopped_at_parent_budget: bool,
}

impl From<&EnqueueReport> for EnqueueSummary {
    fn from(value: &EnqueueReport) -> Self {
        Self {
            discovered_parents: value.discovered_parents,
            skipped_existing_parents: value.skipped_existing_parents,
            enqueued_children: value.enqueued_children,
            skipped_remote_key_too_long: value.skipped_remote_key_too_long,
            stopped_at_parent_budget: value.stopped_at_parent_budget,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[allow(clippy::struct_field_names)]
struct CycleReport {
    producer_failure: Option<String>,
    enqueue_summary: Option<EnqueueSummary>,
    hydrate_failure: Option<String>,
    drain_failure: Option<String>,
    pause: Option<CyclePause>,
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
        let pause = match drain_report.stopped {
            DrainStop::Paused { reason, action } => Some(CyclePause { reason, action }),
            DrainStop::Idle | DrainStop::Infra(_) | DrainStop::Budget => None,
        };
        let drain_error = match drain_report.stopped {
            DrainStop::Infra(reason) => Some(reason),
            DrainStop::Idle | DrainStop::Paused { .. } | DrainStop::Budget => None,
        };
        CycleDrainReport {
            hydrate_error,
            drain_error,
            pause,
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
    let producer_result = producer.run_pass();
    let producer_failure = producer_result.as_ref().err().cloned();
    let enqueue_summary = producer_result.as_ref().ok().map(EnqueueSummary::from);
    let drain_report = drainer.run_pass();
    CycleReport {
        producer_failure,
        enqueue_summary,
        hydrate_failure: drain_report.hydrate_error,
        drain_failure: drain_report.drain_error,
        pause: drain_report.pause,
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
    let mut log_state = CycleLogState::default();
    loop {
        if shutdown.is_shutdown_requested() {
            break;
        }
        let report = executor.run_cycle();
        log_cycle_report(cycles.saturating_add(1), &report, &mut log_state);
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

#[derive(Debug, Clone, PartialEq, Eq, Default)]
struct CycleLogState {
    pause: Option<CyclePause>,
}

fn log_cycle_report(cycle: u64, report: &CycleReport, state: &mut CycleLogState) {
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
    for line in pause_transition_lines(cycle, report, state) {
        write_stderr_line(&line);
    }
}

fn pause_transition_lines(
    cycle: u64,
    report: &CycleReport,
    state: &mut CycleLogState,
) -> Vec<String> {
    if report.pause == state.pause {
        return Vec::new();
    }
    let enqueue = format_enqueue_summary(report.enqueue_summary.as_ref());
    let line = match (state.pause, report.pause) {
        (None, Some(next)) => format!(
            "uploadd serve: cycle {cycle} uploads paused ({:?}/{:?}); {enqueue}",
            next.reason, next.action
        ),
        (Some(previous), Some(next)) => format!(
            "uploadd serve: cycle {cycle} uploads pause changed ({:?}/{:?} -> {:?}/{:?}); {enqueue}",
            previous.reason, previous.action, next.reason, next.action
        ),
        (Some(previous), None) => format!(
            "uploadd serve: cycle {cycle} uploads resumed (was {:?}/{:?}); {enqueue}",
            previous.reason, previous.action
        ),
        (None, None) => String::new(),
    };
    state.pause = report.pause;
    if line.is_empty() {
        Vec::new()
    } else {
        vec![line]
    }
}

fn format_enqueue_summary(summary: Option<&EnqueueSummary>) -> String {
    match summary {
        Some(value) => format!(
            "enqueue discovered={} enqueued={} skipped_existing={} skipped_key_too_long={} budget_stop={}",
            value.discovered_parents,
            value.enqueued_children,
            value.skipped_existing_parents,
            value.skipped_remote_key_too_long,
            value.stopped_at_parent_budget
        ),
        None => "enqueue unavailable (producer failed)".to_owned(),
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
) -> Option<RenderedRuntimeConfig> {
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
) -> Option<RenderedRuntimeConfig> {
    let blob_path = Path::new(&parsed.cloud_state_dir).join(CLOUD_PROVIDER_CREDS_FILENAME);
    if !blob_path.exists() {
        return None;
    }

    let rendered = (|| -> Result<(String, Option<OAuthReadbackBaseline>), teslausb_creds::CredsError> {
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
        let oauth_baseline = oauth_readback_baseline(&document)?;
        let validated = validate_document(&document)?;
        let contents = render_rclone_conf(RENDERED_REMOTE_NAME, &validated)?;
        Ok((contents, oauth_baseline))
    })();

    match rendered {
        Ok((contents, oauth_baseline)) => match write_runtime_rclone_config(runtime_config, &contents) {
            Ok(()) => Some(RenderedRuntimeConfig {
                path: runtime_config.to_path_buf(),
                oauth_baseline,
            }),
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

fn oauth_readback_baseline(
    document: &CredentialDocument,
) -> Result<Option<OAuthReadbackBaseline>, teslausb_creds::CredsError> {
    let teslausb_creds::CredentialFlow::OAuth { token, .. } = &document.flow else {
        return Ok(None);
    };
    let normalized_token = normalize_oauth_token(token)?;
    Ok(Some(OAuthReadbackBaseline {
        normalized_token,
        document: document.clone(),
    }))
}

fn readback_runtime_oauth_token(
    parsed: &ServeArgs,
    baseline: &mut OAuthReadbackBaseline,
    hardware_root: &dyn HardwareRoot,
    mut log: impl FnMut(&str),
) -> Result<(), teslausb_creds::CredsError> {
    let runtime_config = Path::new(&parsed.runtime_dir).join(RENDERED_CONFIG_FILENAME);
    let conf = match std::fs::read_to_string(&runtime_config) {
        Ok(value) => value,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            log("uploadd serve: runtime rclone config missing during token read-back; skipping");
            return Ok(());
        }
        Err(err) => return Err(teslausb_creds::CredsError::Io(err)),
    };
    if !conf.lines().any(|line| line.trim() == "[teslausb]") {
        log("uploadd serve: runtime rclone config missing [teslausb] section; skipping token read-back");
        return Ok(());
    }
    let Ok(parsed_conf) = parse_single_remote_conf(&conf) else {
        log("uploadd serve: runtime rclone config is malformed; skipping token read-back");
        return Ok(());
    };
    let expected_backend = oauth_provider_backend_type(&baseline.document);
    if parsed_conf.backend_type != expected_backend {
        log("uploadd serve: runtime rclone config has unexpected backend for token read-back; skipping");
        return Ok(());
    }
    let Some(raw_token) = parsed_conf.options.get("token") else {
        log("uploadd serve: runtime rclone config has no token key; skipping token read-back");
        return Ok(());
    };
    let Ok(normalized_new) = normalize_oauth_token(raw_token) else {
        log("uploadd serve: runtime token is invalid; skipping token read-back");
        return Ok(());
    };
    if normalized_new == baseline.normalized_token {
        return Ok(());
    }

    let baseline_has_refresh = has_non_empty_refresh_token(&baseline.normalized_token)?;
    if baseline_has_refresh && !has_non_empty_refresh_token(&normalized_new)? {
        log("uploadd serve: refreshed token missing refresh_token; skipping token read-back");
        return Ok(());
    }

    let teslausb_creds::CredentialFlow::OAuth {
        provider: baseline_provider,
        options: baseline_options,
        ..
    } = &baseline.document.flow
    else {
        return Ok(());
    };
    let candidate_doc = CredentialDocument::new(teslausb_creds::CredentialFlow::OAuth {
        provider: *baseline_provider,
        token: normalized_new.clone(),
        options: baseline_options.clone(),
    });
    let Ok(validated) = validate_document(&candidate_doc) else {
        log("uploadd serve: refreshed token failed validation; skipping token read-back");
        return Ok(());
    };
    if render_rclone_conf(RENDERED_REMOTE_NAME, &validated).is_err() {
        log("uploadd serve: refreshed token cannot be rendered into rclone config; skipping token read-back");
        return Ok(());
    }

    let creds_dir = Path::new(&parsed.cloud_state_dir);
    let blob_path = creds_dir.join(CLOUD_PROVIDER_CREDS_FILENAME);
    let salt_path = creds_dir.join(TESLA_SALT_FILENAME);
    let baseline_document = baseline.document.clone();
    let write_result = with_creds_lock(creds_dir, || {
        let blob = match read_blob(&blob_path) {
            Ok(value) => value,
            Err(teslausb_creds::CredsError::Io(err)) if err.kind() == std::io::ErrorKind::NotFound => {
                log("uploadd serve: cloud credential blob removed; skipping token read-back");
                return Ok(false);
            }
            Err(err) => return Err(err),
        };
        let salt = read_salt(&salt_path)?;
        let key = derive_key(hardware_root, &salt, teslausb_creds::DEFAULT_KDF_ITERS)?;
        let material = BlobKeyMaterial {
            key,
            salt,
            kdf_iters: teslausb_creds::DEFAULT_KDF_ITERS,
        };
        let plaintext = decrypt(&blob, &material)?;
        let current_document = CredentialDocument::from_bytes(&plaintext)?;
        if current_document != baseline_document {
            log("uploadd serve: cloud credentials changed by operator; skipping token read-back");
            return Ok(false);
        }
        let next_plaintext = candidate_doc.to_canonical_bytes()?;
        let next_blob = encrypt(&next_plaintext, &material)?;
        write_blob_atomic(&blob_path, &next_blob)?;
        Ok(true)
    });
    let Ok(write_outcome) = write_result else {
        log("uploadd serve: token read-back write failed; continuing");
        return Ok(());
    };
    let wrote_back = write_outcome?;
    if wrote_back {
        baseline.normalized_token = normalized_new;
        baseline.document = candidate_doc;
    }
    Ok(())
}

fn has_non_empty_refresh_token(normalized_token: &str) -> Result<bool, teslausb_creds::CredsError> {
    let value = serde_json::from_str::<Value>(normalized_token)?;
    let refresh = value
        .as_object()
        .and_then(|object| object.get("refresh_token"))
        .and_then(Value::as_str)
        .is_some_and(|token| !token.is_empty());
    Ok(refresh)
}

fn oauth_provider_backend_type(document: &CredentialDocument) -> &'static str {
    match &document.flow {
        teslausb_creds::CredentialFlow::OAuth {
            provider: teslausb_creds::OAuthProvider::Drive,
            ..
        } => "drive",
        teslausb_creds::CredentialFlow::OAuth {
            provider: teslausb_creds::OAuthProvider::Onedrive,
            ..
        } => "onedrive",
        teslausb_creds::CredentialFlow::OAuth {
            provider: teslausb_creds::OAuthProvider::Dropbox,
            ..
        } => "dropbox",
        _ => "",
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

/// The destination id the upload ledger is keyed by.
///
/// An explicit `--destination-id` always wins. Otherwise it is derived from the
/// stored credential's provider, so the id always matches the provider the
/// operator picked in the web UI and switching provider correctly re-uploads.
/// `None` means no destination could be determined (no credential, no flag) —
/// the caller must not enqueue anything under a placeholder id, because
/// `(destination_id, remote_key)` is the ledger's primary key.
fn effective_destination_id(
    parsed: &ServeArgs,
    rendered: Option<&RenderedRuntimeConfig>,
) -> Option<String> {
    if !parsed.destination_id.trim().is_empty() {
        return Some(parsed.destination_id.clone());
    }
    rendered
        .and_then(|config| config.oauth_baseline.as_ref())
        .map(|baseline| oauth_provider_backend_type(&baseline.document).to_owned())
}

/// Block until SIGTERM/SIGINT, doing no queue work.
///
/// Used when no upload destination can be determined. Idling (rather than
/// exiting non-zero) keeps a fresh device's `uploadd` from turning
/// `Restart=on-failure` into a permanent restart loop, and enqueuing under a
/// placeholder destination id is not an option because `(destination_id,
/// remote_key)` is the upload ledger's primary key — those rows would be
/// stranded the moment a real credential arrives under a different id.
fn idle_until_shutdown() {
    let waiter = LiveWaiter;
    while !SHUTDOWN_REQUESTED.load(Ordering::Relaxed) {
        waiter.wait_ms(WAIT_SLICE_MS);
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
    // An empty destination id means "derive it from the stored credential's
    // provider" (see `effective_destination_id`). Only the length cap can be
    // enforced here, because the credential has not been decrypted yet.
    if parsed.destination_id.len() > 128 {
        return Err("uploadd serve: --destination-id must be at most 128 bytes.".to_owned());
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

/// Build the rclone command runner, attaching the OAuth token read-back hook
/// when the rendered runtime config carries a baseline.
fn build_command_runner(
    rendered_runtime_config: Option<&RenderedRuntimeConfig>,
    parsed_for_sync: ServeArgs,
) -> LiveCommandRunner {
    let mut runner = LiveCommandRunner::new();
    if let Some(baseline) =
        rendered_runtime_config.and_then(|config| config.oauth_baseline.clone())
    {
        let baseline_cell = RefCell::new(baseline);
        // TIMING: this hook runs synchronously inside `runner.run()`, so after a
        // `copyto` it extends that subprocess call. Accepted deliberately:
        //   - the common path (token unchanged) only reads a small tmpfs file,
        //     parses it and compares two strings — sub-millisecond, no crypto;
        //   - the expensive path (PBKDF2 at DEFAULT_KDF_ITERS = 600k, ~1-3 s on
        //     this target) runs only when rclone actually rotated the token,
        //     roughly hourly, against a 60 s lease TTL;
        //   - lease renewal runs in a scoped background thread during copy+verify,
        //     so this synchronous hook does not create a renewal gap.
        // Deferring the persist to a lease-free point would need extra state in a
        // credential path where complexity is the larger risk, so we take the
        // occasional retry instead. Note a copy exceeding the TTL already loses
        // the lease on its own (see the mid-copy renewal note in rclone.rs) — this
        // adds a small constant to that pre-existing exposure, it does not create it.
        runner = runner.with_after_run(move || {
            let mut baseline_ref = baseline_cell.borrow_mut();
            if let Err(_err) = readback_runtime_oauth_token(
                &parsed_for_sync,
                &mut baseline_ref,
                &ProcHardwareRoot,
                write_stderr_line,
            ) {
                write_stderr_line("uploadd serve: token read-back failed; continuing");
            }
        });
    }
    runner
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
        .or_else(|| {
            rendered_runtime_config
                .as_ref()
                .map(|config| config.path.to_string_lossy().into_owned())
        });
    let rclone_remote = effective_rclone_remote_name(&parsed, rendered_runtime_config.is_some());
    let destination_id = effective_destination_id(&parsed, rendered_runtime_config.as_ref());
    let parsed_for_sync = parsed.clone();

    install_shutdown_handlers();

    // With no credential and no explicit id there is no destination to key the
    // upload ledger by; see `idle_until_shutdown` for why we idle rather than exit.
    let Some(destination_id) = destination_id else {
        write_stderr_line(
            "uploadd serve: no cloud credential configured and no --destination-id given; \
             idling. Save a credential in the web UI, then restart uploadd.",
        );
        idle_until_shutdown();
        return ExitCode::SUCCESS;
    };

    let indexd_client = UnixIndexdClient::new(parsed.indexd_socket.clone());
    let queue_store = LiveQueueStore::new(indexd_client.clone(), cfg.retry.max_attempts);
    let lease_client = LiveLeaseClient::new(indexd_client.clone());
    let archive_root = ArchiveRoot::new(parsed.archive_root.clone());
    let throttle_source = LiveThrottleSource::with_paths(parsed.wifid_socket, DEFAULT_GOVERNOR_FILE);
    let child_source = LiveChildSource::new(archive_root.clone());
    let enqueuer = match DiscoverEnqueuer::new(
        &indexd_client,
        &child_source,
        destination_id,
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
    let runner = build_command_runner(rendered_runtime_config.as_ref(), parsed_for_sync);
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
    use std::collections::BTreeMap;
    use std::fs;
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;
    use std::path::{Path, PathBuf};
    use std::rc::Rc;
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;
    use teslausb_creds::{
        BlobKeyMaterial, CredentialDocument, CredentialFlow, OAuthProvider, StaticHardwareRoot, decrypt,
        derive_key, encrypt, normalize_oauth_token, read_blob, read_or_create_salt, read_salt,
        write_blob_atomic,
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

    fn write_oauth_blob(
        cloud_state_dir: &Path,
        root: &StaticHardwareRoot,
        provider: OAuthProvider,
        token: &str,
    ) -> CredentialDocument {
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
        let doc = CredentialDocument::new(CredentialFlow::OAuth {
            provider,
            token: normalize_oauth_token(token).expect("normalize token"),
            options: BTreeMap::new(),
        });
        let plaintext = doc.to_canonical_bytes().expect("serialize doc");
        let blob = encrypt(&plaintext, &material).expect("encrypt blob");
        write_blob_atomic(&blob_path, &blob).expect("write blob");
        doc
    }

    fn write_valid_oauth_blob(cloud_state_dir: &Path, root: &StaticHardwareRoot) {
        let _doc = write_oauth_blob(
            cloud_state_dir,
            root,
            OAuthProvider::Onedrive,
            r#"{"access_token":"token-123","expiry":"2026-01-01T00:00:00Z"}"#,
        );
    }

    fn read_blob_document(cloud_state_dir: &Path, root: &StaticHardwareRoot) -> CredentialDocument {
        let blob = read_blob(&cloud_state_dir.join(CLOUD_PROVIDER_CREDS_FILENAME)).expect("read blob");
        let salt = read_salt(&cloud_state_dir.join(TESLA_SALT_FILENAME)).expect("read salt");
        let key = derive_key(root, &salt, teslausb_creds::DEFAULT_KDF_ITERS).expect("derive key");
        let material = BlobKeyMaterial {
            key,
            salt,
            kdf_iters: teslausb_creds::DEFAULT_KDF_ITERS,
        };
        let plaintext = decrypt(&blob, &material).expect("decrypt blob");
        CredentialDocument::from_bytes(&plaintext).expect("parse credential document")
    }

    fn read_oauth_token_from_blob(cloud_state_dir: &Path, root: &StaticHardwareRoot) -> String {
        let document = read_blob_document(cloud_state_dir, root);
        match document.flow {
            CredentialFlow::OAuth { token, .. } => token,
            _ => panic!("expected oauth flow"),
        }
    }

    fn sync_readback_once(
        parsed: &ServeArgs,
        baseline: &mut OAuthReadbackBaseline,
        root: &StaticHardwareRoot,
    ) -> Vec<String> {
        let mut logs = Vec::new();
        let result = readback_runtime_oauth_token(parsed, baseline, root, |line| logs.push(line.to_owned()));
        assert!(result.is_ok(), "unexpected read-back error: {result:?}");
        logs
    }

    fn write_runtime_token_conf(runtime_dir: &Path, backend: &str, token: &str) {
        fs::create_dir_all(runtime_dir).expect("create runtime dir");
        fs::write(
            runtime_dir.join(RENDERED_CONFIG_FILENAME),
            format!("[teslausb]\ntype = {backend}\ntoken = {token}\n"),
        )
        .expect("write runtime config");
    }

    fn oauth_baseline(document: &CredentialDocument) -> OAuthReadbackBaseline {
        oauth_readback_baseline(document)
            .expect("baseline normalization")
            .expect("oauth baseline")
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
                    pause: None,
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
    fn pause_reason_transitions_log_once_per_change() {
        let mut state = CycleLogState::default();
        let enqueue_summary = Some(EnqueueSummary {
            discovered_parents: 3,
            skipped_existing_parents: 2,
            enqueued_children: 1,
            skipped_remote_key_too_long: 0,
            stopped_at_parent_budget: false,
        });
        let paused_report = CycleReport {
            enqueue_summary: enqueue_summary.clone(),
            pause: Some(CyclePause {
                reason: GateReason::Storage,
                action: PauseAction::DrainNoNew,
            }),
            ..CycleReport::default()
        };
        let first = pause_transition_lines(1, &paused_report, &mut state);
        assert_eq!(first.len(), 1, "entering paused must log once");
        let second = pause_transition_lines(2, &paused_report, &mut state);
        assert!(second.is_empty(), "unchanged paused reason must not spam");

        let changed_report = CycleReport {
            enqueue_summary: enqueue_summary.clone(),
            pause: Some(CyclePause {
                reason: GateReason::Link(crate::throttle::PauseReason::LinkDown),
                action: PauseAction::PauseAtCheckpoint,
            }),
            ..CycleReport::default()
        };
        let changed = pause_transition_lines(3, &changed_report, &mut state);
        assert_eq!(changed.len(), 1, "changed pause reason must be logged");

        let resumed_report = CycleReport {
            enqueue_summary,
            pause: None,
            ..CycleReport::default()
        };
        let resumed = pause_transition_lines(4, &resumed_report, &mut state);
        assert_eq!(resumed.len(), 1, "leaving paused must be logged once");
    }

    #[test]
    fn token_readback_ignores_raw_key_order_changes() {
        let base = unique_path("uploadd-readback-order");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        let document = write_oauth_blob(
            &cloud_state_dir,
            &root,
            OAuthProvider::Onedrive,
            r#"{"access_token":"tok-a","refresh_token":"ref-a","expiry":"2026-01-01T00:00:00Z","token_type":"Bearer"}"#,
        );
        let mut baseline = oauth_baseline(&document);
        write_runtime_token_conf(
            &runtime_dir,
            "onedrive",
            r#"{"access_token":"tok-a","token_type":"Bearer","refresh_token":"ref-a","expiry":"2026-01-01T00:00:00Z"}"#,
        );
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            ..ServeArgs::default()
        };
        let before_blob =
            fs::read(cloud_state_dir.join(CLOUD_PROVIDER_CREDS_FILENAME)).expect("read blob before");
        let logs = sync_readback_once(&parsed, &mut baseline, &root);
        let after_blob = fs::read(cloud_state_dir.join(CLOUD_PROVIDER_CREDS_FILENAME)).expect("read blob after");
        assert_eq!(before_blob, after_blob);
        assert!(logs.is_empty(), "unexpected logs: {logs:?}");
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn token_readback_persists_changed_token() {
        let base = unique_path("uploadd-readback-changed");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        let document = write_oauth_blob(
            &cloud_state_dir,
            &root,
            OAuthProvider::Onedrive,
            r#"{"access_token":"tok-a","refresh_token":"ref-a"}"#,
        );
        let mut baseline = oauth_baseline(&document);
        write_runtime_token_conf(
            &runtime_dir,
            "onedrive",
            r#"{"access_token":"tok-b","refresh_token":"ref-b"}"#,
        );
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            ..ServeArgs::default()
        };
        let logs = sync_readback_once(&parsed, &mut baseline, &root);
        assert!(logs.is_empty(), "unexpected logs: {logs:?}");
        assert_eq!(
            read_oauth_token_from_blob(&cloud_state_dir, &root),
            normalize_oauth_token(r#"{"access_token":"tok-b","refresh_token":"ref-b"}"#).unwrap()
        );
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn oauth_options_survive_token_refresh_readback() {
        let base = unique_path("uploadd-readback-options");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        let token = normalize_oauth_token(r#"{"access_token":"tok-a","refresh_token":"ref-a"}"#).unwrap();
        let options = BTreeMap::from([
            ("drive_id".to_owned(), "drive-123".to_owned()),
            ("drive_type".to_owned(), "personal".to_owned()),
        ]);
        let document = CredentialDocument::new(CredentialFlow::OAuth {
            provider: OAuthProvider::Onedrive,
            token,
            options: options.clone(),
        });
        fs::create_dir_all(&cloud_state_dir).expect("create cloud dir");
        let salt = read_or_create_salt(&cloud_state_dir.join(TESLA_SALT_FILENAME)).expect("create salt");
        let key = derive_key(&root, &salt, teslausb_creds::DEFAULT_KDF_ITERS).expect("derive key");
        let material = BlobKeyMaterial {
            key,
            salt,
            kdf_iters: teslausb_creds::DEFAULT_KDF_ITERS,
        };
        let plaintext = document.to_canonical_bytes().expect("serialize doc");
        let blob = encrypt(&plaintext, &material).expect("encrypt blob");
        write_blob_atomic(&cloud_state_dir.join(CLOUD_PROVIDER_CREDS_FILENAME), &blob).expect("write blob");

        let mut baseline = oauth_baseline(&document);
        write_runtime_token_conf(
            &runtime_dir,
            "onedrive",
            r#"{"access_token":"tok-b","refresh_token":"ref-b"}"#,
        );
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            ..ServeArgs::default()
        };
        let logs = sync_readback_once(&parsed, &mut baseline, &root);
        assert!(logs.is_empty(), "unexpected logs: {logs:?}");
        let stored = read_blob_document(&cloud_state_dir, &root);
        let CredentialFlow::OAuth { token, options, .. } = stored.flow else {
            panic!("expected oauth flow");
        };
        assert_eq!(
            token,
            normalize_oauth_token(r#"{"access_token":"tok-b","refresh_token":"ref-b"}"#).unwrap()
        );
        assert_eq!(options, BTreeMap::from([
            ("drive_id".to_owned(), "drive-123".to_owned()),
            ("drive_type".to_owned(), "personal".to_owned()),
        ]));
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn token_readback_persists_two_successive_refreshes() {
        let base = unique_path("uploadd-readback-two-refreshes");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        let document = write_oauth_blob(
            &cloud_state_dir,
            &root,
            OAuthProvider::Onedrive,
            r#"{"access_token":"tok-a","refresh_token":"ref-a"}"#,
        );
        let mut baseline = oauth_baseline(&document);
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            ..ServeArgs::default()
        };

        write_runtime_token_conf(
            &runtime_dir,
            "onedrive",
            r#"{"access_token":"tok-b","refresh_token":"ref-b"}"#,
        );
        let logs_first = sync_readback_once(&parsed, &mut baseline, &root);
        assert!(logs_first.is_empty(), "unexpected first logs: {logs_first:?}");
        assert_eq!(
            read_oauth_token_from_blob(&cloud_state_dir, &root),
            normalize_oauth_token(r#"{"access_token":"tok-b","refresh_token":"ref-b"}"#).unwrap()
        );

        write_runtime_token_conf(
            &runtime_dir,
            "onedrive",
            r#"{"access_token":"tok-c","refresh_token":"ref-c"}"#,
        );
        let logs_second = sync_readback_once(&parsed, &mut baseline, &root);
        assert!(logs_second.is_empty(), "unexpected second logs: {logs_second:?}");
        assert_eq!(
            read_oauth_token_from_blob(&cloud_state_dir, &root),
            normalize_oauth_token(r#"{"access_token":"tok-c","refresh_token":"ref-c"}"#).unwrap()
        );
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn token_readback_rejects_missing_refresh_token() {
        let base = unique_path("uploadd-readback-refresh-guard");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        let document = write_oauth_blob(
            &cloud_state_dir,
            &root,
            OAuthProvider::Onedrive,
            r#"{"access_token":"tok-a","refresh_token":"ref-a"}"#,
        );
        let mut baseline = oauth_baseline(&document);
        write_runtime_token_conf(&runtime_dir, "onedrive", r#"{"access_token":"tok-b"}"#);
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            ..ServeArgs::default()
        };
        let logs = sync_readback_once(&parsed, &mut baseline, &root);
        assert!(logs.iter().any(|line| line.contains("missing refresh_token")));
        assert_eq!(
            read_oauth_token_from_blob(&cloud_state_dir, &root),
            normalize_oauth_token(r#"{"access_token":"tok-a","refresh_token":"ref-a"}"#).unwrap()
        );
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn token_readback_rejects_non_renderable_token() {
        let base = unique_path("uploadd-readback-render-guard");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        let document = write_oauth_blob(
            &cloud_state_dir,
            &root,
            OAuthProvider::Onedrive,
            r#"{"access_token":"tok-a","refresh_token":"ref-a"}"#,
        );
        let mut baseline = oauth_baseline(&document);
        write_runtime_token_conf(
            &runtime_dir,
            "onedrive",
            r#"{"access_token":"tok-b","refresh_token":"ref-b","bad":["x"]}"#,
        );
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            ..ServeArgs::default()
        };
        let logs = sync_readback_once(&parsed, &mut baseline, &root);
        assert!(logs.iter().any(|line| line.contains("cannot be rendered")));
        assert_eq!(
            read_oauth_token_from_blob(&cloud_state_dir, &root),
            normalize_oauth_token(r#"{"access_token":"tok-a","refresh_token":"ref-a"}"#).unwrap()
        );
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn token_readback_skips_when_blob_deleted_mid_run() {
        let base = unique_path("uploadd-readback-blob-deleted");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        let document = write_oauth_blob(
            &cloud_state_dir,
            &root,
            OAuthProvider::Onedrive,
            r#"{"access_token":"tok-a","refresh_token":"ref-a"}"#,
        );
        let mut baseline = oauth_baseline(&document);
        write_runtime_token_conf(
            &runtime_dir,
            "onedrive",
            r#"{"access_token":"tok-b","refresh_token":"ref-b"}"#,
        );
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            ..ServeArgs::default()
        };
        fs::remove_file(cloud_state_dir.join(CLOUD_PROVIDER_CREDS_FILENAME)).expect("delete blob");
        let logs = sync_readback_once(&parsed, &mut baseline, &root);
        assert!(logs.iter().any(|line| line.contains("blob removed")));
        assert!(!cloud_state_dir.join(CLOUD_PROVIDER_CREDS_FILENAME).exists());
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn token_readback_skips_when_blob_replaced_mid_run() {
        let base = unique_path("uploadd-readback-blob-replaced");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        let original_document = write_oauth_blob(
            &cloud_state_dir,
            &root,
            OAuthProvider::Onedrive,
            r#"{"access_token":"tok-a","refresh_token":"ref-a"}"#,
        );
        let mut baseline = oauth_baseline(&original_document);
        let replacement_token = normalize_oauth_token(r#"{"access_token":"op-new","refresh_token":"op-ref"}"#)
            .unwrap();
        let replacement_document = write_oauth_blob(
            &cloud_state_dir,
            &root,
            OAuthProvider::Onedrive,
            &replacement_token,
        );
        write_runtime_token_conf(
            &runtime_dir,
            "onedrive",
            r#"{"access_token":"tok-b","refresh_token":"ref-b"}"#,
        );
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            ..ServeArgs::default()
        };
        let logs = sync_readback_once(&parsed, &mut baseline, &root);
        assert!(logs.iter().any(|line| line.contains("changed by operator")));
        assert_eq!(read_blob_document(&cloud_state_dir, &root), replacement_document);
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn token_readback_logs_missing_or_malformed_runtime_section() {
        let base = unique_path("uploadd-readback-missing-section");
        let cloud_state_dir = base.join("state");
        let runtime_dir = base.join("run");
        let root = StaticHardwareRoot::new("00000000deadbeef", "uploadd-test-machine");
        let document = write_oauth_blob(
            &cloud_state_dir,
            &root,
            OAuthProvider::Onedrive,
            r#"{"access_token":"tok-a","refresh_token":"ref-a"}"#,
        );
        let expected_token = normalize_oauth_token(r#"{"access_token":"tok-a","refresh_token":"ref-a"}"#).unwrap();
        let mut baseline = oauth_baseline(&document);
        let parsed = ServeArgs {
            cloud_state_dir: cloud_state_dir.to_string_lossy().into_owned(),
            runtime_dir: runtime_dir.to_string_lossy().into_owned(),
            ..ServeArgs::default()
        };

        fs::create_dir_all(&runtime_dir).expect("create runtime dir");
        fs::write(
            runtime_dir.join(RENDERED_CONFIG_FILENAME),
            "[other]\ntype = onedrive\ntoken = {\"access_token\":\"tok-b\",\"refresh_token\":\"ref-b\"}\n",
        )
        .expect("write wrong section config");
        let missing_logs = sync_readback_once(&parsed, &mut baseline, &root);
        assert!(missing_logs.iter().any(|line| line.contains("missing [teslausb] section")));
        assert_eq!(read_oauth_token_from_blob(&cloud_state_dir, &root), expected_token.clone());

        fs::write(runtime_dir.join(RENDERED_CONFIG_FILENAME), "[teslausb]\nnot-a-kv-line\n")
            .expect("write malformed config");
        let malformed_logs = sync_readback_once(&parsed, &mut baseline, &root);
        assert!(malformed_logs.iter().any(|line| line.contains("is malformed")));
        assert_eq!(read_oauth_token_from_blob(&cloud_state_dir, &root), expected_token);
        let _ = fs::remove_dir_all(base);
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
        let rendered_path = rendered.expect("rendered path").path;
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
        let rendered_path = rendered.expect("rendered path").path;
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
    fn parse_serve_args_rejects_oversized_destination_id() {
        let parsed_invalid = parse_serve_args(&[
            "--destination-id".to_owned(),
            "x".repeat(129),
        ]);
        assert!(parsed_invalid.is_err());
    }

    #[test]
    fn empty_destination_id_is_accepted_and_means_derive_from_credential() {
        let parsed = parse_serve_args(&["--destination-id".to_owned(), String::new()])
            .expect("empty destination id is allowed; it is derived from the credential");
        assert!(parsed.destination_id.is_empty());
    }

    fn rendered_with_provider(provider: teslausb_creds::OAuthProvider) -> RenderedRuntimeConfig {
        let document = CredentialDocument::new(CredentialFlow::OAuth {
            provider,
            token: normalize_oauth_token(r#"{"access_token":"tok-a","refresh_token":"ref-a"}"#)
                .expect("normalize token"),
            options: BTreeMap::new(),
        });
        RenderedRuntimeConfig {
            path: PathBuf::from("/run/teslausb/rclone.conf"),
            oauth_baseline: oauth_readback_baseline(&document).expect("baseline"),
        }
    }

    #[test]
    fn destination_id_is_derived_from_the_credential_provider() {
        let parsed = ServeArgs::default();
        assert!(parsed.destination_id.is_empty());
        let rendered = rendered_with_provider(teslausb_creds::OAuthProvider::Onedrive);
        assert_eq!(
            effective_destination_id(&parsed, Some(&rendered)),
            Some("onedrive".to_owned())
        );

        let rendered_drive = rendered_with_provider(teslausb_creds::OAuthProvider::Drive);
        assert_eq!(
            effective_destination_id(&parsed, Some(&rendered_drive)),
            Some("drive".to_owned()),
            "switching provider must change the ledger destination so clips re-upload"
        );
    }

    #[test]
    fn explicit_destination_id_overrides_the_credential_provider() {
        let parsed = ServeArgs {
            destination_id: "custom-dest".to_owned(),
            ..ServeArgs::default()
        };
        let rendered = rendered_with_provider(teslausb_creds::OAuthProvider::Onedrive);
        assert_eq!(
            effective_destination_id(&parsed, Some(&rendered)),
            Some("custom-dest".to_owned())
        );
    }

    #[test]
    fn deployed_unit_command_line_parses() {
        // The shipped unit file is the source of truth: parse its ExecStart line
        // and feed those exact arguments to the parser, so the two cannot drift.
        // Before destination_id became derivable this returned Err, which made
        // `serve` exit non-zero and turned Restart=on-failure into a permanent
        // 5-second crash loop on a car battery.
        let unit_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../deploy/systemd/uploadd.service");
        let unit = std::fs::read_to_string(&unit_path)
            .unwrap_or_else(|err| panic!("read {}: {err}", unit_path.display()));
        let exec_start = unit
            .lines()
            .find_map(|line| line.strip_prefix("ExecStart="))
            .expect("uploadd.service must define ExecStart");
        let mut fields = exec_start.split_whitespace();
        let binary = fields.next().expect("ExecStart must name a binary");
        assert!(
            binary.ends_with("/uploadd"),
            "unexpected ExecStart binary: {binary}"
        );
        let subcommand = fields.next().expect("ExecStart must name a subcommand");
        assert_eq!(subcommand, "serve");
        let args: Vec<String> = fields.map(str::to_owned).collect();

        let parsed =
            parse_serve_args(&args).expect("uploadd.service ExecStart arguments must parse");        assert_eq!(
            parsed.archive_root, "/data/teslausb/archive",
            "the unit must pass the real archive root, not uploadd's /srv default"
        );
        assert!(parsed.destination_id.is_empty());
    }

    #[test]
    fn destination_id_is_none_without_a_credential_or_flag() {
        let parsed = ServeArgs::default();
        assert_eq!(
            effective_destination_id(&parsed, None),
            None,
            "no destination means uploadd must idle, never enqueue under a placeholder id"
        );
    }
}
