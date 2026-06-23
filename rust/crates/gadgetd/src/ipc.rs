//! Control-plane IPC for `gadgetd serve`: a length-prefixed JSON protocol over a
//! Unix domain socket. The daemon brings nothing up itself — gadget bring-up is
//! owned by a separate, earlier systemd unit — so a socket-bind or serve-loop
//! failure here can never disturb the car-facing LUN. On start it performs
//! **handoff crash-recovery** (clean stale loops/mounts, then re-present an
//! interrupted eject) and then serves:
//!
//! - `gadget_status` — read-only; answerable concurrently with an in-flight
//!   handoff (each connection is its own thread).
//! - `request_mutation` — serialized via a `try_lock`; a second concurrent
//!   request is refused, never queued.
//! - `handoff_status` — last/current handoff record.

use std::collections::HashMap;
use std::io::{self, Read, Write};
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::json;

use crate::config::{self, GadgetConfig};
use crate::exec::{self, LiveLun, MtimeSaveGuard};
use crate::handoff::{self, HandoffOutcome, ImageMutator, LunControl, Mutation, Partition};
use crate::mediamount::MediaRoMount;
use crate::mutate::LoopMutator;
use crate::queue::{BatchPlan, MutationQueue, MutationState};

/// Maximum accepted frame size (a handful of small JSON fields).
const MAX_FRAME: u32 = 1 << 20;
/// Per-connection I/O timeout so a slow/stuck client can't pin a thread.
const CONN_TIMEOUT: Duration = Duration::from_secs(15);
/// mtime sampling interval for the save-active guard.
const SAVE_SAMPLE: Duration = Duration::from_millis(500);
/// How often the serve loop re-checks that its control socket path still exists.
/// A peer unit sharing the runtime dir can unlink it (the reason both units now
/// set `RuntimeDirectoryPreserve=yes`); if it ever vanishes anyway we re-bind so
/// the write path heals itself instead of "listening into the void".
const SOCKET_HEALTH_INTERVAL: Duration = Duration::from_secs(2);
/// Backoff between non-blocking accept polls when no connection is pending. Adds
/// at most this much latency to picking up a new handoff request — negligible
/// against a multi-second handoff, and the cost of self-healing the socket.
const ACCEPT_POLL: Duration = Duration::from_millis(100);
/// How often the background drain worker wakes to look for queued mutations to
/// apply at the next safe window. Sub-second so an enqueue that lands during an
/// idle period applies promptly, but cheap enough to poll continuously.
const DRAIN_POLL: Duration = Duration::from_secs(1);

/// Per-partition exponential backoff for the transient "LUN busy" retry path, so
/// a car that holds a LUN for hours doesn't make the drain worker hammer configfs
/// and flood journald every `DRAIN_POLL`. Reset as soon as the partition produces
/// any non-busy outcome.
struct BusyBackoff {
    attempts: u32,
    next_eligible: Instant,
}

/// Base/cap for the busy backoff. Exponential: 1s, 2s, 4s, ... capped at 60s.
const BUSY_BACKOFF_BASE: Duration = Duration::from_secs(1);
const BUSY_BACKOFF_CAP: Duration = Duration::from_secs(60);

fn busy_backoff_delay(attempts: u32) -> Duration {
    // attempts is 1-based (first busy = attempt 1 -> BASE).
    let shift = attempts.saturating_sub(1).min(16);
    let scaled = BUSY_BACKOFF_BASE
        .checked_mul(1u32 << shift)
        .unwrap_or(BUSY_BACKOFF_CAP);
    scaled.min(BUSY_BACKOFF_CAP)
}

/// A wire request (`cmd`-tagged JSON).
#[derive(Debug, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum Request {
    /// Read-only gadget + handoff status.
    GadgetStatus,
    /// Request a serialized mutation handoff (legacy synchronous path: refuses
    /// when the host is enumerated or a save is active; kept for compatibility).
    #[serde(rename = "request_mutation")]
    Mutate {
        /// Target partition (1 = `TeslaCam`, 2 = media).
        partition: u8,
        /// The validated mutation to apply.
        mutation: Mutation,
    },
    /// Accept a validated mutation into the durable queue and return immediately.
    /// The mutation is persisted and applied automatically at the next safe
    /// window by the drain worker — it never hard-fails on a connected host.
    EnqueueMutation {
        /// Target partition (1 = `TeslaCam`, 2 = media).
        partition: u8,
        /// The mutation to enqueue (validated here before it is accepted).
        mutation: Mutation,
        /// Absolute path of the persistent staged blob backing an `InstallFile`
        /// (root-owned; reclaimed by the daemon once the entry is terminal).
        #[serde(default)]
        blob_path: Option<String>,
        /// Optional caller dedupe key; a repeat enqueue with a live key is a
        /// no-op returning the existing job id.
        #[serde(default)]
        idempotency_key: Option<String>,
    },
    /// Poll the lifecycle state of a queued mutation by its job id.
    QueueStatus {
        /// The `job_id` returned by a previous `enqueue_mutation`.
        job_id: String,
    },
    /// Poll the record for a prior handoff id.
    HandoffStatus {
        /// The id returned by a previous `request_mutation`.
        handoff_id: String,
    },
}

