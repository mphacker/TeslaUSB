//! Wire protocol for `retentiond → indexd` archive registration.
//!
//! Frames are 4-byte little-endian length + JSON payload, mirroring
//! `scannerd::proto` and bounded to avoid oversized allocation.

use std::io::{self, Read, Write};

use serde::{Deserialize, Serialize};

/// Maximum accepted request/response frame for indexd control RPCs.
pub const MAX_REQUEST_FRAME: u32 = 4 * 1024 * 1024;

/// Inbound control requests.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Request {
    /// Register one completed archive clip copy.
    RegisterArchivedClip(RegisterArchivedClip),
    /// Register one copied-but-undecodable archive clip as quarantined.
    // Deploy `indexd` before `retentiond`: older indexd must reject this
    // unknown verb so retentiond fails closed to pending, never force-publishing.
    RegisterQuarantinedArchive(RegisterArchivedClip),
    /// Set one settings preference value.
    SetPref {
        /// Preference key.
        key: String,
        /// Preference value.
        value: String,
    },
    /// Claim a LIVE, eligible archive item for delete, re-checking the full
    /// eviction allowlist predicate atomically server-side.
    ClaimEvictionCandidate {
        /// Archive item id.
        id: i64,
        /// Items at/after this floor are ineligible (permanent-loss guard).
        recency_floor_epoch: i64,
        /// Opt-in: allow claiming rows that are not cloud-durable.
        #[serde(default)]
        allow_undurable: bool,
    },
    /// Transition a claimed archive item to deleting.
    MarkArchiveDeleting {
        /// Archive item id.
        id: i64,
    },
    /// Transition a deleting archive item to deleted.
    MarkArchiveDeleted {
        /// Archive item id.
        id: i64,
        /// Bytes freed by deletion.
        bytes_freed: i64,
    },
    /// Release a delete claim back to LIVE.
    ReleaseArchiveDeleteClaim {
        /// Archive item id.
        id: i64,
    },
    /// Clear a terminal row's consumed delete generation token.
    ClearArchiveDeleteGeneration {
        /// Archive item id.
        id: i64,
        /// Expected delete generation token to consume.
        delete_gen: String,
    },
    /// Quarantine an archive item.
    QuarantineArchiveItem {
        /// Archive item id.
        id: i64,
        /// Human-readable reason.
        reason: String,
    },
    /// List oldest-first eviction candidates.
    ListEvictionCandidates {
        /// Items newer than or equal to this floor are ineligible.
        recency_floor_epoch: i64,
        /// Opt-in: include rows that are not cloud-durable.
        #[serde(default)]
        allow_undurable: bool,
        /// Max rows requested.
        limit: u32,
    },
    /// Summarize exclusion reasons from a bounded eviction-eligibility sample.
    ListEvictionExclusionReport {
        /// Items newer than or equal to this floor are ineligible.
        recency_floor_epoch: i64,
        /// Opt-in: include rows that are not cloud-durable.
        #[serde(default)]
        allow_undurable: bool,
        /// Max rows requested.
        limit: u32,
    },
    /// List rows that need delete-state crash recovery.
    ListRecoveryRows {},
    /// Paginated cloud upload candidates.
    CloudCandidates {
        /// Folder classes to include.
        folders: Vec<String>,
        /// Optional opaque keyset cursor.
        after_cursor: Option<String>,
        /// Page size (server capped).
        limit: u32,
    },
    /// Paginated cloud catalog discover page.
    CloudDiscover {
        /// Optional opaque keyset cursor.
        after_cursor: Option<String>,
        /// Page size (server capped).
        limit: u32,
    },
    /// Paginated durable upload queue load.
    CloudQueueLoad {
        /// Optional opaque keyset cursor.
        after_cursor: Option<String>,
        /// Page size (server capped).
        limit: u32,
        /// Optional filter: only rows bound to this upload set.
        #[serde(default)]
        upload_set_id: Option<String>,
    },
    /// Paginated load of current prepared-but-unfinalized parent upload sets (post-reboot resume).
    CloudPendingUploadSetsLoad {
        /// Optional opaque keyset cursor.
        after_cursor: Option<String>,
        /// Page size (server capped).
        limit: u32,
    },
    /// Idempotent queue row upsert.
    CloudQueueUpsert {
        /// Upsert payload.
        item: CloudQueueUpsertWire,
    },
    /// Manual retry / parked-collision resolution.
    CloudQueueRetry {
        /// Parent archive item id.
        archive_item_id: i64,
        /// Optional child discriminator.
        child_key: Option<String>,
        /// Optional sealed upload-set fence.
        #[serde(default)]
        upload_set_id: Option<String>,
        /// Resolution mode.
        resolution: CloudQueueRetryResolutionWire,
    },
    /// Child-specific failed-upload retry using durable request identity.
    CloudFailedUploadRetry {
        /// Parent archive item id.
        archive_item_id: i64,
        /// Child discriminator.
        child_key: String,
        /// Optional sealed upload-set fence.
        #[serde(default)]
        upload_set_id: Option<String>,
        /// Logical request id.
        request_id: String,
        /// Durable idempotency key.
        idempotency_key: String,
        /// Canonical request hash.
        request_hash: String,
        /// Stable durable job id.
        job_id: String,
    },
    /// Internal archive-delete request create-or-load (durable idempotency only).
    ArchiveDeleteCreateOrLoad {
        /// Stable durable job id.
        job_id: String,
        /// Logical request id.
        request_id: String,
        /// Durable idempotency key.
        idempotency_key: String,
        /// Canonical request hash.
        request_hash: String,
        /// Target archive item id.
        target_archive_item_id: i64,
        /// Target archive relative path fence.
        target_archive_path: String,
        /// Target archive size fence.
        target_archive_size_bytes: i64,
        /// Target archive file-count fence.
        target_archive_file_count: i64,
        /// Optional linked clip canonical key fence.
        #[serde(default)]
        target_clip_canonical_key: Option<String>,
        /// Optional manifest digest fence.
        #[serde(default)]
        target_manifest_digest: Option<String>,
    },
    /// Internal archive-delete request inspect by job id.
    ArchiveDeleteInspect {
        /// Stable durable job id.
        job_id: String,
    },
    /// Internal archive-delete request owner handoff claim (oldest claimable queued row).
    ArchiveDeleteClaimNext {},
    /// Internal archive-delete request completion acknowledgement.
    ArchiveDeleteComplete {
        /// Stable durable job id.
        job_id: String,
        /// Expected owned delete generation token.
        owned_delete_gen: String,
    },
    /// Internal archive-delete request failure/refusal acknowledgement.
    ArchiveDeleteFail {
        /// Stable durable job id.
        job_id: String,
        /// Expected owned delete generation token.
        owned_delete_gen: String,
        /// Human-readable failure detail (sanitized server-side).
        detail: String,
        /// Explicitly requeue instead of terminal-failing.
        #[serde(default)]
        requeue: bool,
    },
    /// Acquire upload lease token.
    UploadLeaseAcquire {
        /// Parent archive item id.
        archive_item_id: i64,
        /// Monotonic lease ttl in milliseconds.
        ttl_ms: u32,
    },
    /// Renew upload lease token.
    UploadLeaseRenew {
        /// Lease token from `upload_lease_acquire`.
        token: String,
        /// New ttl in milliseconds.
        ttl_ms: u32,
    },
    /// Release upload lease token.
    UploadLeaseRelease {
        /// Lease token.
        token: String,
    },
    /// Commit one successful upload.
    CloudUploadCommit {
        /// Queue primary key.
        queue_pk: CloudQueuePkWire,
        /// Idempotency key for this transfer attempt.
        attempt_id: String,
        /// Optional sealed upload-set fence.
        #[serde(default)]
        upload_set_id: Option<String>,
        /// Backend verification hash.
        hash: String,
        /// Hash algorithm.
        hash_alg: String,
        /// Uploaded bytes.
        size: i64,
    },
    /// Record one failed upload attempt.
    CloudUploadFail {
        /// Queue primary key.
        queue_pk: CloudQueuePkWire,
        /// Idempotency key for this transfer attempt.
        attempt_id: String,
        /// Optional sealed upload-set fence.
        #[serde(default)]
        upload_set_id: Option<String>,
        /// Sanitized error class.
        error_class: String,
        /// Retry gate (unix seconds), null = immediate retry.
        not_before: Option<i64>,
        /// Terminal failure marker.
        terminal: bool,
    },
    /// Derived cloud counters.
    CloudStatsGet {},
    /// Reset cloud counters baseline.
    CloudStatsReset {},
    /// Non-secret cloud config get.
    CloudConfigGet {},
    /// Non-secret cloud config put.
    CloudConfigPut {
        /// Typed config.
        config: CloudConfigWire,
    },
    /// Paginated history load.
    CloudHistoryLoad {
        /// Optional opaque keyset cursor.
        after_cursor: Option<String>,
        /// Page size (server capped).
        limit: u32,
    },
    /// Paginated failed-history load.
    CloudFailedHistoryLoad {
        /// Optional opaque keyset cursor.
        after_cursor: Option<String>,
        /// Page size (server capped).
        limit: u32,
    },
    /// Finalize one immutable verified event archive generation.
    FinalizeEventArchive(FinalizeEventArchiveRequest),
    /// Prepare and seal one parent upload set.
    CloudPrepareParentUpload(CloudPrepareParentUploadRequest),
    /// Finalize one sealed parent upload set.
    CloudFinalizeParentUpload(CloudFinalizeParentUploadRequest),
}

