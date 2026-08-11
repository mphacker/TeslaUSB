//! Shared durable-mutation wire foundations used by B-1 daemon contracts.
//!
//! This module is transport-agnostic: no HTTP/socket framework types, only
//! shared state semantics and validators/sanitizers that `webd`/daemon contracts
//! can reuse.

/// Minimum accepted idempotency-key length.
pub const MIN_IDEMPOTENCY_KEY_LEN: usize = 8;
/// Maximum accepted idempotency-key length.
pub const MAX_IDEMPOTENCY_KEY_LEN: usize = 128;
/// Maximum surfaced mutation-error length after sanitization.
pub const MAX_PUBLIC_ERROR_LEN: usize = 160;
/// Maximum mutation job-id length.
pub const MAX_MUTATION_JOB_ID_LEN: usize = 32;
/// Exact request-hash length (sha256 hex).
pub const REQUEST_HASH_LEN: usize = 64;
/// Maximum owner name length.
pub const MAX_OWNER_LEN: usize = 32;
/// Maximum mutation kind length.
pub const MAX_KIND_LEN: usize = 64;
/// Maximum status URL length.
pub const MAX_STATUS_URL_LEN: usize = 256;

/// Lifecycle state for a durable mutation job.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DurableMutationState {
    /// Accepted and persisted, waiting to run.
    Queued,
    /// In progress.
    Running,
    /// Completed successfully.
    Done,
    /// Failed terminally.
    Failed,
    /// Refused permanently (validation/policy).
    Refused,
    /// Refused transiently for current device state.
    Busy,
    /// Cancellation has been requested and not yet finalized.
    CancelRequested,
    /// Cancelled terminally.
    Cancelled,
}

impl DurableMutationState {
    /// True when this state is terminal.
    #[must_use]
    pub const fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Done | Self::Failed | Self::Refused | Self::Busy | Self::Cancelled
        )
    }

    /// True when cancellation may still be requested.
    #[must_use]
    pub const fn can_request_cancel(self) -> bool {
        matches!(self, Self::Queued | Self::Running | Self::CancelRequested)
    }

    /// Valid state-machine transitions.
    ///
    /// Equal-state transitions are always allowed so replayed updates are
    /// idempotent.
    #[must_use]
    pub fn can_transition_to(self, next: Self) -> bool {
        if self == next {
            return true;
        }
        match self {
            Self::Queued => matches!(
                next,
                Self::Running
                    | Self::Done
                    | Self::Failed
                    | Self::Refused
                    | Self::Busy
                    | Self::CancelRequested
                    | Self::Cancelled
            ),
            Self::Running => matches!(
                next,
                Self::Done | Self::Failed | Self::Busy | Self::CancelRequested | Self::Cancelled
            ),
            Self::CancelRequested => {
                matches!(
                    next,
                    Self::Done | Self::Failed | Self::Busy | Self::Cancelled
                )
            }
            Self::Done | Self::Failed | Self::Refused | Self::Busy | Self::Cancelled => false,
        }
    }

    /// Crash/restart recovery projection for in-flight jobs.
    ///
    /// Durable queues that recover after process restart should requeue in-flight
    /// work (`running` / `cancel_requested`) to `queued`.
    #[must_use]
    pub const fn recover_after_restart(self) -> Self {
        match self {
            Self::Running | Self::CancelRequested => Self::Queued,
            other => other,
        }
    }
}

/// Why a mutation start is disallowed while the device is in a sensitive window.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MutationExclusionReason {
    /// Recording is active.
    RecordingActive,
    /// A gadget handoff is active.
    GadgetHandoffActive,
}

/// Returns the exclusion reason for a destructive mutation start.
///
/// `gadget_handoff_active` wins over `recording_active` because it is the more
/// specific operator action already in flight.
#[must_use]
pub const fn destructive_mutation_exclusion(
    recording_active: bool,
    gadget_handoff_active: bool,
) -> Option<MutationExclusionReason> {
    if gadget_handoff_active {
        return Some(MutationExclusionReason::GadgetHandoffActive);
    }
    if recording_active {
        return Some(MutationExclusionReason::RecordingActive);
    }
    None
}

/// Idempotency behavior for **same idempotency key** replays.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SameKeyIdempotencyResult {
    /// Same key + same request hash: replay the original response.
    Replay,
    /// Same key + different request hash: deterministic conflict.
    Conflict409,
}

/// Shared durable mutation envelope fields.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DurableMutationEnvelope {
    /// Logical request id.
    pub request_id: String,
    /// Idempotency key used for dedupe/replay semantics.
    pub idempotency_key: String,
    /// Canonical request hash (sha256 hex).
    pub request_hash: String,
    /// Stable job id.
    pub job_id: String,
    /// Owning daemon/service (`gadgetd`, `indexd`, `uploadd`, `retentiond`, ...).
    pub owner: String,
    /// Mutation kind discriminator.
    pub kind: String,
    /// Current lifecycle state.
    pub state: DurableMutationState,
    /// Cancellation has been requested.
    pub cancel_requested: bool,
    /// Sanitized public error detail when present.
    pub sanitized_error: Option<String>,
    /// Poll URL for this job.
    pub status_url: String,
}

