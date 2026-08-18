//! The `gadgetd` eject-handoff client and the **car-delete planning** that
//! gates it (contract D2 §2.3, [`webd.md §2.4`], [`gadgetd.md §4`]).
//!
//! `webd` never writes the Tesla filesystem directly. A `DELETE
//! /api/clips/:id?target=car` is validated here into a [`DeletePlan`] (which
//! partition, which exact files) and forwarded to `gadgetd`'s `request_mutation`
//! over a length-prefixed JSON Unix socket. `gadgetd` ejects the LUN, mounts the
//! image, deletes the files, and re-presents.
//!
//! ## Why the planning is fail-closed
//!
//! Deleting the wrong path on the car volume is irrecoverable, so the planner
//! refuses anything it cannot *prove* is exactly the addressed clip's own files:
//!
//! * Only `partition == "slot0"` (the `TeslaCam` volume) maps to `gadgetd`
//!   partition `1`; anything else is refused (a media/unknown slot is never
//!   car-deleted).
//! * Only `SavedClips` / `SentryClips` are car-deletable. `RecentClips` is
//!   car-owned rotation ([`retentiond.md §3.3`]); `ArchivedClips` is Pi-side;
//!   `TeslaTrackMode` is unproven on-disk → all refused.
//! * Each `ro_usb` angle's `file_ref` must **equal** the path derived from the
//!   clip's own `canonical_key` plus that angle's `camera`
//!   (`TeslaCam/<class>/<event>/<stem>-<camera>.mp4`). Because `scannerd`
//!   constructs `file_ref` and `canonical_key` from the same scanned path, a
//!   well-formed clip always satisfies this; any mismatch is treated as a
//!   corrupt/forged row and the whole delete is refused (never a partial guess).
//!
//! The derived paths — not the raw DB strings — are what `webd` sends, and
//! `gadgetd` independently re-validates + jails every path on its side.

use std::collections::HashSet;

use serde_json::{Value, json};
use teslausb_core::durable_mutation::{sanitize_public_error, validate_mutation_job_id};

/// Upper bound on the files in one car-delete (must match `gadgetd`'s
/// `MAX_DELETE_PATHS`). A clip has one file per camera (≤6 today).
const MAX_DELETE_PATHS: usize = 16;

/// A validated, ready-to-send car-delete: the `gadgetd` partition index and the
/// exact partition-root-relative files to remove in one handoff.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct DeletePlan {
    /// `gadgetd` partition (1 = `TeslaCam`).
    pub partition: u8,
    /// Partition-root-relative file paths (deduped, sorted, ≤[`MAX_DELETE_PATHS`]).
    pub rel_paths: Vec<String>,
}

/// Why a car-delete was refused before any handoff (maps to an HTTP status in
/// the route layer).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum DeleteRefusal {
    /// The clip is not on a car-deletable partition/class → `422`.
    NotCarDeletable(String),
    /// The clip is not currently on the live USB volume → `409`.
    NotPresent,
    /// The clip's catalog rows are inconsistent/corrupt; fail closed → `422`.
    InvalidClip(String),
}

/// Plan a `target=car` clip delete from the clip's catalog facts and its
/// `ro_usb` angles. Pure and fail-closed: returns the exact files to delete, or
/// a refusal — it never contacts `gadgetd`.
///
/// `angles` are `(camera, file_ref)` pairs with `view_kind = 'ro_usb'`.
pub(crate) fn plan_car_delete(
    partition: &str,
    folder_class: &str,
    availability: &str,
    canonical_key: &str,
    angles: &[(String, String)],
) -> Result<DeletePlan, DeleteRefusal> {
    // 1. Partition: only slot0 (TeslaCam) → gadgetd partition 1. (B1 invariant:
    //    scannerd writes "slot{N}", N = 0-based MBR slot; slot0 = p1 TeslaCam.)
    let gadget_partition = match partition {
        "slot0" => 1u8,
        other => {
            return Err(DeleteRefusal::NotCarDeletable(format!(
                "clip partition `{other}` is not the TeslaCam volume"
            )));
        }
    };

    // 2. Folder class: only SavedClips / SentryClips are car-deletable.
    if !matches!(folder_class, "SavedClips" | "SentryClips") {
        return Err(DeleteRefusal::NotCarDeletable(format!(
            "folder_class `{folder_class}` is not car-deletable"
        )));
    }

    // 3. Availability: must be live on the USB volume.
    if availability != "present" {
        return Err(DeleteRefusal::NotPresent);
    }

    // 4. Parse the canonical_key: "<slot>:TeslaCam/<class>/<event>/<stem>".
    let (slot_str, path_part) = canonical_key.split_once(':').ok_or_else(|| {
        DeleteRefusal::InvalidClip("canonical_key is missing its slot prefix".to_owned())
    })?;
    if slot_str != "0" {
        return Err(DeleteRefusal::InvalidClip(format!(
            "canonical_key slot `{slot_str}` is inconsistent with partition `{partition}`"
        )));
    }
    let comps: Vec<&str> = path_part.split('/').collect();
    let valid_shape = matches!(
        comps.as_slice(),
        [root, class, _event, _stem] if *root == "TeslaCam" && *class == folder_class
    );
    if !valid_shape {
        return Err(DeleteRefusal::InvalidClip(format!(
            "canonical_key path `{path_part}` is not TeslaCam/{folder_class}/<event>/<stem>"
        )));
    }
    for comp in &comps {
        if comp.is_empty() || *comp == "." || *comp == ".." || comp.contains('\0') {
            return Err(DeleteRefusal::InvalidClip(
                "canonical_key has an empty/traversal/NUL component".to_owned(),
            ));
        }
    }

    // 5. Must have at least one car-visible angle.
    if angles.is_empty() {
        return Err(DeleteRefusal::InvalidClip(
            "clip has no car-visible (ro_usb) angles to delete".to_owned(),
        ));
    }
    if angles.len() > MAX_DELETE_PATHS {
        return Err(DeleteRefusal::InvalidClip(format!(
            "clip has {} angles, over the cap of {MAX_DELETE_PATHS}",
            angles.len()
        )));
    }

    // 6. Each ro_usb file_ref must EQUAL the path derived from canonical_key +
    //    camera. This is exactly how scannerd built file_ref, so it holds for
    //    well-formed data and structurally forbids deleting anything but the
    //    addressed clip's own minute files.
    let mut rel_paths = Vec::with_capacity(angles.len());
    let mut seen = HashSet::new();
    for (camera, file_ref) in angles {
        if camera.is_empty()
            || camera.contains('/')
            || camera.contains('\0')
            || *camera == "."
            || *camera == ".."
        {
            return Err(DeleteRefusal::InvalidClip(format!(
                "angle camera `{camera}` has an unsafe shape"
            )));
        }
        let expected = format!("{path_part}-{camera}.mp4");
        if file_ref != &expected {
            return Err(DeleteRefusal::InvalidClip(format!(
                "angle file_ref `{file_ref}` does not match the derived clip path `{expected}`"
            )));
        }
        if !seen.insert(expected.clone()) {
            return Err(DeleteRefusal::InvalidClip(format!(
                "duplicate camera `{camera}` in the angle set"
            )));
        }
        rel_paths.push(expected);
    }
    rel_paths.sort();

    Ok(DeletePlan {
        partition: gadget_partition,
        rel_paths,
    })
}

