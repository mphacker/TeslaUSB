use axum::extract::{Query, State};
use axum::routing::get;
use axum::{Json, Router};
use rusqlite::params;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::AppState;

const CANDIDATE_SAMPLE_LIMIT: u32 = 256;
const CLEANUP_HISTORY_LIMIT: usize = 8;
const CLOUD_DURABILITY_DISCLOSURE: &str =
    "Armed local cleanup may delete footage before cloud upload confirmation.";

#[derive(Debug, Serialize)]
struct RetentionStatus {
    governor: Option<serde_json::Value>,
    candidate_count: Option<i64>,
    candidate_count_truncated: bool,
    estimated_reclaimable_bytes: Option<i64>,
    estimated_reclaimable_bytes_truncated: bool,
    operator_signal: RetentionOperatorSignal,
    exclusion_report: Option<ExclusionReport>,
    recent_cleanup: Option<Vec<CleanupHistoryEntry>>,
    recent_cleanup_truncated: bool,
    cloud_durability_required: bool,
    cloud_durability_disclosure: &'static str,
}

#[derive(Debug, Deserialize)]
struct PreviewQuery {
    limit: Option<u32>,
}

#[derive(Debug, Serialize)]
struct RetentionCandidate {
    id: i64,
    size_bytes: i64,
    archived_at: i64,
    folder_class: String,
}

#[derive(Debug, Serialize)]
struct RetentionPreview {
    status: &'static str,
    items: Vec<RetentionCandidate>,
}

#[derive(Debug, Serialize)]
struct RetentionPolicyResponse {
    status: &'static str,
    snapshot: Option<RetentionPolicySnapshot>,
}

#[derive(Debug, Serialize)]
struct RetentionPolicySnapshot {
    effective_mode: String,
    target_exit_frac: f64,
    target_free_frac: Option<f64>,
    recency_floor_secs: i64,
    per_cycle_evict_bytes: u64,
    per_cycle_evict_count: u64,
    per_cycle_wall_ms: Option<u64>,
    source: &'static str,
}

#[derive(Debug, Clone, Serialize)]
struct CleanupHistoryEntry {
    at: i64,
    items: i64,
    bytes_freed: i64,
}

#[derive(Debug)]
struct CandidateSummary {
    count: i64,
    count_truncated: bool,
    estimated_reclaimable_bytes: i64,
}

#[derive(Debug, Clone, Serialize)]
struct RetentionOperatorSignal {
    status: &'static str,
    free_frac: Option<f64>,
    target_exit_frac: Option<f64>,
    pressure_below_target_exit: Option<bool>,
    retention_non_progress: Option<bool>,
    no_eligible_candidates: Option<bool>,
    stop_indicates_no_progress: Option<bool>,
}

#[derive(Debug, Clone, Serialize)]
struct ExclusionReport {
    // Lease-protected rows are claim-blocked even when they appear in the
    // read-only candidate list, so the report describes both eligibility and
    // the final claim gate.
    sample_limit: u32,
    sample_size: i64,
    sample_truncated: bool,
    reasons: Vec<ExclusionReasonSummary>,
}

#[derive(Debug, Clone, Serialize)]
struct ExclusionReasonSummary {
    reason: String,
    count: i64,
    size_bytes: i64,
}

#[derive(Debug, Clone, Copy)]
struct EligibilityArgs {
    recency_floor_epoch: i64,
    allow_undurable: bool,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/retention/status", get(status))
        .route("/retention/policy", get(policy))
        .route("/retention/preview", get(preview))
}

fn retention_policy_snapshot(governor: &serde_json::Value) -> Option<RetentionPolicySnapshot> {
    let per_cycle_evict_bytes = governor.get("per_cycle_evict_bytes")?.as_u64()?;
    let per_cycle_evict_count = governor.get("per_cycle_evict_count")?.as_u64()?;
    Some(RetentionPolicySnapshot {
        effective_mode: governor.get("mode")?.as_str()?.to_owned(),
        target_exit_frac: governor.get("target_exit_frac")?.as_f64()?,
        target_free_frac: governor
            .get("target_free_frac")
            .and_then(|value| value.as_f64()),
        recency_floor_secs: governor.get("recency_floor_secs")?.as_i64()?,
        per_cycle_evict_bytes,
        per_cycle_evict_count,
        per_cycle_wall_ms: governor
            .get("per_cycle_wall_ms")
            .and_then(|value| value.as_u64()),
        source: "retentiond_governor",
    })
}

