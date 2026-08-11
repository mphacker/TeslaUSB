use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use teslausb_core::durable_mutation::{
    sanitize_public_error, validate_failed_upload_retry_target, validate_idempotency_key,
    validate_request_hash, validate_request_id,
};

use crate::AppState;
use crate::error::ApiError;
use crate::gadget::TransportError;
use crate::mutation_origin::require_strict_same_origin;

const DEFAULT_QUEUE_LIMIT: u32 = 10;
const MAX_QUEUE_LIMIT: u32 = 16;
const MAX_CURSOR_BYTES: usize = 1024;
const INDEXD_ERROR_MESSAGE_LIMIT: usize = 256;
const RETRY_HASH_DOMAIN_TAG: &[u8] = b"teslausb.cloud_failed_upload_retry.v1\0";
const RETRY_FORBIDDEN_MESSAGE: &str = "cross-origin failed-upload retry mutation refused";
const FAILED_UPLOAD_RETRY_ROUTE_ENABLED: bool = true;

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
struct CloudStatusResp {
    configured: bool,
    provider_type: Option<String>,
    uploader_state: String,
    sync_now_state: String,
}

#[derive(Debug, Deserialize)]
struct CloudStatusWire {
    status: String,
    configured: bool,
    provider_type: Option<String>,
    uploader_state: String,
    sync_now_state: String,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
struct CloudQueueRowResp {
    archive_item_id: i64,
    child_key: String,
    category: String,
    seq: i64,
    total_bytes: i64,
    bytes_uploaded: i64,
    state: String,
    attempts: i64,
    not_before: Option<i64>,
    last_error_class: Option<String>,
    upload_set_id: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
struct CloudQueuePageResp {
    items: Vec<CloudQueueRowResp>,
    next_cursor: Option<String>,
    limit: u32,
}

#[derive(Debug, Deserialize)]
struct CloudQueueQuery {
    cursor: Option<String>,
    limit: Option<u32>,
}

#[derive(Debug, Deserialize)]
struct CloudQueueRowWire {
    archive_item_id: i64,
    child_key: String,
    category: String,
    seq: i64,
    total_bytes: i64,
    bytes_uploaded: i64,
    state: String,
    attempts: i64,
    not_before: Option<i64>,
    last_error: Option<String>,
    #[serde(default)]
    upload_set_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct CloudQueuePageWire {
    status: String,
    items: Vec<CloudQueueRowWire>,
    next_cursor: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
struct CloudHistoryRowResp {
    id: i64,
    completion_seq: i64,
    archive_item_id: i64,
    child_key: String,
    outcome: String,
    size_bytes: i64,
    at: i64,
    error_class: Option<String>,
    upload_set_id: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
struct CloudHistoryPageResp {
    items: Vec<CloudHistoryRowResp>,
    next_cursor: Option<String>,
    limit: u32,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
struct FailedUploadHistoryRowResp {
    archive_item_id: i64,
    child_key: String,
    size_bytes: i64,
    at: i64,
    error_class: Option<String>,
    upload_set_id: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
struct FailedUploadHistoryPageResp {
    items: Vec<FailedUploadHistoryRowResp>,
    next_cursor: Option<String>,
    limit: u32,
}

#[derive(Debug, Deserialize)]
struct CloudHistoryRowWire {
    id: i64,
    completion_seq: i64,
    archive_item_id: i64,
    child_key: String,
    outcome: String,
    size_bytes: i64,
    at: i64,
    error_class: Option<String>,
    #[serde(default)]
    upload_set_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct CloudHistoryPageWire {
    status: String,
    items: Vec<CloudHistoryRowWire>,
    next_cursor: Option<String>,
}

#[derive(Debug, Deserialize)]
struct FailedUploadRetryReq {
    archive_item_id: i64,
    child_key: String,
    #[serde(default)]
    upload_set_id: Option<String>,
    #[serde(rename = "requestId")]
    request_id: String,
    #[serde(rename = "idempotencyKey")]
    idempotency_key: String,
    #[serde(default, rename = "requestHash")]
    request_hash: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
struct FailedUploadRetryResp {
    status: String,
    #[serde(rename = "jobId")]
    job_id: String,
    #[serde(rename = "requestId")]
    request_id: String,
    state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    detail: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
enum FailedUploadRetryWire {
    CloudFailedUploadRetryAccepted {
        job_id: String,
        request_id: String,
        state: String,
    },
    CloudFailedUploadRetryReplay {
        job_id: String,
        request_id: String,
        outcome: String,
        #[serde(default)]
        response_status: Option<String>,
        #[serde(default)]
        response_code: Option<i64>,
        detail: Option<String>,
    },
    CloudFailedUploadRetryConflict {
        job_id: String,
        request_id: String,
        message: String,
    },
    CloudFailedUploadRetryRefused {
        job_id: String,
        request_id: String,
        message: String,
    },
    Rejected {
        message: String,
    },
    Error {
        message: String,
    },
}

pub(crate) fn routes() -> Router<AppState> {
    let routes = Router::new()
        .route("/cloud", get(get_cloud_status))
        .route("/cloud/queue", get(get_cloud_queue))
        .route("/cloud/history", get(get_cloud_history))
        .route("/jobs/failed/uploads", get(get_cloud_failed_history));
    if FAILED_UPLOAD_RETRY_ROUTE_ENABLED {
        routes.route(
            "/cloud/queue/{archive_item_id}/retry",
            post(post_cloud_failed_upload_retry),
        )
    } else {
        routes
    }
}

async fn get_cloud_status(
    State(state): State<AppState>,
) -> Result<Json<CloudStatusResp>, ApiError> {
    let request = json!({ "cmd": "get_status" });
    let response = call_uploadd(&state, request).await?;
    match response
        .get("status")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("")
    {
        "uploadd_status" => {
            let status: CloudStatusWire = serde_json::from_value(response).map_err(|_| {
                ApiError::status(
                    StatusCode::BAD_GATEWAY,
                    "uploadd_protocol",
                    "malformed uploader status response",
                )
            })?;
            if status.status != "uploadd_status" {
                return Err(ApiError::status(
                    StatusCode::BAD_GATEWAY,
                    "uploadd_protocol",
                    "malformed uploader status response",
                ));
            }
            Ok(Json(CloudStatusResp {
                configured: status.configured,
                provider_type: status.provider_type,
                uploader_state: status.uploader_state,
                sync_now_state: status.sync_now_state,
            }))
        }
        "error" => Err(ApiError::status(
            StatusCode::BAD_GATEWAY,
            "uploadd_error",
            indexd_error_message(&response, "uploader status request failed"),
        )),
        _ => Err(ApiError::status(
            StatusCode::BAD_GATEWAY,
            "uploadd_protocol",
            "unexpected uploader status response",
        )),
    }
}

async fn get_cloud_queue(
    State(state): State<AppState>,
    Query(query): Query<CloudQueueQuery>,
) -> Result<Json<CloudQueuePageResp>, ApiError> {
    let limit = validate_queue_limit(query.limit)?;
    let cursor = validate_cursor(query.cursor)?;
    let request = json!({
        "cmd": "cloud_queue_load",
        "after_cursor": cursor,
        "limit": limit
    });
    let response = call_indexd(&state, request).await?;
    match response
        .get("status")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("")
    {
        "cloud_queue_page" => {
            let page: CloudQueuePageWire = serde_json::from_value(response).map_err(|_| {
                ApiError::status(
                    StatusCode::BAD_GATEWAY,
                    "indexd_protocol",
                    "malformed cloud queue response",
                )
            })?;
            if page.status != "cloud_queue_page" {
                return Err(ApiError::status(
                    StatusCode::BAD_GATEWAY,
                    "indexd_protocol",
                    "malformed cloud queue response",
                ));
            }
            Ok(Json(CloudQueuePageResp {
                items: page
                    .items
                    .into_iter()
                    .map(|row| CloudQueueRowResp {
                        archive_item_id: row.archive_item_id,
                        child_key: row.child_key,
                        category: row.category,
                        seq: row.seq,
                        total_bytes: row.total_bytes,
                        bytes_uploaded: row.bytes_uploaded,
                        state: row.state,
                        attempts: row.attempts,
                        not_before: row.not_before,
                        last_error_class: row.last_error.as_deref().map(classify_error),
                        upload_set_id: row.upload_set_id,
                    })
                    .collect(),
                next_cursor: page.next_cursor,
                limit,
            }))
        }
        "error" => Err(ApiError::status(
            StatusCode::BAD_GATEWAY,
            "indexd_error",
            indexd_error_message(&response, "cloud queue request failed in indexd"),
        )),
        _ => Err(ApiError::status(
            StatusCode::BAD_GATEWAY,
            "indexd_protocol",
            "unexpected cloud queue response",
        )),
    }
}

async fn get_cloud_history(
    State(state): State<AppState>,
    Query(query): Query<CloudQueueQuery>,
) -> Result<Json<CloudHistoryPageResp>, ApiError> {
    let limit = validate_queue_limit(query.limit)?;
    let cursor = validate_cursor(query.cursor)?;
    let request = json!({
        "cmd": "cloud_history_load",
        "after_cursor": cursor,
        "limit": limit
    });
    let response = call_indexd(&state, request).await?;
    match response
        .get("status")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("")
    {
        "cloud_history_page" => {
            let page: CloudHistoryPageWire = serde_json::from_value(response).map_err(|_| {
                ApiError::status(
                    StatusCode::BAD_GATEWAY,
                    "indexd_protocol",
                    "malformed cloud history response",
                )
            })?;
            if page.status != "cloud_history_page" {
                return Err(ApiError::status(
                    StatusCode::BAD_GATEWAY,
                    "indexd_protocol",
                    "malformed cloud history response",
                ));
            }
            Ok(Json(CloudHistoryPageResp {
                items: page
                    .items
                    .into_iter()
                    .map(|row| CloudHistoryRowResp {
                        id: row.id,
                        completion_seq: row.completion_seq,
                        archive_item_id: row.archive_item_id,
                        child_key: row.child_key,
                        outcome: row.outcome,
                        size_bytes: row.size_bytes,
                        at: row.at,
                        error_class: row.error_class,
                        upload_set_id: row.upload_set_id,
                    })
                    .collect(),
                next_cursor: page.next_cursor,
                limit,
            }))
        }
        "error" => Err(ApiError::status(
            StatusCode::BAD_GATEWAY,
            "indexd_error",
            indexd_error_message(&response, "cloud history request failed in indexd"),
        )),
        _ => Err(ApiError::status(
            StatusCode::BAD_GATEWAY,
            "indexd_protocol",
            "unexpected cloud history response",
        )),
    }
}

async fn get_cloud_failed_history(
    State(state): State<AppState>,
    Query(query): Query<CloudQueueQuery>,
) -> Result<Json<FailedUploadHistoryPageResp>, ApiError> {
    let limit = validate_queue_limit(query.limit)?;
    let cursor = validate_cursor(query.cursor)?;
    let request = json!({
        "cmd": "cloud_failed_history_load",
        "after_cursor": cursor,
        "limit": limit
    });
    let response = call_indexd(&state, request).await?;
    match response
        .get("status")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("")
    {
        "cloud_history_page" => {
            let page: CloudHistoryPageWire = serde_json::from_value(response).map_err(|_| {
                ApiError::status(
                    StatusCode::BAD_GATEWAY,
                    "indexd_protocol",
                    "malformed failed cloud history response",
                )
            })?;
            if page.status != "cloud_history_page" {
                return Err(ApiError::status(
                    StatusCode::BAD_GATEWAY,
                    "indexd_protocol",
                    "malformed failed cloud history response",
                ));
            }
            Ok(Json(FailedUploadHistoryPageResp {
                items: page
                    .items
                    .into_iter()
                    .map(|row| FailedUploadHistoryRowResp {
                        archive_item_id: row.archive_item_id,
                        child_key: row.child_key,
                        size_bytes: row.size_bytes,
                        at: row.at,
                        error_class: row.error_class,
                        upload_set_id: row.upload_set_id,
                    })
                    .collect(),
                next_cursor: page.next_cursor,
                limit,
            }))
        }
        "error" => Err(ApiError::status(
            StatusCode::BAD_GATEWAY,
            "indexd_error",
            indexd_error_message(&response, "failed cloud history request failed in indexd"),
        )),
        "rejected" => Err(ApiError::bad_request(
            "invalid_cursor",
            indexd_error_message(&response, "invalid cursor"),
        )),
        _ => Err(ApiError::status(
            StatusCode::BAD_GATEWAY,
            "indexd_protocol",
            "unexpected failed cloud history response",
        )),
    }
}

async fn post_cloud_failed_upload_retry(
    headers: HeaderMap,
    Path(path_archive_item_id): Path<i64>,
    State(state): State<AppState>,
    Json(body): Json<FailedUploadRetryReq>,
) -> Result<(StatusCode, Json<FailedUploadRetryResp>), ApiError> {
    require_strict_same_origin(&headers, RETRY_FORBIDDEN_MESSAGE)?;
    if body.archive_item_id != path_archive_item_id {
        return Err(ApiError::bad_request(
            "archive_item_id_mismatch",
            "body archive_item_id must match route archive_item_id",
        ));
    }
    validate_failed_upload_retry_target(
        path_archive_item_id,
        &body.child_key,
        body.upload_set_id.as_deref(),
    )
    .map_err(|reason| ApiError::bad_request("invalid_retry_target", reason))?;
    validate_request_id(&body.request_id)
        .map_err(|reason| ApiError::bad_request("invalid_request_id", reason))?;
    validate_idempotency_key(&body.idempotency_key)
        .map_err(|reason| ApiError::bad_request("invalid_idempotency_key", reason))?;
    if let Some(request_hash) = body.request_hash.as_deref() {
        validate_request_hash(request_hash)
            .map_err(|reason| ApiError::bad_request("invalid_request_hash", reason))?;
    }
    let canonical_hash = canonical_failed_upload_retry_hash(
        path_archive_item_id,
        &body.child_key,
        body.upload_set_id.as_deref(),
        &body.request_id,
        &body.idempotency_key,
    );

    let request = json!({
        "cmd": "cloud_failed_upload_retry",
        "archive_item_id": path_archive_item_id,
        "child_key": body.child_key,
        "upload_set_id": body.upload_set_id,
        "request_id": body.request_id,
        "idempotency_key": body.idempotency_key,
        "request_hash": canonical_hash,
        "job_id": new_failed_upload_retry_job_id(),
    });
    let response = call_indexd(&state, request).await?;
    let wire: FailedUploadRetryWire = serde_json::from_value(response).map_err(|_| {
        ApiError::status(
            StatusCode::BAD_GATEWAY,
            "indexd_protocol",
            "malformed failed-upload retry response",
        )
    })?;
    match wire {
        FailedUploadRetryWire::CloudFailedUploadRetryAccepted {
            job_id,
            request_id,
            state,
        } => Ok((
            StatusCode::ACCEPTED,
            Json(FailedUploadRetryResp {
                status: "accepted".to_owned(),
                job_id,
                request_id,
                state,
                detail: None,
            }),
        )),
        FailedUploadRetryWire::CloudFailedUploadRetryReplay {
            job_id,
            request_id,
            outcome,
            response_status,
            response_code,
            detail,
        } => {
            let detail = detail.map(|value| sanitize_public_error(&value));
            let http_status = replay_http_status(response_status.as_deref(), response_code, &outcome)
                .unwrap_or(StatusCode::OK);
            Ok((
                http_status,
                Json(FailedUploadRetryResp {
                    status: "replay".to_owned(),
                    job_id,
                    request_id,
                    state: replay_state(&outcome, detail.as_deref()),
                    detail,
                }),
            ))
        }
        FailedUploadRetryWire::CloudFailedUploadRetryConflict {
            job_id,
            request_id,
            message,
        } => Ok((
            StatusCode::CONFLICT,
            Json(FailedUploadRetryResp {
                status: "conflict".to_owned(),
                job_id,
                request_id,
                state: "conflict".to_owned(),
                detail: Some(sanitize_public_error(&message)),
            }),
        )),
        FailedUploadRetryWire::CloudFailedUploadRetryRefused {
            job_id,
            request_id,
            message,
        } => Ok((
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(FailedUploadRetryResp {
                status: "refused".to_owned(),
                job_id,
                request_id,
                state: "refused".to_owned(),
                detail: Some(sanitize_public_error(&message)),
            }),
        )),
        FailedUploadRetryWire::Rejected { message } => Err(ApiError::bad_request(
            "invalid_retry_request",
            sanitize_public_error(&message),
        )),
        FailedUploadRetryWire::Error { message } => {
            let _sanitized = sanitize_public_error(&message);
            Err(ApiError::status(
                StatusCode::BAD_GATEWAY,
                "indexd_error",
                "failed-upload retry request failed in indexd",
            ))
        }
    }
}

fn canonical_failed_upload_retry_hash(
    archive_item_id: i64,
    child_key: &str,
    upload_set_id: Option<&str>,
    request_id: &str,
    idempotency_key: &str,
) -> String {
    let mut hasher = Sha256::new();
    hasher.update(RETRY_HASH_DOMAIN_TAG);
    append_canonical_field(&mut hasher, b"archive_item_id", &archive_item_id.to_string());
    append_canonical_field(&mut hasher, b"child_key", child_key);
    append_canonical_field(
        &mut hasher,
        b"upload_set_id",
        upload_set_id.unwrap_or("__none__"),
    );
    append_canonical_field(&mut hasher, b"request_id", request_id);
    append_canonical_field(&mut hasher, b"idempotency_key", idempotency_key);
    hex_encode_lower(&hasher.finalize())
}

fn append_canonical_field(hasher: &mut Sha256, name: &[u8], value: &str) {
    hasher.update(name);
    hasher.update([0u8]);
    let bytes = value.as_bytes();
    hasher.update((bytes.len() as u64).to_le_bytes());
    hasher.update(bytes);
}

fn hex_encode_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for &byte in bytes {
        out.push(char::from(HEX[(byte >> 4) as usize]));
        out.push(char::from(HEX[(byte & 0x0f) as usize]));
    }
    out
}

fn replay_http_status(
    response_status: Option<&str>,
    response_code: Option<i64>,
    outcome: &str,
) -> Option<StatusCode> {
    if let Some(code) = response_code {
        if (100..=599).contains(&code) {
            if let Ok(status) = StatusCode::from_u16(code as u16) {
                return Some(status);
            }
        }
    }
    match response_status {
        Some("accepted") => Some(StatusCode::ACCEPTED),
        Some("rejected") => Some(StatusCode::UNPROCESSABLE_ENTITY),
        Some("conflict") => Some(StatusCode::CONFLICT),
        Some("error") => Some(StatusCode::BAD_GATEWAY),
        Some("replay") => Some(StatusCode::OK),
        _ => match outcome {
            "accepted" => Some(StatusCode::ACCEPTED),
            "refused" => Some(StatusCode::UNPROCESSABLE_ENTITY),
            "conflict" => Some(StatusCode::CONFLICT),
            "error" => Some(StatusCode::BAD_GATEWAY),
            _ => Some(StatusCode::OK),
        },
    }
}

async fn call_indexd(state: &AppState, request: Value) -> Result<Value, ApiError> {
    let client = state.indexd.clone();
    tokio::task::spawn_blocking(move || client.call(request))
        .await
        .map_err(|_| ApiError::Internal)?
        .map_err(indexd_transport_to_error)
}

async fn call_uploadd(state: &AppState, request: Value) -> Result<Value, ApiError> {
    let client = state.uploadd.clone();
    tokio::task::spawn_blocking(move || client.call(request))
        .await
        .map_err(|_| ApiError::Internal)?
        .map_err(uploadd_transport_to_error)
}

fn indexd_transport_to_error(err: TransportError) -> ApiError {
    match err {
        TransportError::Unavailable(_) => ApiError::status(
            StatusCode::SERVICE_UNAVAILABLE,
            "unavailable",
            "cloud service unavailable",
        ),
        TransportError::Protocol(_) => ApiError::status(
            StatusCode::BAD_GATEWAY,
            "indexd_protocol",
            "cloud service protocol error",
        ),
    }
}

fn uploadd_transport_to_error(err: TransportError) -> ApiError {
    match err {
        TransportError::Unavailable(_) => ApiError::status(
            StatusCode::SERVICE_UNAVAILABLE,
            "unavailable",
            "uploader offline",
        ),
        TransportError::Protocol(_) => ApiError::status(
            StatusCode::BAD_GATEWAY,
            "uploadd_protocol",
            "uploader protocol error",
        ),
    }
}

fn validate_queue_limit(limit: Option<u32>) -> Result<u32, ApiError> {
    match limit {
        None => Ok(DEFAULT_QUEUE_LIMIT),
        Some(0) => Err(ApiError::bad_request("invalid_limit", "limit must be >= 1")),
        Some(value) => Ok(value.min(MAX_QUEUE_LIMIT)),
    }
}

fn validate_cursor(cursor: Option<String>) -> Result<Option<String>, ApiError> {
    match cursor {
        Some(value) if value.len() > MAX_CURSOR_BYTES => Err(ApiError::bad_request(
            "invalid_cursor",
            format!("cursor exceeds {} bytes", MAX_CURSOR_BYTES),
        )),
        value => Ok(value),
    }
}

fn indexd_error_message(response: &Value, fallback: &str) -> String {
    response
        .get("message")
        .and_then(serde_json::Value::as_str)
        .map(|value| {
            value
                .chars()
                .take(INDEXD_ERROR_MESSAGE_LIMIT)
                .collect::<String>()
        })
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| fallback.to_owned())
}

fn classify_error(error: &str) -> String {
    let lower = error.to_ascii_lowercase();
    if lower.contains("timeout") {
        "timeout".to_owned()
    } else if lower.contains("auth") || lower.contains("credential") {
        "authentication".to_owned()
    } else if lower.contains("quota") || lower.contains("space") {
        "quota".to_owned()
    } else if lower.contains("network") || lower.contains("connect") {
        "network".to_owned()
    } else {
        "upload".to_owned()
    }
}

fn replay_state(outcome: &str, detail: Option<&str>) -> String {
    match outcome {
        "accepted" => detail.unwrap_or("queued").to_owned(),
        "refused" => "refused".to_owned(),
        "conflict" => "conflict".to_owned(),
        "error" => "failed".to_owned(),
        _ => "queued".to_owned(),
    }
}

fn new_failed_upload_retry_job_id() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or(0);
    format!("m-{now}")
}