/// Build the `request_mutation` wire request for a planned car-delete.
pub(crate) fn delete_request(plan: &DeletePlan) -> Value {
    json!({
        "cmd": "request_mutation",
        "partition": plan.partition,
        "mutation": { "op": "delete_paths", "rel_paths": plan.rel_paths },
    })
}

/// Build the `enqueue_mutation` request to install a staged file on a partition
/// via the durable, frictionless write path. Rather than refusing on a
/// connected host (the old synchronous handoff behaviour), `gadgetd` accepts
/// this immediately, persists it, and applies it at the next safe window.
/// `source_path` doubles as `blob_path` so `gadgetd` reclaims (unlinks) the
/// staged file once the entry reaches a terminal state — `webd` must NOT unlink
/// it on the success path. `rel_path` must be a fixed, validated,
/// partition-root-relative destination, never attacker-controlled.
pub(crate) fn enqueue_install_request(partition: u8, rel_path: &str, source_path: &str) -> Value {
    json!({
        "cmd": "enqueue_mutation",
        "partition": partition,
        "mutation": { "op": "install_file", "rel_path": rel_path, "source_path": source_path },
        "blob_path": source_path,
    })
}

/// Build the `enqueue_mutation` request to remove one or more files in a single
/// future handoff via the durable write path. Uses `gadgetd`'s regular-file-
/// only, idempotent-on-absent `delete_paths` set form (not `delete_path`):
/// removing an already-absent asset is a success (a retried remove is safe),
/// and a directory at a path is refused rather than recursively deleted. A
/// single handoff for the whole set is deliberate — every handoff ejects and
/// remounts the car-facing USB, so deleting `N` files in `N` handoffs would be
/// `N` disconnect cycles. No `blob_path` (a delete stages nothing). `rel_paths`
/// must be fixed, validated, partition-root-relative destinations.
pub(crate) fn enqueue_remove_request_many(partition: u8, rel_paths: &[String]) -> Value {
    json!({
        "cmd": "enqueue_mutation",
        "partition": partition,
        "mutation": { "op": "delete_paths", "rel_paths": rel_paths },
    })
}

/// Build the `enqueue_mutation` request to prune an **empty** directory (and any
/// now-empty ancestors) at `rel_path` via the durable write path. This
/// complements [`enqueue_remove_request_many`]: `delete_paths` is regular-file-
/// only (it refuses directories so a clip delete can never recurse), which
/// leaves the now-empty folder behind after its files are removed. `gadgetd`'s
/// `remove_empty_dir` uses an empty-only `remove_dir` (NEVER recursive, so it
/// can never delete a file), refuses protected/structural directories, and is
/// idempotent on an already-absent directory (so a retried prune is safe). No
/// `blob_path` (a prune stages nothing). `rel_path` must be a fixed, validated,
/// partition-root-relative directory.
pub(crate) fn enqueue_remove_empty_dir_request(partition: u8, rel_path: &str) -> Value {
    json!({
        "cmd": "enqueue_mutation",
        "partition": partition,
        "mutation": { "op": "remove_empty_dir", "rel_path": rel_path },
    })
}

/// Build the `handoff_status` wire request for a prior handoff id.
pub(crate) fn status_request(handoff_id: &str) -> Value {
    json!({ "cmd": "handoff_status", "handoff_id": handoff_id })
}

/// Build the read-only `gadget_status` wire request. `gadgetd` answers this
/// concurrently with an in-flight handoff, so it never blocks the UI.
pub(crate) fn gadget_status_request() -> Value {
    json!({ "cmd": "gadget_status" })
}