fn eligibility_args(governor: &serde_json::Value) -> Option<EligibilityArgs> {
    let floor_secs = governor.get("recency_floor_secs")?.as_i64()?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let now_epoch = i64::try_from(now).ok()?;
    let recency_floor_epoch = now_epoch.saturating_sub(floor_secs);
    let allow_undurable = matches!(
        governor.get("mode").and_then(|mode| mode.as_str()),
        Some("armed" | "dryrun")
    );
    Some(EligibilityArgs {
        recency_floor_epoch,
        allow_undurable,
    })
}

fn candidate_request(
    indexd: &dyn crate::indexd_client::IndexdClient,
    eligibility: EligibilityArgs,
    limit: u32,
) -> Option<serde_json::Value> {
    indexd
        .call(json!({
            "cmd": "list_eviction_candidates",
            "recency_floor_epoch": eligibility.recency_floor_epoch,
            "allow_undurable": eligibility.allow_undurable,
            "limit": limit
        }))
        .ok()
        .filter(|response| {
            response.get("status").and_then(|value| value.as_str()) == Some("eviction_candidates")
        })
}

fn summarize_candidates(response: &serde_json::Value, limit: u32) -> Option<CandidateSummary> {
    let items = response.get("items")?.as_array()?;
    let count = i64::try_from(items.len()).ok()?;
    let estimated_reclaimable_bytes = items.iter().try_fold(0i64, |acc, item| {
        let size_bytes = item.get("size_bytes")?.as_i64()?;
        if size_bytes < 0 {
            return None;
        }
        Some(acc.saturating_add(size_bytes))
    })?;
    Some(CandidateSummary {
        count,
        count_truncated: items.len() >= limit as usize,
        estimated_reclaimable_bytes,
    })
}

fn stop_indicates_no_progress(last_stop: &str) -> Option<bool> {
    match last_stop {
        "no_safe_candidate" | "anomaly_refused" | "delete_failed" | "stat_check_failed"
        | "shutdown" => Some(true),
        "already_healthy" | "target_reached" | "byte_cap" | "count_cap" | "wall_cap" => Some(false),
        _ => None,
    }
}

fn operator_signal(
    governor: Option<&serde_json::Value>,
    candidate: Option<&CandidateSummary>,
) -> RetentionOperatorSignal {
    let governor = match governor {
        Some(value) => value,
        None => {
            return RetentionOperatorSignal {
                status: "unavailable",
                free_frac: None,
                target_exit_frac: None,
                pressure_below_target_exit: None,
                retention_non_progress: None,
                no_eligible_candidates: None,
                stop_indicates_no_progress: None,
            };
        }
    };

    let free_bytes = governor.get("free_bytes").and_then(|value| value.as_f64());
    let total_bytes = governor.get("total_bytes").and_then(|value| value.as_f64());
    let free_frac = match (free_bytes, total_bytes) {
        (Some(free), Some(total)) if total > 0.0 => Some((free / total).clamp(0.0, 1.0)),
        _ => None,
    };
    let target_exit_frac = governor
        .get("target_exit_frac")
        .and_then(|value| value.as_f64())
        .filter(|value| (0.0..=1.0).contains(value));
    let pressure_below_target_exit = free_frac
        .zip(target_exit_frac)
        .map(|(free, target_exit)| free < target_exit);

    let no_eligible_candidates = candidate.map(|summary| summary.count == 0);
    let stop_indicates_no_progress = governor
        .get("last_stop")
        .and_then(|value| value.as_str())
        .and_then(stop_indicates_no_progress);
    let retention_non_progress = candidate.map(|summary| {
        summary.count == 0 || stop_indicates_no_progress.unwrap_or(false)
    });

    let status = if pressure_below_target_exit == Some(true) && retention_non_progress == Some(true)
    {
        "warning"
    } else if pressure_below_target_exit.is_some() && retention_non_progress.is_some() {
        "ok"
    } else {
        "unavailable"
    };

    RetentionOperatorSignal {
        status,
        free_frac,
        target_exit_frac,
        pressure_below_target_exit,
        retention_non_progress,
        no_eligible_candidates,
        stop_indicates_no_progress,
    }
}

