//! Durable persistence for internal archive-delete request idempotency.

use rusqlite::{Connection, OptionalExtension, Transaction, params};
use teslausb_core::durable_mutation::{
    DurableMutationState, SameKeyIdempotencyResult, evaluate_same_key_idempotency,
    sanitize_public_error, validate_idempotency_key, validate_mutation_job_id,
    validate_request_hash, validate_request_id,
};

use crate::db::{DbError, now_epoch_s};

const ARCHIVE_DELETE_STALE_PLAN_ERROR: &str = "archive delete request stale; refresh required";
const ARCHIVE_DELETE_STALE_OWNERSHIP_ERROR: &str =
    "archive delete request ownership stale; refresh required";
const ARCHIVE_DELETE_RECONCILIATION_REQUIRED_ERROR: &str =
    "archive delete request reconciliation required; refresh required";
const MAX_ARCHIVE_PATH_LEN: usize = 1024;
const MAX_CLIP_CANONICAL_KEY_LEN: usize = 512;

/// Persisted archive-delete request identity + stale-plan fence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArchiveDeleteRequestRecord {
    /// Logical request id.
    pub request_id: String,
    /// Durable idempotency key.
    pub idempotency_key: String,
    /// Canonical request hash.
    pub request_hash: String,
    /// Target archive item id.
    pub target_archive_item_id: i64,
    /// Target archive relative path fence.
    pub target_archive_path: String,
    /// Target archive size fence.
    pub target_archive_size_bytes: i64,
    /// Target archive file-count fence.
    pub target_archive_file_count: i64,
    /// Optional linked clip identity fence.
    pub target_clip_canonical_key: Option<String>,
    /// Optional manifest-digest fence.
    pub target_manifest_digest: Option<String>,
}

impl ArchiveDeleteRequestRecord {
    /// Build a validated archive-delete request record.
    ///
    /// # Errors
    ///
    /// Returns a static reason when one or more fields are invalid.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        request_id: String,
        idempotency_key: String,
        request_hash: String,
        target_archive_item_id: i64,
        target_archive_path: String,
        target_archive_size_bytes: i64,
        target_archive_file_count: i64,
        target_clip_canonical_key: Option<String>,
        target_manifest_digest: Option<String>,
    ) -> Result<Self, &'static str> {
        validate_request_id(&request_id)?;
        validate_idempotency_key(&idempotency_key)?;
        validate_request_hash(&request_hash)?;
        validate_archive_delete_target(
            target_archive_item_id,
            &target_archive_path,
            target_archive_size_bytes,
            target_archive_file_count,
            target_clip_canonical_key.as_deref(),
            target_manifest_digest.as_deref(),
        )?;
        Ok(Self {
            request_id,
            idempotency_key,
            request_hash,
            target_archive_item_id,
            target_archive_path,
            target_archive_size_bytes,
            target_archive_file_count,
            target_clip_canonical_key,
            target_manifest_digest,
        })
    }
}

/// Stored archive-delete request row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArchiveDeleteRequestRow {
    /// Stable durable job id.
    pub job_id: String,
    /// Owning subsystem.
    pub owner: String,
    /// Mutation kind discriminator.
    pub kind: String,
    /// Lifecycle state.
    pub state: DurableMutationState,
    /// Request identity + stale-plan fence target.
    pub request: ArchiveDeleteRequestRecord,
    /// Persisted delete ownership marker once claimed by an executor.
    pub owned_delete_gen: Option<String>,
    /// Optional response status discriminator.
    pub response_status: Option<String>,
    /// Optional status code.
    pub response_code: Option<i64>,
    /// Optional sanitized error/detail.
    pub sanitized_error: Option<String>,
    /// Row create timestamp.
    pub created_at: i64,
    /// Last update timestamp.
    pub updated_at: i64,
    /// Terminal completion timestamp, when known.
    pub completed_at: Option<i64>,
}

/// New archive-delete request insert payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NewArchiveDeleteRequestRow {
    /// Stable durable job id.
    pub job_id: String,
    /// Owning subsystem.
    pub owner: String,
    /// Mutation kind discriminator.
    pub kind: String,
    /// Request identity + stale-plan fence target.
    pub request: ArchiveDeleteRequestRecord,
}

/// Insert-or-load idempotency outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CreateOrLoadArchiveDeleteRequestResult {
    /// First insert won.
    Inserted(ArchiveDeleteRequestRow),
    /// Same key + same hash replay.
    Replay(ArchiveDeleteRequestRow),
    /// Same key + different hash (or request-id conflict).
    Conflict409(ArchiveDeleteRequestRow),
}

fn invalid_input(message: &str) -> DbError {
    DbError::Sqlite(rusqlite::Error::InvalidParameterName(message.to_owned()))
}

fn invalid_input_sqlite(message: &str) -> rusqlite::Error {
    rusqlite::Error::InvalidParameterName(message.to_owned())
}

fn map_validation_err(reason: &str) -> DbError {
    invalid_input(reason)
}

fn validate_owner_or_kind(value: &str, field: &str, max_len: usize) -> Result<(), DbError> {
    if value.is_empty() || value.len() > max_len {
        return Err(invalid_input(match field {
            "owner" => "owner must be 1..=32 chars",
            _ => "kind must be 1..=64 chars",
        }));
    }
    if value
        .bytes()
        .any(|byte| !(byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.')))
    {
        return Err(invalid_input("owner/kind has an unsupported character"));
    }
    Ok(())
}

fn validate_response_status(status: Option<&str>) -> Result<(), DbError> {
    let Some(value) = status else {
        return Ok(());
    };
    if matches!(
        value,
        "accepted" | "replay" | "conflict" | "rejected" | "error"
    ) {
        return Ok(());
    }
    Err(invalid_input(
        "response_status must be accepted|replay|conflict|rejected|error",
    ))
}

fn validate_archive_path(path: &str) -> Result<(), &'static str> {
    if path.is_empty() || path.len() > MAX_ARCHIVE_PATH_LEN {
        return Err("archive_path must be 1..=1024 chars");
    }
    if path
        .bytes()
        .any(|byte| byte == 0 || byte.is_ascii_control())
    {
        return Err("archive_path has an unsupported character");
    }
    Ok(())
}

fn validate_clip_canonical_key(value: Option<&str>) -> Result<(), &'static str> {
    let Some(value) = value else {
        return Ok(());
    };
    if value.is_empty() || value.len() > MAX_CLIP_CANONICAL_KEY_LEN {
        return Err("clip_canonical_key must be 1..=512 chars when provided");
    }
    if value
        .bytes()
        .any(|byte| byte == 0 || byte.is_ascii_control())
    {
        return Err("clip_canonical_key has an unsupported character");
    }
    Ok(())
}