/// The terminal outcome of a `gadgetd` mutation handoff (clip delete, media
/// install, or media remove), as interpreted from `gadgetd`'s JSON response
/// (mapped to an HTTP status in the route layer). The response shape is
/// identical across mutation ops, so a single interpreter serves all of them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum MutationOutcome {
    /// Mutation applied, LUN re-presented → `200`.
    Done(String),
    /// `gadgetd` declined because of a transient device state the caller should
    /// retry (another handoff in flight, car mid-save, gadget not currently
    /// bound, or a hot handoff that is not yet HW-validated) → `409`. Carries
    /// the raw `gadgetd` reason.
    Busy(String),
    /// `gadgetd` refused the request for a permanent/validation reason → `422`.
    Refused(String),
    /// The handoff failed but the car got its drive back → `502`.
    Failed { handoff_id: String, detail: String },
    /// The LUN was left ejected (recovery on `gadgetd` restart) → `500`.
    CriticalFault { handoff_id: String, detail: String },
    /// `gadgetd` returned a response `webd` could not interpret → `502`.
    BadResponse(String),
}

/// `gadgetd` guard refusals that reflect a transient device state the caller
/// should retry (HTTP `409`), versus a permanent validation refusal (`422`).
/// These strings are the reasons `gadgetd`'s pre-eject handoff guard emits
/// (`gadgetd/src/handoff.rs`): nothing has been mutated when they fire.
fn is_retryable_refusal(reason: &str) -> bool {
    reason == "handoff_active"
        || reason == "save_active"
        || reason.starts_with("gadget not bound")
        || reason.starts_with("hot_handoff_unvalidated")
}

/// Interpret a `gadgetd` `request_mutation` response into a [`MutationOutcome`].
/// Op-agnostic: the response shape is identical for delete, install, and remove.
pub(crate) fn map_mutation_outcome(resp: &Value) -> MutationOutcome {
    let handoff_id = resp.get("handoff_id").and_then(Value::as_str);

    if let Some(reason) = resp.get("refused").and_then(Value::as_str) {
        let reason = sanitize_public_error(reason);
        return if is_retryable_refusal(&reason) {
            MutationOutcome::Busy(reason)
        } else {
            MutationOutcome::Refused(reason)
        };
    }
    if let Some(err) = resp.get("error").and_then(Value::as_str) {
        return MutationOutcome::BadResponse(sanitize_public_error(err));
    }
    let detail = || sanitize_public_error(resp.get("detail").and_then(Value::as_str).unwrap_or(""));
    match resp.get("result").and_then(Value::as_str) {
        Some("done") => match handoff_id {
            Some(id) => MutationOutcome::Done(id.to_owned()),
            None => MutationOutcome::BadResponse("done without a handoff_id".to_owned()),
        },
        Some("failed") => MutationOutcome::Failed {
            handoff_id: handoff_id.unwrap_or_default().to_owned(),
            detail: detail(),
        },
        Some("busy") => MutationOutcome::Busy(detail()),
        Some("critical_fault") => MutationOutcome::CriticalFault {
            handoff_id: handoff_id.unwrap_or_default().to_owned(),
            detail: detail(),
        },
        _ => MutationOutcome::BadResponse("unexpected gadgetd response".to_owned()),
    }
}

/// The result of an `enqueue_mutation` request (the frictionless write path).
/// Distinct from [`MutationOutcome`]: enqueue does NOT wait for the handoff, so
/// there is no done/failed/critical — only accepted-and-durable, rejected-up-
/// front, or an unparseable reply.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum QueueOutcome {
    /// Accepted into the durable queue; will apply at the next safe window.
    /// Carries the `job_id` the SPA can poll/correlate. → `202`.
    Queued { job_id: String },
    /// `gadgetd` rejected the mutation before queueing it — an invalid mutation
    /// or partition (a client bug) or a full queue (backpressure). → `422`.
    Rejected(String),
    /// `gadgetd` could not durably persist queue state. Retry later. → `503`.
    Unavailable(String),
    /// `gadgetd` could not confirm post-commit durability. Mutation may have
    /// been accepted; caller must not unlink staged blobs. → `503`.
    Ambiguous {
        job_id: Option<String>,
        detail: String,
    },
    /// `gadgetd` returned a reply `webd` could not interpret. → `502`.
    BadResponse(String),
}

/// Interpret a `gadgetd` `enqueue_mutation` response into a [`QueueOutcome`].
pub(crate) fn map_queue_outcome(resp: &Value) -> QueueOutcome {
    if let Some(err) = resp.get("error").and_then(Value::as_str) {
        if resp.get("error_code").and_then(Value::as_str) == Some("queue_persist_ambiguous") {
            let job_id = resp
                .get("job_id")
                .and_then(Value::as_str)
                .filter(|id| validate_mutation_job_id(id).is_ok())
                .map(ToOwned::to_owned);
            let detail = resp.get("detail").and_then(Value::as_str).unwrap_or(err);
            return QueueOutcome::Ambiguous {
                job_id,
                detail: sanitize_public_error(detail),
            };
        }
        if resp.get("error_code").and_then(Value::as_str) == Some("queue_unavailable") {
            let detail = resp.get("detail").and_then(Value::as_str).unwrap_or(err);
            return QueueOutcome::Unavailable(sanitize_public_error(detail));
        }
        return QueueOutcome::Rejected(sanitize_public_error(err));
    }
    match (
        resp.get("job_id").and_then(Value::as_str),
        resp.get("state").and_then(Value::as_str),
    ) {
        (Some(job_id), Some("queued")) => match validate_mutation_job_id(job_id) {
            Ok(()) => QueueOutcome::Queued {
                job_id: job_id.to_owned(),
            },
            Err(_) => QueueOutcome::BadResponse("unexpected gadgetd enqueue response".to_owned()),
        },
        _ => QueueOutcome::BadResponse("unexpected gadgetd enqueue response".to_owned()),
    }
}