/// The last (or in-flight) handoff, for status reporting.
#[derive(Debug, Clone, Default)]
struct HandoffRecord {
    id: String,
    phase: String,
    result: Option<String>,
    detail: Option<String>,
}

/// Shared daemon state.
struct ServeState {
    cfg: GadgetConfig,
    runtime_root: PathBuf,
    allow_hot: bool,
    record: Mutex<Option<HandoffRecord>>,
    handoff_lock: Mutex<()>,
    next_id: AtomicU64,
    /// Durable, coalescing queue of pending mutations (the frictionless-writes
    /// path). `gadgetd` is the single writer to the image, so it owns this state.
    queue: Mutex<MutationQueue>,
    /// JSON journal backing `queue` (atomically rewritten on every transition).
    queue_path: PathBuf,
    /// Persistent read-only mount of the media image for the web read path. Its
    /// gate is suspended/resumed around media (P2) handoffs.
    media_ro: Arc<MediaRoMount>,
    /// Per-partition (keyed by partition u8: 1=TeslaCam, 2=media) busy backoff.
    busy_backoff: Mutex<HashMap<u8, BusyBackoff>>,
}

/// Read a length-prefixed frame (4-byte LE length, then the payload).
fn read_frame(stream: &mut impl Read, cap: u32) -> io::Result<Vec<u8>> {
    let mut len_buf = [0u8; 4];
    stream.read_exact(&mut len_buf)?;
    let len = u32::from_le_bytes(len_buf);
    if len > cap {
        return Err(io::Error::other(format!("frame too large: {len} > {cap}")));
    }
    let mut payload = vec![0u8; len as usize];
    stream.read_exact(&mut payload)?;
    Ok(payload)
}

/// Write a length-prefixed frame.
fn write_frame(stream: &mut impl Write, payload: &[u8]) -> io::Result<()> {
    let len = u32::try_from(payload.len())
        .map_err(|_| io::Error::other("response exceeds u32 length"))?;
    stream.write_all(&len.to_le_bytes())?;
    stream.write_all(payload)?;
    stream.flush()
}

/// Bind (or re-bind) the control socket: clear any stale file, bind, tighten
/// perms to 0o660, log. Factored out so the serve loop can re-bind if the socket
/// path is later unlinked out from under us.
fn bind_listener(socket_path: &Path) -> io::Result<UnixListener> {
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    // A stale socket file from a prior run would make bind fail with EADDRINUSE.
    match std::fs::remove_file(socket_path) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    let listener = UnixListener::bind(socket_path)?;
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o660))?;
    println!("gadgetd serve: listening on {}", socket_path.display());
    Ok(listener)
}

/// True iff the control socket path still exists AND is a socket. A peer service
/// sharing the runtime dir can unlink it on stop; the kernel keeps our existing
/// listener fd alive (so `ss` still shows us `LISTENing`) but new clients get
/// ENOENT on connect. Detecting the vanished path lets us re-bind.
fn socket_path_healthy(socket_path: &Path) -> bool {
    std::fs::symlink_metadata(socket_path)
        .map(|m| m.file_type().is_socket())
        .unwrap_or(false)
}

/// Bring up the control socket and serve until the listener errors.
///
/// # Errors
/// Returns an error only for fatal listener/bind problems; per-connection errors
/// are logged and do not stop the daemon.
pub(crate) fn serve(
    cfg: GadgetConfig,
    runtime_root: PathBuf,
    socket_path: &Path,
    allow_hot: bool,
    queue_path: PathBuf,
) -> io::Result<()> {
    // Handoff crash-recovery BEFORE serving (never touches gadget bring-up).
    // This clears any stale loop/mount on the media image, so the persistent
    // read-only mount below starts from a clean slate.
    recover_interrupted_handoff(&cfg, &runtime_root);

    // Establish the persistent read-only media mount (best-effort: a failure
    // only degrades the web read path — it must never block serving, gadget
    // bring-up, or a TeslaCam handoff).
    let media_ro = Arc::new(MediaRoMount::new(
        cfg.image_for_lun(config::MEDIA_LUN).to_path_buf(),
    ));
    if let Err(e) = media_ro.ensure_mounted() {
        eprintln!("gadgetd serve: media RO mount unavailable at startup: {e}");
    }

    let mut listener = bind_listener(socket_path)?;
    listener.set_nonblocking(true)?;

    // Load the durable queue and recover any mutation interrupted mid-handoff
    // (flip Applying -> Queued so the drain worker retries it).
    let mut queue = MutationQueue::load(&queue_path);
    let requeued = queue.requeue_inflight();
    if requeued > 0 {
        println!("gadgetd serve: requeued {requeued} mutation(s) interrupted by a prior exit");
        if let Err(e) = queue.persist(&queue_path) {
            eprintln!("gadgetd serve: persisting requeued state failed: {e}");
        }
    }

    let state = Arc::new(ServeState {
        cfg,
        runtime_root,
        allow_hot,
        record: Mutex::new(None),
        handoff_lock: Mutex::new(()),
        next_id: AtomicU64::new(1),
        queue: Mutex::new(queue),
        queue_path,
        media_ro,
        busy_backoff: Mutex::new(HashMap::new()),
    });

    // Background drain worker: applies queued mutations at safe windows.
    {
        let st = Arc::clone(&state);
        std::thread::spawn(move || drain_worker(&st));
    }

    let mut last_health = Instant::now();
    loop {
        match listener.accept() {
            Ok((stream, _addr)) => {
                // handle_conn relies on SO_RCVTIMEO/SO_SNDTIMEO, not non-blocking
                // semantics, so the accept'd stream must be blocking.
                stream.set_nonblocking(false)?;
                let st = Arc::clone(&state);
                std::thread::spawn(move || {
                    if let Err(e) = handle_conn(&stream, &st) {
                        eprintln!("gadgetd serve: connection error: {e}");
                    }
                });
            }
            Err(ref e) if e.kind() == io::ErrorKind::WouldBlock => {
                std::thread::sleep(ACCEPT_POLL);
            }
            Err(e) => eprintln!("gadgetd serve: accept error: {e}"),
        }

        // Self-heal: if our socket path was unlinked (e.g. a peer unit sharing
        // the runtime dir tore it down), re-bind so the write path recovers
        // instead of silently listening on an orphaned inode.
        if last_health.elapsed() >= SOCKET_HEALTH_INTERVAL {
            last_health = Instant::now();
            if !socket_path_healthy(socket_path) {
                eprintln!(
                    "gadgetd serve: control socket {} vanished; re-binding",
                    socket_path.display()
                );
                match bind_listener(socket_path) {
                    Ok(l) => {
                        l.set_nonblocking(true)?;
                        listener = l;
                    }
                    Err(e) => eprintln!("gadgetd serve: re-bind failed: {e}"),
                }
            }
        }
    }
}