fn validate_optional_manifest_digest(value: Option<&str>) -> Result<(), &'static str> {
    let Some(value) = value else {
        return Ok(());
    };
    if value.len() != 32 {
        return Err("manifest_digest must be 32 chars");
    }
    if value.bytes().any(|byte| !byte.is_ascii_hexdigit()) {
        return Err("manifest_digest must be lowercase hex");
    }
    if value != value.to_ascii_lowercase() {
        return Err("manifest_digest must be lowercase hex");
    }
    Ok(())
}

fn validate_optional_delete_gen(value: Option<&str>) -> Result<(), &'static str> {
    let Some(value) = value else {
        return Ok(());
    };
    if value.len() != 32 {
        return Err("owned_delete_gen must be 32 chars");
    }
    if value.bytes().any(|byte| !byte.is_ascii_hexdigit()) {
        return Err("owned_delete_gen must be lowercase hex");
    }
    if value != value.to_ascii_lowercase() {
        return Err("owned_delete_gen must be lowercase hex");
    }
    Ok(())
}

fn validate_archive_delete_target(
    archive_item_id: i64,
    archive_path: &str,
    archive_size_bytes: i64,
    archive_file_count: i64,
    clip_canonical_key: Option<&str>,
    manifest_digest: Option<&str>,
) -> Result<(), &'static str> {
    if archive_item_id <= 0 {
        return Err("archive_item_id must be > 0");
    }
    validate_archive_path(archive_path)?;
    if archive_size_bytes < 0 {
        return Err("archive_size_bytes must be >= 0");
    }
    if archive_file_count <= 0 {
        return Err("archive_file_count must be > 0");
    }
    validate_clip_canonical_key(clip_canonical_key)?;
    validate_optional_manifest_digest(manifest_digest)?;
    if clip_canonical_key.is_none() && manifest_digest.is_none() {
        return Err("clip_canonical_key or manifest_digest is required");
    }
    Ok(())
}

fn state_to_str(state: DurableMutationState) -> &'static str {
    match state {
        DurableMutationState::Queued => "queued",
        DurableMutationState::Running => "running",
        DurableMutationState::Done => "done",
        DurableMutationState::Failed => "failed",
        DurableMutationState::Refused => "refused",
        DurableMutationState::Busy => "busy",
        DurableMutationState::CancelRequested => "cancel_requested",
        DurableMutationState::Cancelled => "cancelled",
    }
}

fn state_from_str(raw: &str) -> Result<DurableMutationState, DbError> {
    match raw {
        "queued" => Ok(DurableMutationState::Queued),
        "running" => Ok(DurableMutationState::Running),
        "done" => Ok(DurableMutationState::Done),
        "failed" => Ok(DurableMutationState::Failed),
        "refused" => Ok(DurableMutationState::Refused),
        "busy" => Ok(DurableMutationState::Busy),
        "cancel_requested" => Ok(DurableMutationState::CancelRequested),
        "cancelled" => Ok(DurableMutationState::Cancelled),
        _ => Err(invalid_input("unsupported durable mutation state")),
    }
}

fn validated_request(
    request: &ArchiveDeleteRequestRecord,
) -> Result<ArchiveDeleteRequestRecord, DbError> {
    ArchiveDeleteRequestRecord::new(
        request.request_id.clone(),
        request.idempotency_key.clone(),
        request.request_hash.clone(),
        request.target_archive_item_id,
        request.target_archive_path.clone(),
        request.target_archive_size_bytes,
        request.target_archive_file_count,
        request.target_clip_canonical_key.clone(),
        request.target_manifest_digest.clone(),
    )
    .map_err(map_validation_err)
}

fn same_target_fence(
    existing: &ArchiveDeleteRequestRecord,
    incoming: &ArchiveDeleteRequestRecord,
) -> bool {
    existing.target_archive_item_id == incoming.target_archive_item_id
        && existing.target_archive_path == incoming.target_archive_path
        && existing.target_archive_size_bytes == incoming.target_archive_size_bytes
        && existing.target_archive_file_count == incoming.target_archive_file_count
        && existing.target_clip_canonical_key == incoming.target_clip_canonical_key
        && existing.target_manifest_digest == incoming.target_manifest_digest
}

