use std::collections::{HashMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write};
#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread;
use std::time::Duration;

use axum::extract::State;
use axum::http::header::HOST;
use axum::http::{HeaderMap, StatusCode, Uri};
use axum::routing::post;
use axum::{Json, Router};
use serde::{Deserialize, Serialize};

use crate::AppState;
use crate::error::ApiError;

const MUTATION_HOLD_PATH: &str = "/run/teslausb/wifi-mutation.hold";
const KEYFILE_DIR: &str = "/etc/NetworkManager/system-connections";
const CHECKPOINT_TIMEOUT_SECS: u32 = 60;
const CONNECT_WAIT_SECS: u32 = 15;
const CMD_TIMEOUT_SECS: &str = "20";
const VERIFY_MAX_POLLS: usize = 8;
const RECOVERY_VERIFY_POLLS: usize = 8;
const VERIFY_POLL_INTERVAL: Duration = Duration::from_secs(1);
const SAFE_MUTATION_HOSTS: [&str; 2] = ["localhost", "cybertruckusb.local"];

#[derive(Debug, Deserialize)]
struct ConnectReq {
    ssid: String,
    psk: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ForgetReq {
    ssid: String,
}

#[derive(Debug, Deserialize)]
struct PriorityReq {
    order: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct SelectReq {
    ssid: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
struct ConnectResp {
    connected: bool,
    ssid: String,
    ip: Option<String>,
    autoconnect: bool,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
struct ForgetResp {
    forgotten: bool,
    count: usize,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
struct PriorityResp {
    ok: bool,
    count: usize,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
struct SelectResp {
    connected: bool,
    ssid: String,
    ip: Option<String>,
}

#[derive(Clone)]
pub(crate) enum WifiOpsError {
    Unavailable,
    Failed(String),
}

pub(crate) struct SavedProfile {
    pub uuid: String,
    #[allow(dead_code)] // NM connection name; retained for diagnostics, not read by any flow.
    pub name: String,
    pub ssid: String,
}

pub(crate) struct ProfileSpec {
    pub con_name: String,
    pub ssid: String,
    pub psk: Option<String>,
}

/// Outcome of writing a fresh Wi-Fi profile: the new UUID to activate, plus the
/// prior UUIDs for the same connection name that it supersedes. The stale UUIDs
/// are deleted by `connect_flow` only after the fresh profile is activated,
/// verified, and committed, so a failed or interrupted join never destroys the
/// network the user was previously able to save.
pub(crate) struct ApplyOutcome {
    pub fresh_uuid: String,
    pub stale_uuids: Vec<String>,
}

pub(crate) trait WifiOps: Send + Sync {
    fn active_uuid_on_wlan0(&self) -> Option<String>;
    fn list_saved_wifi(&self) -> Vec<SavedProfile>;
    fn write_hold(&self) -> Result<(), WifiOpsError>;
    fn clear_hold(&self);
    fn create_checkpoint(&self, timeout_s: u32) -> Result<String, WifiOpsError>;
    fn destroy_checkpoint(&self, cp: &str) -> Result<(), WifiOpsError>;
    fn checkpoint_active(&self, checkpoint: &str) -> bool;
    fn rollback_checkpoint(&self, cp: &str) -> Result<(), WifiOpsError>;
    fn apply_profile(&self, spec: &ProfileSpec) -> Result<ApplyOutcome, WifiOpsError>;
    fn activate(&self, uuid: &str) -> Result<(), WifiOpsError>;
    fn verify_active_ip(&self, uuid: &str) -> Option<String>;
    fn delete_profile_unprotected(&self, uuid: &str) -> Result<(), WifiOpsError>;
    fn read_conn_priority(&self, uuid: &str) -> Option<(bool, i32)>;
    /// Sets `connection.autoconnect=yes` and `connection.autoconnect-priority`.
    fn set_conn_priority(&self, uuid: &str, priority: i32) -> Result<(), WifiOpsError>;
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/wifi/connect", post(connect_wifi))
        .route("/wifi/forget", post(forget_wifi))
        .route("/wifi/priority", post(priority_wifi))
        .route("/wifi/select", post(select_wifi))
}

async fn connect_wifi(
    headers: HeaderMap,
    State(state): State<AppState>,
    Json(req): Json<ConnectReq>,
) -> Result<Json<ConnectResp>, ApiError> {
    if !same_origin_ok(&headers) {
        return Err(ApiError::status(
            StatusCode::FORBIDDEN,
            "forbidden_origin",
            "cross-origin Wi-Fi mutation refused",
        ));
    }
    let guard = state.wifi_mutation.clone().try_lock_owned().map_err(|_| {
        ApiError::status(
            StatusCode::CONFLICT,
            "mutation_in_progress",
            "another Wi-Fi change is in progress",
        )
    })?;
    let result = tokio::task::spawn_blocking(move || {
        let _guard = guard;
        let ops = LiveOps;
        connect_flow(&ops, &req)
    })
    .await
    .map_err(|_| {
        ApiError::status(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal",
            "internal server error",
        )
    })??;
    Ok(Json(result))
}

async fn forget_wifi(
    headers: HeaderMap,
    State(state): State<AppState>,
    Json(req): Json<ForgetReq>,
) -> Result<Json<ForgetResp>, ApiError> {
    if !same_origin_ok(&headers) {
        return Err(ApiError::status(
            StatusCode::FORBIDDEN,
            "forbidden_origin",
            "cross-origin Wi-Fi mutation refused",
        ));
    }
    let guard = state.wifi_mutation.clone().try_lock_owned().map_err(|_| {
        ApiError::status(
            StatusCode::CONFLICT,
            "mutation_in_progress",
            "another Wi-Fi change is in progress",
        )
    })?;
    let result = tokio::task::spawn_blocking(move || {
        let _guard = guard;
        let ops = LiveOps;
        forget_flow(&ops, &req)
    })
    .await
    .map_err(|_| {
        ApiError::status(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal",
            "internal server error",
        )
    })??;
    Ok(Json(result))
}

async fn priority_wifi(
    headers: HeaderMap,
    State(state): State<AppState>,
    Json(req): Json<PriorityReq>,
) -> Result<Json<PriorityResp>, ApiError> {
    if !same_origin_ok(&headers) {
        return Err(ApiError::status(
            StatusCode::FORBIDDEN,
            "forbidden_origin",
            "cross-origin Wi-Fi mutation refused",
        ));
    }
    let guard = state.wifi_mutation.clone().try_lock_owned().map_err(|_| {
        ApiError::status(
            StatusCode::CONFLICT,
            "mutation_in_progress",
            "another Wi-Fi change is in progress",
        )
    })?;
    let result = tokio::task::spawn_blocking(move || {
        let _guard = guard;
        let ops = LiveOps;
        priority_flow(&ops, &req)
    })
    .await
    .map_err(|_| {
        ApiError::status(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal",
            "internal server error",
        )
    })??;
    Ok(Json(result))
}

async fn select_wifi(
    headers: HeaderMap,
    State(state): State<AppState>,
    Json(req): Json<SelectReq>,
) -> Result<Json<SelectResp>, ApiError> {
    if !same_origin_ok(&headers) {
        return Err(ApiError::status(
            StatusCode::FORBIDDEN,
            "forbidden_origin",
            "cross-origin Wi-Fi mutation refused",
        ));
    }
    let guard = state.wifi_mutation.clone().try_lock_owned().map_err(|_| {
        ApiError::status(
            StatusCode::CONFLICT,
            "mutation_in_progress",
            "another Wi-Fi change is in progress",
        )
    })?;
    let result = tokio::task::spawn_blocking(move || {
        let _guard = guard;
        let ops = LiveOps;
        select_flow(&ops, &req)
    })
    .await
    .map_err(|_| {
        ApiError::status(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal",
            "internal server error",
        )
    })??;
    Ok(Json(result))
}

fn connect_flow(ops: &dyn WifiOps, req: &ConnectReq) -> Result<ConnectResp, ApiError> {
    validate_ssid(&req.ssid)?;
    validate_psk(req.psk.as_deref())?;
    let Some(prev_uuid) = ops.active_uuid_on_wlan0() else {
        return Err(ApiError::status(
            StatusCode::CONFLICT,
            "precondition_failed",
            "device is not connected to Wi-Fi; refusing Wi-Fi change",
        ));
    };

    if ops.write_hold().is_err() {
        return Err(ApiError::status(
            StatusCode::INTERNAL_SERVER_ERROR,
            "wifi_hold_failed",
            "failed to establish Wi-Fi mutation hold",
        ));
    }
    let checkpoint = match ops.create_checkpoint(CHECKPOINT_TIMEOUT_SECS) {
        Ok(cp) => cp,
        Err(err) => {
            ops.clear_hold();
            return Err(ApiError::status(
                StatusCode::SERVICE_UNAVAILABLE,
                "checkpoint_unavailable",
                format!(
                    "unable to create `NetworkManager` checkpoint ({})",
                    wifi_ops_error_text(&err)
                ),
            ));
        }
    };

    let spec = ProfileSpec {
        con_name: profile_con_name(&req.ssid),
        ssid: req.ssid.clone(),
        psk: req.psk.clone(),
    };
    let (uuid, stale_uuids) = match ops.apply_profile(&spec) {
        Ok(outcome) => (outcome.fresh_uuid, outcome.stale_uuids),
        Err(err) => {
            return connect_failure_epilogue(
                ops,
                &checkpoint,
                None,
                prev_uuid.as_str(),
                StatusCode::BAD_GATEWAY,
                "wifi_join_failed",
                &format!(
                    "failed to apply Wi-Fi profile ({})",
                    wifi_ops_error_text(&err)
                ),
            );
        }
    };

    if let Err(err) = ops.activate(&uuid) {
        return connect_failure_epilogue(
            ops,
            &checkpoint,
            Some(uuid.as_str()),
            prev_uuid.as_str(),
            StatusCode::BAD_GATEWAY,
            "wifi_join_failed",
            &format!(
                "failed to activate Wi-Fi profile ({})",
                wifi_ops_error_text(&err)
            ),
        );
    }

    match ops.verify_active_ip(&uuid) {
        Some(ip) => {
            if ops.destroy_checkpoint(&checkpoint).is_err() && ops.checkpoint_active(&checkpoint) {
                return connect_failure_epilogue(
                    ops,
                    &checkpoint,
                    Some(uuid.as_str()),
                    prev_uuid.as_str(),
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "checkpoint_commit_failed",
                    "failed to commit Wi-Fi checkpoint",
                );
            }
            if ops.active_uuid_on_wlan0().as_deref() != Some(uuid.as_str()) {
                ops.clear_hold();
                return Err(ApiError::status(
                    StatusCode::GATEWAY_TIMEOUT,
                    "wifi_join_timeout",
                    "timed out waiting for Wi-Fi activation".to_owned(),
                ));
            }
            // Fresh profile is active, verified, and committed. Only now delete the profiles it
            // superseded: deferring cleanup until after a confirmed join means a failed
            // activation never destroys the user's prior saved network for this SSID (it stays
            // intact and reconnectable). Best-effort — a leftover duplicate is harmless.
            for stale in &stale_uuids {
                let _ = ops.delete_profile_unprotected(stale);
            }
            ops.clear_hold();
            Ok(ConnectResp {
                connected: true,
                ssid: req.ssid.clone(),
                ip: Some(ip),
                autoconnect: true,
            })
        }
        None => connect_failure_epilogue(
            ops,
            &checkpoint,
            Some(uuid.as_str()),
            prev_uuid.as_str(),
            StatusCode::GATEWAY_TIMEOUT,
            "wifi_join_timeout",
            "timed out waiting for Wi-Fi activation",
        ),
    }
}

fn connect_failure_epilogue(
    ops: &dyn WifiOps,
    checkpoint: &str,
    uuid: Option<&str>,
    prev_uuid: &str,
    status: StatusCode,
    code: &'static str,
    message: &str,
) -> Result<ConnectResp, ApiError> {
    let _ = ops.rollback_checkpoint(checkpoint);
    if let Some(candidate) = uuid {
        if let Some(active) = ops.active_uuid_on_wlan0() {
            if active != candidate {
                let _ = ops.delete_profile_unprotected(candidate);
            }
        }
    }
    let recovered = (0..RECOVERY_VERIFY_POLLS).any(|attempt| {
        if ops.active_uuid_on_wlan0().as_deref() == Some(prev_uuid) {
            return true;
        }
        if attempt + 1 < RECOVERY_VERIFY_POLLS {
            thread::sleep(VERIFY_POLL_INTERVAL);
        }
        false
    });
    ops.clear_hold();
    if !recovered {
        return Err(ApiError::status(
            StatusCode::INTERNAL_SERVER_ERROR,
            "wifi_recovery_uncertain",
            "previous network could not be confirmed after rollback",
        ));
    }
    Err(ApiError::status(status, code, message.to_owned()))
}

fn select_flow(ops: &dyn WifiOps, req: &SelectReq) -> Result<SelectResp, ApiError> {
    validate_ssid(&req.ssid)?;
    let saved = ops.list_saved_wifi();
    let Some(target) = saved.iter().find(|row| row.ssid == req.ssid) else {
        return Err(ApiError::status(
            StatusCode::NOT_FOUND,
            "not_found",
            "Wi-Fi profile not found",
        ));
    };

    let Some(prev_uuid) = ops.active_uuid_on_wlan0() else {
        return Err(ApiError::status(
            StatusCode::CONFLICT,
            "precondition_failed",
            "device is not connected to Wi-Fi; refusing switch",
        ));
    };

    if target.uuid == prev_uuid {
        return Ok(SelectResp {
            connected: true,
            ssid: req.ssid.clone(),
            ip: ops.verify_active_ip(target.uuid.as_str()),
        });
    }

    if ops.write_hold().is_err() {
        return Err(ApiError::status(
            StatusCode::INTERNAL_SERVER_ERROR,
            "wifi_hold_failed",
            "failed to establish Wi-Fi mutation hold",
        ));
    }

    let checkpoint = match ops.create_checkpoint(CHECKPOINT_TIMEOUT_SECS) {
        Ok(cp) => cp,
        Err(_) => {
            ops.clear_hold();
            return Err(ApiError::status(
                StatusCode::SERVICE_UNAVAILABLE,
                "checkpoint_unavailable",
                "unable to create `NetworkManager` checkpoint",
            ));
        }
    };

    if let Err(err) = ops.activate(target.uuid.as_str()) {
        return select_failure_epilogue(
            ops,
            &checkpoint,
            prev_uuid.as_str(),
            StatusCode::BAD_GATEWAY,
            "wifi_select_failed",
            &format!(
                "failed to activate selected Wi-Fi profile ({})",
                wifi_ops_error_text(&err)
            ),
        );
    }

    match ops.verify_active_ip(target.uuid.as_str()) {
        Some(ip) => {
            if ops.destroy_checkpoint(&checkpoint).is_err() && ops.checkpoint_active(&checkpoint) {
                return select_failure_epilogue(
                    ops,
                    &checkpoint,
                    prev_uuid.as_str(),
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "checkpoint_commit_failed",
                    "failed to commit Wi-Fi checkpoint",
                );
            }
            if ops.active_uuid_on_wlan0().as_deref() != Some(target.uuid.as_str()) {
                ops.clear_hold();
                return Err(ApiError::status(
                    StatusCode::GATEWAY_TIMEOUT,
                    "wifi_select_timeout",
                    "timed out waiting for Wi-Fi activation".to_owned(),
                ));
            }
            ops.clear_hold();
            Ok(SelectResp {
                connected: true,
                ssid: req.ssid.clone(),
                ip: Some(ip),
            })
        }
        None => select_failure_epilogue(
            ops,
            &checkpoint,
            prev_uuid.as_str(),
            StatusCode::GATEWAY_TIMEOUT,
            "wifi_select_timeout",
            "timed out waiting for Wi-Fi activation",
        ),
    }
}

fn select_failure_epilogue(
    ops: &dyn WifiOps,
    checkpoint: &str,
    prev_uuid: &str,
    status: StatusCode,
    code: &'static str,
    message: &str,
) -> Result<SelectResp, ApiError> {
    let _ = ops.rollback_checkpoint(checkpoint);
    let recovered = (0..RECOVERY_VERIFY_POLLS).any(|attempt| {
        if ops.active_uuid_on_wlan0().as_deref() == Some(prev_uuid) {
            return true;
        }
        if attempt + 1 < RECOVERY_VERIFY_POLLS {
            thread::sleep(VERIFY_POLL_INTERVAL);
        }
        false
    });
    ops.clear_hold();
    if !recovered {
        return Err(ApiError::status(
            StatusCode::INTERNAL_SERVER_ERROR,
            "wifi_recovery_uncertain",
            "previous network could not be confirmed after rollback",
        ));
    }
    Err(ApiError::status(status, code, message.to_owned()))
}

fn forget_flow(ops: &dyn WifiOps, req: &ForgetReq) -> Result<ForgetResp, ApiError> {
    let saved = ops.list_saved_wifi();
    let matched: Vec<&SavedProfile> = saved.iter().filter(|row| row.ssid == req.ssid).collect();
    if matched.is_empty() {
        return Err(ApiError::status(
            StatusCode::NOT_FOUND,
            "not_found",
            "Wi-Fi profile not found",
        ));
    }

    let active_uuid = ops.active_uuid_on_wlan0();
    let should_refuse = matched
        .iter()
        .any(|profile| active_uuid.as_deref() == Some(profile.uuid.as_str()));
    if should_refuse {
        return Err(ApiError::status(
            StatusCode::CONFLICT,
            "wifi_forget_refused",
            "refusing to forget the active Wi-Fi network",
        ));
    }

    for profile in &matched {
        if ops.active_uuid_on_wlan0().as_deref() == Some(profile.uuid.as_str()) {
            return Err(ApiError::status(
                StatusCode::CONFLICT,
                "wifi_forget_refused",
                "refusing to forget the active Wi-Fi network",
            ));
        }
        ops.delete_profile_unprotected(profile.uuid.as_str())
            .map_err(|err| {
                ApiError::status(
                    StatusCode::BAD_GATEWAY,
                    "wifi_forget_failed",
                    format!(
                        "failed to forget Wi-Fi profile ({})",
                        wifi_ops_error_text(&err)
                    ),
                )
            })?;
    }
    Ok(ForgetResp {
        forgotten: true,
        count: matched.len(),
    })
}

fn priority_flow(ops: &dyn WifiOps, req: &PriorityReq) -> Result<PriorityResp, ApiError> {
    let saved = ops.list_saved_wifi();

    if req.order.len() > 900 {
        return Err(ApiError::bad_request(
            "invalid_order",
            "order contains too many entries",
        ));
    }

    let mut order_set = HashSet::new();
    for ssid in &req.order {
        if !order_set.insert(ssid.clone()) {
            return Err(ApiError::bad_request(
                "invalid_order",
                "order must not contain duplicate ssids",
            ));
        }
    }

    let distinct_saved: HashSet<String> =
        saved.iter().map(|profile| profile.ssid.clone()).collect();
    if order_set != distinct_saved {
        return Err(ApiError::bad_request(
            "invalid_order",
            "order must match saved ssids exactly",
        ));
    }

    let mut priorities = HashMap::new();
    for (index, ssid) in req.order.iter().enumerate() {
        let remaining = req.order.len().saturating_sub(index);
        let Ok(priority) = i32::try_from(remaining) else {
            return Err(ApiError::bad_request(
                "invalid_order",
                "order contains too many entries",
            ));
        };
        priorities.insert(ssid.clone(), priority);
    }

    for profile in &saved {
        let Some(priority) = priorities.get(&profile.ssid) else {
            return Err(ApiError::bad_request(
                "invalid_order",
                "order must match saved ssids exactly",
            ));
        };
        ops.set_conn_priority(profile.uuid.as_str(), *priority)
            .map_err(|err| {
                ApiError::status(
                    StatusCode::BAD_GATEWAY,
                    "wifi_priority_failed",
                    format!(
                        "failed to set Wi-Fi priority ({})",
                        wifi_ops_error_text(&err)
                    ),
                )
            })?;
    }

    for profile in &saved {
        let Some(expected_priority) = priorities.get(&profile.ssid) else {
            return Err(ApiError::status(
                StatusCode::BAD_GATEWAY,
                "wifi_priority_verify_failed",
                "priority assignment missing during verify",
            ));
        };
        match ops.read_conn_priority(profile.uuid.as_str()) {
            Some((true, actual_priority)) if actual_priority == *expected_priority => {}
            _ => {
                return Err(ApiError::status(
                    StatusCode::BAD_GATEWAY,
                    "wifi_priority_verify_failed",
                    "saved Wi-Fi priority or autoconnect mismatch after write",
                ));
            }
        }
    }

    Ok(PriorityResp {
        ok: true,
        count: saved.len(),
    })
}

fn wifi_ops_error_text(err: &WifiOpsError) -> &str {
    match err {
        WifiOpsError::Unavailable => "unavailable",
        WifiOpsError::Failed(msg) => msg.as_str(),
    }
}

fn validate_ssid(ssid: &str) -> Result<(), ApiError> {
    let len = ssid.chars().count();
    if !(1..=32).contains(&len)
        || ssid.starts_with(' ')
        || ssid.ends_with(' ')
        || ssid
            .chars()
            .any(|ch| ch.is_control() || ch == ';' || ch == '\n' || ch == '\r')
    {
        return Err(ApiError::bad_request(
            "invalid_ssid",
            "ssid must be 1-32 chars without controls or surrounding spaces",
        ));
    }
    Ok(())
}

fn validate_psk(psk: Option<&str>) -> Result<(), ApiError> {
    let Some(psk_value) = psk else {
        return Ok(());
    };
    if psk_value.chars().any(char::is_control) {
        return Err(ApiError::bad_request(
            "invalid_psk",
            "psk contains unsupported control characters",
        ));
    }
    let len = psk_value.chars().count();
    let valid = (8..=63).contains(&len)
        || (len == 64 && psk_value.chars().all(|ch| ch.is_ascii_hexdigit()));
    if !valid {
        return Err(ApiError::bad_request(
            "invalid_psk",
            "psk must be 8-63 chars or 64 hex chars",
        ));
    }
    Ok(())
}

fn profile_con_name(ssid: &str) -> String {
    format!("teslausb-ui-{:016x}", fnv1a64(ssid))
}

fn fnv1a64(input: &str) -> u64 {
    const OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0000_0001_0000_01b3;
    let mut hash = OFFSET_BASIS;
    for byte in input.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(PRIME);
    }
    hash
}

fn escape_keyfile_value(value: &str) -> String {
    let escaped = value.replace('\\', "\\\\");
    if let Some(rest) = escaped.strip_prefix(' ') {
        format!("\\s{rest}")
    } else {
        escaped
    }
}

fn render_keyfile(spec: &ProfileSpec, uuid: &str) -> String {
    let mut out = String::new();
    out.push_str("[connection]\n");
    out.push_str(&format!("id={}\n", spec.con_name));
    out.push_str(&format!("uuid={uuid}\n"));
    out.push_str("type=wifi\n");
    out.push_str("interface-name=wlan0\n");
    out.push_str("autoconnect=true\n\n");

    out.push_str("[wifi]\n");
    out.push_str("mode=infrastructure\n");
    out.push_str(&format!("ssid={}\n", escape_keyfile_value(&spec.ssid)));

    if let Some(psk) = &spec.psk {
        out.push_str("\n[wifi-security]\n");
        out.push_str("key-mgmt=wpa-psk\n");
        out.push_str(&format!("psk={}\n", escape_keyfile_value(psk)));
    }

    out.push_str("\n[ipv4]\n");
    out.push_str("method=auto\n\n");
    out.push_str("[ipv6]\n");
    out.push_str("method=auto\n");
    out
}

pub(crate) fn same_origin_ok(headers: &HeaderMap) -> bool {
    if let Some(fetch_site) = headers.get("sec-fetch-site") {
        let Ok(value) = fetch_site.to_str() else {
            return false;
        };
        if !matches!(value, "same-origin" | "same-site" | "none") {
            return false;
        }
    }

    let Some(host_value) = headers.get(HOST) else {
        return false;
    };
    let Ok(host_text) = host_value.to_str() else {
        return false;
    };
    let Ok(host_uri) = format!("http://{host_text}").parse::<Uri>() else {
        return false;
    };
    let Some(host_name) = host_uri.host() else {
        return false;
    };
    let host_allowed = host_name.parse::<std::net::IpAddr>().is_ok()
        || SAFE_MUTATION_HOSTS
            .iter()
            .any(|allowed| host_name.eq_ignore_ascii_case(allowed));
    if !host_allowed {
        return false;
    }

    let origins: Vec<_> = headers.get_all("origin").iter().collect();
    if origins.len() > 1 {
        return false;
    }
    if let Some(origin) = origins.first() {
        let Ok(origin_text) = origin.to_str() else {
            return false;
        };
        let Ok(origin_uri) = origin_text.parse::<Uri>() else {
            return false;
        };
        let Some(origin_host) = origin_uri.host() else {
            return false;
        };
        let origin_host_port = origin_uri.port_u16().map_or_else(
            || origin_host.to_owned(),
            |port| format!("{origin_host}:{port}"),
        );
        let normalized_origin = origin_host_port
            .strip_suffix(":80")
            .unwrap_or(&origin_host_port);
        let normalized_host = host_text.strip_suffix(":80").unwrap_or(host_text);
        if !normalized_origin.eq_ignore_ascii_case(normalized_host) {
            return false;
        }
    }
    true
}

#[derive(Debug, Clone)]
struct ActiveConnection {
    uuid: String,
    device: String,
    state: String,
}

struct LiveOps;

impl LiveOps {
    fn run_command(program: &str, args: &[&str]) -> Result<String, WifiOpsError> {
        let output = match Command::new("timeout")
            .arg("-k")
            .arg("2")
            .arg(CMD_TIMEOUT_SECS)
            .arg(program)
            .args(args)
            .output()
        {
            Ok(output) => output,
            Err(err) if err.kind() == ErrorKind::NotFound => return Err(WifiOpsError::Unavailable),
            Err(_) => {
                return Err(WifiOpsError::Failed(format!("{program} execution failed",)));
            }
        };
        // GNU timeout exits with 124 on timeout; that maps to Failed below.
        if !output.status.success() {
            return Err(WifiOpsError::Failed(format!(
                "{program} exited unsuccessfully",
            )));
        }
        String::from_utf8(output.stdout)
            .map_err(|_| WifiOpsError::Failed(format!("{program} output was not UTF-8")))
    }

    fn parse_quoted_object_path(output: &str) -> Option<String> {
        let start = output.find('"')?;
        let tail = &output[(start + 1)..];
        let end = tail.find('"')?;
        let value = &tail[..end];
        value.starts_with('/').then(|| value.to_owned())
    }

    fn list_active_connections() -> Result<Vec<ActiveConnection>, WifiOpsError> {
        let output = Self::run_command(
            "nmcli",
            &[
                "-t",
                "-f",
                "UUID,DEVICE,STATE",
                "connection",
                "show",
                "--active",
            ],
        )?;
        let mut out = Vec::new();
        for line in output.lines() {
            let fields = split_terse_line(line);
            let [uuid, device, state, ..] = fields.as_slice() else {
                continue;
            };
            out.push(ActiveConnection {
                uuid: uuid.clone(),
                device: device.clone(),
                state: state.clone(),
            });
        }
        Ok(out)
    }

    fn list_connections() -> Result<Vec<(String, String, String)>, WifiOpsError> {
        let output = Self::run_command(
            "nmcli",
            &["-t", "-f", "UUID,NAME,TYPE", "connection", "show"],
        )?;
        let mut out = Vec::new();
        for line in output.lines() {
            let fields = split_terse_line(line);
            let [uuid, name, conn_type, ..] = fields.as_slice() else {
                continue;
            };
            out.push((uuid.clone(), name.clone(), conn_type.clone()));
        }
        Ok(out)
    }

    fn read_ssid_for_uuid(uuid: &str) -> Result<Option<String>, WifiOpsError> {
        let output = Self::run_command(
            "nmcli",
            &[
                "-t",
                "-f",
                "802-11-wireless.ssid",
                "connection",
                "show",
                uuid,
            ],
        )?;
        Ok(parse_ssid_field(&output))
    }

    fn read_random_uuid() -> Result<String, WifiOpsError> {
        let value = fs::read_to_string("/proc/sys/kernel/random/uuid")
            .map_err(|_| WifiOpsError::Failed("failed to read UUID".to_owned()))?;
        let uuid = value.trim().to_owned();
        if uuid.is_empty() {
            return Err(WifiOpsError::Failed("empty UUID".to_owned()));
        }
        Ok(uuid)
    }

    fn read_uptime_token() -> Result<String, WifiOpsError> {
        let uptime = fs::read_to_string("/proc/uptime")
            .map_err(|_| WifiOpsError::Failed("failed to read uptime".to_owned()))?;
        uptime
            .split_whitespace()
            .next()
            .map(str::to_owned)
            .ok_or_else(|| WifiOpsError::Failed("invalid uptime contents".to_owned()))
    }

    fn write_atomic_0600(path: &Path, content: &str) -> Result<(), WifiOpsError> {
        let temp_path = path.with_extension(format!("tmp.{}", std::process::id()));
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            options.mode(0o600);
        }
        let write_result = (|| {
            let mut file = options
                .open(&temp_path)
                .map_err(|_| WifiOpsError::Failed("failed to create temp file".to_owned()))?;
            file.write_all(content.as_bytes())
                .map_err(|_| WifiOpsError::Failed("failed to write temp file".to_owned()))?;
            file.sync_all()
                .map_err(|_| WifiOpsError::Failed("failed to sync temp file".to_owned()))?;
            fs::rename(&temp_path, path)
                .map_err(|_| WifiOpsError::Failed("failed to rename temp file".to_owned()))?;
            // Durability: fsync the parent directory so the rename survives a power cut. Fail
            // closed — swallowing this error could report the write as durable when the directory
            // entry never committed, and on the brick path a caller must never delete an old
            // profile in the belief that a replacement it cannot see is safely on disk.
            if let Some(parent) = path.parent() {
                let dir = fs::File::open(parent)
                    .map_err(|_| WifiOpsError::Failed("failed to open keyfile dir".to_owned()))?;
                dir.sync_all()
                    .map_err(|_| WifiOpsError::Failed("failed to fsync keyfile dir".to_owned()))?;
            }
            Ok(())
        })();
        if write_result.is_err() {
            let _ = fs::remove_file(&temp_path);
        }
        write_result
    }

    fn parse_ip4_address(output: &str) -> Option<String> {
        output.lines().find_map(|line| {
            let fields = split_terse_line(line);
            if fields
                .first()
                .is_none_or(|key| !key.starts_with("IP4.ADDRESS"))
            {
                return None;
            }
            let raw = fields.get(1)?.split('/').next()?.trim();
            (!raw.is_empty()).then(|| raw.to_owned())
        })
    }

    fn find_connection_uuids_by_name(name: &str) -> Result<Vec<String>, WifiOpsError> {
        let output = Self::run_command("nmcli", &["-t", "-f", "UUID,NAME", "connection", "show"])?;
        let mut uuids = Vec::new();
        for line in output.lines() {
            let fields = split_terse_line(line);
            let [uuid, row_name, ..] = fields.as_slice() else {
                continue;
            };
            if row_name == name {
                uuids.push(uuid.clone());
            }
        }
        Ok(uuids)
    }
}

impl WifiOps for LiveOps {
    fn active_uuid_on_wlan0(&self) -> Option<String> {
        Self::list_active_connections()
            .ok()?
            .into_iter()
            .find(|row| row.device == "wlan0" && row.state.eq_ignore_ascii_case("activated"))
            .map(|row| row.uuid)
    }

    fn list_saved_wifi(&self) -> Vec<SavedProfile> {
        let Ok(rows) = Self::list_connections() else {
            return Vec::new();
        };
        let mut saved = Vec::new();
        for (uuid, name, conn_type) in rows {
            if conn_type != "802-11-wireless" {
                continue;
            }
            let Some(ssid) = Self::read_ssid_for_uuid(&uuid).ok().flatten() else {
                continue;
            };
            saved.push(SavedProfile { uuid, name, ssid });
        }
        saved
    }

    fn write_hold(&self) -> Result<(), WifiOpsError> {
        let path = PathBuf::from(MUTATION_HOLD_PATH);
        if let Some(parent) = path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let token = Self::read_uptime_token()?;
        let payload = format!("{token}\n");
        Self::write_atomic_0600(&path, &payload)?;
        Ok(())
    }

    fn clear_hold(&self) {
        let _ = fs::remove_file(MUTATION_HOLD_PATH);
    }

    fn create_checkpoint(&self, timeout_s: u32) -> Result<String, WifiOpsError> {
        let wlan0_path = Self::run_command(
            "busctl",
            &[
                "call",
                "org.freedesktop.NetworkManager",
                "/org/freedesktop/NetworkManager",
                "org.freedesktop.NetworkManager",
                "GetDeviceByIpIface",
                "s",
                "wlan0",
            ],
        )
        .and_then(|out| {
            Self::parse_quoted_object_path(&out)
                .ok_or_else(|| WifiOpsError::Failed("missing device path".to_owned()))
        })?;

        let timeout_arg = timeout_s.to_string();
        let checkpoint_out = Self::run_command(
            "busctl",
            &[
                "call",
                "org.freedesktop.NetworkManager",
                "/org/freedesktop/NetworkManager",
                "org.freedesktop.NetworkManager",
                "CheckpointCreate",
                "aouu",
                "1",
                wlan0_path.as_str(),
                timeout_arg.as_str(),
                // flags = NM_CHECKPOINT_CREATE_FLAG_DELETE_NEW_CONNECTIONS (0x02): drop the candidate profile on rollback
                "2",
            ],
        )?;
        Self::parse_quoted_object_path(&checkpoint_out)
            .ok_or_else(|| WifiOpsError::Failed("missing checkpoint path".to_owned()))
    }

    fn destroy_checkpoint(&self, cp: &str) -> Result<(), WifiOpsError> {
        Self::run_command(
            "busctl",
            &[
                "call",
                "org.freedesktop.NetworkManager",
                "/org/freedesktop/NetworkManager",
                "org.freedesktop.NetworkManager",
                "CheckpointDestroy",
                "o",
                cp,
            ],
        )?;
        Ok(())
    }

    fn checkpoint_active(&self, checkpoint: &str) -> bool {
        Self::run_command(
            "busctl",
            &[
                "get-property",
                "org.freedesktop.NetworkManager",
                "/org/freedesktop/NetworkManager",
                "org.freedesktop.NetworkManager",
                "Checkpoints",
            ],
        )
        .map(|out| checkpoint_present(&out, checkpoint))
        .unwrap_or(true)
    }

    fn rollback_checkpoint(&self, cp: &str) -> Result<(), WifiOpsError> {
        Self::run_command(
            "busctl",
            &[
                "call",
                "org.freedesktop.NetworkManager",
                "/org/freedesktop/NetworkManager",
                "org.freedesktop.NetworkManager",
                "CheckpointRollback",
                "o",
                cp,
            ],
        )?;
        Ok(())
    }

    fn apply_profile(&self, spec: &ProfileSpec) -> Result<ApplyOutcome, WifiOpsError> {
        // Enumerate every existing profile for this SSID's connection name. Duplicates can
        // exist if a prior cleanup was interrupted by power loss (see the additive write
        // below), so consider all of them, not just the first.
        let existing_uuids = Self::find_connection_uuids_by_name(&spec.con_name)?;

        // Never replace a profile currently active on wlan0. Fail closed if the active set
        // cannot be read (a transient nmcli failure must not let us disturb the live link).
        if !existing_uuids.is_empty() {
            let active = Self::list_active_connections()?;
            if existing_uuids.iter().any(|uuid| {
                active.iter().any(|row| {
                    row.device == "wlan0"
                        && row.state.eq_ignore_ascii_case("activated")
                        && &row.uuid == uuid
                })
            }) {
                return Err(WifiOpsError::Failed(
                    "refusing to replace active wlan0 profile".to_owned(),
                ));
            }
        }

        let fresh_uuid = Self::read_random_uuid()?;
        let keyfile = render_keyfile(spec, &fresh_uuid);
        // Additive write: a UNIQUE per-UUID filename so the new profile never overwrites an
        // existing one. The old profile(s) stay intact on disk until the new profile is durably
        // written, loaded, activated, and verified, so a power cut or failed join can never
        // leave this SSID with no usable profile (the window the delete-then-write ordering had).
        let path = Path::new(KEYFILE_DIR).join(format!("teslausb-ui-{fresh_uuid}.nmconnection"));
        Self::write_atomic_0600(&path, &keyfile)?;
        if let Err(err) = Self::run_command("nmcli", &["connection", "reload"]) {
            let _ = fs::remove_file(&path);
            return Err(err);
        }

        // Hand the stale profiles back to the caller instead of deleting them here. connect_flow
        // removes them only after the fresh profile is activated, verified, and committed; on any
        // failure they are left untouched so the user's previously-saved network for this SSID
        // survives the failed join.
        Ok(ApplyOutcome {
            fresh_uuid,
            stale_uuids: existing_uuids,
        })
    }

    fn activate(&self, uuid: &str) -> Result<(), WifiOpsError> {
        let wait_arg = CONNECT_WAIT_SECS.to_string();
        Self::run_command(
            "nmcli",
            &["-w", wait_arg.as_str(), "connection", "up", "uuid", uuid],
        )?;
        Ok(())
    }

    fn verify_active_ip(&self, uuid: &str) -> Option<String> {
        for attempt in 0..VERIFY_MAX_POLLS {
            if self.active_uuid_on_wlan0().as_deref() == Some(uuid) {
                let ip_output = Self::run_command(
                    "nmcli",
                    &["-t", "-f", "IP4.ADDRESS", "device", "show", "wlan0"],
                )
                .ok()?;
                if let Some(ip) = Self::parse_ip4_address(&ip_output) {
                    return Some(ip);
                }
            }
            if attempt + 1 < VERIFY_MAX_POLLS {
                thread::sleep(VERIFY_POLL_INTERVAL);
            }
        }
        None
    }

    fn delete_profile_unprotected(&self, uuid: &str) -> Result<(), WifiOpsError> {
        // Fail closed: if active connections cannot be read, refuse to delete rather than
        // assume nothing is active (a transient nmcli failure must not delete the live profile).
        let active = Self::list_active_connections()?;
        let is_active = active.iter().any(|row| {
            row.device == "wlan0" && row.state.eq_ignore_ascii_case("activated") && row.uuid == uuid
        });
        if is_active {
            return Err(WifiOpsError::Failed(
                "refusing to delete active wlan0 profile".to_owned(),
            ));
        }
        Self::run_command("nmcli", &["connection", "delete", "uuid", uuid])?;
        Ok(())
    }

    fn read_conn_priority(&self, uuid: &str) -> Option<(bool, i32)> {
        let output = Self::run_command(
            "nmcli",
            &[
                "-t",
                "-f",
                "connection.autoconnect,connection.autoconnect-priority",
                "connection",
                "show",
                "uuid",
                uuid,
            ],
        )
        .ok()?;

        let mut autoconnect = None;
        let mut priority = 0;
        for line in output.lines() {
            let Some((field, value)) = line.split_once(':') else {
                continue;
            };
            match field {
                "connection.autoconnect" => autoconnect = Some(value.trim() == "yes"),
                "connection.autoconnect-priority" => {
                    priority = value.trim().parse::<i32>().unwrap_or(0);
                }
                _ => {}
            }
        }
        autoconnect.map(|value| (value, priority))
    }

    fn set_conn_priority(&self, uuid: &str, priority: i32) -> Result<(), WifiOpsError> {
        let priority_text = priority.to_string();
        Self::run_command(
            "nmcli",
            &[
                "connection",
                "modify",
                "uuid",
                uuid,
                "connection.autoconnect",
                "yes",
                "connection.autoconnect-priority",
                priority_text.as_str(),
            ],
        )
        .map(|_| ())
    }
}

fn split_terse_line(line: &str) -> Vec<String> {
    let mut fields = Vec::new();
    let mut current = String::new();
    let mut escaped = false;
    for ch in line.chars() {
        if escaped {
            current.push(ch);
            escaped = false;
            continue;
        }
        match ch {
            '\\' => escaped = true,
            ':' => {
                fields.push(current);
                current = String::new();
            }
            _ => current.push(ch),
        }
    }
    if escaped {
        current.push('\\');
    }
    fields.push(current);
    fields
}

fn parse_ssid_field(output: &str) -> Option<String> {
    output.lines().find_map(|line| {
        let fields = split_terse_line(line);
        if fields
            .first()
            .is_none_or(|key| key != "802-11-wireless.ssid")
        {
            return None;
        }
        let value = fields.iter().skip(1).cloned().collect::<Vec<_>>().join(":");
        (!value.trim().is_empty()).then_some(value)
    })
}

/// True iff `checkpoint` (a NM D-Bus object path) appears as a whole,
/// quote-delimited entry in `busctl get-property ... Checkpoints` output
/// (`ao N "/path" ...`). Matching the surrounding quotes prevents a shorter
/// path like `/Checkpoint/1` from falsely matching inside `/Checkpoint/10`.
fn checkpoint_present(output: &str, checkpoint: &str) -> bool {
    output.contains(&format!("\"{checkpoint}\""))
}

#[cfg(test)]
#[allow(
    clippy::expect_used,
    clippy::ignored_unit_patterns,
    clippy::indexing_slicing,
    clippy::needless_pass_by_value,
    clippy::panic,
    clippy::unwrap_used
)]
mod tests {
    use std::sync::Mutex;

