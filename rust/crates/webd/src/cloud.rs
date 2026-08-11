use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::routing::get;
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::AppState;
use crate::error::ApiError;
use crate::gadget::TransportError;

const DEFAULT_QUEUE_LIMIT: u32 = 10;
const MAX_QUEUE_LIMIT: u32 = 16;
const MAX_CURSOR_BYTES: usize = 1024;
const INDEXD_ERROR_MESSAGE_LIMIT: usize = 256;

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
}

#[derive(Debug, Deserialize)]
struct CloudHistoryPageWire {
    status: String,
    items: Vec<CloudHistoryRowWire>,
    next_cursor: Option<String>,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/cloud", get(get_cloud_status))
        .route("/cloud/queue", get(get_cloud_queue))
        .route("/cloud/history", get(get_cloud_history))
        .route("/jobs/failed/uploads", get(get_cloud_failed_history))
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