impl DurableMutationEnvelope {
    /// Build a validated durable envelope.
    ///
    /// # Errors
    /// Returns a static reason when one or more fields are invalid.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        request_id: String,
        idempotency_key: String,
        request_hash: String,
        job_id: String,
        owner: String,
        kind: String,
        state: DurableMutationState,
        cancel_requested: bool,
        raw_error: Option<&str>,
        status_url: String,
    ) -> Result<Self, &'static str> {
        validate_request_id(&request_id)?;
        validate_idempotency_key(&idempotency_key)?;
        validate_request_hash(&request_hash)?;
        validate_mutation_job_id(&job_id)?;
        validate_owner_or_kind(&owner, "owner", MAX_OWNER_LEN)?;
        validate_owner_or_kind(&kind, "kind", MAX_KIND_LEN)?;
        validate_status_url(&status_url)?;
        Ok(Self {
            request_id,
            idempotency_key,
            request_hash,
            job_id,
            owner,
            kind,
            state,
            cancel_requested,
            sanitized_error: raw_error.map(sanitize_public_error),
            status_url,
        })
    }
}

/// Validate an idempotency key.
///
/// Accepted charset: ASCII alnum plus `- _ . :`.
///
/// # Errors
/// Returns a static reason when the key is empty, too short/long, or contains an
/// unsupported byte.
pub fn validate_idempotency_key(key: &str) -> Result<(), &'static str> {
    if key.is_empty() {
        return Err("idempotency key is required");
    }
    if key.len() < MIN_IDEMPOTENCY_KEY_LEN {
        return Err("idempotency key is too short");
    }
    if key.len() > MAX_IDEMPOTENCY_KEY_LEN {
        return Err("idempotency key is too long");
    }
    if key
        .bytes()
        .any(|byte| !(byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':')))
    {
        return Err("idempotency key has an unsupported character");
    }
    Ok(())
}

/// Validate a durable request id.
///
/// # Errors
/// Returns a static reason when the id is invalid.
pub fn validate_request_id(request_id: &str) -> Result<(), &'static str> {
    validate_idempotency_key(request_id)
}

/// Validate a canonical request hash.
///
/// # Errors
/// Returns a static reason when the hash is invalid.
pub fn validate_request_hash(request_hash: &str) -> Result<(), &'static str> {
    if request_hash.len() != REQUEST_HASH_LEN {
        return Err("request hash must be 64 chars");
    }
    if request_hash.bytes().any(|byte| !byte.is_ascii_hexdigit()) {
        return Err("request hash must be hex");
    }
    if request_hash != request_hash.to_ascii_lowercase() {
        return Err("request hash must be lowercase hex");
    }
    Ok(())
}

/// Evaluate idempotency replay for an already-seen key.
///
/// # Errors
/// Returns a static reason when either hash is invalid.
pub fn evaluate_same_key_idempotency(
    existing_request_hash: &str,
    incoming_request_hash: &str,
) -> Result<SameKeyIdempotencyResult, &'static str> {
    validate_request_hash(existing_request_hash)?;
    validate_request_hash(incoming_request_hash)?;
    if existing_request_hash == incoming_request_hash {
        return Ok(SameKeyIdempotencyResult::Replay);
    }
    Ok(SameKeyIdempotencyResult::Conflict409)
}

/// Validate a mutation job id.
///
/// Accepted formats:
/// - `m-<digits>` (gadget queue ids)
/// - `<digits>` (legacy webd in-process job ids)
///
/// # Errors
/// Returns a static reason when the shape is invalid.
pub fn validate_mutation_job_id(job_id: &str) -> Result<(), &'static str> {
    if job_id.is_empty() {
        return Err("job id is required");
    }
    if job_id.len() > MAX_MUTATION_JOB_ID_LEN {
        return Err("job id is too long");
    }
    if job_id.bytes().all(|byte| byte.is_ascii_digit()) {
        return Ok(());
    }
    let Some(rest) = job_id.strip_prefix("m-") else {
        return Err("job id has an unsupported format");
    };
    if rest.is_empty() || !rest.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err("job id has an unsupported format");
    }
    Ok(())
}

