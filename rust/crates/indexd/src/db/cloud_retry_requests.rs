//! Durable persistence primitives for failed-upload retry request idempotency.
//!
//! This is storage scaffolding only for the future retry mutation RPC/HTTP lane.

use rusqlite::{Connection, OptionalExtension, Transaction, params};
use teslausb_core::durable_mutation::{
    CloudQueueRetryTarget, DurableMutationState, FailedUploadRetryRequestRecord,
    SameKeyIdempotencyResult, evaluate_failed_upload_retry_idempotency, sanitize_public_error,
    validate_mutation_job_id, validate_request_id,
};

use crate::db::{DbError, now_epoch_s};

/// Stored failed-upload retry request row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FailedUploadRetryRequestRow {
    /// Stable durable job id.
    pub job_id: String,
    /// Owning subsystem.
    pub owner: String,
    /// Mutation kind discriminator.
    pub kind: String,
    /// Lifecycle state.
    pub state: DurableMutationState,
    /// Request identity + target.
    pub request: FailedUploadRetryRequestRecord,
    /// Optional sanitized response status discriminator.
    pub response_status: Option<String>,
    /// Optional response status code (HTTP-ish).
    pub response_code: Option<i64>,
    /// Optional sanitized response detail.
    pub sanitized_response: Option<String>,
    /// Row create timestamp.
    pub created_at: i64,
    /// Last update timestamp.
    pub updated_at: i64,
    /// Terminal completion timestamp, when known.
    pub completed_at: Option<i64>,
}

/// New failed-upload retry request insert payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NewFailedUploadRetryRequestRow {
    /// Stable durable job id.
    pub job_id: String,
    /// Owning subsystem.
    pub owner: String,
    /// Mutation kind discriminator.
    pub kind: String,
    /// Initial lifecycle state.
    pub state: DurableMutationState,
    /// Request identity + target.
    pub request: FailedUploadRetryRequestRecord,
    /// Optional sanitized response status discriminator.
    pub response_status: Option<String>,
    /// Optional response status code (HTTP-ish).
    pub response_code: Option<i64>,
    /// Optional response detail before sanitization.
    pub sanitized_response: Option<String>,
}

/// Insert-or-load idempotency outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InsertFailedUploadRetryRequestResult {
    /// First insert won.
    Inserted(FailedUploadRetryRequestRow),
    /// Same key + same hash+target replay.
    Replay(FailedUploadRetryRequestRow),
    /// Same key + different hash or target conflict.
    Conflict409(FailedUploadRetryRequestRow),
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
    request: &FailedUploadRetryRequestRecord,
) -> Result<FailedUploadRetryRequestRecord, DbError> {
    FailedUploadRetryRequestRecord::new(
        request.request_id.clone(),
        request.idempotency_key.clone(),
        request.request_hash.clone(),
        request.target.clone(),
    )
    .map_err(map_validation_err)
}

fn validate_new_row(new_row: &NewFailedUploadRetryRequestRow) -> Result<(), DbError> {
    validate_mutation_job_id(&new_row.job_id).map_err(map_validation_err)?;
    validate_owner_or_kind(&new_row.owner, "owner", 32)?;
    validate_owner_or_kind(&new_row.kind, "kind", 64)?;
    validate_response_status(new_row.response_status.as_deref())?;
    if let Some(code) = new_row.response_code {
        if !(100..=599).contains(&code) {
            return Err(invalid_input("response_code must be in 100..=599"));
        }
    }
    Ok(())
}

fn decode_retry_request_row(
    job_id: String,
    request_id: String,
    idempotency_key: String,
    request_hash: String,
    owner: String,
    kind: String,
    state_raw: String,
    target_archive_item_id: i64,
    target_child_key: String,
    target_upload_set_id: Option<String>,
    response_status: Option<String>,
    response_code: Option<i64>,
    sanitized_response: Option<String>,
    created_at: i64,
    updated_at: i64,
    completed_at: Option<i64>,
) -> rusqlite::Result<FailedUploadRetryRequestRow> {
    let state = state_from_str(&state_raw).map_err(|_| invalid_input_sqlite("invalid state"))?;
    validate_response_status(response_status.as_deref())
        .map_err(|_| invalid_input_sqlite("invalid response_status"))?;
    if let Some(code) = response_code {
        if !(100..=599).contains(&code) {
            return Err(invalid_input_sqlite("invalid response_code"));
        }
    }
    let target = CloudQueueRetryTarget::new(
        target_archive_item_id,
        target_child_key,
        target_upload_set_id,
    )
    .map_err(invalid_input_sqlite)?;
    let request =
        FailedUploadRetryRequestRecord::new(request_id, idempotency_key, request_hash, target)
            .map_err(invalid_input_sqlite)?;
    Ok(FailedUploadRetryRequestRow {
        job_id,
        owner,
        kind,
        state,
        request,
        response_status,
        response_code,
        sanitized_response,
        created_at,
        updated_at,
        completed_at,
    })
}