    use axum::http::HeaderValue;

    use super::*;

    struct FakeOps {
        active_uuid: Option<String>,
        rollback_active_uuid: Option<String>,
        saved_wifi: Vec<SavedProfile>,
        checkpoint_result: Result<String, WifiOpsError>,
        apply_result: Result<String, WifiOpsError>,
        apply_stale: Vec<String>,
        activate_result: Result<(), WifiOpsError>,
        verify_result: Option<String>,
        destroy_result: Result<(), WifiOpsError>,
        checkpoint_active: bool,
        rollback_result: Result<(), WifiOpsError>,
        delete_result: Result<(), WifiOpsError>,
        write_hold_result: Result<(), WifiOpsError>,
        set_priority_result: Result<(), WifiOpsError>,
        priority_state: Mutex<HashMap<String, (bool, i32)>>,
        priority_read_override: Option<(String, (bool, i32))>,
        activated_uuid: Mutex<Option<String>>,
        calls: Mutex<Vec<String>>,
        rolled_back: Mutex<bool>,
    }

    impl Default for FakeOps {
        fn default() -> Self {
            Self {
                active_uuid: Some("prev-uuid".to_owned()),
                rollback_active_uuid: None,
                saved_wifi: Vec::new(),
                checkpoint_result: Ok("cp-1".to_owned()),
                apply_result: Ok("candidate-uuid".to_owned()),
                apply_stale: Vec::new(),
                activate_result: Ok(()),
                verify_result: Some("192.168.4.22".to_owned()),
                destroy_result: Ok(()),
                checkpoint_active: true,
                rollback_result: Ok(()),
                delete_result: Ok(()),
                write_hold_result: Ok(()),
                set_priority_result: Ok(()),
                priority_state: Mutex::new(HashMap::new()),
                priority_read_override: None,
                activated_uuid: Mutex::new(None),
                calls: Mutex::new(Vec::new()),
                rolled_back: Mutex::new(false),
            }
        }
    }