/// Normalize a `gadgetd` `handoff_status` response to the D2 shape
/// `{handoff_id, state, detail}`. `None` means an unknown handoff id (`404`).
pub(crate) fn map_status(resp: &Value) -> Option<Value> {
    if resp.get("error").is_some() {
        return None;
    }
    let handoff_id = resp.get("handoff_id").and_then(Value::as_str)?;
    // A terminal `result` (done/failed/...) outranks the in-flight `phase`.
    let state = resp
        .get("result")
        .and_then(Value::as_str)
        .or_else(|| resp.get("phase").and_then(Value::as_str))
        .unwrap_or("unknown");
    Some(json!({
        "handoff_id": handoff_id,
        "state": state,
        "detail": resp.get("detail").cloned().unwrap_or(Value::Null),
    }))
}

/// Normalize a `gadgetd` `gadget_status` response into the stable
/// `/api/gadget/status` shape the SPA consumes. `present` is the load-bearing
/// field; if it is absent the frame is unusable and we return `None` (mapped to
/// `502`). All other fields degrade to `false`/`null` so the read never 500s on
/// a partial reply. The `media_ro_*` fields (RO media-mount health) and the
/// `pending_mutations`/`applying_mutations` queue counts are passed through for
/// observability; they are `null` when an older `gadgetd` omits them. The
/// `chime_reenum_pending` flag + `last_reenum` object drive the SPA's "syncing
/// chime to the car — keep the doors closed" overlay (i2 auto-reenum); they
/// degrade to `false`/`null` so an older `gadgetd` simply never shows the overlay.
pub(crate) fn map_gadget_status(resp: &Value) -> Option<Value> {
    if resp.get("error").is_some() {
        return None;
    }
    let present = resp.get("present").and_then(Value::as_bool)?;
    let field = |k: &str| resp.get(k).cloned().unwrap_or(Value::Null);
    let flag = |k: &str| resp.get(k).and_then(Value::as_bool).unwrap_or(false);
    Some(json!({
        "present": present,
        "bound": flag("bound"),
        "bound_udc": field("bound_udc"),
        "udc_state": field("udc_state"),
        "lun_file": field("lun_file"),
        "media_lun_file": field("media_lun_file"),
        "handoff_active": flag("handoff_active"),
        "pending_mutations": field("pending_mutations"),
        "applying_mutations": field("applying_mutations"),
        "media_ro_mounted": field("media_ro_mounted"),
        "media_ro_path": field("media_ro_path"),
        "media_ro_error": field("media_ro_error"),
        "chime_reenum_pending": flag("chime_reenum_pending"),
        "last_reenum": field("last_reenum"),
        "last_handoff_id": field("last_handoff_id"),
        "last_result": field("last_result"),
    }))
}

/// Normalize a `gadgetd` `gadget_status` response into a bounded mode/status
/// envelope for UI parity with legacy main status cards:
/// - current gadget mode (`presented`/`syncing`/`degraded`/`unavailable`);
/// - handoff lifecycle facts (active/idle, queue counts, last terminal result);
/// - LUN/image status (path + loaded flag per LUN);
/// - recovery/banner facts backed only by existing runtime/persisted status.
///
/// Returns `None` when `present` is absent (unusable frame). Optional fields from
/// older `gadgetd` builds degrade to `null`/`false`; no synthetic mutation state
/// is invented.
pub(crate) fn map_gadget_mode_status(resp: &Value) -> Option<Value> {
    if resp.get("error").is_some() {
        return None;
    }
    let present = resp.get("present").and_then(Value::as_bool)?;
    let bound = resp.get("bound").and_then(Value::as_bool).unwrap_or(false);
    let handoff_active = resp
        .get("handoff_active")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let chime_reenum_pending = resp
        .get("chime_reenum_pending")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let udc_configured = resp.get("udc_state").and_then(Value::as_str) == Some("configured");
    let mode = if handoff_active || chime_reenum_pending {
        "syncing"
    } else if present && bound && udc_configured {
        "presented"
    } else if present {
        "degraded"
    } else {
        "unavailable"
    };
    let field = |k: &str| resp.get(k).cloned().unwrap_or(Value::Null);
    let handoff_state = if handoff_active { "active" } else { "idle" };

    let loaded = |k: &str| match resp.get(k) {
        Some(Value::String(v)) => Value::Bool(!v.is_empty()),
        Some(Value::Null) => Value::Bool(false),
        Some(_) => Value::Null,
        None => Value::Null,
    };

    let banner = if handoff_active {
        json!({
            "level": "info",
            "code": "handoff_active",
            "message": "USB file operation in progress."
        })
    } else if chime_reenum_pending {
        json!({
            "level": "info",
            "code": "chime_reenum_pending",
            "message": "LockChime sync pending; keep vehicle doors closed until USB re-enumeration finishes."
        })
    } else {
        match resp.get("last_result").and_then(Value::as_str) {
            Some("critical_fault") => json!({
                "level": "warning",
                "code": "handoff_critical_fault",
                "message": "Last handoff ended in a critical fault; recovery may be required."
            }),
            Some("failed") => json!({
                "level": "warning",
                "code": "handoff_failed",
                "message": "Last handoff failed; inspect status before retrying."
            }),
            _ => Value::Null,
        }
    };

    Some(json!({
        "mode": mode,
        "present": present,
        "bound": bound,
        "bound_udc": field("bound_udc"),
        "udc_state": field("udc_state"),
        "handoff": {
            "state": handoff_state,
            "active": handoff_active,
            "pending_mutations": field("pending_mutations"),
            "applying_mutations": field("applying_mutations"),
            "last_handoff_id": field("last_handoff_id"),
            "last_result": field("last_result"),
        },
        "lun": {
            "teslacam": {
                "image": field("lun_file"),
                "loaded": loaded("lun_file"),
            },
            "media": {
                "image": field("media_lun_file"),
                "loaded": loaded("media_lun_file"),
            }
        },
        "recovery": {
            "chime_reenum_pending": chime_reenum_pending,
            "last_reenum": field("last_reenum"),
            "media_ro_mounted": field("media_ro_mounted"),
            "media_ro_path": field("media_ro_path"),
            "media_ro_error": field("media_ro_error"),
        },
        "banner": banner,
    }))
}

