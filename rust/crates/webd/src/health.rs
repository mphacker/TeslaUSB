//! Device-status handlers: system health, live metrics, storage, and storage
//! health (`webd.md` §2.2 / §3). Every reading comes from the read-only
//! [`SystemProbe`] in [`crate::sysinfo`] — nothing here writes or shells out.
//! Each handler offloads its blocking `/proc`/`statvfs` reads onto a blocking
//! task, mirroring the catalog read handlers in [`crate::route`].
//!
//! These endpoints never error: a probe that cannot read a fact reports it as
//! `unknown`/`null` so the SPA's device-status sections degrade gracefully
//! (and the zero-console UAT gate holds) instead of surfacing a `5xx`.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::Router;
use axum::extract::State;
use axum::routing::get;

use crate::AppState;
use crate::sysinfo::{self, Storage, StorageHealth, SystemHealth, SystemMetrics};

/// The device-status sub-routes, mounted under `/api` by [`crate::route`].
pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/system/health", get(system_health))
        .route("/system/metrics", get(system_metrics))
        .route("/storage", get(storage))
        .route("/storage/health", get(storage_health))
}

/// Current wall-clock time as epoch seconds, or `None` if the clock is before
/// the epoch.
fn now_epoch() -> Option<u64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .map(|d| d.as_secs())
}

async fn system_health(State(state): State<AppState>) -> Json<SystemHealth> {
    let sys = state.sys;
    let now = now_epoch()
        .and_then(|s| i64::try_from(s).ok())
        .unwrap_or(0);
    let out = tokio::task::spawn_blocking(move || {
        sysinfo::system_health(sys.probe.as_ref(), sys.paths.as_ref(), now)
    })
    .await
    .unwrap_or_else(|_| SystemHealth::degraded());
    Json(out)
}

async fn system_metrics(State(state): State<AppState>) -> Json<SystemMetrics> {
    let sys = state.sys;
    let now = now_epoch();
    let out = tokio::task::spawn_blocking(move || sysinfo::system_metrics(sys.probe.as_ref(), now))
        .await
        .unwrap_or(SystemMetrics {
            uptime_s: None,
            load: None,
            mem: None,
            swap: None,
            cpu_temp_c: None,
            updated_at: now,
        });
    Json(out)
}

async fn storage(State(state): State<AppState>) -> Json<Storage> {
    let sys = state.sys;
    let out = tokio::task::spawn_blocking(move || {
        sysinfo::storage(sys.probe.as_ref(), sys.paths.as_ref())
    })
    .await
    .unwrap_or_else(|_| Storage {
        filesystems: Vec::new(),
        governor: None,
    });
    Json(out)
}

async fn storage_health(State(state): State<AppState>) -> Json<StorageHealth> {
    let sys = state.sys;
    let out = tokio::task::spawn_blocking(move || {
        sysinfo::storage_health(sys.probe.as_ref(), sys.paths.as_ref())
    })
    .await
    .unwrap_or_else(|_| StorageHealth::unavailable());
    Json(out)
}