    impl FakeOps {
        fn push_call(&self, call: String) {
            self.calls.lock().expect("calls lock").push(call);
        }

        fn calls(&self) -> Vec<String> {
            self.calls.lock().expect("calls lock").clone()
        }
    }

    impl WifiOps for FakeOps {
        fn active_uuid_on_wlan0(&self) -> Option<String> {
            self.push_call("active_uuid_on_wlan0".to_owned());
            if *self.rolled_back.lock().expect("rollback lock") {
                return self
                    .rollback_active_uuid
                    .clone()
                    .or_else(|| self.active_uuid.clone());
            }
            self.activated_uuid
                .lock()
                .expect("activated uuid lock")
                .clone()
                .or_else(|| self.active_uuid.clone())
        }

        fn list_saved_wifi(&self) -> Vec<SavedProfile> {
            self.push_call("list_saved_wifi".to_owned());
            self.saved_wifi
                .iter()
                .map(|row| SavedProfile {
                    uuid: row.uuid.clone(),
                    name: row.name.clone(),
                    ssid: row.ssid.clone(),
                })
                .collect()
        }

        fn write_hold(&self) -> Result<(), WifiOpsError> {
            self.push_call("write_hold".to_owned());
            self.write_hold_result
                .as_ref()
                .map_or_else(|err| Err(err.clone()), |()| Ok(()))
        }