fn cleanup_history(
    catalog: &crate::Catalog,
    limit: usize,
) -> Option<(Vec<CleanupHistoryEntry>, bool)> {
    let conn = catalog.connect().ok()?;
    let mut stmt = conn
        .prepare(
            "SELECT updated_at,
                    COUNT(*) AS item_count,
                    SUM(
                      CASE
                        WHEN bytes_freed IS NOT NULL AND bytes_freed >= 0 THEN bytes_freed
                        WHEN size_bytes >= 0 THEN size_bytes
                        ELSE 0
                      END
                    ) AS bytes_freed
               FROM archive_items
              WHERE delete_state = 'DELETED'
                AND updated_at > 0
              GROUP BY updated_at
              ORDER BY updated_at DESC
              LIMIT ?1",
        )
        .ok()?;
    let mut rows = stmt
        .query_map(
            params![i64::try_from(limit.saturating_add(1)).ok()?],
            |row| {
                Ok(CleanupHistoryEntry {
                    at: row.get(0)?,
                    items: row.get(1)?,
                    bytes_freed: row.get(2)?,
                })
            },
        )
        .ok()?
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    let truncated = rows.len() > limit;
    rows.truncate(limit);
    Some((rows, truncated))
}

fn exclusion_report_request(
    indexd: &dyn crate::indexd_client::IndexdClient,
    eligibility: EligibilityArgs,
    limit: u32,
) -> Option<ExclusionReport> {
    let response = indexd
        .call(json!({
            "cmd": "list_eviction_exclusion_report",
            "recency_floor_epoch": eligibility.recency_floor_epoch,
            "allow_undurable": eligibility.allow_undurable,
            "limit": limit,
        }))
        .ok()?;
    if response.get("status").and_then(|value| value.as_str()) != Some("eviction_exclusion_report")
    {
        return None;
    }
    let sample_size = response.get("sample_size")?.as_i64()?;
    if sample_size < 0 {
        return None;
    }
    let sample_truncated = response.get("sample_truncated")?.as_bool()?;
    let reasons = response
        .get("reasons")?
        .as_array()?
        .iter()
        .map(|reason| {
            Some(ExclusionReasonSummary {
                reason: reason.get("reason")?.as_str()?.to_owned(),
                count: reason.get("count")?.as_i64()?,
                size_bytes: reason.get("size_bytes")?.as_i64()?,
            })
        })
        .collect::<Option<Vec<_>>>()?;
    Some(ExclusionReport {
        sample_limit: limit,
        sample_size,
        sample_truncated,
        reasons,
    })
}

async fn status(State(state): State<AppState>) -> Json<RetentionStatus> {
    let sys = state.sys;
    let governor = tokio::task::spawn_blocking(move || {
        crate::sysinfo::retention_governor(sys.probe.as_ref(), sys.paths.as_ref())
    })
    .await
    .ok()
    .flatten();
    let governor_for_exclusions = governor.clone();
    let indexd = state.indexd;
    let indexd_for_exclusions = indexd.clone();
    let exclusions = tokio::task::spawn_blocking(move || {
        let governor = governor_for_exclusions.as_ref()?;
        let eligibility = eligibility_args(governor)?;
        exclusion_report_request(
            indexd_for_exclusions.as_ref(),
            eligibility,
            CANDIDATE_SAMPLE_LIMIT,
        )
    })
    .await
    .ok()
    .flatten();
    let governor_for_count = governor.clone();
    let candidate = tokio::task::spawn_blocking(move || {
        let governor = governor_for_count.as_ref()?;
        let eligibility = eligibility_args(governor)?;
        let response = candidate_request(indexd.as_ref(), eligibility, CANDIDATE_SAMPLE_LIMIT)?;
        summarize_candidates(&response, CANDIDATE_SAMPLE_LIMIT)
    })
    .await
    .ok()
    .flatten();
    let catalog = state.catalog;
    let recent_cleanup =
        tokio::task::spawn_blocking(move || cleanup_history(&catalog, CLEANUP_HISTORY_LIMIT))
            .await
            .ok()
            .flatten();
    let operator_signal = operator_signal(governor.as_ref(), candidate.as_ref());

    Json(RetentionStatus {
        governor,
        candidate_count: candidate.as_ref().map(|summary| summary.count),
        candidate_count_truncated: candidate
            .as_ref()
            .map(|summary| summary.count_truncated)
            .unwrap_or(false),
        estimated_reclaimable_bytes: candidate
            .as_ref()
            .map(|summary| summary.estimated_reclaimable_bytes),
        estimated_reclaimable_bytes_truncated: candidate
            .as_ref()
            .map(|summary| summary.count_truncated)
            .unwrap_or(false),
        operator_signal,
        exclusion_report: exclusions,
        recent_cleanup: recent_cleanup.as_ref().map(|(rows, _)| rows.clone()),
        recent_cleanup_truncated: recent_cleanup
            .as_ref()
            .map(|(_, truncated)| *truncated)
            .unwrap_or(false),
        cloud_durability_required: false,
        cloud_durability_disclosure: CLOUD_DURABILITY_DISCLOSURE,
    })
}