/// On startup, reconcile an interrupted handoff on BOTH LUNs independently. Each
/// LUN backs its own single-partition image, so recovery is per-LUN: clear any
/// stale loop device / mount left on THAT LUN's image, and only then — if the
/// gadget is bound but that LUN is empty — re-present THAT LUN's own image. The
/// `TeslaCam` LUN (`lun.0`) is recovered first (it is sacred — the car-facing
/// dashcam drive must never stay ejected) and an image is only ever re-presented
/// onto its own LUN (never cross-wired). Never re-present while the image might
/// still be held locally (the never-double-writer precedence).
fn recover_interrupted_handoff(cfg: &GadgetConfig, runtime_root: &Path) {
    for lun in [config::TESLACAM_LUN, config::MEDIA_LUN] {
        recover_lun(cfg, runtime_root, lun);
    }
}

fn recover_lun(cfg: &GadgetConfig, runtime_root: &Path, lun_index: u8) {
    let image = cfg.image_for_lun(lun_index);
    let lun = LiveLun::for_lun(cfg.clone(), lun_index);
    let mutator = LoopMutator::new(image.to_path_buf(), runtime_root.to_path_buf());

    let cleanup = mutator.cleanup_stale();
    if let Err(e) = &cleanup {
        eprintln!(
            "gadgetd serve: stale-mount cleanup failed during recovery (lun.{lun_index}): {e}"
        );
    }

    match (lun.is_bound(), lun.lun_is_empty()) {
        (Ok(true), Ok(true)) => {
            if cleanup.is_ok() {
                match lun.represent() {
                    Ok(()) => {
                        println!(
                            "gadgetd serve: recovered interrupted handoff (re-presented lun.{lun_index})"
                        );
                    }
                    Err(e) => eprintln!(
                        "gadgetd serve: CRITICAL recovery re-present failed (lun.{lun_index}): {e}"
                    ),
                }
            } else {
                eprintln!(
                    "gadgetd serve: CRITICAL: lun.{lun_index} ejected but stale mounts remain; \
                     NOT re-presenting to avoid a double-writer — manual recovery needed"
                );
            }
        }
        (Ok(_), Ok(_)) => {}
        (b, f) => eprintln!(
            "gadgetd serve: recovery state read failed (lun.{lun_index}, bound={b:?}, empty={f:?})"
        ),
    }
}

fn handle_conn(stream: &UnixStream, state: &ServeState) -> io::Result<()> {
    stream.set_read_timeout(Some(CONN_TIMEOUT))?;
    stream.set_write_timeout(Some(CONN_TIMEOUT))?;
    let mut reader = stream;
    let payload = read_frame(&mut reader, MAX_FRAME)?;

    let response = match serde_json::from_slice::<Request>(&payload) {
        Ok(req) => dispatch(req, state),
        Err(e) => json!({ "error": format!("bad request: {e}") }),
    };
    let bytes = serde_json::to_vec(&response).map_err(io::Error::other)?;
    let mut writer = stream;
    write_frame(&mut writer, &bytes)
}

fn dispatch(req: Request, state: &ServeState) -> serde_json::Value {
    match req {
        Request::GadgetStatus => gadget_status(state),
        Request::Mutate {
            partition,
            mutation,
        } => request_mutation(state, partition, &mutation),
        Request::EnqueueMutation {
            partition,
            mutation,
            blob_path,
            idempotency_key,
        } => enqueue_mutation(state, partition, mutation, blob_path, idempotency_key),
        Request::QueueStatus { job_id } => queue_status(state, &job_id),
        Request::HandoffStatus { handoff_id } => handoff_status(state, &handoff_id),
    }
}