/// Archive registration payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RegisterArchivedClip {
    /// Clip identity key matching scanner ingest.
    pub canonical_key: String,
    /// Source folder class (`RecentClips`, `SavedClips`, ...).
    pub folder_class: String,
    /// Source partition label.
    pub partition: String,
    /// Clip start epoch seconds.
    pub started_at: i64,
    /// Clip end epoch seconds.
    pub ended_at: i64,
    /// Clip duration in seconds when known.
    pub duration_s: Option<i64>,
    /// Archive unit metadata.
    pub archive: ArchiveUnit,
    /// Per-camera archive-backed angles.
    pub angles: Vec<ArchiveAngle>,
}

/// One durable archive item unit.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArchiveUnit {
    /// Deterministic archive-root-relative item path.
    pub path: String,
    /// Total bytes in the archive unit.
    pub size_bytes: i64,
    /// Number of files in the archive unit.
    pub file_count: i64,
    /// Archive completion epoch seconds.
    pub archived_at: i64,
}

/// One camera angle now backed by archive storage.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArchiveAngle {
    /// Camera label (`front`, `back`, `left_repeater`, ...).
    pub camera: String,
    /// Archive-root-relative file reference for playback.
    pub file_ref: String,
    /// Milliseconds relative to clip start.
    pub offset_ms: i64,
    /// Angle duration in seconds when known.
    pub duration_s: Option<i64>,
    /// File size in bytes.
    pub size_bytes: i64,
}

/// One eviction candidate row over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvictionCandidateWire {
    /// Archive item id.
    pub id: i64,
    /// Archive-root-relative item path.
    pub path: String,
    /// Archive bytes.
    pub size_bytes: i64,
    /// Archive completion epoch seconds.
    pub archived_at: i64,
    /// Source folder class.
    pub folder_class: String,
}

/// One exclusion-reason aggregate row over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvictionExclusionReasonWire {
    /// Stable reason code.
    pub reason: String,
    /// Number of sampled archive items with this exclusion reason.
    pub count: i64,
    /// Summed bytes across sampled items for this reason.
    pub size_bytes: i64,
}

/// One delete-state recovery row over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecoveryRowWire {
    /// Archive item id.
    pub id: i64,
    /// Current delete state.
    pub delete_state: String,
    /// Archive-root-relative item path.
    pub path: String,
    /// Archive bytes.
    pub size_bytes: i64,
    /// Delete generation token, when present.
    pub delete_gen: Option<String>,
}

/// Queue primary key over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudQueuePkWire {
    /// Destination id.
    pub destination_id: String,
    /// Canonical remote key.
    pub remote_key: String,
}

/// Queue upsert payload over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudQueueUpsertWire {
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Child key.
    pub child_key: String,
    /// Destination id.
    pub destination_id: String,
    /// Destination key.
    pub remote_key: String,
    /// Upload category.
    pub category: String,
    /// FIFO sequence.
    pub seq: i64,
    /// Total bytes.
    pub total_bytes: i64,
    /// Local content hash.
    pub content_sha256: String,
    /// Optional backend verification hash.
    pub expected_hash: Option<String>,
    /// Verification algorithm.
    pub verify_alg: String,
}

/// Manual retry collision resolution mode over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case")]
pub enum CloudQueueRetryResolutionWire {
    /// Keep remote existing object.
    KeepExisting,
    /// Retry with a new remote key.
    Rekey {
        /// New key.
        remote_key: String,
    },
    /// Retry with overwrite intent.
    Replace,
}

/// Candidate row over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudCandidateWire {
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Child key.
    pub child_key: String,
    /// Source path.
    pub source_rel: String,
    /// Destination id.
    pub destination_id: String,
    /// Destination key.
    pub remote_key: String,
    /// Size bytes.
    pub size_bytes: i64,
    /// Local content hash.
    pub content_sha256: String,
    /// Queue state.
    pub state: String,
    /// Upload category.
    pub category: String,
    /// FIFO sequence.
    pub seq: i64,
}

/// Discover row over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudDiscoverWire {
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Source folder class.
    pub folder_class: String,
    /// Source path.
    pub path: String,
    /// Parent manifest digest, when available.
    pub manifest_digest: Option<String>,
    /// Upload category.
    pub category: String,
}

/// Queue row over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudQueueRowWire {
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Child key.
    pub child_key: String,
    /// Source archive-root-relative path.
    #[serde(default)]
    pub source_rel: String,
    /// Destination id.
    pub destination_id: String,
    /// Destination key.
    pub remote_key: String,
    /// Category.
    pub category: String,
    /// FIFO sequence.
    pub seq: i64,
    /// Total bytes.
    pub total_bytes: i64,
    /// Uploaded bytes.
    pub bytes_uploaded: i64,
    /// Expected backend verification value.
    pub expected_hash: Option<String>,
    /// Expected backend verification algorithm.
    pub verify_alg: String,
    /// Local content hash.
    pub content_sha256: String,
    /// Queue state.
    pub state: String,
    /// Attempts.
    pub attempts: i64,
    /// Retry gate.
    pub not_before: Option<i64>,
    /// Last error.
    pub last_error: Option<String>,
    /// Sealed upload set id, if any.
    #[serde(default)]
    pub upload_set_id: Option<String>,
}

/// History row over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudHistoryRowWire {
    /// Row id.
    pub id: i64,
    /// Completion sequence.
    pub completion_seq: i64,
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Child key.
    pub child_key: String,
    /// Destination id.
    pub destination_id: String,
    /// Outcome.
    pub outcome: String,
    /// Size bytes.
    pub size_bytes: i64,
    /// Timestamp.
    pub at: i64,
    /// Sanitized error class.
    pub error_class: Option<String>,
    /// Sealed upload set fence, when available.
    #[serde(default)]
    pub upload_set_id: Option<String>,
}

/// Typed non-secret cloud config over the wire.
#[allow(clippy::struct_excessive_bools)]
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudConfigWire {
    /// Sentry folder enabled.
    pub sentry_enabled: bool,
    /// Saved folder enabled.
    pub saved_enabled: bool,
    /// Recent folder enabled.
    pub recent_enabled: bool,
    /// Sentry priority.
    pub sentry_priority: i64,
    /// Saved priority.
    pub saved_priority: i64,
    /// Recent priority.
    pub recent_priority: i64,
    /// Remote reserve in GiB.
    pub reserve_gb: i64,
    /// Max attempts.
    pub max_attempts: i64,
    /// Base backoff seconds.
    pub base_backoff_secs: i64,
    /// Keep local files until backed up.
    pub keep_until_backed_up: bool,
    /// Auto sync toggle.
    pub auto_sync: bool,
}