/// A failure talking to `gadgetd`, distinguished so the route can answer `503`
/// (gadgetd down / socket absent / timed out) vs `502` (protocol/parse error).
#[derive(Debug)]
pub(crate) enum TransportError {
    /// Could not reach `gadgetd` (connect refused/missing socket/timeout).
    Unavailable(String),
    /// Reached `gadgetd` but the framing/JSON was unusable. Only the real
    /// (cfg(unix)) socket client produces this; the non-Unix stub never does.
    #[cfg_attr(not(unix), allow(dead_code))]
    Protocol(String),
}

/// A one-shot request/response client for the `gadgetd` control socket. Boxed as
/// `dyn` in [`crate::AppState`] so tests can inject a mock; the blocking socket
/// I/O is offloaded via `spawn_blocking` by the caller.
pub(crate) trait GadgetClient: Send + Sync {
    /// Send one framed JSON request and return the parsed JSON response.
    fn call(&self, request: Value) -> Result<Value, TransportError>;
}

#[cfg(unix)]
pub(crate) use unix_client::UnixGadgetClient;

#[cfg(not(unix))]
pub(crate) use stub_client::UnavailableGadgetClient;

#[cfg(unix)]
mod unix_client {
    use std::io::{self, Read, Write};
    use std::os::unix::net::UnixStream;
    use std::path::PathBuf;
    use std::time::Duration;

    use serde_json::Value;

    use super::{GadgetClient, TransportError};

    /// Maximum accepted frame size (matches `gadgetd`'s `MAX_FRAME`).
    const MAX_FRAME: u32 = 1 << 20;
    /// Socket read/write timeout. Generous: a handoff runs ~5 s synchronously
    /// between `gadgetd` reading the request and writing the response.
    const CLIENT_TIMEOUT: Duration = Duration::from_secs(30);

    /// A `gadgetd` control-socket client over a Unix domain socket.
    pub(crate) struct UnixGadgetClient {
        sock: PathBuf,
    }

    impl UnixGadgetClient {
        pub(crate) fn new(sock: PathBuf) -> Self {
            Self { sock }
        }
    }

    impl GadgetClient for UnixGadgetClient {
        fn call(&self, request: Value) -> Result<Value, TransportError> {
            let payload = serde_json::to_vec(&request)
                .map_err(|e| TransportError::Protocol(e.to_string()))?;

            let mut stream = UnixStream::connect(&self.sock).map_err(|e| {
                TransportError::Unavailable(format!("connect {}: {e}", self.sock.display()))
            })?;
            stream.set_read_timeout(Some(CLIENT_TIMEOUT)).ok();
            stream.set_write_timeout(Some(CLIENT_TIMEOUT)).ok();

            write_frame(&mut stream, &payload)
                .map_err(|e| TransportError::Unavailable(format!("write: {e}")))?;
            let resp = read_frame(&mut stream, MAX_FRAME).map_err(map_read_error)?;
            serde_json::from_slice(&resp)
                .map_err(|e| TransportError::Protocol(format!("decode: {e}")))
        }
    }

    fn map_read_error(error: io::Error) -> TransportError {
        match error.kind() {
            io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock => {
                TransportError::Unavailable(format!("read: {error}"))
            }
            _ => TransportError::Protocol(format!("read: {error}")),
        }
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
            .map_err(|_| io::Error::other("request exceeds u32 length"))?;
        stream.write_all(&len.to_le_bytes())?;
        stream.write_all(payload)?;
        stream.flush()
    }

    #[cfg(test)]
    mod tests {
        use std::io;

        use super::{TransportError, map_read_error};

        #[test]
        fn timeout_read_maps_to_unavailable() {
            let mapped = map_read_error(io::Error::new(io::ErrorKind::TimedOut, "timed out"));
            assert!(matches!(mapped, TransportError::Unavailable(_)));
        }

        #[test]
        fn non_timeout_read_maps_to_protocol() {
            let mapped = map_read_error(io::Error::new(io::ErrorKind::InvalidData, "bad frame"));
            assert!(matches!(mapped, TransportError::Protocol(_)));
        }
    }
}

#[cfg(not(unix))]
mod stub_client {
    use serde_json::Value;

    use super::{GadgetClient, TransportError};

    /// A no-op client for non-Unix build hosts: `gadgetd`'s Unix socket does not
    /// exist there, so every call reports the service as unavailable. The `webd`
    /// binary only runs on the Pi (Linux); this keeps the dev host compiling.
    pub(crate) struct UnavailableGadgetClient;

    impl GadgetClient for UnavailableGadgetClient {
        fn call(&self, _request: Value) -> Result<Value, TransportError> {
            Err(TransportError::Unavailable(
                "gadgetd socket is not available on this platform".to_owned(),
            ))
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::panic, clippy::indexing_slicing)]
mod tests {
    use super::{
        DeleteRefusal, MutationOutcome, QueueOutcome, enqueue_install_request,
        enqueue_remove_empty_dir_request, enqueue_remove_request_many, map_gadget_mode_status,
        map_gadget_status, map_mutation_outcome, map_queue_outcome, map_status, plan_car_delete,
    };
    use serde_json::{Value, json};

    const KEY: &str = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";

    fn angles() -> Vec<(String, String)> {
        vec![
            (
                "back".to_owned(),
                "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-back.mp4".to_owned(),
            ),
            (
                "front".to_owned(),
                "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-front.mp4".to_owned(),
            ),
        ]
    }