fn validate_owner_or_kind(value: &str, field: &str, max_len: usize) -> Result<(), &'static str> {
    if value.is_empty() || value.len() > max_len {
        return Err(match field {
            "owner" => "owner must be 1..=32 chars",
            _ => "kind must be 1..=64 chars",
        });
    }
    if value
        .bytes()
        .any(|byte| !(byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.')))
    {
        return Err("owner/kind has an unsupported character");
    }
    Ok(())
}

/// Validate a status URL.
///
/// # Errors
/// Returns a static reason when the URL is invalid.
pub fn validate_status_url(status_url: &str) -> Result<(), &'static str> {
    if status_url.is_empty() || status_url.len() > MAX_STATUS_URL_LEN {
        return Err("status_url must be 1..=256 chars");
    }
    if !status_url.starts_with('/') {
        return Err("status_url must be an absolute path");
    }
    if status_url
        .chars()
        .any(|ch| ch.is_control() || ch.is_whitespace())
    {
        return Err("status_url contains unsupported whitespace/control characters");
    }
    Ok(())
}

fn normalize_authority(authority: &str) -> Option<String> {
    let trimmed = authority.trim();
    if trimmed.is_empty()
        || trimmed
            .chars()
            .any(|ch| ch.is_control() || ch.is_whitespace())
    {
        return None;
    }
    Some(
        trimmed
            .strip_suffix(":80")
            .unwrap_or(trimmed)
            .to_ascii_lowercase(),
    )
}

fn origin_authority(origin: &str) -> Option<&str> {
    let without_scheme = origin
        .strip_prefix("http://")
        .or_else(|| origin.strip_prefix("https://"))?;
    let authority = without_scheme.split('/').next()?;
    if authority.is_empty() {
        return None;
    }
    Some(authority)
}

/// Validate same-origin non-GET request evidence (Host/Origin/Sec-Fetch-Site).
///
/// # Errors
/// Returns a static reason when the evidence is missing or inconsistent.
pub fn validate_non_get_same_origin(
    host_header: &str,
    origin_header: Option<&str>,
    sec_fetch_site: Option<&str>,
) -> Result<(), &'static str> {
    if let Some(fetch_site) = sec_fetch_site {
        let normalized = fetch_site.trim().to_ascii_lowercase();
        if !matches!(normalized.as_str(), "same-origin" | "same-site" | "none") {
            return Err("sec-fetch-site must be same-origin/same-site/none");
        }
    }

    let host = normalize_authority(host_header).ok_or("host header is required")?;
    let origin = origin_header.ok_or("origin header is required")?;
    let origin_authority =
        normalize_authority(origin_authority(origin).ok_or("origin must be absolute http(s)")?)
            .ok_or("origin authority is invalid")?;
    if host != origin_authority {
        return Err("origin does not match host");
    }
    Ok(())
}

/// Sanitize and bound an error detail before exposing it to API clients/logical
/// job streams.
#[must_use]
pub fn sanitize_public_error(detail: &str) -> String {
    let mut out = String::new();
    let mut pending_space = false;

    for ch in detail.chars() {
        let mapped = if ch.is_control() || ch.is_whitespace() {
            ' '
        } else {
            ch
        };
        if mapped == ' ' {
            pending_space = !out.is_empty();
            continue;
        }
        if pending_space {
            if out.len() + 1 > MAX_PUBLIC_ERROR_LEN {
                break;
            }
            out.push(' ');
            pending_space = false;
        }
        let next_len = out.len() + mapped.len_utf8();
        if next_len > MAX_PUBLIC_ERROR_LEN {
            break;
        }
        out.push(mapped);
    }

    if out.is_empty() {
        return "operation failed".to_owned();
    }
    out
}

#[cfg(test)]
mod tests {
    #![allow(clippy::panic)]

    use super::{
        DurableMutationEnvelope, DurableMutationState, MAX_IDEMPOTENCY_KEY_LEN, REQUEST_HASH_LEN,
        SameKeyIdempotencyResult, destructive_mutation_exclusion, evaluate_same_key_idempotency,
        sanitize_public_error, validate_idempotency_key, validate_mutation_job_id,
        validate_non_get_same_origin,
    };

    #[test]
    fn lifecycle_allows_cancel_path_and_terminal_replay() {
        assert!(
            DurableMutationState::Queued.can_transition_to(DurableMutationState::CancelRequested)
        );
        assert!(
            DurableMutationState::CancelRequested
                .can_transition_to(DurableMutationState::Cancelled)
        );
        assert!(DurableMutationState::Cancelled.can_transition_to(DurableMutationState::Cancelled));
    }

    #[test]
    fn lifecycle_rejects_terminal_regression() {
        assert!(!DurableMutationState::Done.can_transition_to(DurableMutationState::Running));
        assert!(!DurableMutationState::Failed.can_transition_to(DurableMutationState::Queued));
    }