        fn clear_hold(&self) {
            self.push_call("clear_hold".to_owned());
        }

        fn create_checkpoint(&self, _: u32) -> Result<String, WifiOpsError> {
            self.push_call("create_checkpoint".to_owned());
            self.checkpoint_result
                .as_ref()
                .map_or_else(|err| Err(err.clone()), |value| Ok(value.clone()))
        }

        fn destroy_checkpoint(&self, _: &str) -> Result<(), WifiOpsError> {
            self.push_call("destroy_checkpoint".to_owned());
            self.destroy_result
                .as_ref()
                .map_or_else(|err| Err(err.clone()), |()| Ok(()))
        }

        fn checkpoint_active(&self, _: &str) -> bool {
            self.push_call("checkpoint_active".to_owned());
            self.checkpoint_active
        }

        fn rollback_checkpoint(&self, _: &str) -> Result<(), WifiOpsError> {
            self.push_call("rollback_checkpoint".to_owned());
            *self.rolled_back.lock().expect("rollback lock") = true;
            self.rollback_result
                .as_ref()
                .map_or_else(|err| Err(err.clone()), |()| Ok(()))
        }

        fn apply_profile(&self, _: &ProfileSpec) -> Result<ApplyOutcome, WifiOpsError> {
            self.push_call("apply_profile".to_owned());
            self.apply_result.as_ref().map_or_else(
                |err| Err(err.clone()),
                |value| {
                    Ok(ApplyOutcome {
                        fresh_uuid: value.clone(),
                        stale_uuids: self.apply_stale.clone(),
                    })
                },
            )
        }