fn gadget_status(state: &ServeState) -> serde_json::Value {
    let status = exec::read_status(&state.cfg);
    let record = state.record.lock().ok().and_then(|r| r.clone());
    let handoff_active = state.handoff_lock.try_lock().is_err();
    let (pending, applying) = queue_counts(state);
    let (media_ro_mounted, media_ro_path, media_ro_error) = state.media_ro.health_snapshot();
    json!({
        "present": status.present,
        "bound": status.bound_udc.is_some(),
        "bound_udc": status.bound_udc,
        "udc_state": status.udc_state,
        "lun_file": status.lun_file,
        "media_lun_file": status.media_lun_file,
        "handoff_active": handoff_active,
        "pending_mutations": pending,
        "applying_mutations": applying,
        "media_ro_mounted": media_ro_mounted,
        "media_ro_path": media_ro_path,
        "media_ro_error": media_ro_error,
        "last_result": record.as_ref().and_then(|r| r.result.clone()),
        "last_handoff_id": record.as_ref().map(|r| r.id.clone()),
    })
}

/// (pending = queued, applying = in-flight) counts for status reporting.
fn queue_counts(state: &ServeState) -> (usize, usize) {
    let Ok(q) = state.queue.lock() else {
        return (0, 0);
    };
    let mut pending = 0;
    let mut applying = 0;
    for entry in q.entries() {
        match entry.state {
            MutationState::Queued => pending += 1,
            MutationState::Applying => applying += 1,
            _ => {}
        }
    }
    (pending, applying)
}

fn handoff_status(state: &ServeState, handoff_id: &str) -> serde_json::Value {
    match state.record.lock().ok().and_then(|r| r.clone()) {
        Some(rec) if rec.id == handoff_id => json!({
            "handoff_id": rec.id,
            "phase": rec.phase,
            "result": rec.result,
            "detail": rec.detail,
        }),
        _ => json!({ "error": format!("unknown handoff_id: {handoff_id}") }),
    }
}

fn request_mutation(state: &ServeState, partition: u8, mutation: &Mutation) -> serde_json::Value {
    let partition = match Partition::from_u8(partition) {
        Ok(p) => p,
        Err(e) => return json!({ "refused": e }),
    };
    if let Err(e) = mutation.validate() {
        return json!({ "refused": format!("invalid mutation: {e}") });
    }

    // Serialize: a second handoff is refused, never queued (spec §4).
    let Ok(_guard) = state.handoff_lock.try_lock() else {
        return json!({ "refused": "handoff_active" });
    };

    let id = format!("h-{}", state.next_id.fetch_add(1, Ordering::SeqCst));
    set_record(
        state,
        HandoffRecord {
            id: id.clone(),
            phase: "queued".to_owned(),
            ..Default::default()
        },
    );

    let lun_index = partition.lun_index();
    let image = state.cfg.image_for_lun(lun_index).to_path_buf();
    let lun = LiveLun::for_lun(state.cfg.clone(), lun_index);
    let guard = MtimeSaveGuard::new(image.clone(), SAVE_SAMPLE);
    let mutator = LoopMutator::new(image, state.runtime_root.clone());

    let outcome = handoff::run_handoff(
        &lun,
        &guard,
        &mutator,
        state.media_ro.as_ref(),
        partition,
        mutation,
        state.allow_hot,
        |phase| update_phase(state, phase.as_str()),
    );

    finalize_record(state, &id, &outcome);
    match &outcome {
        HandoffOutcome::Done => json!({ "handoff_id": id, "result": "done" }),
        HandoffOutcome::Refused(r) => json!({ "handoff_id": id, "refused": r }),
        HandoffOutcome::Failed(d) => json!({ "handoff_id": id, "result": "failed", "detail": d }),
        HandoffOutcome::Busy(d) => json!({ "handoff_id": id, "result": "busy", "detail": d }),
        HandoffOutcome::CriticalFault(d) => {
            json!({ "handoff_id": id, "result": "critical_fault", "detail": d })
        }
    }
}

fn set_record(state: &ServeState, rec: HandoffRecord) {
    if let Ok(mut r) = state.record.lock() {
        *r = Some(rec);
    }
}

/// Accept a validated mutation into the durable queue. This NEVER hard-fails on
/// a busy/connected host — that is the whole point. It refuses only when the
/// mutation is itself invalid (a real client bug, surfaced as an error) or the
/// queue is full (backpressure). On success it returns `{job_id, state}`.
fn enqueue_mutation(
    state: &ServeState,
    partition: u8,
    mutation: Mutation,
    blob_path: Option<String>,
    idempotency_key: Option<String>,
) -> serde_json::Value {
    // Validate the partition and the mutation up front so only well-formed,
    // appliable work ever enters the queue (the drain worker trusts entries).
    if let Err(e) = Partition::from_u8(partition) {
        return json!({ "error": format!("invalid partition: {e}") });
    }
    if let Err(e) = mutation.validate() {
        return json!({ "error": format!("invalid mutation: {e}") });
    }

    let Ok(mut queue) = state.queue.lock() else {
        return json!({ "error": "queue unavailable" });
    };
    match queue.enqueue(partition, mutation, blob_path, idempotency_key) {
        Ok(job_id) => {
            // Persist before returning so the accepted work is durable. A persist
            // failure is logged but still reported as queued: the entry is in the
            // live queue and will be applied + persisted on its next transition;
            // the only exposure is loss across an immediate crash, which is rare
            // and far better UX than rejecting a valid upload.
            if let Err(e) = queue.persist(&state.queue_path) {
                eprintln!(
                    "gadgetd queue: enqueue persist failed (will retry on next transition): {e}"
                );
            }
            json!({ "job_id": job_id, "state": "queued" })
        }
        Err(e) => json!({ "error": e }),
    }
}