fn row_by_job_id_tx(
    tx: &Transaction<'_>,
    job_id: &str,
) -> Result<Option<FailedUploadRetryRequestRow>, DbError> {
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
             target_child_key,
             target_upload_set_id,
             response_status,
             response_code,
             sanitized_response,
             created_at,
             updated_at,
             completed_at
           FROM cloud_failed_upload_retry_requests
          WHERE job_id = ?1",
        params![job_id],
        |row| {
            decode_retry_request_row(
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
            )
        },
    )
    .optional()
    .map_err(Into::into)
}

fn row_by_scope_and_idempotency_key_tx(
    tx: &Transaction<'_>,
    owner: &str,
    kind: &str,
    idempotency_key: &str,
) -> Result<Option<FailedUploadRetryRequestRow>, DbError> {
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
             target_child_key,
             target_upload_set_id,
             response_status,
             response_code,
             sanitized_response,
             created_at,
             updated_at,
             completed_at
           FROM cloud_failed_upload_retry_requests
          WHERE owner = ?1 AND kind = ?2 AND idempotency_key = ?3",
        params![owner, kind, idempotency_key],
        |row| {
            decode_retry_request_row(
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
            )
        },
    )
    .optional()
    .map_err(Into::into)
}

fn row_by_request_id(
    conn: &Connection,
    request_id: &str,
) -> Result<Option<FailedUploadRetryRequestRow>, DbError> {
    conn.query_row(
        "SELECT
             job_id,
             request_id,
             idempotency_key,
             request_hash,
             owner,
             kind,
             state,
             target_archive_item_id,
             target_child_key,
             target_upload_set_id,
             response_status,
             response_code,
             sanitized_response,
             created_at,
             updated_at,
             completed_at
           FROM cloud_failed_upload_retry_requests
          WHERE request_id = ?1",
        params![request_id],
        |row| {
            decode_retry_request_row(
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
            )
        },
    )
    .optional()
    .map_err(Into::into)
}

fn row_by_request_id_tx(
    tx: &Transaction<'_>,
    request_id: &str,
) -> Result<Option<FailedUploadRetryRequestRow>, DbError> {
    tx.query_row(
        "SELECT
             job_id, request_id, idempotency_key, request_hash, owner, kind, state,
             target_archive_item_id, target_child_key, target_upload_set_id,
             response_status, response_code, sanitized_response,
             created_at, updated_at, completed_at
           FROM cloud_failed_upload_retry_requests
          WHERE request_id = ?1",
        params![request_id],
        |row| {
            decode_retry_request_row(
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
            )
        },
    )
    .optional()
    .map_err(Into::into)
}

/// Insert one failed-upload retry request row inside an existing transaction, or
/// load the existing row with the same `(owner, kind, idempotency_key)` scope.
pub fn cloud_failed_upload_retry_request_insert_or_load_tx(
    tx: &Transaction<'_>,
    new_row: &NewFailedUploadRetryRequestRow,
) -> Result<InsertFailedUploadRetryRequestResult, DbError> {
    validate_new_row(new_row)?;
    let request = validated_request(&new_row.request)?;
    let now = now_epoch_s();
    let response_status = new_row.response_status.clone();
    let response_code = new_row.response_code;
    let sanitized_response = new_row
        .sanitized_response
        .as_deref()
        .map(sanitize_public_error);
    let completed_at = new_row.state.is_terminal().then_some(now);

    if let Some(existing) = row_by_request_id_tx(tx, &request.request_id)? {
        if existing.owner != new_row.owner
            || existing.kind != new_row.kind
            || existing.request.idempotency_key != request.idempotency_key
        {
            return Ok(InsertFailedUploadRetryRequestResult::Conflict409(existing));
        }
    }
    if let Some(existing) = row_by_scope_and_idempotency_key_tx(
        tx,
        &new_row.owner,
        &new_row.kind,
        &request.idempotency_key,
    )? {
        let replay = evaluate_failed_upload_retry_idempotency(
            &existing.request,
            &request.request_hash,
            &request.target,
        )
        .map_err(map_validation_err)?;
        return Ok(match replay {
            SameKeyIdempotencyResult::Replay => {
                InsertFailedUploadRetryRequestResult::Replay(existing)
            }
            SameKeyIdempotencyResult::Conflict409 => {
                InsertFailedUploadRetryRequestResult::Conflict409(existing)
            }
        });
    }
    tx.execute(
        "INSERT INTO cloud_failed_upload_retry_requests
            (job_id, request_id, idempotency_key, request_hash,
             owner, kind, state,
             target_archive_item_id, target_child_key, target_upload_set_id,
             response_status, response_code, sanitized_response,
             created_at, updated_at, completed_at)
         VALUES
            (?1, ?2, ?3, ?4,
             ?5, ?6, ?7,
             ?8, ?9, ?10,
             ?11, ?12, ?13,
             ?14, ?14, ?15)",
        params![
            new_row.job_id,
            request.request_id,
            request.idempotency_key,
            request.request_hash,
            new_row.owner,
            new_row.kind,
            state_to_str(new_row.state),
            request.target.archive_item_id,
            request.target.child_key,
            request.target.upload_set_id,
            response_status,
            response_code,
            sanitized_response,
            now,
            completed_at,
        ],
    )?;
    let inserted = row_by_job_id_tx(tx, &new_row.job_id)?
        .ok_or_else(|| invalid_input("inserted retry request row not found"))?;
    Ok(InsertFailedUploadRetryRequestResult::Inserted(inserted))
}

