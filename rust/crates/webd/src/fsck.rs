use std::time::{SystemTime, UNIX_EPOCH};

use axum::extract::{Path, State};
use axum::routing::get;
use axum::{Json, Router};
use jiff::Timestamp;
use serde::{Deserialize, Serialize};

use crate::error::ApiError;
use crate::{AppState, sysinfo};

const MAX_HISTORY_ENTRIES: usize = 20;
const MAX_TEXT_LEN: usize = 1_024;

#[derive(Debug, Deserialize)]
struct RawFsckStatus {
    running: bool,
    #[serde(default)]
    partition: Option<String>,
    #[serde(default)]
    mode: Option<String>,
    #[serde(default)]
    progress: Option<String>,
    #[serde(default)]
    start_time: Option<String>,
    #[serde(default)]
    result: Option<String>,
    #[serde(default)]
    details: Option<String>,
    #[serde(default)]
    duration: Option<f64>,
    #[serde(default)]
    error: Option<String>,
}

#[derive(Debug, Serialize)]
struct FsckStatus {
    running: bool,
    partition: Option<String>,
    mode: Option<String>,
    progress: Option<String>,
    start_time: Option<String>,
    result: Option<String>,
    details: Option<String>,
    duration: Option<f64>,
    error: Option<String>,
}

#[derive(Debug, Deserialize)]
struct RawFsckHistoryEntry {
    timestamp: String,
    partition: String,
    mode: String,
    result: String,
    details: String,
    duration_seconds: f64,
}

#[derive(Debug, Clone, Serialize)]
struct FsckHistoryEntry {
    timestamp: String,
    partition: String,
    mode: String,
    result: String,
    details: String,
    duration_seconds: f64,
}

#[derive(Debug, Serialize)]
struct LastCheckResponse {
    timestamp: Option<String>,
    result: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    details: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    age_hours: Option<f64>,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/fsck/status", get(status))
        .route("/fsck/history", get(history))
        .route("/fsck/last-check/{partition}", get(last_check))
}

fn default_status() -> FsckStatus {
    FsckStatus {
        running: false,
        partition: None,
        mode: None,
        progress: None,
        start_time: None,
        result: None,
        details: None,
        duration: None,
        error: None,
    }
}

fn sanitize_partition(raw: Option<String>) -> Option<String> {
    match raw.as_deref() {
        Some("part1" | "part2" | "part3") => raw,
        _ => None,
    }
}

fn sanitize_mode(raw: Option<String>) -> Option<String> {
    match raw.as_deref() {
        Some("quick" | "repair") => raw,
        _ => None,
    }
}

fn sanitize_text(raw: Option<String>) -> Option<String> {
    let value = raw?;
    let trimmed = value.trim();
    if trimmed.is_empty() || trimmed.len() > MAX_TEXT_LEN {
        None
    } else {
        Some(trimmed.to_owned())
    }
}

fn sanitize_result(raw: Option<String>) -> Option<String> {
    match raw.as_deref() {
        Some(
            "healthy" | "repaired" | "recording" | "errors" | "failed" | "timeout" | "cancelled"
            | "never_checked",
        ) => raw,
        _ => None,
    }
}

fn sanitize_duration(raw: Option<f64>) -> Option<f64> {
    let value = raw?;
    if !value.is_finite() || value < 0.0 {
        None
    } else {
        Some(value)
    }
}

fn is_valid_timestamp(raw: &str) -> bool {
    raw.parse::<Timestamp>().is_ok()
}

fn parse_status(raw: &str) -> Option<FsckStatus> {
    let parsed: RawFsckStatus = serde_json::from_str(raw).ok()?;
    let start_time = parsed.start_time.filter(|value| is_valid_timestamp(value));
    Some(FsckStatus {
        running: parsed.running,
        partition: sanitize_partition(parsed.partition),
        mode: sanitize_mode(parsed.mode),
        progress: sanitize_text(parsed.progress),
        start_time,
        result: sanitize_result(parsed.result),
        details: sanitize_text(parsed.details),
        duration: sanitize_duration(parsed.duration),
        error: sanitize_text(parsed.error),
    })
}