fn validate_new_row(new_row: &NewArchiveDeleteRequestRow) -> Result<(), DbError> {
    validate_mutation_job_id(&new_row.job_id).map_err(map_validation_err)?;
    validate_owner_or_kind(&new_row.owner, "owner", 32)?;
    validate_owner_or_kind(&new_row.kind, "kind", 64)?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn decode_row(
    job_id: String,
    request_id: String,
    idempotency_key: String,
    request_hash: String,
    owner: String,
    kind: String,
    state_raw: String,
    target_archive_item_id: i64,
    target_archive_path: String,
    target_archive_size_bytes: i64,
    target_archive_file_count: i64,
    target_clip_canonical_key: Option<String>,
    target_manifest_digest: Option<String>,
    owned_delete_gen: Option<String>,
    response_status: Option<String>,
    response_code: Option<i64>,
    sanitized_error: Option<String>,
    created_at: i64,
    updated_at: i64,
    completed_at: Option<i64>,
) -> rusqlite::Result<ArchiveDeleteRequestRow> {
    let state = state_from_str(&state_raw).map_err(|_| invalid_input_sqlite("invalid state"))?;
    validate_response_status(response_status.as_deref())
        .map_err(|_| invalid_input_sqlite("invalid response_status"))?;
    validate_optional_delete_gen(owned_delete_gen.as_deref())
        .map_err(|_| invalid_input_sqlite("invalid owned_delete_gen"))?;
    if let Some(code) = response_code {
        if !(100..=599).contains(&code) {
            return Err(invalid_input_sqlite("invalid response_code"));
        }
    }
    let request = ArchiveDeleteRequestRecord::new(
        request_id,
        idempotency_key,
        request_hash,
        target_archive_item_id,
        target_archive_path,
        target_archive_size_bytes,
        target_archive_file_count,
        target_clip_canonical_key,
        target_manifest_digest,
    )
    .map_err(invalid_input_sqlite)?;
    Ok(ArchiveDeleteRequestRow {
        job_id,
        owner,
        kind,
        state,
        request,
        owned_delete_gen,
        response_status,
        response_code,
        sanitized_error,
        created_at,
        updated_at,
        completed_at,
    })
}

fn row_by_job_id_tx(
    tx: &Transaction<'_>,
    job_id: &str,
) -> Result<Option<ArchiveDeleteRequestRow>, DbError> {
    tx.query_row(
        "SELECT
             job_id,
             request_id,
             idempotency_key,
             request_hash,
             owner,
             kind,
             state,
             target_archive_item_id,
             target_archive_path,
             target_archive_size_bytes,
             target_archive_file_count,
             target_clip_canonical_key,
             target_manifest_digest,
             owned_delete_gen,
             response_status,
             response_code,
             sanitized_error,
             created_at,
             updated_at,
             completed_at
           FROM archive_delete_requests
          WHERE job_id = ?1",
        params![job_id],
        |row| {
            decode_row(
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
                row.get(5)?,
                row.get(6)?,
                row.get(7)?,
                row.get(8)?,
                row.get(9)?,
                row.get(10)?,
                row.get(11)?,
                row.get(12)?,
                row.get(13)?,
                row.get(14)?,
                row.get(15)?,
                row.get(16)?,
                row.get(17)?,
                row.get(18)?,
                row.get(19)?,
            )
        },
    )
    .optional()
    .map_err(Into::into)
}

fn row_by_request_id_tx(
    tx: &Transaction<'_>,
    request_id: &str,
) -> Result<Option<ArchiveDeleteRequestRow>, DbError> {
    tx.query_row(
        "SELECT
             job_id,
             request_id,
             idempotency_key,
             request_hash,
             owner,
             kind,
             state,
             target_archive_item_id,
             target_archive_path,
             target_archive_size_bytes,
             target_archive_file_count,
             target_clip_canonical_key,
             target_manifest_digest,
             owned_delete_gen,
             response_status,
             response_code,
             sanitized_error,
             created_at,
             updated_at,
             completed_at
           FROM archive_delete_requests
          WHERE request_id = ?1",
        params![request_id],
        |row| {
            decode_row(
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
                row.get(5)?,
                row.get(6)?,
                row.get(7)?,
                row.get(8)?,
                row.get(9)?,
                row.get(10)?,
                row.get(11)?,
                row.get(12)?,
                row.get(13)?,
                row.get(14)?,
                row.get(15)?,
                row.get(16)?,
                row.get(17)?,
                row.get(18)?,
                row.get(19)?,
            )
        },
    )
    .optional()
    .map_err(Into::into)
}

fn row_by_scope_and_key_tx(
    tx: &Transaction<'_>,
    owner: &str,
    kind: &str,
    idempotency_key: &str,
) -> Result<Option<ArchiveDeleteRequestRow>, DbError> {
    tx.query_row(
        "SELECT
             job_id,
             request_id,
             idempotency_key,
             request_hash,
             owner,
             kind,
             state,
             target_archive_item_id,
             target_archive_path,
             target_archive_size_bytes,
             target_archive_file_count,
             target_clip_canonical_key,
             target_manifest_digest,
             owned_delete_gen,
             response_status,
             response_code,
             sanitized_error,
             created_at,
             updated_at,
             completed_at
           FROM archive_delete_requests
          WHERE owner = ?1 AND kind = ?2 AND idempotency_key = ?3",
        params![owner, kind, idempotency_key],
        |row| {
            decode_row(
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
                row.get(5)?,
                row.get(6)?,
                row.get(7)?,
                row.get(8)?,
                row.get(9)?,
                row.get(10)?,
                row.get(11)?,
                row.get(12)?,
                row.get(13)?,
                row.get(14)?,
                row.get(15)?,
                row.get(16)?,
                row.get(17)?,
                row.get(18)?,
                row.get(19)?,
            )
        },
    )
    .optional()
    .map_err(Into::into)
}

fn target_matches_live_archive_item_tx(
    tx: &Transaction<'_>,
    request: &ArchiveDeleteRequestRecord,
) -> Result<bool, DbError> {
    let row = tx
        .query_row(
            "SELECT
                 ai.path,
                 ai.size_bytes,
                 ai.file_count,
                 ai.manifest_digest,
                 EXISTS(
                     SELECT 1
                       FROM archive_item_clips aic
                       JOIN clips c ON c.id = aic.clip_id
                      WHERE aic.archive_item_id = ai.id
                        AND c.canonical_key = ?2
                 )
               FROM archive_items ai
              WHERE ai.id = ?1
                AND ai.delete_state = 'LIVE'",
            params![
                request.target_archive_item_id,
                request.target_clip_canonical_key.as_deref()
            ],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, Option<String>>(3)?,
                    row.get::<_, i64>(4)?,
                ))
            },
        )
        .optional()?;
    let Some((path, size_bytes, file_count, manifest_digest, linked_clip_exists)) = row else {
        return Ok(false);
    };
    if path != request.target_archive_path
        || size_bytes != request.target_archive_size_bytes
        || file_count != request.target_archive_file_count
    {
        return Ok(false);
    }
    if request.target_clip_canonical_key.is_some() && linked_clip_exists != 1 {
        return Ok(false);
    }
    if let Some(expected_manifest_digest) = request.target_manifest_digest.as_deref() {
        if manifest_digest.as_deref() != Some(expected_manifest_digest) {
            return Ok(false);
        }
    }
    Ok(true)
}

enum OwnedClaimProjection {
    Requeue,
    Done,
    FailedNeedsReconciliation,
    RefusedStaleOwnership,
}

fn project_owned_claim_state_tx(
    tx: &Transaction<'_>,
    archive_item_id: i64,
    owned_delete_gen: &str,
) -> Result<OwnedClaimProjection, DbError> {
    let row = tx
        .query_row(
            "SELECT delete_state, delete_gen
               FROM archive_items
              WHERE id = ?1",
            params![archive_item_id],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?)),
        )
        .optional()?;
    let Some((delete_state, delete_gen)) = row else {
        return Ok(OwnedClaimProjection::RefusedStaleOwnership);
    };
    if delete_gen.as_deref() != Some(owned_delete_gen) {
        return Ok(OwnedClaimProjection::RefusedStaleOwnership);
    }
    Ok(match delete_state.as_str() {
        "DELETE_CLAIMED" | "DELETING" => OwnedClaimProjection::Requeue,
        "DELETED" => OwnedClaimProjection::Done,
        "DELETE_FAILED" => OwnedClaimProjection::FailedNeedsReconciliation,
        _ => OwnedClaimProjection::RefusedStaleOwnership,
    })
}