/// Finalize-event authoritative segment record.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FinalizeEventArchiveSegmentWire {
    /// Segment key relative to the generation root.
    pub segment_key: String,
    /// Segment bytes.
    pub size_bytes: i64,
    /// Segment modified time in milliseconds.
    pub mtime_ms: i64,
    /// Segment content sha256.
    pub content_sha256: String,
}

/// Finalize-event authoritative clip record.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FinalizeEventArchiveClipWire {
    /// Clip canonical key.
    pub canonical_key: String,
    /// Clip start epoch seconds.
    pub started_at: i64,
    /// Clip end epoch seconds.
    pub ended_at: i64,
    /// Source folder class.
    pub folder_class: String,
    /// Source partition label.
    pub partition: String,
}

/// Finalize-event authoritative angle record.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FinalizeEventArchiveAngleWire {
    /// Clip canonical key.
    pub canonical_key: String,
    /// Camera name.
    pub camera: String,
    /// Archive-root-relative file reference.
    pub file_ref: String,
    /// Relative offset in milliseconds.
    pub offset_ms: i64,
    /// Optional duration in seconds.
    pub duration_s: Option<i64>,
    /// File bytes.
    pub size_bytes: i64,
}

/// `finalize_event_archive` request payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FinalizeEventArchiveRequest {
    /// Verification pass id (32-hex).
    pub pass_id: String,
    /// Source event key.
    pub source_event_key: String,
    /// Source volume id when known.
    pub source_volume_id: Option<String>,
    /// Opaque source generation id.
    pub source_generation: String,
    /// Compare-and-swap prior digest.
    pub expected_prior_manifest_digest: Option<String>,
    /// Event manifest digest (FNV-1a-128, 32-hex).
    pub manifest_digest: String,
    /// Event segment-set digest (sha256, 64-hex).
    pub segment_set_digest: String,
    /// Expected segment count.
    pub expected_segment_count: i64,
    /// Archive bytes.
    pub size_bytes: i64,
    /// Archive file count.
    pub file_count: i64,
    /// Archive completion epoch seconds.
    pub archived_at: i64,
    /// Immutable generation directory path.
    pub generation_dir_path: String,
    /// Source folder class.
    pub folder_class: String,
    /// Source partition.
    pub partition: String,
    /// Authoritative segment records.
    pub segments: Vec<FinalizeEventArchiveSegmentWire>,
    /// Authoritative clip records.
    pub clips: Vec<FinalizeEventArchiveClipWire>,
    /// Authoritative angle records.
    pub angles: Vec<FinalizeEventArchiveAngleWire>,
}

/// `finalize_event_archive` response payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FinalizeEventArchiveResponse {
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// True when this was an idempotent replay.
    pub already_finalized: bool,
}

/// `cloud_prepare_parent_upload` child payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudPrepareParentUploadChildWire {
    /// Child discriminator.
    pub child_key: String,
    /// Destination id.
    pub destination_id: String,
    /// Destination remote key.
    pub remote_key: String,
    /// Upload category.
    pub category: String,
    /// Queue ordering sequence.
    pub seq: i64,
    /// Total bytes.
    pub total_bytes: i64,
    /// Manifest mtime in milliseconds.
    pub manifest_mtime_ms: i64,
    /// Local content sha256.
    pub content_sha256: String,
    /// Expected backend verify hash.
    pub expected_hash: String,
    /// Verify algorithm.
    pub verify_alg: String,
}

/// `cloud_prepare_parent_upload` request payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudPrepareParentUploadRequest {
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Destination id.
    pub destination_id: String,
    /// Source manifest digest (32-hex).
    pub source_manifest_digest: String,
    /// Sealed child membership.
    pub children: Vec<CloudPrepareParentUploadChildWire>,
}

/// `cloud_prepare_parent_upload` response payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudPrepareParentUploadResponse {
    /// Upload-set id.
    pub upload_set_id: String,
    /// True when this was an idempotent replay.
    pub already_prepared: bool,
}

/// `cloud_finalize_parent_upload` request payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudFinalizeParentUploadRequest {
    /// Upload-set id.
    pub upload_set_id: String,
}

/// `cloud_finalize_parent_upload` response payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudFinalizeParentUploadResponse {
    /// Finalize request accepted.
    pub ok: bool,
    /// Parent became durable.
    pub durable_parent: bool,
    /// True when this was an idempotent replay.
    pub already_finalized: bool,
}

/// Pending upload set row over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudPendingUploadSetWire {
    /// Upload set id.
    pub upload_set_id: String,
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Destination id.
    pub destination_id: String,
    /// Source manifest digest (32-hex).
    pub source_manifest_digest: String,
    /// Expected child count.
    pub expected_child_count: i64,
}