    #[test]
    fn plans_a_well_formed_saved_clip() {
        let plan = plan_car_delete("slot0", "SavedClips", "present", KEY, &angles()).unwrap();
        assert_eq!(plan.partition, 1);
        assert_eq!(
            plan.rel_paths,
            vec![
                "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-back.mp4".to_owned(),
                "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-front.mp4".to_owned(),
            ]
        );
    }

    #[test]
    fn refuses_non_slot0_partition() {
        let err = plan_car_delete("slot1", "SavedClips", "present", KEY, &angles()).unwrap_err();
        assert!(matches!(err, DeleteRefusal::NotCarDeletable(_)));
    }

    #[test]
    fn refuses_recentclips() {
        let key = "0:TeslaCam/RecentClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let err = plan_car_delete("slot0", "RecentClips", "present", key, &[]).unwrap_err();
        assert!(matches!(err, DeleteRefusal::NotCarDeletable(_)));
    }

    #[test]
    fn refuses_when_not_present() {
        let err = plan_car_delete("slot0", "SavedClips", "missing", KEY, &angles()).unwrap_err();
        assert_eq!(err, DeleteRefusal::NotPresent);
    }

    #[test]
    fn refuses_file_ref_that_escapes_the_clip() {
        // A forged file_ref pointing at a sibling clip's file must be refused.
        let bad = vec![(
            "front".to_owned(),
            "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-09-04-front.mp4".to_owned(),
        )];
        let err = plan_car_delete("slot0", "SavedClips", "present", KEY, &bad).unwrap_err();
        assert!(matches!(err, DeleteRefusal::InvalidClip(_)));
    }

    #[test]
    fn refuses_canonical_key_class_mismatch() {
        // canonical_key says SentryClips but the row's folder_class is SavedClips.
        let key = "0:TeslaCam/SentryClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let err = plan_car_delete("slot0", "SavedClips", "present", key, &angles()).unwrap_err();
        assert!(matches!(err, DeleteRefusal::InvalidClip(_)));
    }

    #[test]
    fn refuses_empty_angle_set() {
        let err = plan_car_delete("slot0", "SavedClips", "present", KEY, &[]).unwrap_err();
        assert!(matches!(err, DeleteRefusal::InvalidClip(_)));
    }

    #[test]
    fn refuses_slot_prefix_mismatch() {
        // partition slot0 but canonical_key claims slot 1.
        let key = "1:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let err = plan_car_delete("slot0", "SavedClips", "present", key, &angles()).unwrap_err();
        assert!(matches!(err, DeleteRefusal::InvalidClip(_)));
    }

    #[test]
    fn maps_done_outcome() {
        let resp = json!({ "handoff_id": "h-7", "result": "done" });
        assert_eq!(
            map_mutation_outcome(&resp),
            MutationOutcome::Done("h-7".to_owned())
        );
    }

    #[test]
    fn maps_busy_outcome() {
        let resp = json!({ "refused": "handoff_active" });
        assert_eq!(
            map_mutation_outcome(&resp),
            MutationOutcome::Busy("handoff_active".to_owned())
        );
    }

    #[test]
    fn maps_save_active_as_busy() {
        // Car mid-save is a transient, retryable (409) state, not a 422.
        let resp = json!({ "handoff_id": "h-9", "refused": "save_active" });
        assert_eq!(
            map_mutation_outcome(&resp),
            MutationOutcome::Busy("save_active".to_owned())
        );
    }

    #[test]
    fn maps_gadget_unbound_and_hot_handoff_as_busy() {
        for reason in [
            "gadget not bound",
            "hot_handoff_unvalidated: host is enumerated",
        ] {
            let resp = json!({ "handoff_id": "h-9", "refused": reason });
            assert!(
                matches!(map_mutation_outcome(&resp), MutationOutcome::Busy(_)),
                "reason `{reason}` should be retryable"
            );
        }
    }

    #[test]
    fn refuses_unsafe_camera_shape() {
        for cam in ["", "front/back", "..", "."] {
            let bad = vec![(
                cam.to_owned(),
                format!("TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-{cam}.mp4"),
            )];
            let err = plan_car_delete("slot0", "SavedClips", "present", KEY, &bad).unwrap_err();
            assert!(
                matches!(err, DeleteRefusal::InvalidClip(_)),
                "camera `{cam}` should be refused"
            );
        }
    }

    #[test]
    fn refuses_canonical_key_traversal_component() {
        let key = "0:TeslaCam/SavedClips/../2026-06-01_20-10-04";
        let err = plan_car_delete("slot0", "SavedClips", "present", key, &angles()).unwrap_err();
        assert!(matches!(err, DeleteRefusal::InvalidClip(_)));
    }

    #[test]
    fn refuses_duplicate_camera() {
        let dup = vec![
            (
                "front".to_owned(),
                "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-front.mp4".to_owned(),
            ),
            (
                "front".to_owned(),
                "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-front.mp4".to_owned(),
            ),
        ];
        let err = plan_car_delete("slot0", "SavedClips", "present", KEY, &dup).unwrap_err();
        assert!(matches!(err, DeleteRefusal::InvalidClip(_)));
    }

    #[test]
    fn refuses_over_cap_angle_set() {
        let many: Vec<(String, String)> = (0..32)
            .map(|i| {
                let cam = format!("cam{i}");
                (
                    cam.clone(),
                    format!(
                        "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-{cam}.mp4"
                    ),
                )
            })
            .collect();
        let err = plan_car_delete("slot0", "SavedClips", "present", KEY, &many).unwrap_err();
        assert!(matches!(err, DeleteRefusal::InvalidClip(_)));
    }