    #[test]
    fn lifecycle_restart_recovery_requeues_inflight() {
        assert_eq!(
            DurableMutationState::Running.recover_after_restart(),
            DurableMutationState::Queued
        );
        assert_eq!(
            DurableMutationState::CancelRequested.recover_after_restart(),
            DurableMutationState::Queued
        );
        assert_eq!(
            DurableMutationState::Done.recover_after_restart(),
            DurableMutationState::Done
        );
    }

    #[test]
    fn validates_idempotency_key_shape() {
        assert!(validate_idempotency_key("k-1234567").is_ok());
        assert!(validate_idempotency_key("sync:drive_01.2026-08-11").is_ok());
        assert!(validate_idempotency_key("").is_err());
        assert!(validate_idempotency_key("short").is_err());
        assert!(validate_idempotency_key("bad key").is_err());
        assert!(validate_idempotency_key(&"a".repeat(MAX_IDEMPOTENCY_KEY_LEN + 1)).is_err());
    }

    #[test]
    fn validates_mutation_job_id_shape() {
        assert!(validate_mutation_job_id("7").is_ok());
        assert!(validate_mutation_job_id("m-77").is_ok());
        assert!(validate_mutation_job_id("m-").is_err());
        assert!(validate_mutation_job_id("m-x7").is_err());
        assert!(validate_mutation_job_id("other-7").is_err());
    }

    #[test]
    fn validates_request_hash_and_idempotency_replay_behavior() {
        let hash_a = "a".repeat(REQUEST_HASH_LEN);
        let hash_b = "b".repeat(REQUEST_HASH_LEN);
        assert_eq!(
            evaluate_same_key_idempotency(&hash_a, &hash_a),
            Ok(SameKeyIdempotencyResult::Replay)
        );
        assert_eq!(
            evaluate_same_key_idempotency(&hash_a, &hash_b),
            Ok(SameKeyIdempotencyResult::Conflict409)
        );
        assert!(evaluate_same_key_idempotency("abc", &hash_a).is_err());
    }

    #[test]
    fn validates_same_origin_non_get_headers() {
        assert!(
            validate_non_get_same_origin(
                "cybertruckusb.local",
                Some("http://cybertruckusb.local"),
                Some("same-origin")
            )
            .is_ok()
        );
        assert!(
            validate_non_get_same_origin(
                "cybertruckusb.local:80",
                Some("http://cybertruckusb.local"),
                Some("same-site")
            )
            .is_ok()
        );
        assert!(
            validate_non_get_same_origin(
                "cybertruckusb.local",
                Some("http://evil.test"),
                Some("same-origin")
            )
            .is_err()
        );
        assert!(
            validate_non_get_same_origin(
                "cybertruckusb.local",
                Some("http://cybertruckusb.local"),
                Some("cross-site")
            )
            .is_err()
        );
        assert!(validate_non_get_same_origin("cybertruckusb.local", None, None).is_err());
    }

    #[test]
    fn envelope_builds_with_sanitized_error() {
        let result = DurableMutationEnvelope::new(
            "req-12345678".to_owned(),
            "idem-12345678".to_owned(),
            "a".repeat(REQUEST_HASH_LEN),
            "m-7".to_owned(),
            "gadgetd".to_owned(),
            "clip_delete".to_owned(),
            DurableMutationState::Queued,
            false,
            Some("io\nerror"),
            "/api/jobs/m-7/status".to_owned(),
        );
        let Ok(env) = result else {
            panic!("expected valid envelope");
        };
        assert_eq!(env.idempotency_key, "idem-12345678");
        assert_eq!(env.sanitized_error.as_deref(), Some("io error"));
    }

    #[test]
    fn envelope_rejects_invalid_idempotency_key() {
        let result = DurableMutationEnvelope::new(
            "req-12345678".to_owned(),
            "bad key".to_owned(),
            "a".repeat(REQUEST_HASH_LEN),
            "m-7".to_owned(),
            "gadgetd".to_owned(),
            "clip_delete".to_owned(),
            DurableMutationState::Queued,
            false,
            None,
            "/api/jobs/m-7/status".to_owned(),
        );
        assert!(result.is_err());
    }

    #[test]
    fn sanitize_public_error_collapses_whitespace_and_caps_length() {
        let sanitized = sanitize_public_error("  line1\r\n\tline2  ");
        assert_eq!(sanitized, "line1 line2");

        let capped = sanitize_public_error(&"x".repeat(512));
        assert!(capped.len() <= super::MAX_PUBLIC_ERROR_LEN);
        assert!(capped.starts_with('x'));
    }

    #[test]
    fn exclusion_reason_prefers_active_handoff() {
        assert_eq!(
            destructive_mutation_exclusion(false, true),
            Some(super::MutationExclusionReason::GadgetHandoffActive)
        );
        assert_eq!(
            destructive_mutation_exclusion(true, false),
            Some(super::MutationExclusionReason::RecordingActive)
        );
        assert_eq!(destructive_mutation_exclusion(false, false), None);
    }
}