async fn preview(
    State(state): State<AppState>,
    Query(query): Query<PreviewQuery>,
) -> Json<RetentionPreview> {
    let limit = query.limit.unwrap_or(32).clamp(1, 64);
    let sys = state.sys;
    let governor = tokio::task::spawn_blocking(move || {
        crate::sysinfo::retention_governor(sys.probe.as_ref(), sys.paths.as_ref())
    })
    .await
    .ok()
    .flatten();
    let indexd = state.indexd;
    let result = tokio::task::spawn_blocking(move || {
        let governor = governor.as_ref()?;
        let eligibility = eligibility_args(governor)?;
        let response = candidate_request(indexd.as_ref(), eligibility, limit)?;
        let items = response.get("items")?.as_array()?;
        Some(
            items
                .iter()
                .filter_map(|item| {
                    Some(RetentionCandidate {
                        id: item.get("id")?.as_i64()?,
                        size_bytes: item.get("size_bytes")?.as_i64()?,
                        archived_at: item.get("archived_at")?.as_i64()?,
                        folder_class: item.get("folder_class")?.as_str()?.to_owned(),
                    })
                })
                .collect(),
        )
    })
    .await
    .ok()
    .flatten();

    match result {
        Some(items) => Json(RetentionPreview {
            status: "ready",
            items,
        }),
        None => Json(RetentionPreview {
            status: "unavailable",
            items: Vec::new(),
        }),
    }
}

async fn policy(State(state): State<AppState>) -> Json<RetentionPolicyResponse> {
    let sys = state.sys;
    let snapshot = tokio::task::spawn_blocking(move || {
        let governor = crate::sysinfo::retention_governor(sys.probe.as_ref(), sys.paths.as_ref())?;
        retention_policy_snapshot(&governor)
    })
    .await
    .ok()
    .flatten();

    match snapshot {
        Some(snapshot) => Json(RetentionPolicyResponse {
            status: "ready",
            snapshot: Some(snapshot),
        }),
        None => Json(RetentionPolicyResponse {
            status: "unavailable",
            snapshot: None,
        }),
    }
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]
mod tests {
    use super::{cleanup_history, operator_signal, summarize_candidates};
    use crate::Catalog;
    use serde_json::json;

    fn seed_catalog() -> (tempfile::TempDir, std::path::PathBuf, Catalog) {
        let dir = tempfile::tempdir().expect("tempdir");
        let db_path = dir.path().join("catalog.db");
        let mut conn = rusqlite::Connection::open(&db_path).expect("open db");
        indexd::db::apply_migrations(&mut conn).expect("apply migrations");
        let catalog = Catalog::open(&db_path).expect("open catalog read-only");
        (dir, db_path, catalog)
    }

    #[test]
    fn summarize_candidates_reports_count_bytes_and_truncation() {
        let response = json!({
            "status": "eviction_candidates",
            "items": [
                { "id": 1, "size_bytes": 1024, "archived_at": 100, "folder_class": "RecentClips" },
                { "id": 2, "size_bytes": 2048, "archived_at": 90, "folder_class": "RecentClips" }
            ]
        });
        let summary = summarize_candidates(&response, 2).expect("summary");
        assert_eq!(summary.count, 2);
        assert_eq!(summary.estimated_reclaimable_bytes, 3072);
        assert!(summary.count_truncated);
    }