    #[test]
    fn maps_other_refusal() {
        let resp = json!({ "refused": "partition must be 1 or 2, got 3" });
        assert!(matches!(
            map_mutation_outcome(&resp),
            MutationOutcome::Refused(_)
        ));
    }

    #[test]
    fn maps_failed_and_critical() {
        let failed = json!({ "handoff_id": "h-1", "result": "failed", "detail": "mount" });
        assert!(matches!(
            map_mutation_outcome(&failed),
            MutationOutcome::Failed { .. }
        ));
        let crit = json!({ "handoff_id": "h-2", "result": "critical_fault", "detail": "stuck" });
        assert!(matches!(
            map_mutation_outcome(&crit),
            MutationOutcome::CriticalFault { .. }
        ));
    }

    #[test]
    fn maps_direct_busy_result() {
        let resp = json!({
            "handoff_id": "h-1",
            "result": "busy",
            "detail": "eject busy, medium intact: host has open handle"
        });
        assert!(matches!(
            map_mutation_outcome(&resp),
            MutationOutcome::Busy(_)
        ));
    }

    #[test]
    fn maps_unparseable_response() {
        let resp = json!({ "weird": true });
        assert!(matches!(
            map_mutation_outcome(&resp),
            MutationOutcome::BadResponse(_)
        ));
    }

    #[test]
    fn normalizes_in_flight_status_to_phase() {
        let resp = json!({ "handoff_id": "h-3", "phase": "applying", "result": null });
        let out = map_status(&resp).unwrap();
        assert_eq!(out["state"], "applying");
        assert_eq!(out["handoff_id"], "h-3");
    }

    #[test]
    fn normalizes_terminal_status_to_result() {
        let resp = json!({ "handoff_id": "h-3", "phase": "representing", "result": "done" });
        assert_eq!(map_status(&resp).unwrap()["state"], "done");
    }

    #[test]
    fn unknown_handoff_status_is_none() {
        let resp = json!({ "error": "unknown handoff_id: h-9" });
        assert!(map_status(&resp).is_none());
    }

    #[test]
    fn enqueue_install_request_carries_blob_path_for_reclaim() {
        let req = enqueue_install_request(2, "LockChime.wav", "/data/teslausb/stage/x.wav");
        assert_eq!(req["cmd"], "enqueue_mutation");
        assert_eq!(req["partition"], 2);
        assert_eq!(req["mutation"]["op"], "install_file");
        assert_eq!(req["mutation"]["source_path"], "/data/teslausb/stage/x.wav");
        // blob_path mirrors source_path so gadgetd unlinks the staged file.
        assert_eq!(req["blob_path"], "/data/teslausb/stage/x.wav");
    }

    #[test]
    fn enqueue_remove_request_many_has_no_blob_path() {
        let req = enqueue_remove_request_many(2, &["Music/a.mp3".to_owned()]);
        assert_eq!(req["cmd"], "enqueue_mutation");
        assert_eq!(req["mutation"]["op"], "delete_paths");
        assert!(req.get("blob_path").is_none(), "a delete stages no blob");
    }

    #[test]
    fn enqueue_remove_empty_dir_request_shape() {
        let req = enqueue_remove_empty_dir_request(2, "Music/Artist/Album");
        assert_eq!(req["cmd"], "enqueue_mutation");
        assert_eq!(req["partition"], 2);
        assert_eq!(req["mutation"]["op"], "remove_empty_dir");
        assert_eq!(req["mutation"]["rel_path"], "Music/Artist/Album");
        assert!(req.get("blob_path").is_none(), "a prune stages no blob");
    }

    #[test]
    fn maps_queued_enqueue_response() {
        let resp = json!({ "job_id": "m-7", "state": "queued" });
        assert_eq!(
            map_queue_outcome(&resp),
            QueueOutcome::Queued {
                job_id: "m-7".to_owned()
            }
        );
    }

    #[test]
    fn rejects_queued_enqueue_response_with_invalid_job_id() {
        let resp = json!({ "job_id": "bad-id", "state": "queued" });
        assert!(matches!(
            map_queue_outcome(&resp),
            QueueOutcome::BadResponse(_)
        ));
    }

    #[test]
    fn maps_enqueue_error_to_rejected() {
        let resp = json!({ "error": "invalid mutation: empty path" });
        assert!(matches!(
            map_queue_outcome(&resp),
            QueueOutcome::Rejected(_)
        ));
    }

    #[test]
    fn maps_queue_unavailable_error_to_unavailable() {
        let resp = json!({
            "error_code": "queue_unavailable",
            "error": "queue unavailable",
            "detail": "queue persist failed: io error"
        });
        assert!(matches!(
            map_queue_outcome(&resp),
            QueueOutcome::Unavailable(_)
        ));
    }

    #[test]
    fn maps_queue_persist_ambiguous_to_ambiguous() {
        let resp = json!({
            "error_code": "queue_persist_ambiguous",
            "error": "queue persist status ambiguous",
            "detail": "post-commit parent sync failed",
            "job_id": "m-9"
        });
        assert!(matches!(
            map_queue_outcome(&resp),
            QueueOutcome::Ambiguous {
                job_id: Some(_),
                ..
            }
        ));
    }

    #[test]
    fn mutation_outcome_sanitizes_public_error_detail() {
        let mut long = "x".repeat(400);
        long.push('\n');
        let resp = json!({ "error": long });
        match map_mutation_outcome(&resp) {
            MutationOutcome::BadResponse(detail) => assert!(detail.len() <= 160),
            other => panic!("unexpected outcome: {other:?}"),
        }
    }