/// Insert one failed-upload retry request row, or load the existing row with the
/// same `(owner, kind, idempotency_key)` scope.
pub fn cloud_failed_upload_retry_request_insert_or_load(
    conn: &Connection,
    new_row: &NewFailedUploadRetryRequestRow,
) -> Result<InsertFailedUploadRetryRequestResult, DbError> {
    let tx = conn.unchecked_transaction()?;
    let result = cloud_failed_upload_retry_request_insert_or_load_tx(&tx, new_row)?;
    tx.commit()?;
    Ok(result)
}

/// Load one failed-upload retry request row by durable job id.
pub fn cloud_failed_upload_retry_request_load_by_job_id(
    conn: &Connection,
    job_id: &str,
) -> Result<Option<FailedUploadRetryRequestRow>, DbError> {
    validate_mutation_job_id(job_id).map_err(map_validation_err)?;
    conn.query_row(
        "SELECT
             job_id,
             request_id,
             idempotency_key,
             request_hash,
             owner,
             kind,
             state,
             target_archive_item_id,
             target_child_key,
             target_upload_set_id,
             response_status,
             response_code,
             sanitized_response,
             created_at,
             updated_at,
             completed_at
           FROM cloud_failed_upload_retry_requests
          WHERE job_id = ?1",
        params![job_id],
        |row| {
            decode_retry_request_row(
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
            )
        },
    )
    .optional()
    .map_err(Into::into)
}