        fn activate(&self, uuid: &str) -> Result<(), WifiOpsError> {
            self.push_call(format!("activate:{uuid}"));
            if self.activate_result.is_ok() {
                *self.activated_uuid.lock().expect("activated uuid lock") = Some(uuid.to_owned());
            }
            self.activate_result
                .as_ref()
                .map_or_else(|err| Err(err.clone()), |()| Ok(()))
        }

        fn verify_active_ip(&self, _: &str) -> Option<String> {
            self.push_call("verify_active_ip".to_owned());
            self.verify_result.clone()
        }

        fn delete_profile_unprotected(&self, uuid: &str) -> Result<(), WifiOpsError> {
            self.push_call(format!("delete_profile_unprotected:{uuid}"));
            self.delete_result
                .as_ref()
                .map_or_else(|err| Err(err.clone()), |()| Ok(()))
        }

        fn read_conn_priority(&self, uuid: &str) -> Option<(bool, i32)> {
            self.push_call(format!("read_conn_priority:{uuid}"));
            if let Some((override_uuid, override_value)) = &self.priority_read_override {
                if override_uuid == uuid {
                    return Some(*override_value);
                }
            }
            self.priority_state
                .lock()
                .expect("priority state lock")
                .get(uuid)
                .copied()
        }

        fn set_conn_priority(&self, uuid: &str, priority: i32) -> Result<(), WifiOpsError> {
            self.push_call(format!("set_conn_priority:{uuid}={priority}"));
            if let Err(err) = &self.set_priority_result {
                return Err(err.clone());
            }
            self.priority_state
                .lock()
                .expect("priority state lock")
                .insert(uuid.to_owned(), (true, priority));
            Ok(())
        }
    }

    fn status_and_code(err: &ApiError) -> (StatusCode, &'static str) {
        match err {
            ApiError::BadRequest { code, .. } => (StatusCode::BAD_REQUEST, code),
            ApiError::Status { status, code, .. } => (*status, code),
            ApiError::NotFound => (StatusCode::NOT_FOUND, "not_found"),
            ApiError::Upstream { .. } | ApiError::Internal => {
                panic!("unexpected error variant")
            }
        }
    }

    #[test]
    fn validate_rejects_invalid_ssid_and_psk_inputs() {
        let err = validate_ssid("").expect_err("empty ssid");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(code, "invalid_ssid");

        let long_ssid = "a".repeat(33);
        assert_eq!(
            status_and_code(&validate_ssid(&long_ssid).expect_err("long ssid")).1,
            "invalid_ssid"
        );
        assert_eq!(
            status_and_code(&validate_ssid("bad\u{7}ssid").expect_err("control")).1,
            "invalid_ssid"
        );
        assert_eq!(
            status_and_code(&validate_ssid("bad;ssid").expect_err("semicolon")).1,
            "invalid_ssid"
        );
        assert_eq!(
            status_and_code(&validate_ssid(" leading").expect_err("leading space")).1,
            "invalid_ssid"
        );

        assert_eq!(
            status_and_code(&validate_psk(Some("1234567")).expect_err("short psk")).1,
            "invalid_psk"
        );
        assert_eq!(
            status_and_code(&validate_psk(Some(&"a".repeat(65))).expect_err("long psk")).1,
            "invalid_psk"
        );
        assert_eq!(
            status_and_code(&validate_psk(Some(&"g".repeat(64))).expect_err("non-hex psk")).1,
            "invalid_psk"
        );
        assert!(validate_psk(Some(&"a".repeat(64))).is_ok());
        assert!(validate_psk(Some("12345678")).is_ok());
        assert!(validate_psk(None).is_ok());
    }

    #[test]
    fn connect_precondition_failure_when_not_connected_skips_mutations() {
        let ops = FakeOps {
            active_uuid: None,
            ..FakeOps::default()
        };
        let req = ConnectReq {
            ssid: "Guest".to_owned(),
            psk: Some("password1".to_owned()),
        };
        let err = connect_flow(&ops, &req).expect_err("precondition failure");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::CONFLICT);
        assert_eq!(code, "precondition_failed");
        assert_eq!(ops.calls(), vec!["active_uuid_on_wlan0"]);
    }