/// Report the lifecycle state of a queued mutation. A pruned (terminal, swept)
/// entry reads as `applied` — once gone from the journal it has been applied or
/// superseded, never lost.
fn queue_status(state: &ServeState, job_id: &str) -> serde_json::Value {
    let Ok(queue) = state.queue.lock() else {
        return json!({ "error": "queue unavailable" });
    };
    match queue.find(job_id) {
        Some(entry) => json!({
            "job_id": entry.id,
            "partition": entry.partition,
            "state": entry.state,
        }),
        None => json!({ "job_id": job_id, "state": "applied" }),
    }
}

/// Background loop: at a steady cadence, apply any queued mutations that can be
/// applied right now. All the safety gating (gadget bound, host not mid-save,
/// hot-handoff policy) is enforced inside [`handoff::run_handoff`]; a refusal
/// here is transient and simply leaves the work queued for the next tick.
fn drain_worker(state: &ServeState) {
    loop {
        std::thread::sleep(DRAIN_POLL);
        drain_once(state);
    }
}

fn drain_once(state: &ServeState) {
    let partitions = match state.queue.lock() {
        Ok(q) => q.pending_partitions(),
        Err(_) => return,
    };
    for partition in partitions {
        apply_partition(state, partition);
    }
}

/// Reconcile and apply the pending work for one partition in a single idle
/// window (one handoff lock hold). Coalesced entries are retired first; the
/// winning batch is then applied, with a transient refusal leaving everything
/// queued for a later retry.
fn apply_partition(state: &ServeState, partition_u8: u8) {
    let Ok(partition) = Partition::from_u8(partition_u8) else {
        return;
    };
    if busy_backoff_active(state, partition_u8) {
        return;
    }
    let plan = match state.queue.lock() {
        Ok(q) => q.plan_batch(partition_u8),
        Err(_) => return,
    };
    if plan.is_empty() {
        return;
    }
    // Entries superseded by a later same-path mutation never apply — retire them
    // up front so their staged blobs are reclaimed even if the apply waits.
    if !plan.coalesced_seqs.is_empty() {
        retire_seqs(state, &plan.coalesced_seqs, MutationState::Coalesced);
    }
    if plan.applies.is_empty() {
        return;
    }
    // Defensive: the synthesized batch must still be valid (queue chunks deletes
    // to the cap; installs were validated at enqueue). A violation is a logic
    // bug, not a transient condition — fail those entries rather than spin.
    for mutation in &plan.applies {
        if let Err(e) = mutation.validate() {
            eprintln!("gadgetd drain: batched mutation failed validation ({e}); failing it");
            retire_seqs(state, &plan.apply_seqs, MutationState::FailedFatal);
            return;
        }
    }
    // One handoff lock for the whole window. If a legacy direct request holds it,
    // skip this tick and retry — never block the worker on a foreign handoff.
    let Ok(_guard) = state.handoff_lock.try_lock() else {
        return;
    };
    // Mark Applying and persist BEFORE touching the LUN: a crash mid-handoff is
    // then recovered by requeue_inflight on the next start.
    mark_and_persist(state, &plan.apply_seqs, MutationState::Applying);
    let disposition = run_batch_handoffs(state, &plan, partition);

    // Completed entries were retired Applied in-loop on each HandoffOutcome::Done;
    // crash/transient/fatal handling below only touches the unfinished remainder.
    dispose_batch(state, partition_u8, &plan.apply_seq_groups, disposition);
}

struct BatchDisposition {
    done_prefix: usize,
    busy: bool,
    transient: bool,
    fatal: Option<String>,
    busy_reason: Option<String>,
}