fn parse_history(raw: &str) -> Option<Vec<FsckHistoryEntry>> {
    let parsed: Vec<RawFsckHistoryEntry> = serde_json::from_str(raw).ok()?;
    let mut out: Vec<FsckHistoryEntry> = parsed
        .into_iter()
        .filter_map(|entry| {
            if !is_valid_timestamp(&entry.timestamp) {
                return None;
            }
            Some(FsckHistoryEntry {
                timestamp: entry.timestamp,
                partition: sanitize_partition(Some(entry.partition))?,
                mode: sanitize_mode(Some(entry.mode))?,
                result: sanitize_result(Some(entry.result))?,
                details: sanitize_text(Some(entry.details))?,
                duration_seconds: sanitize_duration(Some(entry.duration_seconds))?,
            })
        })
        .collect();
    if out.len() > MAX_HISTORY_ENTRIES {
        out = out.split_off(out.len() - MAX_HISTORY_ENTRIES);
    }
    Some(out)
}

fn load_status(probe: &dyn sysinfo::SystemProbe, paths: &sysinfo::SysPaths) -> FsckStatus {
    probe
        .read_file_string(&paths.fsck_status_file)
        .as_deref()
        .and_then(parse_status)
        .unwrap_or_else(default_status)
}

fn load_history(
    probe: &dyn sysinfo::SystemProbe,
    paths: &sysinfo::SysPaths,
) -> Vec<FsckHistoryEntry> {
    probe
        .read_file_string(&paths.fsck_history_file)
        .as_deref()
        .and_then(parse_history)
        .unwrap_or_default()
}

fn partition_name(partition: u8) -> &'static str {
    match partition {
        1 => "part1",
        2 => "part2",
        3 => "part3",
        _ => unreachable!(),
    }
}

fn age_hours(timestamp: &str) -> Option<f64> {
    let parsed = timestamp.parse::<Timestamp>().ok()?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|d| i64::try_from(d.as_secs()).ok())?;
    let delta = now.saturating_sub(parsed.as_second());
    Some(delta as f64 / 3600.0)
}

async fn status(State(state): State<AppState>) -> Json<FsckStatus> {
    let sys = state.sys;
    let response =
        tokio::task::spawn_blocking(move || load_status(sys.probe.as_ref(), sys.paths.as_ref()))
            .await
            .unwrap_or_else(|_| default_status());
    Json(response)
}

async fn history(State(state): State<AppState>) -> Json<Vec<FsckHistoryEntry>> {
    let sys = state.sys;
    let response =
        tokio::task::spawn_blocking(move || load_history(sys.probe.as_ref(), sys.paths.as_ref()))
            .await
            .unwrap_or_default();
    Json(response)
}

async fn last_check(
    State(state): State<AppState>,
    Path(partition): Path<u8>,
) -> Result<Json<LastCheckResponse>, ApiError> {
    if !(1..=3).contains(&partition) {
        return Err(ApiError::bad_request(
            "invalid_partition",
            "partition must be 1, 2, or 3",
        ));
    }
    let partition = partition_name(partition);
    let sys = state.sys;
    let response = tokio::task::spawn_blocking(move || {
        let history = load_history(sys.probe.as_ref(), sys.paths.as_ref());
        history
            .into_iter()
            .rev()
            .find(|entry| entry.partition == partition)
    })
    .await
    .ok()
    .flatten();
    Ok(Json(match response {
        Some(entry) => LastCheckResponse {
            timestamp: Some(entry.timestamp.clone()),
            result: entry.result,
            details: Some(entry.details),
            age_hours: age_hours(&entry.timestamp),
        },
        None => LastCheckResponse {
            timestamp: None,
            result: "never_checked".to_owned(),
            details: None,
            age_hours: None,
        },
    }))
}