    #[test]
    fn cleanup_history_groups_by_timestamp_and_is_bounded() {
        let (_dir, db_path, catalog) = seed_catalog();
        {
            let conn = rusqlite::Connection::open(db_path).expect("open writable db");
            conn.execute(
                "INSERT INTO archive_items
                 (id, folder_class, path, size_bytes, file_count, archived_at, delete_state, bytes_freed, durable, pinned, user_disposable, has_event_json, has_geo, sentry_flood, created_at, updated_at)
                 VALUES (?1, 'RecentClips', ?2, 100, 1, 1, 'DELETED', ?3, 1, 0, 0, 0, 0, 0, 1, ?4)",
                rusqlite::params![1i64, "archive/a", 400i64, 1_700_000_100i64],
            )
            .expect("insert deleted row #1");
            conn.execute(
                "INSERT INTO archive_items
                 (id, folder_class, path, size_bytes, file_count, archived_at, delete_state, bytes_freed, durable, pinned, user_disposable, has_event_json, has_geo, sentry_flood, created_at, updated_at)
                 VALUES (?1, 'RecentClips', ?2, 100, 1, 1, 'DELETED', ?3, 1, 0, 0, 0, 0, 0, 1, ?4)",
                rusqlite::params![2i64, "archive/b", 600i64, 1_700_000_100i64],
            )
            .expect("insert deleted row #2");
            conn.execute(
                "INSERT INTO archive_items
                 (id, folder_class, path, size_bytes, file_count, archived_at, delete_state, bytes_freed, durable, pinned, user_disposable, has_event_json, has_geo, sentry_flood, created_at, updated_at)
                 VALUES (?1, 'RecentClips', ?2, 100, 1, 1, 'DELETED', ?3, 1, 0, 0, 0, 0, 0, 1, ?4)",
                rusqlite::params![3i64, "archive/c", 1200i64, 1_700_000_050i64],
            )
            .expect("insert deleted row #3");
        }
        let (history, truncated) = cleanup_history(&catalog, 1).expect("history available");
        assert!(truncated);
        assert_eq!(history.len(), 1);
        assert_eq!(history[0].at, 1_700_000_100);
        assert_eq!(history[0].items, 2);
        assert_eq!(history[0].bytes_freed, 1000);
    }

    #[test]
    fn operator_signal_warns_on_low_space_and_no_progress() {
        let governor = json!({
            "free_bytes": 5,
            "total_bytes": 100,
            "target_exit_frac": 0.10,
            "last_stop": "no_safe_candidate"
        });
        let candidate = super::CandidateSummary {
            count: 0,
            count_truncated: false,
            estimated_reclaimable_bytes: 0,
        };
        let signal = operator_signal(Some(&governor), Some(&candidate));
        assert_eq!(signal.status, "warning");
        assert_eq!(signal.pressure_below_target_exit, Some(true));
        assert_eq!(signal.retention_non_progress, Some(true));
        assert_eq!(signal.no_eligible_candidates, Some(true));
        assert_eq!(signal.stop_indicates_no_progress, Some(true));
    }

    #[test]
    fn operator_signal_is_unavailable_when_governor_missing() {
        let signal = operator_signal(None, None);
        assert_eq!(signal.status, "unavailable");
        assert_eq!(signal.pressure_below_target_exit, None);
        assert_eq!(signal.retention_non_progress, None);
        assert_eq!(signal.no_eligible_candidates, None);
        assert_eq!(signal.stop_indicates_no_progress, None);
    }

    #[test]
    fn operator_signal_is_unavailable_when_candidates_missing() {
        let governor = json!({
            "free_bytes": 5,
            "total_bytes": 100,
            "target_exit_frac": 0.10,
            "last_stop": "no_safe_candidate"
        });
        let signal = operator_signal(Some(&governor), None);
        assert_eq!(signal.status, "unavailable");
        assert_eq!(signal.pressure_below_target_exit, Some(true));
        assert_eq!(signal.retention_non_progress, None);
    }
}