fn run_batch_handoffs(
    state: &ServeState,
    plan: &BatchPlan,
    partition: Partition,
) -> BatchDisposition {
    let lun_index = partition.lun_index();
    let image = state.cfg.image_for_lun(lun_index).to_path_buf();
    let lun = LiveLun::for_lun(state.cfg.clone(), lun_index);
    let guard = MtimeSaveGuard::new(image.clone(), SAVE_SAMPLE);
    let mutator = LoopMutator::new(image, state.runtime_root.clone());

    let (mut transient, mut busy) = (false, false);
    let mut done_prefix = 0usize;
    let (mut busy_reason, mut fatal): (Option<String>, Option<String>) = (None, None);
    for (idx, mutation) in plan.applies.iter().enumerate() {
        let id = format!("h-{}", state.next_id.fetch_add(1, Ordering::SeqCst));
        set_record(
            state,
            HandoffRecord {
                id: id.clone(),
                phase: "queued".to_owned(),
                ..Default::default()
            },
        );
        let outcome = handoff::run_handoff(
            &lun,
            &guard,
            &mutator,
            state.media_ro.as_ref(),
            partition,
            mutation,
            state.allow_hot,
            |phase| update_phase(state, phase.as_str()),
        );
        finalize_record(state, &id, &outcome);
        match outcome {
            HandoffOutcome::Done => {
                if let Some(group) = plan.apply_seq_groups.get(idx) {
                    retire_seqs(state, group, MutationState::Applied);
                }
                done_prefix += 1;
            }
            HandoffOutcome::Busy(reason) => {
                // The car holds the LUN. Keep everything queued and retry later,
                // backing off so we don't hammer configfs/journald every tick.
                busy = true;
                busy_reason = Some(reason);
                break;
            }
            HandoffOutcome::Refused(reason) => {
                // Not the mutation's fault (gadget unbound / host enumerated with
                // hot handoff off / a save in progress). Leave it queued.
                eprintln!("gadgetd drain: handoff deferred, will retry ({reason})");
                transient = true;
                break;
            }
            HandoffOutcome::Failed(detail) => {
                fatal = Some(detail);
                break;
            }
            HandoffOutcome::CriticalFault(detail) => {
                fatal = Some(format!("critical_fault: {detail}"));
                break;
            }
        }
    }
    BatchDisposition {
        done_prefix,
        busy,
        transient,
        fatal,
        busy_reason,
    }
}