/// Outbound RPC response.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum Response {
    /// Successful archive registration with ids.
    Ok {
        /// The clip row id.
        clip_id: i64,
        /// The archive item row id.
        archive_item_id: i64,
    },
    /// Request/handler failure.
    Error {
        /// Human-readable error message.
        message: String,
    },
    /// Deterministic request rejection: the payload is invalid and will never
    /// succeed on retry. Distinct from `Error` (operational/transient) so
    /// clients can avoid futile retries and poison-drops.
    Rejected {
        /// Human-readable rejection reason.
        message: String,
    },
    /// Preference write acknowledged.
    PrefSet {
        /// The updated preference key.
        key: String,
    },
    /// Claim succeeded (`LIVE → DELETE_CLAIMED`).
    Claimed {
        /// Persisted delete generation token for trash naming.
        delete_gen: String,
    },
    /// Claim was denied.
    ClaimDenied {
        /// Human-readable reason.
        reason: String,
    },
    /// No such row exists.
    NotFound {},
    /// Generic write-ack response for delete-state transitions.
    Acked {},
    /// Eviction candidates query result.
    EvictionCandidates {
        /// Candidate rows.
        items: Vec<EvictionCandidateWire>,
    },
    /// Bounded exclusion-report query result.
    EvictionExclusionReport {
        /// Number of sampled rows included before grouping.
        sample_size: i64,
        /// True when more rows existed than the requested sample limit.
        sample_truncated: bool,
        /// Stable exclusion-reason aggregates.
        reasons: Vec<EvictionExclusionReasonWire>,
    },
    /// Delete-state recovery rows query result.
    RecoveryRows {
        /// Rows needing recovery.
        rows: Vec<RecoveryRowWire>,
    },
    /// Cloud candidates page.
    CloudCandidates {
        /// Candidate rows.
        items: Vec<CloudCandidateWire>,
        /// Opaque next cursor.
        next_cursor: Option<String>,
    },
    /// Cloud discover page.
    CloudDiscoverPage {
        /// Discover rows.
        items: Vec<CloudDiscoverWire>,
        /// Opaque next cursor.
        next_cursor: Option<String>,
    },
    /// Cloud queue page.
    CloudQueuePage {
        /// Queue rows.
        items: Vec<CloudQueueRowWire>,
        /// Opaque next cursor.
        next_cursor: Option<String>,
    },
    /// Queue state response.
    CloudQueueState {
        /// Resulting state.
        state: String,
    },
    /// Failed-upload retry accepted.
    CloudFailedUploadRetryAccepted {
        /// Stable durable job id.
        job_id: String,
        /// Logical request id.
        request_id: String,
        /// Resulting queue state.
        state: String,
    },
    /// Failed-upload retry idempotent replay.
    CloudFailedUploadRetryReplay {
        /// Stable durable job id.
        job_id: String,
        /// Logical request id.
        request_id: String,
        /// Replayed prior outcome (`accepted`/`refused`/`error`).
        outcome: String,
        /// Stored prior response status discriminator (`accepted`/`rejected`/`error`...).
        response_status: Option<String>,
        /// Stored prior HTTP-ish status code.
        response_code: Option<i64>,
        /// Optional replay detail.
        detail: Option<String>,
    },
    /// Failed-upload retry idempotency conflict.
    CloudFailedUploadRetryConflict {
        /// Existing durable job id.
        job_id: String,
        /// Existing logical request id.
        request_id: String,
        /// Human-readable conflict reason.
        message: String,
    },
    /// Failed-upload retry refused without queue mutation.
    CloudFailedUploadRetryRefused {
        /// Stable durable job id.
        job_id: String,
        /// Logical request id.
        request_id: String,
        /// Deterministic refusal reason.
        message: String,
    },
    /// Archive-delete request accepted/persisted.
    ArchiveDeleteAccepted {
        /// Stable durable job id.
        job_id: String,
        /// Logical request id.
        request_id: String,
        /// Persisted lifecycle state.
        state: String,
    },
    /// Archive-delete request idempotent replay.
    ArchiveDeleteReplay {
        /// Stable durable job id.
        job_id: String,
        /// Logical request id.
        request_id: String,
        /// Replayed prior outcome.
        outcome: String,
        /// Stored prior response status discriminator.
        response_status: Option<String>,
        /// Stored prior status code.
        response_code: Option<i64>,
        /// Optional replay detail.
        detail: Option<String>,
    },
    /// Archive-delete request idempotency conflict.
    ArchiveDeleteConflict {
        /// Existing durable job id.
        job_id: String,
        /// Existing logical request id.
        request_id: String,
        /// Human-readable conflict reason.
        message: String,
    },
    /// Archive-delete request refused without side effects.
    ArchiveDeleteRefused {
        /// Stable durable job id.
        job_id: String,
        /// Logical request id.
        request_id: String,
        /// Deterministic refusal reason.
        message: String,
    },
    /// Archive-delete request inspect result.
    ArchiveDeleteInspect {
        /// Stable durable job id.
        job_id: String,
        /// Logical request id.
        request_id: String,
        /// Persisted lifecycle state.
        state: String,
        /// Stored response status discriminator.
        response_status: Option<String>,
        /// Stored response status code.
        response_code: Option<i64>,
        /// Stored sanitized detail.
        detail: Option<String>,
    },
    /// Archive-delete request claimed for retentiond execution.
    ArchiveDeleteClaimed {
        /// Stable durable job id.
        job_id: String,
        /// Logical request id.
        request_id: String,
        /// Target archive item id.
        target_archive_item_id: i64,
        /// Target archive relative path fence.
        target_archive_path: String,
        /// Target archive size fence.
        target_archive_size_bytes: i64,
        /// Persisted delete generation token from claim.
        delete_gen: String,
    },
    /// Upload lease acquire response.
    UploadLeaseAcquired {
        /// Lease granted.
        granted: bool,
        /// Lease token.
        token: Option<String>,
        /// Lease boot id.
        boot_id: Option<String>,
        /// Monotonic expiry.
        expires_mono_ms: Option<i64>,
    },
    /// Upload lease renew response.
    UploadLeaseRenewed {
        /// Renew success.
        ok: bool,
        /// New expiry.
        expires_mono_ms: Option<i64>,
    },
    /// Upload lease release response.
    UploadLeaseReleased {
        /// Release success.
        ok: bool,
    },
    /// Upload commit response.
    CloudUploadCommitted {
        /// Commit success.
        ok: bool,
        /// Parent became durable.
        durable_parent: bool,
    },
    /// Upload fail response.
    CloudUploadFailed {
        /// Fail record success.
        ok: bool,
        /// Resulting queue state.
        state: String,
    },
    /// Derived stats response.
    CloudStats {
        /// Uploaded item count since baseline.
        synced_count: i64,
        /// Uploaded bytes since baseline.
        synced_bytes: i64,
        /// Baseline timestamp.
        since_at: i64,
    },
    /// Stats reset response.
    CloudStatsReset {
        /// Reset success.
        ok: bool,
        /// New baseline sequence.
        baseline_seq: i64,
    },
    /// Config response.
    CloudConfig {
        /// Typed config.
        config: CloudConfigWire,
    },
    /// History page response.
    CloudHistoryPage {
        /// History rows.
        items: Vec<CloudHistoryRowWire>,
        /// Opaque next cursor.
        next_cursor: Option<String>,
    },
    /// Pending upload sets page.
    CloudPendingUploadSetsPage {
        /// Pending set rows.
        items: Vec<CloudPendingUploadSetWire>,
        /// Opaque next cursor.
        next_cursor: Option<String>,
    },
    /// Finalize-event response.
    FinalizeEventArchive(FinalizeEventArchiveResponse),
    /// Prepare-parent-upload response.
    CloudPrepareParentUpload(CloudPrepareParentUploadResponse),
    /// Finalize-parent-upload response.
    CloudFinalizeParentUpload(CloudFinalizeParentUploadResponse),
}