/// Restart projection for queued/running archive-delete rows.
///
/// Unowned rows are projected to `queued` when the stale-plan fence still
/// matches the current LIVE archive item, otherwise to deterministic `refused`.
/// Owned rows are projected according to current delete ownership state.
pub fn archive_delete_request_project_restart_tx(tx: &Transaction<'_>) -> Result<i64, DbError> {
    let mut stmt = tx.prepare(
        "SELECT
             job_id,
             request_id,
             idempotency_key,
             request_hash,
             owner,
             kind,
             state,
             target_archive_item_id,
             target_archive_path,
             target_archive_size_bytes,
             target_archive_file_count,
             target_clip_canonical_key,
             target_manifest_digest,
             owned_delete_gen,
             response_status,
             response_code,
             sanitized_error,
             created_at,
             updated_at,
             completed_at
           FROM archive_delete_requests
          WHERE state IN ('queued', 'running')
          ORDER BY created_at ASC, job_id ASC",
    )?;
    let rows = stmt.query_map([], |row| {
        decode_row(
            row.get(0)?,
            row.get(1)?,
            row.get(2)?,
            row.get(3)?,
            row.get(4)?,
            row.get(5)?,
            row.get(6)?,
            row.get(7)?,
            row.get(8)?,
            row.get(9)?,
            row.get(10)?,
            row.get(11)?,
            row.get(12)?,
            row.get(13)?,
            row.get(14)?,
            row.get(15)?,
            row.get(16)?,
            row.get(17)?,
            row.get(18)?,
            row.get(19)?,
        )
    })?;

    let mut projected = 0_i64;
    let now = now_epoch_s();
    for row in rows {
        let row = row?;
        let (next_state, next_status, next_code, next_error) =
            if let Some(owned_delete_gen) = row.owned_delete_gen.as_deref() {
                match project_owned_claim_state_tx(
                    tx,
                    row.request.target_archive_item_id,
                    owned_delete_gen,
                )? {
                    OwnedClaimProjection::Requeue => (
                        DurableMutationState::Queued,
                        Some("accepted".to_owned()),
                        Some(202),
                        Some("queued".to_owned()),
                    ),
                    OwnedClaimProjection::Done => (
                        DurableMutationState::Done,
                        Some("accepted".to_owned()),
                        Some(200),
                        Some("done".to_owned()),
                    ),
                    OwnedClaimProjection::FailedNeedsReconciliation => (
                        DurableMutationState::Failed,
                        Some("error".to_owned()),
                        Some(500),
                        Some(sanitize_public_error(
                            ARCHIVE_DELETE_RECONCILIATION_REQUIRED_ERROR,
                        )),
                    ),
                    OwnedClaimProjection::RefusedStaleOwnership => (
                        DurableMutationState::Refused,
                        Some("rejected".to_owned()),
                        Some(409),
                        Some(sanitize_public_error(ARCHIVE_DELETE_STALE_OWNERSHIP_ERROR)),
                    ),
                }
            } else {
                let is_fresh = target_matches_live_archive_item_tx(tx, &row.request)?;
                if is_fresh {
                    (
                        DurableMutationState::Queued,
                        Some("accepted".to_owned()),
                        Some(202),
                        Some("queued".to_owned()),
                    )
                } else {
                    (
                        DurableMutationState::Refused,
                        Some("rejected".to_owned()),
                        Some(409),
                        Some(sanitize_public_error(ARCHIVE_DELETE_STALE_PLAN_ERROR)),
                    )
                }
            };
        let projected_at = now.max(row.updated_at).max(row.created_at);
        let next_completed_at = if next_state.is_terminal() {
            Some(row.completed_at.unwrap_or(projected_at).max(projected_at))
        } else {
            None
        };
        let changed = row.state != next_state
            || row.response_status.as_deref() != next_status.as_deref()
            || row.response_code != next_code
            || row.sanitized_error.as_deref() != next_error.as_deref()
            || (next_state.is_terminal() && row.completed_at.is_none());
        if !changed {
            continue;
        }
        tx.execute(
            "UPDATE archive_delete_requests
                SET state = ?1,
                    response_status = ?2,
                    response_code = ?3,
                    sanitized_error = ?4,
                    updated_at = ?5,
                    completed_at = ?6
              WHERE job_id = ?7",
            params![
                state_to_str(next_state),
                next_status,
                next_code,
                next_error,
                projected_at,
                next_completed_at,
                row.job_id,
            ],
        )?;
        projected += 1;
    }
    Ok(projected)
}

/// Restart projection for queued/running archive-delete rows.
pub fn archive_delete_request_project_restart(conn: &Connection) -> Result<i64, DbError> {
    let tx = conn.unchecked_transaction()?;
    let projected = archive_delete_request_project_restart_tx(&tx)?;
    tx.commit()?;
    Ok(projected)
}

/// Insert one archive-delete request row inside an existing transaction, or load
/// the existing row for idempotency replay/conflict.
pub fn archive_delete_request_create_or_load_tx(
    tx: &Transaction<'_>,
    new_row: &NewArchiveDeleteRequestRow,
) -> Result<CreateOrLoadArchiveDeleteRequestResult, DbError> {
    validate_new_row(new_row)?;
    let request = validated_request(&new_row.request)?;
    archive_delete_request_project_restart_tx(tx)?;

    if let Some(existing) = row_by_request_id_tx(tx, &request.request_id)? {
        if existing.owner != new_row.owner
            || existing.kind != new_row.kind
            || existing.request.idempotency_key != request.idempotency_key
        {
            return Ok(CreateOrLoadArchiveDeleteRequestResult::Conflict409(
                existing,
            ));
        }
    }
    if let Some(existing) =
        row_by_scope_and_key_tx(tx, &new_row.owner, &new_row.kind, &request.idempotency_key)?
    {
        if !same_target_fence(&existing.request, &request) {
            return Ok(CreateOrLoadArchiveDeleteRequestResult::Conflict409(
                existing,
            ));
        }
        let same_key =
            evaluate_same_key_idempotency(&existing.request.request_hash, &request.request_hash)
                .map_err(map_validation_err)?;
        return Ok(match same_key {
            SameKeyIdempotencyResult::Replay => {
                CreateOrLoadArchiveDeleteRequestResult::Replay(existing)
            }
            SameKeyIdempotencyResult::Conflict409 => {
                CreateOrLoadArchiveDeleteRequestResult::Conflict409(existing)
            }
        });
    }

    let is_fresh = target_matches_live_archive_item_tx(tx, &request)?;
    let state = if is_fresh {
        DurableMutationState::Queued
    } else {
        DurableMutationState::Refused
    };
    let response_status = if is_fresh {
        Some("accepted".to_owned())
    } else {
        Some("rejected".to_owned())
    };
    let response_code = if is_fresh { Some(202) } else { Some(409) };
    let sanitized_error = if is_fresh {
        Some("queued".to_owned())
    } else {
        Some(sanitize_public_error(ARCHIVE_DELETE_STALE_PLAN_ERROR))
    };
    validate_response_status(response_status.as_deref())?;
    let now = now_epoch_s();
    let completed_at = state.is_terminal().then_some(now);

    tx.execute(
        "INSERT INTO archive_delete_requests
            (job_id, request_id, idempotency_key, request_hash,
             owner, kind, state,
             target_archive_item_id, target_archive_path, target_archive_size_bytes,
             target_archive_file_count, target_clip_canonical_key, target_manifest_digest,
             response_status, response_code, sanitized_error,
             created_at, updated_at, completed_at)
         VALUES
            (?1, ?2, ?3, ?4,
             ?5, ?6, ?7,
             ?8, ?9, ?10,
             ?11, ?12, ?13,
             ?14, ?15, ?16,
             ?17, ?17, ?18)",
        params![
            new_row.job_id,
            request.request_id,
            request.idempotency_key,
            request.request_hash,
            new_row.owner,
            new_row.kind,
            state_to_str(state),
            request.target_archive_item_id,
            request.target_archive_path,
            request.target_archive_size_bytes,
            request.target_archive_file_count,
            request.target_clip_canonical_key,
            request.target_manifest_digest,
            response_status,
            response_code,
            sanitized_error,
            now,
            completed_at,
        ],
    )?;
    let inserted = row_by_job_id_tx(tx, &new_row.job_id)?
        .ok_or_else(|| invalid_input("inserted archive delete row not found"))?;
    Ok(CreateOrLoadArchiveDeleteRequestResult::Inserted(inserted))
}

