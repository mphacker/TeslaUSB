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

/// Build the `request_mutation` wire request to install a staged file into a
/// partition (the generic media-install primitive). `source_path` is the
/// absolute path of a staged source file on the Pi data area that `gadgetd`
/// re-opens (`O_NOFOLLOW`, regular-file-only) and copies into `rel_path` under
/// the mounted partition root via temp + atomic rename. `rel_path` must be a
/// fixed, validated, partition-root-relative destination — never an
/// attacker-controlled value.
pub(crate) fn install_request(partition: u8, rel_path: &str, source_path: &str) -> Value {
    json!({
        "cmd": "request_mutation",
        "partition": partition,
        "mutation": { "op": "install_file", "rel_path": rel_path, "source_path": source_path },
    })
}

/// Build the `request_mutation` wire request to remove one or more files from a
/// partition in a SINGLE handoff (the generic media-remove primitive). Uses
/// `gadgetd`'s regular-file-only, idempotent-on-absent `delete_paths` set form
/// (not `delete_path`): removing an already-absent asset is a success (a
/// retried remove is safe), and a directory at a path is refused rather than
/// recursively deleted. A single handoff for the whole set is deliberate —
/// every handoff ejects and remounts the car-facing USB, so deleting `N` files
/// in `N` handoffs would be `N` disconnect cycles. `rel_paths` must be fixed,
/// validated, partition-root-relative destinations — never attacker-controlled.
pub(crate) fn remove_request_many(partition: u8, rel_paths: &[String]) -> Value {
    json!({
        "cmd": "request_mutation",
        "partition": partition,
        "mutation": { "op": "delete_paths", "rel_paths": rel_paths },
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
        return if is_retryable_refusal(reason) {
            MutationOutcome::Busy(reason.to_owned())
        } else {
            MutationOutcome::Refused(reason.to_owned())
        };
    }
    if let Some(err) = resp.get("error").and_then(Value::as_str) {
        return MutationOutcome::BadResponse(err.to_owned());
    }
    let detail = || {
        resp.get("detail")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned()
    };
    match resp.get("result").and_then(Value::as_str) {
        Some("done") => match handoff_id {
            Some(id) => MutationOutcome::Done(id.to_owned()),
            None => MutationOutcome::BadResponse("done without a handoff_id".to_owned()),
        },
        Some("failed") => MutationOutcome::Failed {
            handoff_id: handoff_id.unwrap_or_default().to_owned(),
            detail: detail(),
        },
        Some("critical_fault") => MutationOutcome::CriticalFault {
            handoff_id: handoff_id.unwrap_or_default().to_owned(),
            detail: detail(),
        },
        _ => MutationOutcome::BadResponse(format!("unexpected gadgetd response: {resp}")),
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
/// a partial reply.
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
        "last_handoff_id": field("last_handoff_id"),
        "last_result": field("last_result"),
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
            let resp = read_frame(&mut stream, MAX_FRAME)
                .map_err(|e| TransportError::Protocol(format!("read: {e}")))?;
            serde_json::from_slice(&resp)
                .map_err(|e| TransportError::Protocol(format!("decode: {e}")))
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
        DeleteRefusal, MutationOutcome, install_request, map_mutation_outcome, map_status,
        plan_car_delete, remove_request_many,
    };
    use serde_json::json;

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
    fn install_request_carries_install_file_op() {
        let req = install_request(2, "LockChime.wav", "/data/cache/media-staging/x.wav");
        assert_eq!(req["cmd"], "request_mutation");
        assert_eq!(req["partition"], 2);
        assert_eq!(req["mutation"]["op"], "install_file");
        assert_eq!(req["mutation"]["rel_path"], "LockChime.wav");
        assert_eq!(
            req["mutation"]["source_path"],
            "/data/cache/media-staging/x.wav"
        );
    }

    #[test]
    fn remove_request_many_single_path_is_a_one_element_set() {
        // The idempotent, file-only `delete_paths` set form (not `delete_path`),
        // so removing an absent single-slot asset is a success and a directory
        // is refused rather than recursively deleted.
        let req = remove_request_many(2, &["LockChime.wav".to_owned()]);
        assert_eq!(req["cmd"], "request_mutation");
        assert_eq!(req["partition"], 2);
        assert_eq!(req["mutation"]["op"], "delete_paths");
        let paths = req["mutation"]["rel_paths"].as_array().unwrap();
        assert_eq!(paths.len(), 1);
        assert_eq!(paths[0], "LockChime.wav");
    }

    #[test]
    fn remove_request_many_carries_all_paths_in_one_mutation() {
        let req = remove_request_many(2, &["Boombox/a.wav".to_owned(), "Boombox/b.mp3".to_owned()]);
        assert_eq!(req["cmd"], "request_mutation");
        assert_eq!(req["partition"], 2);
        assert_eq!(req["mutation"]["op"], "delete_paths");
        let paths = req["mutation"]["rel_paths"].as_array().unwrap();
        assert_eq!(paths.len(), 2);
        assert_eq!(paths[0], "Boombox/a.wav");
        assert_eq!(paths[1], "Boombox/b.mp3");
    }
}
