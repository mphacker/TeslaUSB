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

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/retention/status", get(status))
        .route("/retention/preview", get(preview))
}

fn candidate_request(
    indexd: &dyn crate::indexd_client::IndexdClient,
    governor: &serde_json::Value,
    limit: u32,
) -> Option<serde_json::Value> {
    let floor_secs = governor.get("recency_floor_secs")?.as_i64()?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let floor = i64::try_from(now).ok()?.saturating_sub(floor_secs);
    let allow_undurable = governor.get("mode").and_then(|mode| mode.as_str()) == Some("armed");
    indexd
        .call(json!({
            "cmd": "list_eviction_candidates",
            "recency_floor_epoch": floor,
            "allow_undurable": allow_undurable,
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

async fn status(State(state): State<AppState>) -> Json<RetentionStatus> {
    let sys = state.sys;
    let governor = tokio::task::spawn_blocking(move || {
        crate::sysinfo::retention_governor(sys.probe.as_ref(), sys.paths.as_ref())
    })
    .await
    .ok()
    .flatten();
    let governor_for_count = governor.clone();
    let indexd = state.indexd;
    let candidate = tokio::task::spawn_blocking(move || {
        let governor = governor_for_count.as_ref()?;
        let response = candidate_request(indexd.as_ref(), governor, CANDIDATE_SAMPLE_LIMIT)?;
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
        let response = candidate_request(indexd.as_ref(), governor, limit)?;
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

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]
mod tests {
    use super::{cleanup_history, summarize_candidates};
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
}