/// Insert one archive-delete request row, or load existing idempotency state.
pub fn archive_delete_request_create_or_load(
    conn: &Connection,
    new_row: &NewArchiveDeleteRequestRow,
) -> Result<CreateOrLoadArchiveDeleteRequestResult, DbError> {
    let tx = conn.unchecked_transaction()?;
    let result = archive_delete_request_create_or_load_tx(&tx, new_row)?;
    tx.commit()?;
    Ok(result)
}

/// Inspect one archive-delete request row by durable job id.
///
/// Applies restart projection before loading.
pub fn archive_delete_request_inspect_by_job_id(
    conn: &Connection,
    job_id: &str,
) -> Result<Option<ArchiveDeleteRequestRow>, DbError> {
    validate_mutation_job_id(job_id).map_err(map_validation_err)?;
    let tx = conn.unchecked_transaction()?;
    archive_delete_request_project_restart_tx(&tx)?;
    let row = row_by_job_id_tx(&tx, job_id)?;
    tx.commit()?;
    Ok(row)
}

/// Load one archive-delete request row by durable job id (no projection).
pub fn archive_delete_request_load_by_job_id(
    conn: &Connection,
    job_id: &str,
) -> Result<Option<ArchiveDeleteRequestRow>, DbError> {
    validate_mutation_job_id(job_id).map_err(map_validation_err)?;
    let tx = conn.unchecked_transaction()?;
    let row = row_by_job_id_tx(&tx, job_id)?;
    tx.commit()?;
    Ok(row)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::expect_used, clippy::unwrap_used)]

    use std::time::{SystemTime, UNIX_EPOCH};

    use rusqlite::params;
    use teslausb_core::durable_mutation::DurableMutationState;

    use super::{
        ARCHIVE_DELETE_RECONCILIATION_REQUIRED_ERROR, ARCHIVE_DELETE_STALE_PLAN_ERROR,
        ArchiveDeleteRequestRecord, CreateOrLoadArchiveDeleteRequestResult,
        NewArchiveDeleteRequestRow, archive_delete_request_create_or_load,
        archive_delete_request_inspect_by_job_id, archive_delete_request_load_by_job_id,
        archive_delete_request_project_restart,
    };
    use crate::db::{open, open_in_memory};

    fn seed_archive_item(
        conn: &rusqlite::Connection,
        path: &str,
        size_bytes: i64,
        file_count: i64,
        manifest_digest: Option<&str>,
    ) -> (i64, String) {
        conn.execute(
            "INSERT INTO archive_items
                (folder_class, path, size_bytes, file_count, archived_at, created_at, updated_at, manifest_digest)
             VALUES ('RecentClips', ?1, ?2, ?3, 100, 0, 0, ?4)",
            params![path, size_bytes, file_count, manifest_digest],
        )
        .expect("insert archive item");
        let archive_item_id = conn.last_insert_rowid();
        let clip_canonical_key = format!("slot0:TeslaCam/{path}");
        conn.execute(
            "INSERT INTO clips
                (canonical_key, started_at, partition, folder_class, created_at, updated_at)
             VALUES (?1, 90, 'slot0', 'RecentClips', 0, 0)",
            params![clip_canonical_key],
        )
        .expect("insert clip");
        let clip_id = conn.last_insert_rowid();
        conn.execute(
            "INSERT INTO archive_item_clips (archive_item_id, clip_id) VALUES (?1, ?2)",
            params![archive_item_id, clip_id],
        )
        .expect("link clip");
        (archive_item_id, clip_canonical_key)
    }

    fn make_request(
        request_id: &str,
        idempotency_key: &str,
        request_hash: &str,
        archive_item_id: i64,
        archive_path: &str,
        archive_size_bytes: i64,
        archive_file_count: i64,
        clip_canonical_key: Option<&str>,
        manifest_digest: Option<&str>,
    ) -> ArchiveDeleteRequestRecord {
        ArchiveDeleteRequestRecord::new(
            request_id.to_owned(),
            idempotency_key.to_owned(),
            request_hash.to_owned(),
            archive_item_id,
            archive_path.to_owned(),
            archive_size_bytes,
            archive_file_count,
            clip_canonical_key.map(str::to_owned),
            manifest_digest.map(str::to_owned),
        )
        .expect("valid request")
    }

    #[test]
    fn create_or_load_persists_and_inspects() {
        let conn = open_in_memory().unwrap();
        let (archive_item_id, clip_key) =
            seed_archive_item(&conn, "archive/recent/a", 4096, 4, None);
        let request = make_request(
            "req-7001",
            "idem-7001",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            archive_item_id,
            "archive/recent/a",
            4096,
            4,
            Some(clip_key.as_str()),
            None,
        );
        let inserted = archive_delete_request_create_or_load(
            &conn,
            &NewArchiveDeleteRequestRow {
                job_id: "m-7001".to_owned(),
                owner: "indexd".to_owned(),
                kind: "archive_delete".to_owned(),
                request,
            },
        )
        .unwrap();
        let CreateOrLoadArchiveDeleteRequestResult::Inserted(row) = inserted else {
            panic!("expected inserted");
        };
        assert_eq!(row.job_id, "m-7001");
        assert_eq!(row.request.request_id, "req-7001");
        assert_eq!(row.state, DurableMutationState::Queued);
        assert_eq!(row.sanitized_error.as_deref(), Some("queued"));
        let inspected = archive_delete_request_inspect_by_job_id(&conn, "m-7001")
            .unwrap()
            .expect("inspect row");
        assert_eq!(inspected.request.request_id, "req-7001");
        assert_eq!(inspected.state, DurableMutationState::Queued);
    }

    #[test]
    fn same_key_same_hash_replays_existing_row() {
        let conn = open_in_memory().unwrap();
        let (archive_item_id, clip_key) =
            seed_archive_item(&conn, "archive/recent/b", 100, 2, None);
        let first = NewArchiveDeleteRequestRow {
            job_id: "m-7101".to_owned(),
            owner: "indexd".to_owned(),
            kind: "archive_delete".to_owned(),
            request: make_request(
                "req-7101",
                "idem-7101",
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                archive_item_id,
                "archive/recent/b",
                100,
                2,
                Some(clip_key.as_str()),
                None,
            ),
        };
        archive_delete_request_create_or_load(&conn, &first).unwrap();

        let replay = NewArchiveDeleteRequestRow {
            job_id: "m-7102".to_owned(),
            owner: "indexd".to_owned(),
            kind: "archive_delete".to_owned(),
            request: make_request(
                "req-7102",
                "idem-7101",
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                archive_item_id,
                "archive/recent/b",
                100,
                2,
                Some(clip_key.as_str()),
                None,
            ),
        };
        let result = archive_delete_request_create_or_load(&conn, &replay).unwrap();
        let CreateOrLoadArchiveDeleteRequestResult::Replay(existing) = result else {
            panic!("expected replay");
        };
        assert_eq!(existing.job_id, "m-7101");
        assert_eq!(existing.request.request_id, "req-7101");
    }

    #[test]
    fn same_key_different_hash_conflicts() {
        let conn = open_in_memory().unwrap();
        let (archive_item_id, clip_key) =
            seed_archive_item(&conn, "archive/recent/c", 200, 3, None);
        archive_delete_request_create_or_load(
            &conn,
            &NewArchiveDeleteRequestRow {
                job_id: "m-7201".to_owned(),
                owner: "indexd".to_owned(),
                kind: "archive_delete".to_owned(),
                request: make_request(
                    "req-7201",
                    "idem-7201",
                    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                    archive_item_id,
                    "archive/recent/c",
                    200,
                    3,
                    Some(clip_key.as_str()),
                    None,
                ),
            },
        )
        .unwrap();

        let result = archive_delete_request_create_or_load(
            &conn,
            &NewArchiveDeleteRequestRow {
                job_id: "m-7202".to_owned(),
                owner: "indexd".to_owned(),
                kind: "archive_delete".to_owned(),
                request: make_request(
                    "req-7202",
                    "idem-7201",
                    "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    archive_item_id,
                    "archive/recent/c",
                    200,
                    3,
                    Some(clip_key.as_str()),
                    None,
                ),
            },
        )
        .unwrap();
        let CreateOrLoadArchiveDeleteRequestResult::Conflict409(existing) = result else {
            panic!("expected conflict");
        };
        assert_eq!(existing.job_id, "m-7201");
    }

    #[test]
    fn same_key_same_hash_different_target_fence_conflicts() {
        let conn = open_in_memory().unwrap();
        let (archive_item_id, clip_key) =
            seed_archive_item(&conn, "archive/recent/c2", 210, 3, None);
        archive_delete_request_create_or_load(
            &conn,
            &NewArchiveDeleteRequestRow {
                job_id: "m-7211".to_owned(),
                owner: "indexd".to_owned(),
                kind: "archive_delete".to_owned(),
                request: make_request(
                    "req-7211",
                    "idem-7211",
                    "abababababababababababababababababababababababababababababababab",
                    archive_item_id,
                    "archive/recent/c2",
                    210,
                    3,
                    Some(clip_key.as_str()),
                    None,
                ),
            },
        )
        .unwrap();

        let result = archive_delete_request_create_or_load(
            &conn,
            &NewArchiveDeleteRequestRow {
                job_id: "m-7212".to_owned(),
                owner: "indexd".to_owned(),
                kind: "archive_delete".to_owned(),
                request: make_request(
                    "req-7212",
                    "idem-7211",
                    "abababababababababababababababababababababababababababababababab",
                    archive_item_id,
                    "archive/recent/c2",
                    211,
                    3,
                    Some(clip_key.as_str()),
                    None,
                ),
            },
        )
        .unwrap();
        let CreateOrLoadArchiveDeleteRequestResult::Conflict409(existing) = result else {
            panic!("expected conflict");
        };
        assert_eq!(existing.job_id, "m-7211");
    }

    #[test]
    fn duplicate_request_id_conflicts_even_with_another_key() {
        let conn = open_in_memory().unwrap();
        let (archive_item_id, clip_key) =
            seed_archive_item(&conn, "archive/recent/d", 300, 5, None);
        archive_delete_request_create_or_load(
            &conn,
            &NewArchiveDeleteRequestRow {
                job_id: "m-7251".to_owned(),
                owner: "indexd".to_owned(),
                kind: "archive_delete".to_owned(),
                request: make_request(
                    "req-7251",
                    "idem-7251",
                    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                    archive_item_id,
                    "archive/recent/d",
                    300,
                    5,
                    Some(clip_key.as_str()),
                    None,
                ),
            },
        )
        .unwrap();

        let result = archive_delete_request_create_or_load(
            &conn,
            &NewArchiveDeleteRequestRow {
                job_id: "m-7252".to_owned(),
                owner: "other-owner".to_owned(),
                kind: "other_kind".to_owned(),
                request: make_request(
                    "req-7251",
                    "idem-7252",
                    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
                    archive_item_id,
                    "archive/recent/d",
                    300,
                    5,
                    Some(clip_key.as_str()),
                    None,
                ),
            },
        )
        .unwrap();
        let CreateOrLoadArchiveDeleteRequestResult::Conflict409(existing) = result else {
            panic!("expected conflict");
        };
        assert_eq!(existing.job_id, "m-7251");
    }

    #[test]
    fn stale_plan_is_refused_deterministically() {
        let conn = open_in_memory().unwrap();
        let (archive_item_id, clip_key) =
            seed_archive_item(&conn, "archive/recent/e", 400, 2, None);
        let result = archive_delete_request_create_or_load(
            &conn,
            &NewArchiveDeleteRequestRow {
                job_id: "m-7301".to_owned(),
                owner: "indexd".to_owned(),
                kind: "archive_delete".to_owned(),
                request: make_request(
                    "req-7301",
                    "idem-7301",
                    "1111111111111111111111111111111111111111111111111111111111111111",
                    archive_item_id,
                    "archive/recent/e",
                    401,
                    2,
                    Some(clip_key.as_str()),
                    None,
                ),
            },
        )
        .unwrap();
        let CreateOrLoadArchiveDeleteRequestResult::Inserted(row) = result else {
            panic!("expected inserted");
        };
        assert_eq!(row.state, DurableMutationState::Refused);
        assert_eq!(row.response_status.as_deref(), Some("rejected"));
        assert_eq!(row.response_code, Some(409));
        assert_eq!(
            row.sanitized_error.as_deref(),
            Some(ARCHIVE_DELETE_STALE_PLAN_ERROR)
        );
        assert!(row.completed_at.is_some());
    }

    #[test]
    fn restart_projection_keeps_owned_inflight_claim_queued() {
        let conn = open_in_memory().unwrap();
        let (archive_item_id, clip_key) =
            seed_archive_item(&conn, "archive/recent/owned-claim", 512, 2, None);
        let owned_delete_gen = "abababababababababababababababab";
        conn.execute(
            "UPDATE archive_items
                SET delete_state = 'DELETING',
                    delete_gen = ?2
              WHERE id = ?1",
            params![archive_item_id, owned_delete_gen],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO archive_delete_requests
                (job_id, request_id, idempotency_key, request_hash, owner, kind, state,
                 target_archive_item_id, target_archive_path, target_archive_size_bytes,
                 target_archive_file_count, target_clip_canonical_key, target_manifest_digest,
                 owned_delete_gen, response_status, response_code, sanitized_error,
                 created_at, updated_at, completed_at)
             VALUES
                ('m-7421', 'req-7421', 'idem-7421',
                 '6666666666666666666666666666666666666666666666666666666666666666',
                 'indexd', 'archive_delete', 'running',
                 ?1, 'archive/recent/owned-claim', 9999, 2, ?2, NULL,
                 ?3, 'accepted', 202, 'queued', 1, 1, NULL)",
            params![archive_item_id, clip_key, owned_delete_gen],
        )
        .unwrap();

        let projected = archive_delete_request_project_restart(&conn).unwrap();
        assert_eq!(projected, 1);
        let row = archive_delete_request_load_by_job_id(&conn, "m-7421")
            .unwrap()
            .expect("row");
        assert_eq!(row.state, DurableMutationState::Queued);
        assert_eq!(row.response_status.as_deref(), Some("accepted"));
        assert_eq!(row.response_code, Some(202));
        assert_eq!(row.sanitized_error.as_deref(), Some("queued"));
        assert_eq!(row.owned_delete_gen.as_deref(), Some(owned_delete_gen));
        assert_eq!(row.completed_at, None);
    }

    #[test]
    fn restart_projection_marks_owned_deleted_claim_done() {
        let conn = open_in_memory().unwrap();
        let (archive_item_id, clip_key) =
            seed_archive_item(&conn, "archive/recent/owned-deleted", 513, 2, None);
        let owned_delete_gen = "babababababababababababababababa";
        conn.execute(
            "UPDATE archive_items
                SET delete_state = 'DELETED',
                    delete_gen = ?2
              WHERE id = ?1",
            params![archive_item_id, owned_delete_gen],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO archive_delete_requests
                (job_id, request_id, idempotency_key, request_hash, owner, kind, state,
                 target_archive_item_id, target_archive_path, target_archive_size_bytes,
                 target_archive_file_count, target_clip_canonical_key, target_manifest_digest,
                 owned_delete_gen, response_status, response_code, sanitized_error,
                 created_at, updated_at, completed_at)
             VALUES
                ('m-7423', 'req-7423', 'idem-7423',
                 '8888888888888888888888888888888888888888888888888888888888888888',
                 'indexd', 'archive_delete', 'running',
                 ?1, 'archive/recent/owned-deleted', 513, 2, ?2, NULL,
                 ?3, 'accepted', 202, 'queued', 1, 1, NULL)",
            params![archive_item_id, clip_key, owned_delete_gen],
        )
        .unwrap();

        let projected = archive_delete_request_project_restart(&conn).unwrap();
        assert_eq!(projected, 1);
        let row = archive_delete_request_load_by_job_id(&conn, "m-7423")
            .unwrap()
            .expect("row");
        assert_eq!(row.state, DurableMutationState::Done);
        assert_eq!(row.response_status.as_deref(), Some("accepted"));
        assert_eq!(row.response_code, Some(200));
        assert_eq!(row.sanitized_error.as_deref(), Some("done"));
        assert_eq!(row.owned_delete_gen.as_deref(), Some(owned_delete_gen));
        assert!(row.completed_at.is_some());
    }

    #[test]
    fn restart_projection_marks_owned_delete_failed_claim_failed() {
        let conn = open_in_memory().unwrap();
        let (archive_item_id, clip_key) =
            seed_archive_item(&conn, "archive/recent/owned-failed", 514, 2, None);
        let owned_delete_gen = "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd";
        conn.execute(
            "UPDATE archive_items
                SET delete_state = 'DELETE_FAILED',
                    delete_gen = ?2
              WHERE id = ?1",
            params![archive_item_id, owned_delete_gen],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO archive_delete_requests
                (job_id, request_id, idempotency_key, request_hash, owner, kind, state,
                 target_archive_item_id, target_archive_path, target_archive_size_bytes,
                 target_archive_file_count, target_clip_canonical_key, target_manifest_digest,
                 owned_delete_gen, response_status, response_code, sanitized_error,
                 created_at, updated_at, completed_at)
             VALUES
                ('m-7424', 'req-7424', 'idem-7424',
                 '9999999999999999999999999999999999999999999999999999999999999999',
                 'indexd', 'archive_delete', 'running',
                 ?1, 'archive/recent/owned-failed', 514, 2, ?2, NULL,
                 ?3, 'accepted', 202, 'queued', 1, 1, NULL)",
            params![archive_item_id, clip_key, owned_delete_gen],
        )
        .unwrap();

        let projected = archive_delete_request_project_restart(&conn).unwrap();
        assert_eq!(projected, 1);
        let row = archive_delete_request_load_by_job_id(&conn, "m-7424")
            .unwrap()
            .expect("row");
        assert_eq!(row.state, DurableMutationState::Failed);
        assert_eq!(row.response_status.as_deref(), Some("error"));
        assert_eq!(row.response_code, Some(500));
        assert_eq!(
            row.sanitized_error.as_deref(),
            Some(ARCHIVE_DELETE_RECONCILIATION_REQUIRED_ERROR)
        );
        assert_eq!(row.owned_delete_gen.as_deref(), Some(owned_delete_gen));
        assert!(row.completed_at.is_some());
    }

    #[test]
    fn restart_projection_refuses_stale_live_fence_without_owned_claim() {
        let conn = open_in_memory().unwrap();
        let (archive_item_id, clip_key) =
            seed_archive_item(&conn, "archive/recent/stale-live", 1024, 3, None);
        conn.execute(
            "INSERT INTO archive_delete_requests
                (job_id, request_id, idempotency_key, request_hash, owner, kind, state,
                 target_archive_item_id, target_archive_path, target_archive_size_bytes,
                 target_archive_file_count, target_clip_canonical_key, target_manifest_digest,
                 response_status, response_code, sanitized_error, created_at, updated_at, completed_at)
             VALUES
                ('m-7422', 'req-7422', 'idem-7422',
                 '7777777777777777777777777777777777777777777777777777777777777777',
                 'indexd', 'archive_delete', 'running',
                 ?1, 'archive/recent/stale-live', 2048, 3, ?2, NULL,
                 'accepted', 202, 'queued', 1, 1, NULL)",
            params![archive_item_id, clip_key],
        )
        .unwrap();

        let projected = archive_delete_request_project_restart(&conn).unwrap();
        assert_eq!(projected, 1);
        let row = archive_delete_request_load_by_job_id(&conn, "m-7422")
            .unwrap()
            .expect("row");
        assert_eq!(row.state, DurableMutationState::Refused);
        assert_eq!(row.response_status.as_deref(), Some("rejected"));
        assert_eq!(row.response_code, Some(409));
        assert_eq!(
            row.sanitized_error.as_deref(),
            Some(ARCHIVE_DELETE_STALE_PLAN_ERROR)
        );
        assert!(row.completed_at.is_some());
    }

    #[test]
    fn restart_projection_requeues_running_or_refuses_stale_without_delete_transition() {
        let conn = open_in_memory().unwrap();
        let (fresh_item, fresh_clip) =
            seed_archive_item(&conn, "archive/recent/fresh", 500, 2, None);
        let (stale_item, stale_clip) =
            seed_archive_item(&conn, "archive/recent/stale", 600, 3, None);
        conn.execute(
            "INSERT INTO archive_delete_requests
                (job_id, request_id, idempotency_key, request_hash, owner, kind, state,
                 target_archive_item_id, target_archive_path, target_archive_size_bytes,
                 target_archive_file_count, target_clip_canonical_key, target_manifest_digest,
                 response_status, response_code, sanitized_error, created_at, updated_at, completed_at)
             VALUES
                ('m-7401', 'req-7401', 'idem-7401',
                 '2222222222222222222222222222222222222222222222222222222222222222',
                 'indexd', 'archive_delete', 'running',
                 ?1, 'archive/recent/fresh', 500, 2, ?2, NULL,
                 'accepted', 202, 'queued', 1, 1, NULL)",
            params![fresh_item, fresh_clip],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO archive_delete_requests
                (job_id, request_id, idempotency_key, request_hash, owner, kind, state,
                 target_archive_item_id, target_archive_path, target_archive_size_bytes,
                 target_archive_file_count, target_clip_canonical_key, target_manifest_digest,
                 response_status, response_code, sanitized_error, created_at, updated_at, completed_at)
             VALUES
                ('m-7402', 'req-7402', 'idem-7402',
                 '3333333333333333333333333333333333333333333333333333333333333333',
                 'indexd', 'archive_delete', 'running',
                 ?1, 'archive/recent/stale', 601, 3, ?2, NULL,
                 'accepted', 202, 'queued', 1, 1, NULL)",
            params![stale_item, stale_clip],
        )
        .unwrap();

        let projected = archive_delete_request_project_restart(&conn).unwrap();
        assert_eq!(projected, 2);

        let fresh = archive_delete_request_load_by_job_id(&conn, "m-7401")
            .unwrap()
            .expect("fresh row");
        assert_eq!(fresh.state, DurableMutationState::Queued);
        assert_eq!(fresh.response_status.as_deref(), Some("accepted"));
        assert_eq!(fresh.completed_at, None);

        let stale = archive_delete_request_load_by_job_id(&conn, "m-7402")
            .unwrap()
            .expect("stale row");
        assert_eq!(stale.state, DurableMutationState::Refused);
        assert_eq!(stale.response_status.as_deref(), Some("rejected"));
        assert_eq!(stale.response_code, Some(409));
        assert_eq!(
            stale.sanitized_error.as_deref(),
            Some(ARCHIVE_DELETE_STALE_PLAN_ERROR)
        );
        assert!(stale.completed_at.is_some());

        let delete_states: (String, String) = conn
            .query_row(
                "SELECT
                    (SELECT delete_state FROM archive_items WHERE id = ?1),
                    (SELECT delete_state FROM archive_items WHERE id = ?2)",
                params![fresh_item, stale_item],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(delete_states.0, "LIVE");
        assert_eq!(delete_states.1, "LIVE");
    }

    #[test]
    fn restart_projection_clamps_timestamps_when_clock_regresses() {
        let conn = open_in_memory().unwrap();
        let (archive_item_id, clip_key) =
            seed_archive_item(&conn, "archive/recent/future-ts", 640, 2, None);
        conn.execute(
            "INSERT INTO archive_delete_requests
                (job_id, request_id, idempotency_key, request_hash, owner, kind, state,
                 target_archive_item_id, target_archive_path, target_archive_size_bytes,
                 target_archive_file_count, target_clip_canonical_key, target_manifest_digest,
                 response_status, response_code, sanitized_error, created_at, updated_at, completed_at)
             VALUES
                ('m-7451', 'req-7451', 'idem-7451',
                 '5555555555555555555555555555555555555555555555555555555555555555',
                 'indexd', 'archive_delete', 'running',
                 ?1, 'archive/recent/future-ts', 641, 2, ?2, NULL,
                 'accepted', 202, 'queued', 5000000000, 5000000000, NULL)",
            params![archive_item_id, clip_key],
        )
        .unwrap();

        let projected = archive_delete_request_project_restart(&conn).unwrap();
        assert_eq!(projected, 1);

        let row = archive_delete_request_load_by_job_id(&conn, "m-7451")
            .unwrap()
            .expect("row");
        assert_eq!(row.state, DurableMutationState::Refused);
        assert_eq!(row.updated_at, 5_000_000_000);
        assert_eq!(row.completed_at, Some(5_000_000_000));
    }

    #[test]
    fn terminal_state_survives_reopen_restart() {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let db_path = std::env::temp_dir().join(format!(
            "indexd-archive-delete-requests-{nanos}-{}.sqlite3",
            std::process::id()
        ));

        {
            let conn = open(&db_path).unwrap();
            let (archive_item_id, clip_key) =
                seed_archive_item(&conn, "archive/recent/reopen", 700, 2, None);
            archive_delete_request_create_or_load(
                &conn,
                &NewArchiveDeleteRequestRow {
                    job_id: "m-7501".to_owned(),
                    owner: "indexd".to_owned(),
                    kind: "archive_delete".to_owned(),
                    request: make_request(
                        "req-7501",
                        "idem-7501",
                        "4444444444444444444444444444444444444444444444444444444444444444",
                        archive_item_id,
                        "archive/recent/reopen",
                        700,
                        2,
                        Some(clip_key.as_str()),
                        None,
                    ),
                },
            )
            .unwrap();
            conn.execute(
                "UPDATE archive_delete_requests
                    SET state='done', response_status='accepted', response_code=200,
                        sanitized_error='done', updated_at=created_at, completed_at=created_at
                  WHERE job_id='m-7501'",
                [],
            )
            .unwrap();
        }

        let reopened = open(&db_path).unwrap();
        let row = archive_delete_request_load_by_job_id(&reopened, "m-7501")
            .unwrap()
            .expect("row");
        assert_eq!(row.state, DurableMutationState::Done);
        assert_eq!(row.completed_at, Some(row.created_at));

        drop(reopened);
        let _ = std::fs::remove_file(db_path);
    }
}