/// Read one framed payload (4-byte LE length then bytes).
///
/// # Errors
///
/// Returns an error if the frame is torn or larger than `cap`.
pub fn read_frame(stream: &mut impl Read, cap: u32) -> io::Result<Vec<u8>> {
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

/// Write one framed payload (4-byte LE length then bytes).
///
/// # Errors
///
/// Returns an error if the payload cannot be framed or write fails.
pub fn write_frame(stream: &mut impl Write, payload: &[u8]) -> io::Result<()> {
    if payload.len() > MAX_REQUEST_FRAME as usize {
        return Err(io::Error::other(format!(
            "frame too large: {} > {}",
            payload.len(),
            MAX_REQUEST_FRAME
        )));
    }
    let len =
        u32::try_from(payload.len()).map_err(|_| io::Error::other("frame exceeds u32 length"))?;
    stream.write_all(&len.to_le_bytes())?;
    stream.write_all(payload)?;
    stream.flush()
}

/// Read and decode one [`Request`] frame.
///
/// # Errors
///
/// Returns an error on framing or JSON decode failures.
pub fn read_request(stream: &mut impl Read) -> io::Result<Request> {
    let payload = read_frame(stream, MAX_REQUEST_FRAME)?;
    serde_json::from_slice(&payload).map_err(io::Error::other)
}

/// Write one framed [`Response`].
///
/// # Errors
///
/// Returns an error on JSON encode or socket write failures.
pub fn write_response(stream: &mut impl Write, response: &Response) -> io::Result<()> {
    let payload = serde_json::to_vec(response).map_err(io::Error::other)?;
    write_frame(stream, &payload)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use std::io::Cursor;

    use serde_json::json;

    use super::{
        ArchiveAngle, ArchiveUnit, CloudCandidateWire, CloudConfigWire, CloudDiscoverWire,
        CloudFinalizeParentUploadRequest, CloudFinalizeParentUploadResponse, CloudHistoryRowWire,
        CloudPendingUploadSetWire, CloudPrepareParentUploadChildWire,
        CloudPrepareParentUploadRequest, CloudPrepareParentUploadResponse, CloudQueuePkWire,
        CloudQueueRetryResolutionWire, CloudQueueRowWire, CloudQueueUpsertWire,
        EvictionCandidateWire, EvictionExclusionReasonWire, FinalizeEventArchiveAngleWire,
        FinalizeEventArchiveClipWire, FinalizeEventArchiveRequest, FinalizeEventArchiveResponse,
        FinalizeEventArchiveSegmentWire, MAX_REQUEST_FRAME, RecoveryRowWire, RegisterArchivedClip,
        Request, Response, read_frame, read_request, write_frame, write_response,
    };

    #[test]
    fn request_roundtrip_frame_codec() {
        let req = Request::RegisterArchivedClip(RegisterArchivedClip {
            canonical_key: "slot0:TeslaCam/RecentClips/2026-06-19/2026-06-19_10-00-00".to_owned(),
            folder_class: "RecentClips".to_owned(),
            partition: "slot0".to_owned(),
            started_at: 1_718_805_600,
            ended_at: 1_718_805_660,
            duration_s: Some(60),
            archive: ArchiveUnit {
                path: "archive/2026-06-19/clip-001".to_owned(),
                size_bytes: 12_345,
                file_count: 4,
                archived_at: 1_718_805_700,
            },
            angles: vec![ArchiveAngle {
                camera: "front".to_owned(),
                file_ref: "archive/2026-06-19/clip-001/front.mp4".to_owned(),
                offset_ms: 0,
                duration_s: Some(60),
                size_bytes: 3_086,
            }],
        });

        let mut buf = Vec::new();
        let json = serde_json::to_vec(&req).unwrap();
        write_frame(&mut buf, &json).unwrap();
        let mut cur = Cursor::new(buf);
        let decoded = read_request(&mut cur).unwrap();
        assert_eq!(decoded, req);
    }

    #[test]
    fn quarantined_request_roundtrip_frame_codec() {
        let req = Request::RegisterQuarantinedArchive(RegisterArchivedClip {
            canonical_key: "slot0:TeslaCam/RecentClips/2026-06-19/2026-06-19_10-00-00".to_owned(),
            folder_class: "RecentClips".to_owned(),
            partition: "slot0".to_owned(),
            started_at: 1_718_805_600,
            ended_at: 1_718_805_660,
            duration_s: Some(60),
            archive: ArchiveUnit {
                path: "archive/2026-06-19/clip-001".to_owned(),
                size_bytes: 12_345,
                file_count: 4,
                archived_at: 1_718_805_700,
            },
            angles: vec![ArchiveAngle {
                camera: "front".to_owned(),
                file_ref: "archive/2026-06-19/clip-001/front.mp4".to_owned(),
                offset_ms: 0,
                duration_s: Some(60),
                size_bytes: 3_086,
            }],
        });

        let mut buf = Vec::new();
        let json = serde_json::to_vec(&req).unwrap();
        write_frame(&mut buf, &json).unwrap();
        let mut cur = Cursor::new(buf);
        let decoded = read_request(&mut cur).unwrap();
        assert_eq!(decoded, req);
    }

    #[test]
    fn read_frame_rejects_oversize() {
        let mut buf = Vec::new();
        buf.extend_from_slice(&(MAX_REQUEST_FRAME + 1).to_le_bytes());
        let mut cur = Cursor::new(buf);
        assert!(read_frame(&mut cur, MAX_REQUEST_FRAME).is_err());
    }

    #[test]
    fn write_response_rejects_oversize() {
        let response = Response::Rejected {
            message: "x".repeat(MAX_REQUEST_FRAME as usize),
        };
        let mut buf = Vec::new();
        assert!(write_response(&mut buf, &response).is_err());
    }

    #[test]
    fn write_frame_rejects_payload_above_cap() {
        let payload = vec![0_u8; MAX_REQUEST_FRAME as usize + 1];
        let mut buf = Vec::new();
        assert!(write_frame(&mut buf, &payload).is_err());
    }

    #[test]
    fn response_roundtrip_frame_codec() {
        let response = Response::Ok {
            clip_id: 7,
            archive_item_id: 11,
        };
        let mut buf = Vec::new();
        write_response(&mut buf, &response).unwrap();
        let mut cur = Cursor::new(buf);
        let payload = read_frame(&mut cur, MAX_REQUEST_FRAME).unwrap();
        let decoded: Response = serde_json::from_slice(&payload).unwrap();
        assert_eq!(decoded, response);
    }

    #[test]
    fn request_set_pref_serializes_with_set_pref_cmd() {
        let request = Request::SetPref {
            key: "speed_unit".to_owned(),
            value: "kph".to_owned(),
        };
        let encoded = serde_json::to_value(&request).unwrap();
        assert_eq!(
            encoded.get("cmd").and_then(serde_json::Value::as_str),
            Some("set_pref")
        );
        let decoded: Request = serde_json::from_value(encoded).unwrap();
        assert_eq!(decoded, request);
    }

    #[test]
    fn response_pref_set_serializes_with_pref_set_status() {
        let response = Response::PrefSet {
            key: "speed_unit".to_owned(),
        };
        let encoded = serde_json::to_value(&response).unwrap();
        assert_eq!(
            encoded.get("status").and_then(serde_json::Value::as_str),
            Some("pref_set")
        );
        let decoded: Response = serde_json::from_value(encoded).unwrap();
        assert_eq!(decoded, response);
    }

    #[test]
    fn response_rejected_serializes_with_rejected_status() {
        let response = Response::Rejected {
            message: "invalid camera: left_pillar".to_owned(),
        };
        let encoded = serde_json::to_value(&response).unwrap();
        assert_eq!(
            encoded.get("status").and_then(serde_json::Value::as_str),
            Some("rejected")
        );
        let decoded: Response = serde_json::from_value(encoded).unwrap();
        assert_eq!(decoded, response);
    }

    #[test]
    fn delete_control_requests_serialize_with_expected_cmd_tags() {
        let cases = vec![
            (
                "claim_eviction_candidate",
                Request::ClaimEvictionCandidate {
                    id: 1,
                    recency_floor_epoch: 1_700_000_000,
                    allow_undurable: false,
                },
            ),
            (
                "mark_archive_deleting",
                Request::MarkArchiveDeleting { id: 2 },
            ),
            (
                "mark_archive_deleted",
                Request::MarkArchiveDeleted {
                    id: 3,
                    bytes_freed: 4096,
                },
            ),
            (
                "release_archive_delete_claim",
                Request::ReleaseArchiveDeleteClaim { id: 4 },
            ),
            (
                "clear_archive_delete_generation",
                Request::ClearArchiveDeleteGeneration {
                    id: 5,
                    delete_gen: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_owned(),
                },
            ),
            (
                "quarantine_archive_item",
                Request::QuarantineArchiveItem {
                    id: 6,
                    reason: "bad state".to_owned(),
                },
            ),
            (
                "list_eviction_candidates",
                Request::ListEvictionCandidates {
                    recency_floor_epoch: 1_700_000_000,
                    allow_undurable: false,
                    limit: 100,
                },
            ),
            (
                "list_eviction_exclusion_report",
                Request::ListEvictionExclusionReport {
                    recency_floor_epoch: 1_700_000_000,
                    allow_undurable: false,
                    limit: 100,
                },
            ),
            ("list_recovery_rows", Request::ListRecoveryRows {}),
        ];

        for (expected_cmd, request) in cases {
            let encoded = serde_json::to_value(&request).unwrap();
            assert_eq!(
                encoded.get("cmd").and_then(serde_json::Value::as_str),
                Some(expected_cmd)
            );
            let decoded: Request = serde_json::from_value(encoded).unwrap();
            assert_eq!(decoded, request);
        }
    }

    #[test]
    #[allow(clippy::too_many_lines)]
    fn cloud_control_requests_serialize_with_expected_cmd_tags() {
        let cases = vec![
            (
                "cloud_candidates",
                Request::CloudCandidates {
                    folders: vec!["RecentClips".to_owned()],
                    after_cursor: None,
                    limit: 10,
                },
            ),
            (
                "cloud_queue_load",
                Request::CloudQueueLoad {
                    after_cursor: Some("opaque".to_owned()),
                    limit: 10,
                    upload_set_id: None,
                },
            ),
            (
                "cloud_pending_upload_sets_load",
                Request::CloudPendingUploadSetsLoad {
                    after_cursor: Some("opaque".to_owned()),
                    limit: 10,
                },
            ),
            (
                "cloud_discover",
                Request::CloudDiscover {
                    after_cursor: Some("opaque".to_owned()),
                    limit: 10,
                },
            ),
            (
                "cloud_queue_upsert",
                Request::CloudQueueUpsert {
                    item: CloudQueueUpsertWire {
                        archive_item_id: 1,
                        child_key: "child".to_owned(),
                        destination_id: "dest".to_owned(),
                        remote_key: "rk".to_owned(),
                        category: "bulk".to_owned(),
                        seq: 1,
                        total_bytes: 2,
                        content_sha256:
                            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                                .to_owned(),
                        expected_hash: None,
                        verify_alg: "none".to_owned(),
                    },
                },
            ),
            (
                "cloud_queue_retry",
                Request::CloudQueueRetry {
                    archive_item_id: 1,
                    child_key: Some("child".to_owned()),
                    upload_set_id: None,
                    resolution: CloudQueueRetryResolutionWire::Replace,
                },
            ),
            (
                "cloud_failed_upload_retry",
                Request::CloudFailedUploadRetry {
                    archive_item_id: 1,
                    child_key: "child".to_owned(),
                    upload_set_id: None,
                    request_id: "req-1abc".to_owned(),
                    idempotency_key: "idem-1abc".to_owned(),
                    request_hash:
                        "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
                            .to_owned(),
                    job_id: "m-123".to_owned(),
                },
            ),
            (
                "archive_delete_create_or_load",
                Request::ArchiveDeleteCreateOrLoad {
                    job_id: "m-124".to_owned(),
                    request_id: "req-124".to_owned(),
                    idempotency_key: "idem-124".to_owned(),
                    request_hash:
                        "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
                            .to_owned(),
                    target_archive_item_id: 7,
                    target_archive_path: "archive/recent/sample".to_owned(),
                    target_archive_size_bytes: 4096,
                    target_archive_file_count: 4,
                    target_clip_canonical_key: Some(
                        "slot0:TeslaCam/RecentClips/2026-08-11/sample".to_owned(),
                    ),
                    target_manifest_digest: Some("0123456789abcdef0123456789abcdef".to_owned()),
                },
            ),
            (
                "archive_delete_inspect",
                Request::ArchiveDeleteInspect {
                    job_id: "m-124".to_owned(),
                },
            ),
            (
                "archive_delete_claim_next",
                Request::ArchiveDeleteClaimNext {},
            ),
            (
                "archive_delete_complete",
                Request::ArchiveDeleteComplete {
                    job_id: "m-124".to_owned(),
                    owned_delete_gen: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_owned(),
                },
            ),
            (
                "archive_delete_fail",
                Request::ArchiveDeleteFail {
                    job_id: "m-124".to_owned(),
                    owned_delete_gen: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_owned(),
                    detail: "delete failed".to_owned(),
                    requeue: false,
                },
            ),
            (
                "upload_lease_acquire",
                Request::UploadLeaseAcquire {
                    archive_item_id: 1,
                    ttl_ms: 1_000,
                },
            ),
            (
                "upload_lease_renew",
                Request::UploadLeaseRenew {
                    token: "1:abc".to_owned(),
                    ttl_ms: 1_000,
                },
            ),
            (
                "upload_lease_release",
                Request::UploadLeaseRelease {
                    token: "1:abc".to_owned(),
                },
            ),
            (
                "cloud_upload_commit",
                Request::CloudUploadCommit {
                    queue_pk: CloudQueuePkWire {
                        destination_id: "dest".to_owned(),
                        remote_key: "rk".to_owned(),
                    },
                    attempt_id: "a1".to_owned(),
                    upload_set_id: None,
                    hash: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                        .to_owned(),
                    hash_alg: "sha256".to_owned(),
                    size: 12,
                },
            ),
            (
                "cloud_upload_fail",
                Request::CloudUploadFail {
                    queue_pk: CloudQueuePkWire {
                        destination_id: "dest".to_owned(),
                        remote_key: "rk".to_owned(),
                    },
                    attempt_id: "a2".to_owned(),
                    upload_set_id: None,
                    error_class: "timeout".to_owned(),
                    not_before: Some(123),
                    terminal: false,
                },
            ),
            ("cloud_stats_get", Request::CloudStatsGet {}),
            ("cloud_stats_reset", Request::CloudStatsReset {}),
            ("cloud_config_get", Request::CloudConfigGet {}),
            (
                "cloud_config_put",
                Request::CloudConfigPut {
                    config: CloudConfigWire {
                        sentry_enabled: true,
                        saved_enabled: true,
                        recent_enabled: false,
                        sentry_priority: 0,
                        saved_priority: 1,
                        recent_priority: 2,
                        reserve_gb: 4,
                        max_attempts: 5,
                        base_backoff_secs: 60,
                        keep_until_backed_up: true,
                        auto_sync: true,
                    },
                },
            ),
            (
                "cloud_history_load",
                Request::CloudHistoryLoad {
                    after_cursor: None,
                    limit: 25,
                },
            ),
            (
                "cloud_failed_history_load",
                Request::CloudFailedHistoryLoad {
                    after_cursor: Some("failed-cursor".to_owned()),
                    limit: 8,
                },
            ),
            (
                "finalize_event_archive",
                Request::FinalizeEventArchive(FinalizeEventArchiveRequest {
                    pass_id: "11111111111111111111111111111111".to_owned(),
                    source_event_key: "event-1".to_owned(),
                    source_volume_id: Some("vol-1".to_owned()),
                    source_generation: "boot-a:42".to_owned(),
                    expected_prior_manifest_digest: None,
                    manifest_digest: "22222222222222222222222222222222".to_owned(),
                    segment_set_digest:
                        "3333333333333333333333333333333333333333333333333333333333333333"
                            .to_owned(),
                    expected_segment_count: 1,
                    size_bytes: 10,
                    file_count: 1,
                    archived_at: 123,
                    generation_dir_path: "archive/events/e1".to_owned(),
                    folder_class: "SavedClips".to_owned(),
                    partition: "slot0".to_owned(),
                    segments: vec![FinalizeEventArchiveSegmentWire {
                        segment_key: "f.mp4".to_owned(),
                        size_bytes: 10,
                        mtime_ms: 1,
                        content_sha256:
                            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                                .to_owned(),
                    }],
                    clips: vec![FinalizeEventArchiveClipWire {
                        canonical_key: "clip-1".to_owned(),
                        started_at: 1,
                        ended_at: 2,
                        folder_class: "SavedClips".to_owned(),
                        partition: "slot0".to_owned(),
                    }],
                    angles: vec![FinalizeEventArchiveAngleWire {
                        canonical_key: "clip-1".to_owned(),
                        camera: "front".to_owned(),
                        file_ref: "archive/events/e1/front.mp4".to_owned(),
                        offset_ms: 0,
                        duration_s: Some(1),
                        size_bytes: 10,
                    }],
                }),
            ),
            (
                "cloud_prepare_parent_upload",
                Request::CloudPrepareParentUpload(CloudPrepareParentUploadRequest {
                    archive_item_id: 1,
                    destination_id: "dest".to_owned(),
                    source_manifest_digest: "44444444444444444444444444444444".to_owned(),
                    children: vec![CloudPrepareParentUploadChildWire {
                        child_key: "child-1".to_owned(),
                        destination_id: "dest".to_owned(),
                        remote_key: "rk/1".to_owned(),
                        category: "bulk".to_owned(),
                        seq: 0,
                        total_bytes: 10,
                        manifest_mtime_ms: 1,
                        content_sha256:
                            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                                .to_owned(),
                        expected_hash: "etag-1".to_owned(),
                        verify_alg: "md5".to_owned(),
                    }],
                }),
            ),
            (
                "cloud_finalize_parent_upload",
                Request::CloudFinalizeParentUpload(CloudFinalizeParentUploadRequest {
                    upload_set_id: "55555555555555555555555555555555".to_owned(),
                }),
            ),
        ];

        for (expected_cmd, request) in cases {
            let encoded = serde_json::to_value(&request).unwrap();
            assert_eq!(
                encoded.get("cmd").and_then(serde_json::Value::as_str),
                Some(expected_cmd)
            );
            let decoded: Request = serde_json::from_value(encoded).unwrap();
            assert_eq!(decoded, request);
        }
    }

    #[test]
    fn delete_control_responses_serialize_with_expected_status_tags() {
        let cases = vec![
            (
                "claimed",
                Response::Claimed {
                    delete_gen: "0000000000000000000000000000000f".to_owned(),
                },
            ),
            (
                "claim_denied",
                Response::ClaimDenied {
                    reason: "unexpired lease".to_owned(),
                },
            ),
            ("not_found", Response::NotFound {}),
            ("acked", Response::Acked {}),
            (
                "eviction_candidates",
                Response::EvictionCandidates {
                    items: vec![EvictionCandidateWire {
                        id: 9,
                        path: "archive/old/clip".to_owned(),
                        size_bytes: 1_024,
                        archived_at: 1_700_000_000,
                        folder_class: "RecentClips".to_owned(),
                    }],
                },
            ),
            (
                "eviction_exclusion_report",
                Response::EvictionExclusionReport {
                    sample_size: 2,
                    sample_truncated: false,
                    reasons: vec![EvictionExclusionReasonWire {
                        reason: "too_recent".to_owned(),
                        count: 2,
                        size_bytes: 2_048,
                    }],
                },
            ),
            (
                "recovery_rows",
                Response::RecoveryRows {
                    rows: vec![RecoveryRowWire {
                        id: 10,
                        delete_state: "DELETE_CLAIMED".to_owned(),
                        path: "archive/old/clip".to_owned(),
                        size_bytes: 2_048,
                        delete_gen: Some("abc".to_owned()),
                    }],
                },
            ),
        ];

        for (expected_status, response) in cases {
            let encoded = serde_json::to_value(&response).unwrap();
            assert_eq!(
                encoded.get("status").and_then(serde_json::Value::as_str),
                Some(expected_status)
            );
            let decoded: Response = serde_json::from_value(encoded).unwrap();
            assert_eq!(decoded, response);
        }
    }

    #[test]
    #[allow(clippy::too_many_lines)]
    fn cloud_control_responses_serialize_with_expected_status_tags() {
        let cases = vec![
            (
                "cloud_candidates",
                Response::CloudCandidates {
                    items: vec![CloudCandidateWire {
                        archive_item_id: 1,
                        child_key: "child".to_owned(),
                        source_rel: "archive/a/child".to_owned(),
                        destination_id: "dest".to_owned(),
                        remote_key: "rk".to_owned(),
                        size_bytes: 10,
                        content_sha256:
                            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                                .to_owned(),
                        state: "queued".to_owned(),
                        category: "bulk".to_owned(),
                        seq: 1,
                    }],
                    next_cursor: Some("opaque".to_owned()),
                },
            ),
            (
                "cloud_queue_page",
                Response::CloudQueuePage {
                    items: vec![CloudQueueRowWire {
                        archive_item_id: 1,
                        child_key: "child".to_owned(),
                        source_rel: "archive/a/child".to_owned(),
                        destination_id: "dest".to_owned(),
                        remote_key: "rk".to_owned(),
                        category: "bulk".to_owned(),
                        seq: 1,
                        total_bytes: 10,
                        bytes_uploaded: 0,
                        expected_hash: Some("etag-value".to_owned()),
                        verify_alg: "md5".to_owned(),
                        content_sha256:
                            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                                .to_owned(),
                        state: "queued".to_owned(),
                        attempts: 0,
                        not_before: None,
                        last_error: None,
                        upload_set_id: Some("44444444444444444444444444444444".to_owned()),
                    }],
                    next_cursor: None,
                },
            ),
            (
                "cloud_discover_page",
                Response::CloudDiscoverPage {
                    items: vec![CloudDiscoverWire {
                        archive_item_id: 1,
                        folder_class: "RecentClips".to_owned(),
                        path: "archive/a".to_owned(),
                        manifest_digest: Some("cccccccccccccccccccccccccccccccc".to_owned()),
                        category: "bulk".to_owned(),
                    }],
                    next_cursor: Some("opaque".to_owned()),
                },
            ),
            (
                "cloud_queue_state",
                Response::CloudQueueState {
                    state: "queued".to_owned(),
                },
            ),
            (
                "cloud_failed_upload_retry_accepted",
                Response::CloudFailedUploadRetryAccepted {
                    job_id: "m-501".to_owned(),
                    request_id: "req-501".to_owned(),
                    state: "queued".to_owned(),
                },
            ),
            (
                "cloud_failed_upload_retry_replay",
                Response::CloudFailedUploadRetryReplay {
                    job_id: "m-501".to_owned(),
                    request_id: "req-501".to_owned(),
                    outcome: "accepted".to_owned(),
                    response_status: Some("accepted".to_owned()),
                    response_code: Some(202),
                    detail: Some("queued".to_owned()),
                },
            ),
            (
                "cloud_failed_upload_retry_conflict",
                Response::CloudFailedUploadRetryConflict {
                    job_id: "m-501".to_owned(),
                    request_id: "req-501".to_owned(),
                    message: "idempotency key conflict".to_owned(),
                },
            ),
            (
                "cloud_failed_upload_retry_refused",
                Response::CloudFailedUploadRetryRefused {
                    job_id: "m-502".to_owned(),
                    request_id: "req-502".to_owned(),
                    message: "cannot retry a queued queue row".to_owned(),
                },
            ),
            (
                "archive_delete_accepted",
                Response::ArchiveDeleteAccepted {
                    job_id: "m-601".to_owned(),
                    request_id: "req-601".to_owned(),
                    state: "queued".to_owned(),
                },
            ),
            (
                "archive_delete_replay",
                Response::ArchiveDeleteReplay {
                    job_id: "m-601".to_owned(),
                    request_id: "req-601".to_owned(),
                    outcome: "accepted".to_owned(),
                    response_status: Some("accepted".to_owned()),
                    response_code: Some(202),
                    detail: Some("queued".to_owned()),
                },
            ),
            (
                "archive_delete_conflict",
                Response::ArchiveDeleteConflict {
                    job_id: "m-601".to_owned(),
                    request_id: "req-601".to_owned(),
                    message: "idempotency key conflict".to_owned(),
                },
            ),
            (
                "archive_delete_refused",
                Response::ArchiveDeleteRefused {
                    job_id: "m-602".to_owned(),
                    request_id: "req-602".to_owned(),
                    message: "archive delete request stale; refresh required".to_owned(),
                },
            ),
            (
                "archive_delete_inspect",
                Response::ArchiveDeleteInspect {
                    job_id: "m-603".to_owned(),
                    request_id: "req-603".to_owned(),
                    state: "refused".to_owned(),
                    response_status: Some("rejected".to_owned()),
                    response_code: Some(409),
                    detail: Some("archive delete request stale; refresh required".to_owned()),
                },
            ),
            (
                "archive_delete_claimed",
                Response::ArchiveDeleteClaimed {
                    job_id: "m-604".to_owned(),
                    request_id: "req-604".to_owned(),
                    target_archive_item_id: 7,
                    target_archive_path: "archive/recent/sample".to_owned(),
                    target_archive_size_bytes: 4096,
                    delete_gen: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_owned(),
                },
            ),
            (
                "upload_lease_acquired",
                Response::UploadLeaseAcquired {
                    granted: true,
                    token: Some("1:abc".to_owned()),
                    boot_id: Some("boot".to_owned()),
                    expires_mono_ms: Some(1200),
                },
            ),
            (
                "upload_lease_renewed",
                Response::UploadLeaseRenewed {
                    ok: true,
                    expires_mono_ms: Some(2200),
                },
            ),
            (
                "upload_lease_released",
                Response::UploadLeaseReleased { ok: true },
            ),
            (
                "cloud_upload_committed",
                Response::CloudUploadCommitted {
                    ok: true,
                    durable_parent: false,
                },
            ),
            (
                "cloud_upload_failed",
                Response::CloudUploadFailed {
                    ok: true,
                    state: "failed".to_owned(),
                },
            ),
            (
                "cloud_stats",
                Response::CloudStats {
                    synced_count: 1,
                    synced_bytes: 100,
                    since_at: 0,
                },
            ),
            (
                "cloud_stats_reset",
                Response::CloudStatsReset {
                    ok: true,
                    baseline_seq: 9,
                },
            ),
            (
                "cloud_config",
                Response::CloudConfig {
                    config: CloudConfigWire {
                        sentry_enabled: true,
                        saved_enabled: true,
                        recent_enabled: false,
                        sentry_priority: 0,
                        saved_priority: 1,
                        recent_priority: 2,
                        reserve_gb: 4,
                        max_attempts: 5,
                        base_backoff_secs: 60,
                        keep_until_backed_up: true,
                        auto_sync: true,
                    },
                },
            ),
            (
                "cloud_history_page",
                Response::CloudHistoryPage {
                    items: vec![CloudHistoryRowWire {
                        id: 1,
                        completion_seq: 7,
                        archive_item_id: 1,
                        child_key: "child".to_owned(),
                        destination_id: "dest".to_owned(),
                        outcome: "uploaded".to_owned(),
                        size_bytes: 10,
                        at: 100,
                        error_class: None,
                        upload_set_id: None,
                    }],
                    next_cursor: None,
                },
            ),
            (
                "cloud_pending_upload_sets_page",
                Response::CloudPendingUploadSetsPage {
                    items: vec![CloudPendingUploadSetWire {
                        upload_set_id: "99999999999999999999999999999999".to_owned(),
                        archive_item_id: 2,
                        destination_id: "dest".to_owned(),
                        source_manifest_digest: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_owned(),
                        expected_child_count: 3,
                    }],
                    next_cursor: Some("opaque".to_owned()),
                },
            ),
            (
                "finalize_event_archive",
                Response::FinalizeEventArchive(FinalizeEventArchiveResponse {
                    archive_item_id: 7,
                    already_finalized: false,
                }),
            ),
            (
                "cloud_prepare_parent_upload",
                Response::CloudPrepareParentUpload(CloudPrepareParentUploadResponse {
                    upload_set_id: "66666666666666666666666666666666".to_owned(),
                    already_prepared: false,
                }),
            ),
            (
                "cloud_finalize_parent_upload",
                Response::CloudFinalizeParentUpload(CloudFinalizeParentUploadResponse {
                    ok: true,
                    durable_parent: false,
                    already_finalized: false,
                }),
            ),
        ];

        for (expected_status, response) in cases {
            let encoded = serde_json::to_value(&response).unwrap();
            assert_eq!(
                encoded.get("status").and_then(serde_json::Value::as_str),
                Some(expected_status)
            );
            let decoded: Response = serde_json::from_value(encoded).unwrap();
            assert_eq!(decoded, response);
        }
    }

    #[test]
    fn unknown_request_cmd_fails_to_deserialize() {
        let raw = json!({
            "cmd": "definitely_unknown_cmd",
            "id": 42
        });
        let result = serde_json::from_value::<Request>(raw);
        assert!(result.is_err());
    }

    #[test]
    fn cloud_queue_load_upload_set_id_defaults_to_none_when_absent() {
        let raw = json!({
            "cmd": "cloud_queue_load",
            "after_cursor": "opaque",
            "limit": 10
        });
        let decoded: Request = serde_json::from_value(raw).unwrap();
        assert_eq!(
            decoded,
            Request::CloudQueueLoad {
                after_cursor: Some("opaque".to_owned()),
                limit: 10,
                upload_set_id: None,
            }
        );
    }

    #[test]
    fn cloud_queue_row_wire_upload_set_id_roundtrips() {
        let row = CloudQueueRowWire {
            archive_item_id: 1,
            child_key: "child".to_owned(),
            source_rel: "archive/a/child".to_owned(),
            destination_id: "dest".to_owned(),
            remote_key: "rk".to_owned(),
            category: "bulk".to_owned(),
            seq: 1,
            total_bytes: 10,
            bytes_uploaded: 0,
            expected_hash: Some("etag-value".to_owned()),
            verify_alg: "md5".to_owned(),
            content_sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                .to_owned(),
            state: "queued".to_owned(),
            attempts: 0,
            not_before: None,
            last_error: None,
            upload_set_id: Some("12341234123412341234123412341234".to_owned()),
        };
        let decoded: CloudQueueRowWire =
            serde_json::from_value(serde_json::to_value(&row).unwrap()).unwrap();
        assert_eq!(decoded, row);
    }

    #[test]
    fn finalize_event_archive_structs_roundtrip() {
        let request = FinalizeEventArchiveRequest {
            pass_id: "77777777777777777777777777777777".to_owned(),
            source_event_key: "event-2".to_owned(),
            source_volume_id: None,
            source_generation: "boot-b:9".to_owned(),
            expected_prior_manifest_digest: Some("88888888888888888888888888888888".to_owned()),
            manifest_digest: "99999999999999999999999999999999".to_owned(),
            segment_set_digest: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                .to_owned(),
            expected_segment_count: 2,
            size_bytes: 20,
            file_count: 2,
            archived_at: 456,
            generation_dir_path: "archive/events/e2".to_owned(),
            folder_class: "SentryClips".to_owned(),
            partition: "slot1".to_owned(),
            segments: vec![FinalizeEventArchiveSegmentWire {
                segment_key: "seg-1".to_owned(),
                size_bytes: 20,
                mtime_ms: 2,
                content_sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                    .to_owned(),
            }],
            clips: vec![FinalizeEventArchiveClipWire {
                canonical_key: "clip-2".to_owned(),
                started_at: 10,
                ended_at: 20,
                folder_class: "SentryClips".to_owned(),
                partition: "slot1".to_owned(),
            }],
            angles: vec![FinalizeEventArchiveAngleWire {
                canonical_key: "clip-2".to_owned(),
                camera: "back".to_owned(),
                file_ref: "archive/events/e2/back.mp4".to_owned(),
                offset_ms: 100,
                duration_s: Some(10),
                size_bytes: 20,
            }],
        };
        let response = FinalizeEventArchiveResponse {
            archive_item_id: 12,
            already_finalized: true,
        };
        let decoded_request: FinalizeEventArchiveRequest =
            serde_json::from_value(serde_json::to_value(&request).unwrap()).unwrap();
        let decoded_response: FinalizeEventArchiveResponse =
            serde_json::from_value(serde_json::to_value(&response).unwrap()).unwrap();
        assert_eq!(decoded_request, request);
        assert_eq!(decoded_response, response);
    }

    #[test]
    fn cloud_prepare_parent_upload_structs_roundtrip() {
        let request = CloudPrepareParentUploadRequest {
            archive_item_id: 99,
            destination_id: "dest".to_owned(),
            source_manifest_digest: "cccccccccccccccccccccccccccccccc".to_owned(),
            children: vec![CloudPrepareParentUploadChildWire {
                child_key: "child-z".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/z".to_owned(),
                category: "trip".to_owned(),
                seq: 5,
                total_bytes: 50,
                manifest_mtime_ms: 1234,
                content_sha256: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
                    .to_owned(),
                expected_hash: "etag-z".to_owned(),
                verify_alg: "sha1".to_owned(),
            }],
        };
        let response = CloudPrepareParentUploadResponse {
            upload_set_id: "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee".to_owned(),
            already_prepared: true,
        };
        let decoded_request: CloudPrepareParentUploadRequest =
            serde_json::from_value(serde_json::to_value(&request).unwrap()).unwrap();
        let decoded_response: CloudPrepareParentUploadResponse =
            serde_json::from_value(serde_json::to_value(&response).unwrap()).unwrap();
        assert_eq!(decoded_request, request);
        assert_eq!(decoded_response, response);
    }

    #[test]
    fn cloud_finalize_parent_upload_structs_roundtrip() {
        let request = CloudFinalizeParentUploadRequest {
            upload_set_id: "ffffffffffffffffffffffffffffffff".to_owned(),
        };
        let response = CloudFinalizeParentUploadResponse {
            ok: true,
            durable_parent: true,
            already_finalized: true,
        };
        let decoded_request: CloudFinalizeParentUploadRequest =
            serde_json::from_value(serde_json::to_value(&request).unwrap()).unwrap();
        let decoded_response: CloudFinalizeParentUploadResponse =
            serde_json::from_value(serde_json::to_value(&response).unwrap()).unwrap();
        assert_eq!(decoded_request, request);
        assert_eq!(decoded_response, response);
    }
}