    #[test]
    fn connect_checkpoint_creation_failure_clears_hold() {
        let ops = FakeOps {
            checkpoint_result: Err(WifiOpsError::Unavailable),
            ..FakeOps::default()
        };
        let req = ConnectReq {
            ssid: "Guest".to_owned(),
            psk: Some("password1".to_owned()),
        };
        let err = connect_flow(&ops, &req).expect_err("checkpoint failure");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(code, "checkpoint_unavailable");
        assert_eq!(
            ops.calls(),
            vec![
                "active_uuid_on_wlan0",
                "write_hold",
                "create_checkpoint",
                "clear_hold"
            ]
        );
    }

    #[test]
    fn connect_happy_path_orders_calls_and_returns_ip() {
        let ops = FakeOps::default();
        let req = ConnectReq {
            ssid: "Guest".to_owned(),
            psk: Some("password1".to_owned()),
        };
        let resp = connect_flow(&ops, &req).expect("happy path");
        assert_eq!(
            resp,
            ConnectResp {
                connected: true,
                ssid: "Guest".to_owned(),
                ip: Some("192.168.4.22".to_owned()),
                autoconnect: true
            }
        );
        assert_eq!(
            ops.calls(),
            vec![
                "active_uuid_on_wlan0",
                "write_hold",
                "create_checkpoint",
                "apply_profile",
                "activate:candidate-uuid",
                "verify_active_ip",
                "destroy_checkpoint",
                "active_uuid_on_wlan0",
                "clear_hold"
            ]
        );
    }

    #[test]
    fn connect_success_deletes_stale_profiles_after_commit() {
        let ops = FakeOps {
            apply_stale: vec!["stale-old-uuid".to_owned()],
            ..FakeOps::default()
        };
        let req = ConnectReq {
            ssid: "Guest".to_owned(),
            psk: Some("password1".to_owned()),
        };
        connect_flow(&ops, &req).expect("happy path");
        let calls = ops.calls();
        // The superseded profile is deleted only after the checkpoint is committed.
        let commit_idx = calls
            .iter()
            .position(|c| c == "destroy_checkpoint")
            .expect("destroy_checkpoint call");
        let delete_idx = calls
            .iter()
            .position(|c| c == "delete_profile_unprotected:stale-old-uuid")
            .expect("stale profile delete call");
        assert!(
            delete_idx > commit_idx,
            "stale profile must be deleted only after commit"
        );
    }

    #[test]
    fn connect_failed_activation_preserves_stale_profiles() {
        let ops = FakeOps {
            active_uuid: Some("prev-uuid".to_owned()),
            rollback_active_uuid: Some("prev-uuid".to_owned()),
            activate_result: Err(WifiOpsError::Failed("activate failed".to_owned())),
            apply_stale: vec!["stale-old-uuid".to_owned()],
            ..FakeOps::default()
        };
        let req = ConnectReq {
            ssid: "Guest".to_owned(),
            psk: Some("password1".to_owned()),
        };
        connect_flow(&ops, &req).expect_err("activate failure");
        let calls = ops.calls();
        // A failed join must never delete the user's previously-saved profile for this SSID;
        // only the fresh candidate is cleaned up by the failure epilogue.
        assert!(
            !calls.contains(&"delete_profile_unprotected:stale-old-uuid".to_owned()),
            "stale profile must survive a failed activation"
        );
        assert!(calls.contains(&"delete_profile_unprotected:candidate-uuid".to_owned()));
    }

    #[test]
    fn connect_activate_error_rolls_back_deletes_and_clears_hold() {
        let ops = FakeOps {
            active_uuid: Some("prev-uuid".to_owned()),
            rollback_active_uuid: Some("prev-uuid".to_owned()),
            activate_result: Err(WifiOpsError::Failed("activate failed".to_owned())),
            ..FakeOps::default()
        };
        let req = ConnectReq {
            ssid: "Guest".to_owned(),
            psk: Some("password1".to_owned()),
        };
        let err = connect_flow(&ops, &req).expect_err("activate failure");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::BAD_GATEWAY);
        assert_eq!(code, "wifi_join_failed");
        assert!(ops.calls().contains(&"rollback_checkpoint".to_owned()));
        assert!(
            ops.calls()
                .contains(&"delete_profile_unprotected:candidate-uuid".to_owned())
        );
        assert!(ops.calls().contains(&"active_uuid_on_wlan0".to_owned()));
        assert!(!ops.calls().contains(&"destroy_checkpoint".to_owned()));
        assert!(ops.calls().contains(&"clear_hold".to_owned()));
    }