/// Load one failed-upload retry request row by logical request id.
pub fn cloud_failed_upload_retry_request_load_by_request_id(
    conn: &Connection,
    request_id: &str,
) -> Result<Option<FailedUploadRetryRequestRow>, DbError> {
    validate_request_id(request_id).map_err(map_validation_err)?;
    row_by_request_id(conn, request_id)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::expect_used, clippy::unwrap_used)]

    use super::{
        InsertFailedUploadRetryRequestResult, NewFailedUploadRetryRequestRow,
        cloud_failed_upload_retry_request_insert_or_load,
        cloud_failed_upload_retry_request_load_by_job_id,
        cloud_failed_upload_retry_request_load_by_request_id,
    };
    use crate::db::open_in_memory;
    use teslausb_core::durable_mutation::{
        CloudQueueRetryTarget, DurableMutationState, FailedUploadRetryRequestRecord,
    };

    fn base_request(
        request_id: &str,
        idempotency_key: &str,
        request_hash: &str,
        upload_set_id: Option<&str>,
    ) -> FailedUploadRetryRequestRecord {
        let target = CloudQueueRetryTarget::new(
            42,
            "cam/front-2026-08-11_12-34-56.mp4".to_owned(),
            upload_set_id.map(str::to_owned),
        )
        .expect("valid target");
        FailedUploadRetryRequestRecord::new(
            request_id.to_owned(),
            idempotency_key.to_owned(),
            request_hash.to_owned(),
            target,
        )
        .expect("valid record")
    }

    #[test]
    fn first_insert_persists_and_loads_by_job_and_request_id() {
        let conn = open_in_memory().unwrap();
        let new_row = NewFailedUploadRetryRequestRow {
            job_id: "m-7001".to_owned(),
            owner: "indexd".to_owned(),
            kind: "cloud_failed_upload_retry".to_owned(),
            state: DurableMutationState::Queued,
            request: base_request(
                "req-7001",
                "idem-7001",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                None,
            ),
            response_status: Some("accepted".to_owned()),
            response_code: Some(202),
            sanitized_response: Some("  queued\tfor\r\nretry  ".to_owned()),
        };

        let inserted = cloud_failed_upload_retry_request_insert_or_load(&conn, &new_row).unwrap();
        let InsertFailedUploadRetryRequestResult::Inserted(row) = inserted else {
            panic!("expected inserted");
        };
        assert_eq!(row.job_id, "m-7001");
        assert_eq!(row.request.request_id, "req-7001");
        assert_eq!(row.request.idempotency_key, "idem-7001");
        assert_eq!(row.sanitized_response.as_deref(), Some("queued for retry"));
        assert!(row.created_at >= 0);
        assert_eq!(row.created_at, row.updated_at);

        let by_job = cloud_failed_upload_retry_request_load_by_job_id(&conn, "m-7001")
            .unwrap()
            .expect("job row");
        assert_eq!(by_job.request.request_id, "req-7001");

        let by_request = cloud_failed_upload_retry_request_load_by_request_id(&conn, "req-7001")
            .unwrap()
            .expect("request row");
        assert_eq!(by_request.job_id, "m-7001");
    }

    #[test]
    fn same_key_same_hash_replay_returns_existing_row() {
        let conn = open_in_memory().unwrap();
        let first = NewFailedUploadRetryRequestRow {
            job_id: "m-7101".to_owned(),
            owner: "indexd".to_owned(),
            kind: "cloud_failed_upload_retry".to_owned(),
            state: DurableMutationState::Queued,
            request: base_request(
                "req-7101",
                "idem-7101",
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                Some("11111111111111111111111111111111"),
            ),
            response_status: Some("accepted".to_owned()),
            response_code: Some(202),
            sanitized_response: None,
        };
        cloud_failed_upload_retry_request_insert_or_load(&conn, &first).unwrap();

        let replay = NewFailedUploadRetryRequestRow {
            job_id: "m-7102".to_owned(),
            owner: "indexd".to_owned(),
            kind: "cloud_failed_upload_retry".to_owned(),
            state: DurableMutationState::Queued,
            request: base_request(
                "req-7102",
                "idem-7101",
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                Some("11111111111111111111111111111111"),
            ),
            response_status: Some("accepted".to_owned()),
            response_code: Some(202),
            sanitized_response: None,
        };

        let result = cloud_failed_upload_retry_request_insert_or_load(&conn, &replay).unwrap();
        let InsertFailedUploadRetryRequestResult::Replay(existing) = result else {
            panic!("expected replay");
        };
        assert_eq!(existing.job_id, "m-7101");
        assert_eq!(existing.request.request_id, "req-7101");
        let rows: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM cloud_failed_upload_retry_requests",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(rows, 1);
    }

    #[test]
    fn same_key_different_hash_is_conflict() {
        let conn = open_in_memory().unwrap();
        let first = NewFailedUploadRetryRequestRow {
            job_id: "m-7201".to_owned(),
            owner: "indexd".to_owned(),
            kind: "cloud_failed_upload_retry".to_owned(),
            state: DurableMutationState::Queued,
            request: base_request(
                "req-7201",
                "idem-7201",
                "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                None,
            ),
            response_status: Some("accepted".to_owned()),
            response_code: Some(202),
            sanitized_response: None,
        };
        cloud_failed_upload_retry_request_insert_or_load(&conn, &first).unwrap();

        let conflict = NewFailedUploadRetryRequestRow {
            job_id: "m-7202".to_owned(),
            owner: "indexd".to_owned(),
            kind: "cloud_failed_upload_retry".to_owned(),
            state: DurableMutationState::Queued,
            request: base_request(
                "req-7202",
                "idem-7201",
                "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                None,
            ),
            response_status: Some("accepted".to_owned()),
            response_code: Some(202),
            sanitized_response: None,
        };

        let result = cloud_failed_upload_retry_request_insert_or_load(&conn, &conflict).unwrap();
        let InsertFailedUploadRetryRequestResult::Conflict409(existing) = result else {
            panic!("expected conflict");
        };
        assert_eq!(existing.job_id, "m-7201");
        let rows: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM cloud_failed_upload_retry_requests",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(rows, 1);
    }

    #[test]
    fn duplicate_request_id_is_conflict_even_with_new_idempotency_key() {
        let conn = open_in_memory().unwrap();
        let first = NewFailedUploadRetryRequestRow {
            job_id: "m-7251".to_owned(),
            owner: "indexd".to_owned(),
            kind: "cloud_failed_upload_retry".to_owned(),
            state: DurableMutationState::Queued,
            request: base_request(
                "req-7251",
                "idem-7251",
                "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                None,
            ),
            response_status: Some("accepted".to_owned()),
            response_code: Some(202),
            sanitized_response: None,
        };
        cloud_failed_upload_retry_request_insert_or_load(&conn, &first).unwrap();

        let duplicate_request = NewFailedUploadRetryRequestRow {
            job_id: "m-7252".to_owned(),
            owner: "other-owner".to_owned(),
            kind: "other_kind".to_owned(),
            state: DurableMutationState::Queued,
            request: base_request(
                "req-7251",
                "idem-7252",
                "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                None,
            ),
            response_status: Some("accepted".to_owned()),
            response_code: Some(202),
            sanitized_response: None,
        };
        let result =
            cloud_failed_upload_retry_request_insert_or_load(&conn, &duplicate_request).unwrap();
        let InsertFailedUploadRetryRequestResult::Conflict409(existing) = result else {
            panic!("expected request-id conflict");
        };
        assert_eq!(existing.job_id, "m-7251");
    }

    #[test]
    fn request_id_conflict_is_not_hidden_by_idempotency_replay() {
        let conn = open_in_memory().unwrap();
        let first = NewFailedUploadRetryRequestRow {
            job_id: "m-7261".to_owned(),
            owner: "indexd".to_owned(),
            kind: "cloud_failed_upload_retry".to_owned(),
            state: DurableMutationState::Queued,
            request: base_request(
                "req-7261",
                "idem-7261",
                "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                None,
            ),
            response_status: Some("accepted".to_owned()),
            response_code: Some(202),
            sanitized_response: None,
        };
        cloud_failed_upload_retry_request_insert_or_load(&conn, &first).unwrap();

        let second = NewFailedUploadRetryRequestRow {
            job_id: "m-7262".to_owned(),
            owner: "indexd".to_owned(),
            kind: "cloud_failed_upload_retry".to_owned(),
            state: DurableMutationState::Queued,
            request: base_request(
                "req-7262",
                "idem-7262",
                "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                None,
            ),
            response_status: Some("accepted".to_owned()),
            response_code: Some(202),
            sanitized_response: None,
        };
        cloud_failed_upload_retry_request_insert_or_load(&conn, &second).unwrap();

        let reused_request_id = NewFailedUploadRetryRequestRow {
            job_id: "m-7263".to_owned(),
            owner: "indexd".to_owned(),
            kind: "cloud_failed_upload_retry".to_owned(),
            state: DurableMutationState::Queued,
            request: base_request(
                "req-7262",
                "idem-7261",
                "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                None,
            ),
            response_status: Some("accepted".to_owned()),
            response_code: Some(202),
            sanitized_response: None,
        };
        let result =
            cloud_failed_upload_retry_request_insert_or_load(&conn, &reused_request_id).unwrap();
        let InsertFailedUploadRetryRequestResult::Conflict409(existing) = result else {
            panic!("expected request-id conflict");
        };
        assert_eq!(existing.job_id, "m-7262");
    }

    #[test]
    fn persists_target_fence_identity() {
        let conn = open_in_memory().unwrap();
        let upload_set_id = "abcdefabcdefabcdefabcdefabcdefab";
        let new_row = NewFailedUploadRetryRequestRow {
            job_id: "m-7301".to_owned(),
            owner: "indexd".to_owned(),
            kind: "cloud_failed_upload_retry".to_owned(),
            state: DurableMutationState::Queued,
            request: base_request(
                "req-7301",
                "idem-7301",
                "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                Some(upload_set_id),
            ),
            response_status: Some("accepted".to_owned()),
            response_code: Some(202),
            sanitized_response: None,
        };
        cloud_failed_upload_retry_request_insert_or_load(&conn, &new_row).unwrap();
        let loaded = cloud_failed_upload_retry_request_load_by_job_id(&conn, "m-7301")
            .unwrap()
            .expect("row");
        assert_eq!(loaded.request.target.archive_item_id, 42);
        assert_eq!(
            loaded.request.target.child_key,
            "cam/front-2026-08-11_12-34-56.mp4"
        );
        assert_eq!(
            loaded.request.target.upload_set_id.as_deref(),
            Some(upload_set_id)
        );
    }
}