fn requeue_suffix(groups: &[Vec<u64>], done_prefix: usize) -> Vec<u64> {
    use std::collections::BTreeSet;
    groups
        .iter()
        .skip(done_prefix)
        .flatten()
        .copied()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

/// On a fatal outcome, split the unfinished suffix into the single failing
/// group (`groups[done_prefix]`) and the un-attempted remainder
/// (`groups[done_prefix+1..]`). The failing group is poisoned (`FailedFatal`);
/// the remainder is requeued to retry independently.
fn fatal_split(groups: &[Vec<u64>], done_prefix: usize) -> (Vec<u64>, Vec<u64>) {
    use std::collections::BTreeSet;
    let fail: Vec<u64> = groups.get(done_prefix).cloned().unwrap_or_default();
    let requeue: Vec<u64> = groups
        .iter()
        .skip(done_prefix.saturating_add(1))
        .flatten()
        .copied()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    (fail, requeue)
}

fn dispose_batch(
    state: &ServeState,
    partition_u8: u8,
    groups: &[Vec<u64>],
    disposition: BatchDisposition,
) {
    let remainder = requeue_suffix(groups, disposition.done_prefix);
    if disposition.busy {
        mark_and_persist(state, &remainder, MutationState::Queued);
        let attempts = note_busy_backoff(state, partition_u8);
        if let Some(reason) = disposition.busy_reason {
            eprintln!(
                "gadgetd drain: partition {partition_u8} busy (attempt {attempts}), staying queued: {reason}"
            );
        }
        return;
    }
    clear_busy_backoff(state, partition_u8);
    if disposition.transient {
        mark_and_persist(state, &remainder, MutationState::Queued);
    } else if let Some(detail) = disposition.fatal {
        let (fail, requeue) = fatal_split(groups, disposition.done_prefix);
        eprintln!("gadgetd drain: batch apply failed ({detail}); marking failed_fatal");
        if !fail.is_empty() {
            retire_seqs(state, &fail, MutationState::FailedFatal);
        }
        if !requeue.is_empty() {
            mark_and_persist(state, &requeue, MutationState::Queued);
        }
    }
}

fn busy_backoff_active(state: &ServeState, partition: u8) -> bool {
    match state.busy_backoff.lock() {
        Ok(map) => map
            .get(&partition)
            .is_some_and(|b| Instant::now() < b.next_eligible),
        Err(_) => false,
    }
}

/// Record a busy outcome for `partition`, advancing the backoff. Returns the new
/// attempt count (for logging).
fn note_busy_backoff(state: &ServeState, partition: u8) -> u32 {
    let Ok(mut map) = state.busy_backoff.lock() else {
        return 0;
    };
    let entry = map.entry(partition).or_insert(BusyBackoff {
        attempts: 0,
        next_eligible: Instant::now(),
    });
    entry.attempts = entry.attempts.saturating_add(1);
    entry.next_eligible = Instant::now() + busy_backoff_delay(entry.attempts);
    entry.attempts
}

fn clear_busy_backoff(state: &ServeState, partition: u8) {
    if let Ok(mut map) = state.busy_backoff.lock() {
        map.remove(&partition);
    }
}

/// Set a non-terminal state and persist. Used for Applying (pre-handoff) and the
/// transient Applying->Queued revert.
fn mark_and_persist(state: &ServeState, seqs: &[u64], new_state: MutationState) {
    if let Ok(mut queue) = state.queue.lock() {
        queue.set_state(seqs, new_state);
        if let Err(e) = queue.persist(&state.queue_path) {
            eprintln!("gadgetd queue: state persist failed: {e}");
        }
    }
}

/// Move `seqs` to a terminal state, persist, then — only after a durable
/// persist — reclaim their staged blobs and prune. Reclaiming strictly after
/// persist guarantees a crash re-applies from the still-present blob instead of
/// losing it.
fn retire_seqs(state: &ServeState, seqs: &[u64], terminal: MutationState) {
    let mut blobs = Vec::new();
    if let Ok(mut queue) = state.queue.lock() {
        queue.set_state(seqs, terminal);
        match queue.persist(&state.queue_path) {
            Ok(()) => {
                blobs = queue.reclaimable_blobs(seqs);
                queue.prune_terminal();
                if let Err(e) = queue.persist(&state.queue_path) {
                    eprintln!("gadgetd queue: persist after prune failed: {e}");
                }
            }
            Err(e) => {
                eprintln!("gadgetd queue: terminal persist failed, not reclaiming blobs: {e}");
            }
        }
    }
    for blob in blobs {
        match std::fs::remove_file(&blob) {
            Ok(()) => {}
            Err(ref e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => eprintln!("gadgetd queue: blob reclaim failed for {blob}: {e}"),
        }
    }
}

fn update_phase(state: &ServeState, phase: &str) {
    if let Ok(mut r) = state.record.lock() {
        if let Some(rec) = r.as_mut() {
            phase.clone_into(&mut rec.phase);
        }
    }
}

fn finalize_record(state: &ServeState, id: &str, outcome: &HandoffOutcome) {
    if let Ok(mut r) = state.record.lock() {
        if let Some(rec) = r.as_mut() {
            if rec.id == id {
                rec.result = Some(outcome.kind().to_owned());
                rec.detail = outcome.detail().map(ToOwned::to_owned);
            }
        }
    }
}

#[cfg(test)]
#[allow(clippy::panic, clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::{
        MAX_FRAME, Request, busy_backoff_delay, fatal_split, read_frame, requeue_suffix,
        write_frame,
    };
    use crate::handoff::Mutation;
    use std::io::Cursor;
    use std::time::Duration;

    #[test]
    fn frame_roundtrips() {
        let mut buf = Vec::new();
        write_frame(&mut buf, b"hello").expect("write");
        let mut cur = Cursor::new(buf);
        let got = read_frame(&mut cur, MAX_FRAME).expect("read");
        assert_eq!(got, b"hello");
    }

    #[test]
    fn read_frame_rejects_oversize() {
        // Length prefix claims 2 MiB, over the 1 MiB cap.
        let mut buf = Vec::new();
        buf.extend_from_slice(&(2u32 << 20).to_le_bytes());
        let mut cur = Cursor::new(buf);
        assert!(read_frame(&mut cur, MAX_FRAME).is_err());
    }

    #[test]
    fn read_frame_errors_on_truncated_length() {
        let mut cur = Cursor::new(vec![1u8, 2u8]); // < 4 bytes
        assert!(read_frame(&mut cur, MAX_FRAME).is_err());
    }

    #[test]
    fn parses_request_mutation_delete() {
        let raw = br#"{"cmd":"request_mutation","partition":1,"mutation":{"op":"delete_path","rel_path":"TeslaCam/x"}}"#;
        let req: Request = serde_json::from_slice(raw).expect("parse");
        match req {
            Request::Mutate {
                partition,
                mutation,
            } => {
                assert_eq!(partition, 1);
                assert_eq!(
                    mutation,
                    Mutation::DeletePath {
                        rel_path: "TeslaCam/x".to_owned()
                    }
                );
            }
            other => panic!("expected request_mutation, got {other:?}"),
        }
    }

    #[test]
    fn parses_gadget_status() {
        let req: Request = serde_json::from_slice(br#"{"cmd":"gadget_status"}"#).expect("parse");
        assert!(matches!(req, Request::GadgetStatus));
    }

    #[test]
    fn parses_enqueue_mutation_with_blob_and_key() {
        let raw = br#"{"cmd":"enqueue_mutation","partition":2,"mutation":{"op":"install_file","rel_path":"LockChime.wav","source_path":"/stage/abc.wav"},"blob_path":"/stage/abc.wav","idempotency_key":"k-1"}"#;
        let req: Request = serde_json::from_slice(raw).expect("parse");
        match req {
            Request::EnqueueMutation {
                partition,
                mutation,
                blob_path,
                idempotency_key,
            } => {
                assert_eq!(partition, 2);
                assert_eq!(
                    mutation,
                    Mutation::InstallFile {
                        rel_path: "LockChime.wav".to_owned(),
                        source_path: "/stage/abc.wav".to_owned(),
                    }
                );
                assert_eq!(blob_path.as_deref(), Some("/stage/abc.wav"));
                assert_eq!(idempotency_key.as_deref(), Some("k-1"));
            }
            other => panic!("expected enqueue_mutation, got {other:?}"),
        }
    }

    #[test]
    fn parses_enqueue_mutation_without_optionals() {
        // blob_path / idempotency_key default to None when omitted.
        let raw = br#"{"cmd":"enqueue_mutation","partition":2,"mutation":{"op":"delete_path","rel_path":"Music/x.mp3"}}"#;
        let req: Request = serde_json::from_slice(raw).expect("parse");
        match req {
            Request::EnqueueMutation {
                blob_path,
                idempotency_key,
                ..
            } => {
                assert!(blob_path.is_none());
                assert!(idempotency_key.is_none());
            }
            other => panic!("expected enqueue_mutation, got {other:?}"),
        }
    }

    #[test]
    fn parses_queue_status() {
        let raw = br#"{"cmd":"queue_status","job_id":"m-7"}"#;
        let req: Request = serde_json::from_slice(raw).expect("parse");
        match req {
            Request::QueueStatus { job_id } => assert_eq!(job_id, "m-7"),
            other => panic!("expected queue_status, got {other:?}"),
        }
    }

    #[test]
    fn busy_backoff_delay_is_exponential_and_capped() {
        assert_eq!(busy_backoff_delay(1), Duration::from_secs(1));
        assert_eq!(busy_backoff_delay(2), Duration::from_secs(2));
        assert_eq!(busy_backoff_delay(3), Duration::from_secs(4));
        assert_eq!(busy_backoff_delay(7), Duration::from_secs(60));
        assert_eq!(busy_backoff_delay(100), Duration::from_secs(60));
    }

    #[test]
    fn requeue_suffix_prefix_done() {
        let groups = vec![vec![1], vec![2], vec![3]];
        assert_eq!(requeue_suffix(&groups, 1), vec![2, 3]);
    }

    #[test]
    fn requeue_suffix_none_done() {
        let groups = vec![vec![1], vec![2], vec![3]];
        assert_eq!(requeue_suffix(&groups, 0), vec![1, 2, 3]);
    }

    #[test]
    fn requeue_suffix_all_done() {
        let groups = vec![vec![1], vec![2], vec![3]];
        assert!(requeue_suffix(&groups, 3).is_empty());
    }

    #[test]
    fn requeue_suffix_handles_multi_seq_group() {
        let groups = vec![vec![1, 2], vec![3]];
        assert_eq!(requeue_suffix(&groups, 1), vec![3]);
    }

    #[test]
    fn fatal_split_middle_group_failed() {
        let groups = vec![vec![1], vec![2], vec![3]];
        assert_eq!(fatal_split(&groups, 1), (vec![2], vec![3]));
    }

    #[test]
    fn fatal_split_first_group_failed() {
        let groups = vec![vec![1], vec![2], vec![3]];
        assert_eq!(fatal_split(&groups, 0), (vec![1], vec![2, 3]));
    }

    #[test]
    fn fatal_split_last_group_failed() {
        let groups = vec![vec![1], vec![2], vec![3]];
        assert_eq!(fatal_split(&groups, 2), (vec![3], vec![]));
    }

    use super::{bind_listener, socket_path_healthy};
    use std::os::unix::fs::{FileTypeExt, PermissionsExt};

    /// Unique scratch dir under the system temp dir (no tempfile dev-dep).
    fn scratch_dir(tag: &str) -> std::path::PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let dir =
            std::env::temp_dir().join(format!("gadgetd-test-{tag}-{}-{nanos}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("mkdir");
        dir
    }

    #[test]
    fn bind_listener_creates_socket_with_0660() {
        let dir = scratch_dir("bind");
        let sock = dir.join("gadgetd.sock");
        let listener = bind_listener(&sock).expect("bind");
        let meta = std::fs::symlink_metadata(&sock).expect("stat");
        assert!(meta.file_type().is_socket(), "path should be a socket");
        assert_eq!(meta.permissions().mode() & 0o777, 0o660);
        assert!(socket_path_healthy(&sock));
        drop(listener);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn bind_listener_replaces_stale_socket() {
        let dir = scratch_dir("stale");
        let sock = dir.join("gadgetd.sock");
        let first = bind_listener(&sock).expect("first bind");
        // A second bind over the live path must succeed by clearing the stale file
        // (the EADDRINUSE-avoidance path), proving re-bind is idempotent.
        let second = bind_listener(&sock).expect("re-bind over existing");
        assert!(socket_path_healthy(&sock));
        drop(first);
        drop(second);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn socket_path_healthy_false_when_missing_or_wrong_type() {
        let dir = scratch_dir("healthy");
        let missing = dir.join("nope.sock");
        assert!(!socket_path_healthy(&missing), "missing path is unhealthy");
        // A regular file at the path is NOT a healthy socket.
        let regular = dir.join("regular");
        std::fs::write(&regular, b"x").expect("write");
        assert!(
            !socket_path_healthy(&regular),
            "regular file is not a socket"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn rebind_recovers_after_socket_unlinked() {
        // Reproduces the RuntimeDirectory bug: a peer unlinks our live socket. The
        // first listener keeps its fd, but the path is gone (unhealthy). bind_listener
        // restores a fresh, healthy socket at the same path.
        let dir = scratch_dir("rebind");
        let sock = dir.join("gadgetd.sock");
        let orphaned = bind_listener(&sock).expect("initial bind");
        std::fs::remove_file(&sock).expect("simulate peer unlink");
        assert!(!socket_path_healthy(&sock), "socket path gone after unlink");
        let healed = bind_listener(&sock).expect("re-bind");
        assert!(socket_path_healthy(&sock), "socket restored after re-bind");
        drop(orphaned);
        drop(healed);
        std::fs::remove_dir_all(&dir).ok();
    }
}