    #[test]
    fn connect_verify_timeout_rolls_back() {
        let ops = FakeOps {
            active_uuid: Some("prev-uuid".to_owned()),
            rollback_active_uuid: Some("prev-uuid".to_owned()),
            verify_result: None,
            ..FakeOps::default()
        };
        let req = ConnectReq {
            ssid: "Guest".to_owned(),
            psk: Some("password1".to_owned()),
        };
        let err = connect_flow(&ops, &req).expect_err("timeout");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::GATEWAY_TIMEOUT);
        assert_eq!(code, "wifi_join_timeout");
        assert!(ops.calls().contains(&"rollback_checkpoint".to_owned()));
        assert!(
            ops.calls()
                .contains(&"delete_profile_unprotected:candidate-uuid".to_owned())
        );
        assert!(ops.calls().contains(&"active_uuid_on_wlan0".to_owned()));
        assert!(!ops.calls().contains(&"destroy_checkpoint".to_owned()));
        assert!(ops.calls().contains(&"clear_hold".to_owned()));
    }

    #[test]
    fn connect_destroy_failure_returns_checkpoint_commit_failed() {
        let ops = FakeOps {
            active_uuid: Some("prev-uuid".to_owned()),
            rollback_active_uuid: Some("prev-uuid".to_owned()),
            destroy_result: Err(WifiOpsError::Failed("destroy failed".to_owned())),
            ..FakeOps::default()
        };
        let req = ConnectReq {
            ssid: "Guest".to_owned(),
            psk: Some("password1".to_owned()),
        };
        let err = connect_flow(&ops, &req).expect_err("destroy failure");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(code, "checkpoint_commit_failed");
        assert!(ops.calls().contains(&"checkpoint_active".to_owned()));
        assert!(ops.calls().contains(&"rollback_checkpoint".to_owned()));
        assert!(ops.calls().contains(&"active_uuid_on_wlan0".to_owned()));
    }

    #[test]
    fn connect_destroy_ambiguous_but_checkpoint_gone_commits() {
        let ops = FakeOps {
            destroy_result: Err(WifiOpsError::Failed("lost reply".to_owned())),
            checkpoint_active: false,
            ..FakeOps::default()
        };
        let req = ConnectReq {
            ssid: "Guest".to_owned(),
            psk: Some("password1".to_owned()),
        };
        let resp = connect_flow(&ops, &req).expect("destroy ambiguous but committed");
        assert_eq!(
            resp,
            ConnectResp {
                connected: true,
                ssid: "Guest".to_owned(),
                ip: Some("192.168.4.22".to_owned()),
                autoconnect: true
            }
        );
        let calls = ops.calls();
        assert!(calls.contains(&"destroy_checkpoint".to_owned()));
        assert!(calls.contains(&"checkpoint_active".to_owned()));
        assert!(calls.contains(&"active_uuid_on_wlan0".to_owned()));
        assert!(calls.contains(&"clear_hold".to_owned()));
        assert!(!calls.contains(&"rollback_checkpoint".to_owned()));
    }

    #[test]
    fn checkpoint_present_matches_whole_path_not_substring() {
        let two = "ao 2 \"/org/freedesktop/NetworkManager/Checkpoint/1\" \"/org/freedesktop/NetworkManager/Checkpoint/10\"\n";
        assert!(checkpoint_present(
            two,
            "/org/freedesktop/NetworkManager/Checkpoint/1"
        ));
        assert!(checkpoint_present(
            two,
            "/org/freedesktop/NetworkManager/Checkpoint/10"
        ));
        // The bug this fix closes: /1 must NOT match inside /10.
        let only_ten = "ao 1 \"/org/freedesktop/NetworkManager/Checkpoint/10\"\n";
        assert!(!checkpoint_present(
            only_ten,
            "/org/freedesktop/NetworkManager/Checkpoint/1"
        ));
        // Empty checkpoint list => gone.
        assert!(!checkpoint_present(
            "ao 0\n",
            "/org/freedesktop/NetworkManager/Checkpoint/1"
        ));
    }

    #[test]
    fn connect_failure_when_previous_network_not_restored_returns_recovery_uncertain() {
        let ops = FakeOps {
            active_uuid: Some("prev-uuid".to_owned()),
            rollback_active_uuid: Some("other-uuid".to_owned()),
            activate_result: Err(WifiOpsError::Failed("activate failed".to_owned())),
            ..FakeOps::default()
        };
        let req = ConnectReq {
            ssid: "Guest".to_owned(),
            psk: Some("password1".to_owned()),
        };
        let err = connect_flow(&ops, &req).expect_err("recovery uncertain");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(code, "wifi_recovery_uncertain");
    }

    #[test]
    fn connect_hold_write_failure_aborts_before_checkpoint() {
        let ops = FakeOps {
            active_uuid: Some("prev-uuid".to_owned()),
            write_hold_result: Err(WifiOpsError::Failed("no hold".to_owned())),
            ..FakeOps::default()
        };
        let req = ConnectReq {
            ssid: "Guest".to_owned(),
            psk: Some("password1".to_owned()),
        };
        let err = connect_flow(&ops, &req).expect_err("hold write failure");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(code, "wifi_hold_failed");
        assert_eq!(ops.calls(), vec!["active_uuid_on_wlan0", "write_hold"]);
    }

    #[test]
    fn same_origin_checks_reject_cross_site_and_allow_safe_cases() {
        let mut headers = HeaderMap::new();
        headers.insert("sec-fetch-site", HeaderValue::from_static("cross-site"));
        assert!(!same_origin_ok(&headers));

        let mut headers = HeaderMap::new();
        headers.insert("origin", HeaderValue::from_static("http://evil.test"));
        headers.insert(HOST, HeaderValue::from_static("cybertruckusb.local"));
        assert!(!same_origin_ok(&headers));

        let headers = HeaderMap::new();
        assert!(!same_origin_ok(&headers));

        let mut headers = HeaderMap::new();
        headers.insert(HOST, HeaderValue::from_static("cybertruckusb.local"));
        assert!(same_origin_ok(&headers));

        let mut headers = HeaderMap::new();
        headers.insert(HOST, HeaderValue::from_static("evil.com"));
        assert!(!same_origin_ok(&headers));

        let mut headers = HeaderMap::new();
        headers.insert(
            "origin",
            HeaderValue::from_static("http://cybertruckusb.local"),
        );
        headers.insert(HOST, HeaderValue::from_static("cybertruckusb.local"));
        headers.insert("sec-fetch-site", HeaderValue::from_static("same-origin"));
        assert!(same_origin_ok(&headers));

        let mut headers = HeaderMap::new();
        headers.insert("origin", HeaderValue::from_static("http://evil.com"));
        headers.insert(HOST, HeaderValue::from_static("evil.com"));
        headers.insert("sec-fetch-site", HeaderValue::from_static("same-origin"));
        assert!(!same_origin_ok(&headers));

        let mut headers = HeaderMap::new();
        headers.insert("origin", HeaderValue::from_static("http://192.168.4.1"));
        headers.insert(HOST, HeaderValue::from_static("192.168.4.1"));
        headers.insert("sec-fetch-site", HeaderValue::from_static("same-origin"));
        assert!(same_origin_ok(&headers));

        let mut headers = HeaderMap::new();
        headers.insert(
            "origin",
            HeaderValue::from_static("http://cybertruckusb.local"),
        );
        headers.insert(HOST, HeaderValue::from_static("cybertruckusb.local:80"));
        headers.insert("sec-fetch-site", HeaderValue::from_static("same-origin"));
        assert!(same_origin_ok(&headers));
    }

    #[test]
    fn render_keyfile_matches_expected_for_open_and_psk_networks() {
        let open_spec = ProfileSpec {
            con_name: "teslausb-ui-open".to_owned(),
            ssid: "Guest".to_owned(),
            psk: None,
        };
        let open = render_keyfile(&open_spec, "uuid-open");
        assert_eq!(
            open,
            "[connection]\nid=teslausb-ui-open\nuuid=uuid-open\ntype=wifi\ninterface-name=wlan0\nautoconnect=true\n\n[wifi]\nmode=infrastructure\nssid=Guest\n\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n"
        );
        assert!(!open.contains("[wifi-security]"));

        let psk_spec = ProfileSpec {
            con_name: "teslausb-ui-psk".to_owned(),
            ssid: "Guest".to_owned(),
            psk: Some("password1".to_owned()),
        };
        let psk = render_keyfile(&psk_spec, "uuid-psk");
        assert!(psk.contains("autoconnect=true"));
        assert!(psk.contains("key-mgmt=wpa-psk"));
        assert!(psk.contains("psk=password1"));
    }

    #[test]
    fn escape_keyfile_value_escapes_backslashes_and_leading_space() {
        assert_eq!(escape_keyfile_value(r"my\pass"), r"my\\pass");
        assert_eq!(escape_keyfile_value("pass\\"), "pass\\\\");
        assert_eq!(escape_keyfile_value(" secret"), r"\ssecret");
    }

    #[test]
    fn render_keyfile_escapes_psk_backslashes() {
        let spec = ProfileSpec {
            con_name: "teslausb-ui-escaped".to_owned(),
            ssid: "Guest".to_owned(),
            psk: Some("wpa\\pass".to_owned()),
        };
        let rendered = render_keyfile(&spec, "uuid-escaped");
        assert!(rendered.contains("psk=wpa\\\\pass"));
        assert!(!rendered.contains("psk=wpa\\pass\n"));
    }

    #[test]
    fn profile_name_is_deterministic_and_prefixed() {
        let first = profile_con_name("Guest");
        let second = profile_con_name("Guest");
        assert_eq!(first, second);
        assert!(first.starts_with("teslausb-ui-"));
    }

    #[test]
    fn forget_returns_not_found_when_ssid_missing() {
        let ops = FakeOps::default();
        let req = ForgetReq {
            ssid: "Missing".to_owned(),
        };
        let err = forget_flow(&ops, &req).expect_err("missing profile");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert_eq!(code, "not_found");
    }

    #[test]
    fn forget_allows_netplan_profile() {
        let ops = FakeOps {
            saved_wifi: vec![SavedProfile {
                uuid: "uuid-netplan".to_owned(),
                name: "netplan-wlan0-Trez".to_owned(),
                ssid: "Trez".to_owned(),
            }],
            ..FakeOps::default()
        };
        let req = ForgetReq {
            ssid: "Trez".to_owned(),
        };
        let resp = forget_flow(&ops, &req).expect("netplan is deletable");
        assert_eq!(
            resp,
            ForgetResp {
                forgotten: true,
                count: 1
            }
        );
        assert!(
            ops.calls()
                .contains(&"delete_profile_unprotected:uuid-netplan".to_owned())
        );
    }

    #[test]
    fn forget_refuses_active_profile() {
        let ops = FakeOps {
            active_uuid: Some("uuid-active".to_owned()),
            saved_wifi: vec![
                SavedProfile {
                    uuid: "uuid-active".to_owned(),
                    name: "Guest".to_owned(),
                    ssid: "Guest".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-other".to_owned(),
                    name: "Other".to_owned(),
                    ssid: "Other".to_owned(),
                },
            ],
            ..FakeOps::default()
        };
        let req = ForgetReq {
            ssid: "Guest".to_owned(),
        };
        let err = forget_flow(&ops, &req).expect_err("active profile");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::CONFLICT);
        assert_eq!(code, "wifi_forget_refused");
    }

    #[test]
    fn forget_allows_last_non_active_profile() {
        let ops = FakeOps {
            saved_wifi: vec![SavedProfile {
                uuid: "uuid-only".to_owned(),
                name: "Guest".to_owned(),
                ssid: "Guest".to_owned(),
            }],
            ..FakeOps::default()
        };
        let req = ForgetReq {
            ssid: "Guest".to_owned(),
        };
        let resp = forget_flow(&ops, &req).expect("last non-active profile");
        assert_eq!(
            resp,
            ForgetResp {
                forgotten: true,
                count: 1
            }
        );
        assert!(
            ops.calls()
                .contains(&"delete_profile_unprotected:uuid-only".to_owned())
        );
    }

    #[test]
    fn forget_happy_path_deletes_matching_profile() {
        let ops = FakeOps {
            saved_wifi: vec![
                SavedProfile {
                    uuid: "uuid-drop".to_owned(),
                    name: "Guest".to_owned(),
                    ssid: "Guest".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-keep".to_owned(),
                    name: "Office".to_owned(),
                    ssid: "Office".to_owned(),
                },
            ],
            ..FakeOps::default()
        };
        let req = ForgetReq {
            ssid: "Guest".to_owned(),
        };
        let resp = forget_flow(&ops, &req).expect("forget happy path");
        assert_eq!(
            resp,
            ForgetResp {
                forgotten: true,
                count: 1
            }
        );
        assert!(
            ops.calls()
                .contains(&"delete_profile_unprotected:uuid-drop".to_owned())
        );
    }

    #[test]
    fn priority_reorder_happy_path() {
        let ops = FakeOps {
            saved_wifi: vec![
                SavedProfile {
                    uuid: "uuid-trez".to_owned(),
                    name: "netplan-wlan0-Trez".to_owned(),
                    ssid: "Trez".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-a".to_owned(),
                    name: "A-profile".to_owned(),
                    ssid: "A".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-b".to_owned(),
                    name: "B-profile".to_owned(),
                    ssid: "B".to_owned(),
                },
            ],
            priority_state: Mutex::new(HashMap::from([
                ("uuid-trez".to_owned(), (true, 0)),
                ("uuid-a".to_owned(), (true, 0)),
                ("uuid-b".to_owned(), (true, 0)),
            ])),
            ..FakeOps::default()
        };
        let req = PriorityReq {
            order: vec!["B".to_owned(), "A".to_owned(), "Trez".to_owned()],
        };
        let resp = priority_flow(&ops, &req).expect("priority happy path");
        assert_eq!(resp, PriorityResp { ok: true, count: 3 });
        let state = ops.priority_state.lock().expect("priority state lock");
        assert_eq!(state.get("uuid-b"), Some(&(true, 3)));
        assert_eq!(state.get("uuid-a"), Some(&(true, 2)));
        assert_eq!(state.get("uuid-trez"), Some(&(true, 1)));
    }

    #[test]
    fn priority_reorder_sets_autoconnect_and_priority_on_all() {
        let ops = FakeOps {
            saved_wifi: vec![
                SavedProfile {
                    uuid: "uuid-netplan".to_owned(),
                    name: "netplan-wlan0-Trez".to_owned(),
                    ssid: "Trez".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-office".to_owned(),
                    name: "Office".to_owned(),
                    ssid: "Office".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-guest".to_owned(),
                    name: "Guest".to_owned(),
                    ssid: "Guest".to_owned(),
                },
            ],
            priority_state: Mutex::new(HashMap::from([
                ("uuid-netplan".to_owned(), (false, 500)),
                ("uuid-office".to_owned(), (false, 400)),
                ("uuid-guest".to_owned(), (false, 300)),
            ])),
            ..FakeOps::default()
        };
        let req = PriorityReq {
            order: vec!["Office".to_owned(), "Trez".to_owned(), "Guest".to_owned()],
        };
        let resp = priority_flow(&ops, &req).expect("all profiles updated");
        assert_eq!(resp, PriorityResp { ok: true, count: 3 });
        let state = ops.priority_state.lock().expect("priority state lock");
        assert_eq!(state.get("uuid-office"), Some(&(true, 3)));
        assert_eq!(state.get("uuid-netplan"), Some(&(true, 2)));
        assert_eq!(state.get("uuid-guest"), Some(&(true, 1)));
    }

    #[test]
    fn priority_reorder_unknown_ssid_rejected() {
        let ops = FakeOps {
            saved_wifi: vec![
                SavedProfile {
                    uuid: "uuid-trez".to_owned(),
                    name: "netplan-wlan0-Trez".to_owned(),
                    ssid: "Trez".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-a".to_owned(),
                    name: "A-profile".to_owned(),
                    ssid: "A".to_owned(),
                },
            ],
            priority_state: Mutex::new(HashMap::from([
                ("uuid-trez".to_owned(), (true, 0)),
                ("uuid-a".to_owned(), (true, 0)),
            ])),
            ..FakeOps::default()
        };
        let req = PriorityReq {
            order: vec!["Unknown".to_owned()],
        };
        let err = priority_flow(&ops, &req).expect_err("unknown ssid");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(code, "invalid_order");
        assert!(
            !ops.calls()
                .iter()
                .any(|call| call.starts_with("set_conn_priority:"))
        );
    }

    #[test]
    fn priority_reorder_missing_ssid_rejected() {
        let ops = FakeOps {
            saved_wifi: vec![
                SavedProfile {
                    uuid: "uuid-trez".to_owned(),
                    name: "netplan-wlan0-Trez".to_owned(),
                    ssid: "Trez".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-a".to_owned(),
                    name: "A-profile".to_owned(),
                    ssid: "A".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-b".to_owned(),
                    name: "B-profile".to_owned(),
                    ssid: "B".to_owned(),
                },
            ],
            priority_state: Mutex::new(HashMap::from([
                ("uuid-trez".to_owned(), (true, 0)),
                ("uuid-a".to_owned(), (true, 0)),
                ("uuid-b".to_owned(), (true, 0)),
            ])),
            ..FakeOps::default()
        };
        let req = PriorityReq {
            order: vec!["A".to_owned(), "Trez".to_owned()],
        };
        let err = priority_flow(&ops, &req).expect_err("missing ssid");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(code, "invalid_order");
        assert!(
            !ops.calls()
                .iter()
                .any(|call| call.starts_with("set_conn_priority:"))
        );
    }

    #[test]
    fn priority_reorder_duplicate_ssid_rejected() {
        let ops = FakeOps {
            saved_wifi: vec![
                SavedProfile {
                    uuid: "uuid-trez".to_owned(),
                    name: "netplan-wlan0-Trez".to_owned(),
                    ssid: "Trez".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-a".to_owned(),
                    name: "A-profile".to_owned(),
                    ssid: "A".to_owned(),
                },
            ],
            priority_state: Mutex::new(HashMap::from([
                ("uuid-trez".to_owned(), (true, 0)),
                ("uuid-a".to_owned(), (true, 0)),
            ])),
            ..FakeOps::default()
        };
        let req = PriorityReq {
            order: vec!["A".to_owned(), "A".to_owned()],
        };
        let err = priority_flow(&ops, &req).expect_err("duplicate ssid");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(code, "invalid_order");
        assert!(
            !ops.calls()
                .iter()
                .any(|call| call.starts_with("set_conn_priority:"))
        );
    }

    #[test]
    fn priority_reorder_write_failure_maps_502() {
        let ops = FakeOps {
            saved_wifi: vec![SavedProfile {
                uuid: "uuid-trez".to_owned(),
                name: "netplan-wlan0-Trez".to_owned(),
                ssid: "Trez".to_owned(),
            }],
            priority_state: Mutex::new(HashMap::from([("uuid-trez".to_owned(), (true, 0))])),
            set_priority_result: Err(WifiOpsError::Failed("set failed".to_owned())),
            ..FakeOps::default()
        };
        let req = PriorityReq {
            order: vec!["Trez".to_owned()],
        };
        let err = priority_flow(&ops, &req).expect_err("write failure");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::BAD_GATEWAY);
        assert_eq!(code, "wifi_priority_failed");
    }

    #[test]
    fn priority_reorder_verify_failure_maps_502() {
        let ops = FakeOps {
            saved_wifi: vec![SavedProfile {
                uuid: "uuid-trez".to_owned(),
                name: "netplan-wlan0-Trez".to_owned(),
                ssid: "Trez".to_owned(),
            }],
            priority_state: Mutex::new(HashMap::from([("uuid-trez".to_owned(), (true, 0))])),
            priority_read_override: Some(("uuid-trez".to_owned(), (true, 0))),
            ..FakeOps::default()
        };
        let req = PriorityReq {
            order: vec!["Trez".to_owned()],
        };
        let err = priority_flow(&ops, &req).expect_err("verify failure");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::BAD_GATEWAY);
        assert_eq!(code, "wifi_priority_verify_failed");
    }

    #[test]
    fn priority_reorder_duplicate_saved_ssids_share_same_priority() {
        let ops = FakeOps {
            saved_wifi: vec![
                SavedProfile {
                    uuid: "uuid-trez".to_owned(),
                    name: "netplan-wlan0-Trez".to_owned(),
                    ssid: "Trez".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-a-1".to_owned(),
                    name: "A-profile-1".to_owned(),
                    ssid: "A".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-a-2".to_owned(),
                    name: "A-profile-2".to_owned(),
                    ssid: "A".to_owned(),
                },
            ],
            priority_state: Mutex::new(HashMap::from([
                ("uuid-trez".to_owned(), (true, 0)),
                ("uuid-a-1".to_owned(), (true, 0)),
                ("uuid-a-2".to_owned(), (true, 0)),
            ])),
            ..FakeOps::default()
        };
        let req = PriorityReq {
            order: vec!["A".to_owned(), "Trez".to_owned()],
        };
        let resp = priority_flow(&ops, &req).expect("duplicate saved ssids");
        assert_eq!(resp, PriorityResp { ok: true, count: 3 });
        let state = ops.priority_state.lock().expect("priority state lock");
        assert_eq!(state.get("uuid-a-1"), Some(&(true, 2)));
        assert_eq!(state.get("uuid-a-2"), Some(&(true, 2)));
        assert_eq!(state.get("uuid-trez"), Some(&(true, 1)));
    }

    #[test]
    fn select_happy_path_activates_and_commits() {
        let ops = FakeOps {
            active_uuid: Some("uuid-prev".to_owned()),
            saved_wifi: vec![
                SavedProfile {
                    uuid: "uuid-prev".to_owned(),
                    name: "Prev".to_owned(),
                    ssid: "Prev".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-target".to_owned(),
                    name: "Target".to_owned(),
                    ssid: "Target".to_owned(),
                },
            ],
            verify_result: Some("192.168.4.99".to_owned()),
            ..FakeOps::default()
        };
        let req = SelectReq {
            ssid: "Target".to_owned(),
        };
        let resp = select_flow(&ops, &req).expect("select happy path");
        assert_eq!(
            resp,
            SelectResp {
                connected: true,
                ssid: "Target".to_owned(),
                ip: Some("192.168.4.99".to_owned()),
            }
        );
        assert_eq!(
            ops.calls(),
            vec![
                "list_saved_wifi",
                "active_uuid_on_wlan0",
                "write_hold",
                "create_checkpoint",
                "activate:uuid-target",
                "verify_active_ip",
                "destroy_checkpoint",
                "active_uuid_on_wlan0",
                "clear_hold"
            ]
        );
        assert!(
            !ops.calls()
                .iter()
                .any(|call| call.starts_with("delete_profile_unprotected:"))
        );
    }

    #[test]
    fn select_timeout_rolls_back_to_previous() {
        let ops = FakeOps {
            active_uuid: Some("uuid-prev".to_owned()),
            rollback_active_uuid: Some("uuid-prev".to_owned()),
            saved_wifi: vec![
                SavedProfile {
                    uuid: "uuid-prev".to_owned(),
                    name: "Prev".to_owned(),
                    ssid: "Prev".to_owned(),
                },
                SavedProfile {
                    uuid: "uuid-target".to_owned(),
                    name: "Target".to_owned(),
                    ssid: "Target".to_owned(),
                },
            ],
            verify_result: None,
            ..FakeOps::default()
        };
        let req = SelectReq {
            ssid: "Target".to_owned(),
        };
        let err = select_flow(&ops, &req).expect_err("select timeout");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::GATEWAY_TIMEOUT);
        assert_eq!(code, "wifi_select_timeout");
        let calls = ops.calls();
        assert!(calls.contains(&"rollback_checkpoint".to_owned()));
        assert!(calls.contains(&"active_uuid_on_wlan0".to_owned()));
        assert!(calls.contains(&"clear_hold".to_owned()));
        assert!(
            !calls
                .iter()
                .any(|call| call.starts_with("delete_profile_unprotected:"))
        );
    }

    #[test]
    fn select_not_found_when_ssid_missing() {
        let ops = FakeOps {
            active_uuid: Some("uuid-prev".to_owned()),
            saved_wifi: vec![SavedProfile {
                uuid: "uuid-prev".to_owned(),
                name: "Prev".to_owned(),
                ssid: "Prev".to_owned(),
            }],
            ..FakeOps::default()
        };
        let req = SelectReq {
            ssid: "Missing".to_owned(),
        };
        let err = select_flow(&ops, &req).expect_err("missing profile");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert_eq!(code, "not_found");
    }

    #[test]
    fn select_refused_when_not_connected() {
        let ops = FakeOps {
            active_uuid: None,
            saved_wifi: vec![SavedProfile {
                uuid: "uuid-target".to_owned(),
                name: "Target".to_owned(),
                ssid: "Target".to_owned(),
            }],
            ..FakeOps::default()
        };
        let req = SelectReq {
            ssid: "Target".to_owned(),
        };
        let err = select_flow(&ops, &req).expect_err("not connected");
        let (status, code) = status_and_code(&err);
        assert_eq!(status, StatusCode::CONFLICT);
        assert_eq!(code, "precondition_failed");
    }

    #[test]
    fn select_already_active_short_circuits() {
        let ops = FakeOps {
            active_uuid: Some("uuid-target".to_owned()),
            saved_wifi: vec![SavedProfile {
                uuid: "uuid-target".to_owned(),
                name: "Target".to_owned(),
                ssid: "Target".to_owned(),
            }],
            verify_result: Some("192.168.4.22".to_owned()),
            ..FakeOps::default()
        };
        let req = SelectReq {
            ssid: "Target".to_owned(),
        };
        let resp = select_flow(&ops, &req).expect("already active");
        assert_eq!(
            resp,
            SelectResp {
                connected: true,
                ssid: "Target".to_owned(),
                ip: Some("192.168.4.22".to_owned()),
            }
        );
        let calls = ops.calls();
        assert_eq!(
            calls,
            vec![
                "list_saved_wifi",
                "active_uuid_on_wlan0",
                "verify_active_ip"
            ]
        );
        assert!(!calls.contains(&"create_checkpoint".to_owned()));
    }
}