    #[test]
    fn maps_unparseable_enqueue_response_to_bad_response() {
        let resp = json!({ "state": "weird" });
        assert!(matches!(
            map_queue_outcome(&resp),
            QueueOutcome::BadResponse(_)
        ));
    }

    #[test]
    fn gadget_status_passes_through_media_ro_and_queue_counts() {
        let resp = json!({
            "present": true,
            "bound": true,
            "udc_state": "configured",
            "pending_mutations": 2,
            "applying_mutations": 1,
            "media_ro_mounted": true,
            "media_ro_path": "/run/teslausb/media-ro",
            "media_ro_error": null,
        });
        let out = map_gadget_status(&resp).unwrap();
        assert_eq!(out["pending_mutations"], 2);
        assert_eq!(out["applying_mutations"], 1);
        assert_eq!(out["media_ro_mounted"], true);
        assert_eq!(out["media_ro_path"], "/run/teslausb/media-ro");
        assert_eq!(out["media_ro_error"], Value::Null);
    }

    #[test]
    fn gadget_status_passes_through_chime_reenum_fields() {
        // i2 auto-reenum: the pending flag + last_reenum object the SPA overlay
        // consumes must survive the passthrough verbatim.
        let resp = json!({
            "present": true,
            "chime_reenum_pending": true,
            "last_reenum": { "result": "done", "disconnect_ms": 420, "reason": "chime_apply" },
        });
        let out = map_gadget_status(&resp).unwrap();
        assert_eq!(out["chime_reenum_pending"], true);
        assert_eq!(out["last_reenum"]["result"], "done");
        assert_eq!(out["last_reenum"]["disconnect_ms"], 420);
        assert_eq!(out["last_reenum"]["reason"], "chime_apply");
    }

    #[test]
    fn gadget_status_degrades_media_ro_to_null_when_absent() {
        // An older gadgetd that omits the media_ro_* / count fields must not
        // 500 the read; the new keys degrade to null.
        let resp = json!({ "present": true, "bound": false });
        let out = map_gadget_status(&resp).unwrap();
        assert_eq!(out["media_ro_mounted"], Value::Null);
        assert_eq!(out["media_ro_path"], Value::Null);
        assert_eq!(out["media_ro_error"], Value::Null);
        assert_eq!(out["pending_mutations"], Value::Null);
        assert_eq!(out["applying_mutations"], Value::Null);
        // i2: a gadgetd that predates auto-reenum must default the overlay off
        // (pending=false) and report no last_reenum, never 500.
        assert_eq!(out["chime_reenum_pending"], false);
        assert_eq!(out["last_reenum"], Value::Null);
    }

    #[test]
    fn gadget_status_passes_through_media_ro_unmounted_with_error() {
        // The not-mounted-with-reason case the SPA renders as
        // "Not mounted — <error>" must survive the passthrough.
        let resp = json!({
            "present": true,
            "media_ro_mounted": false,
            "media_ro_path": null,
            "media_ro_error": "mount failed: device busy",
        });
        let out = map_gadget_status(&resp).unwrap();
        assert_eq!(out["media_ro_mounted"], false);
        assert_eq!(out["media_ro_path"], Value::Null);
        assert_eq!(out["media_ro_error"], "mount failed: device busy");
    }

    #[test]
    fn gadget_mode_status_summarizes_mode_handoff_and_luns() {
        let resp = json!({
            "present": true,
            "bound": true,
            "udc_state": "configured",
            "lun_file": "/data/teslausb/cam.img",
            "media_lun_file": "/data/teslausb/media.img",
            "handoff_active": false,
            "pending_mutations": 2,
            "applying_mutations": 1,
            "last_handoff_id": "h-77",
            "last_result": "done",
            "media_ro_mounted": true,
            "media_ro_path": "/run/teslausb/media-ro",
            "media_ro_error": null,
            "chime_reenum_pending": false,
            "last_reenum": { "result": "done", "disconnect_ms": 412 },
        });
        let out = map_gadget_mode_status(&resp).expect("mode status");
        assert_eq!(out["mode"], "presented");
        assert_eq!(out["handoff"]["state"], "idle");
        assert_eq!(out["handoff"]["pending_mutations"], 2);
        assert_eq!(out["handoff"]["applying_mutations"], 1);
        assert_eq!(out["lun"]["teslacam"]["image"], "/data/teslausb/cam.img");
        assert_eq!(out["lun"]["teslacam"]["loaded"], true);
        assert_eq!(out["lun"]["media"]["image"], "/data/teslausb/media.img");
        assert_eq!(out["lun"]["media"]["loaded"], true);
        assert_eq!(out["banner"], Value::Null);
    }

    #[test]
    fn gadget_mode_status_emits_recovery_banner_when_pending_or_failed() {
        let resp = json!({
            "present": true,
            "bound": true,
            "udc_state": "configured",
            "handoff_active": false,
            "chime_reenum_pending": true,
            "last_handoff_id": "h-88",
            "last_result": "critical_fault",
        });
        let out = map_gadget_mode_status(&resp).expect("mode status");
        assert_eq!(out["mode"], "syncing");
        assert_eq!(out["banner"]["code"], "chime_reenum_pending");
        assert_eq!(out["banner"]["level"], "info");
    }

    #[test]
    fn gadget_mode_status_degrades_when_fields_are_absent() {
        let out = map_gadget_mode_status(&json!({ "present": false })).expect("mode status");
        assert_eq!(out["mode"], "unavailable");
        assert_eq!(out["handoff"]["state"], "idle");
        assert_eq!(out["lun"]["teslacam"]["loaded"], Value::Null);
        assert_eq!(out["lun"]["media"]["loaded"], Value::Null);
        assert_eq!(out["recovery"]["last_reenum"], Value::Null);
        assert_eq!(out["banner"], Value::Null);
    }
}
