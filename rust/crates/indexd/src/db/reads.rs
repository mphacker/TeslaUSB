//! indexd read-side queries for eviction and crash-recovery workflows.

use std::collections::HashMap;

use rusqlite::{Connection, params};

use crate::db::{DbError, PROVEN_DURABLE_PROOF_SQL};

/// Hard cap for server-exposed list queries to bound frame size.
const MAX_LIST_ROWS: u32 = 512;

/// One safe eviction candidate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EvictionCandidate {
    /// Archive item id.
    pub id: i64,
    /// Archive-root-relative path.
    pub path: String,
    /// Archive bytes.
    pub size_bytes: i64,
    /// Archive completion epoch seconds.
    pub archived_at: i64,
    /// Source folder class.
    pub folder_class: String,
}

/// One row requiring delete-state recovery handling.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoveryRow {
    /// Archive item id.
    pub id: i64,
    /// Current delete state.
    pub delete_state: String,
    /// Archive-root-relative path.
    pub path: String,
    /// Archive bytes.
    pub size_bytes: i64,
    /// Delete generation token (if present).
    pub delete_gen: Option<String>,
}

/// One stable exclusion-reason aggregate row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EvictionExclusionReason {
    /// Stable exclusion reason code.
    pub reason: String,
    /// Number of sampled rows carrying this reason.
    pub count: i64,
    /// Summed sampled bytes for this reason.
    pub size_bytes: i64,
}

/// Bounded eviction exclusion report.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EvictionExclusionReport {
    /// Number of sampled rows included before grouping.
    pub sample_size: i64,
    /// True when more rows existed than the sample limit.
    pub sample_truncated: bool,
    /// Stable grouped reason aggregates.
    pub reasons: Vec<EvictionExclusionReason>,
}

/// List strict hard-delete allowlist candidates with value-tiered ordering:
/// confirmed parked junk first, then SEI/GPS footage, with event footage last.
/// Eligibility gates are unchanged.
///
/// # Errors
///
/// Returns [`DbError`] if the query fails.
pub fn list_eviction_candidates(
    conn: &Connection,
    recency_floor_epoch: i64,
    now_epoch: i64,
    allow_undurable: bool,
    limit: u32,
) -> Result<Vec<EvictionCandidate>, DbError> {
    let capped = i64::from(limit.min(MAX_LIST_ROWS));
    // Recency is gated on clips.started_at (the true recording instant from the
    // Tesla filename/mvhd, epoch-seconds) — NOT archive_items.archived_at, whose
    // Pi wall-clock value is unreliable on a clock-less device. recency_floor_epoch
    // is likewise epoch-seconds. An item is a candidate only if EVERY linked clip is
    // RecentClips, non-Sentry, has a known (>0) start, and the NEWEST is older than
    // the floor; anything else fails closed (INNER JOIN + all-clip HAVING guards).
    let sql = format!(
        "SELECT ai.id, ai.path, ai.size_bytes, ai.archived_at, ai.folder_class
           FROM archive_items AS ai
           JOIN archive_item_clips AS aic ON aic.archive_item_id = ai.id
           JOIN clips AS c ON c.id = aic.clip_id
           LEFT JOIN front_parse_attempts AS fpa ON fpa.canonical_key = c.canonical_key
          WHERE ai.delete_state = 'LIVE'
            AND (?3 = 1 OR {PROVEN_DURABLE_PROOF_SQL})
            AND ai.pinned = 0
            AND ai.folder_class = 'RecentClips'
            AND (ai.suppress_until IS NULL OR ai.suppress_until < ?2)
          GROUP BY ai.id, ai.path, ai.size_bytes, ai.archived_at, ai.folder_class
         HAVING MIN(CASE WHEN c.folder_class = 'RecentClips' THEN 1 ELSE 0 END) = 1
            AND MAX(c.is_sentry) = 0
            AND MIN(CASE WHEN c.started_at > 0 THEN 1 ELSE 0 END) = 1
            AND MAX(c.started_at) < ?1
          ORDER BY
            (CASE
               WHEN MAX(CASE WHEN EXISTS(SELECT 1 FROM events e WHERE e.clip_id = c.id) THEN 1 ELSE 0 END) = 1 THEN 2
               WHEN MIN(CASE WHEN fpa.parse_state = 'no_waypoints' THEN 1 ELSE 0 END) = 1 THEN 0
               ELSE 1
             END) ASC,
            MIN(c.started_at) ASC, ai.id ASC
          LIMIT ?4"
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(
        params![
            recency_floor_epoch,
            now_epoch,
            i64::from(allow_undurable),
            capped
        ],
        |row| {
            Ok(EvictionCandidate {
                id: row.get(0)?,
                path: row.get(1)?,
                size_bytes: row.get(2)?,
                archived_at: row.get(3)?,
                folder_class: row.get(4)?,
            })
        },
    )?;

    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

/// List rows that are in transitional delete states and require recovery.
///
/// # Errors
///
/// Returns [`DbError`] if the query fails.
pub fn list_recovery_rows(conn: &Connection) -> Result<Vec<RecoveryRow>, DbError> {
    let mut stmt = conn.prepare(
        "SELECT id, delete_state, path, size_bytes, delete_gen
           FROM archive_items
          WHERE delete_state NOT IN ('LIVE','DELETED')
          ORDER BY id ASC
          LIMIT 512",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok(RecoveryRow {
            id: row.get(0)?,
            delete_state: row.get(1)?,
            path: row.get(2)?,
            size_bytes: row.get(3)?,
            delete_gen: row.get(4)?,
        })
    })?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

/// Build a bounded exclusion-reason report from the same server-side gates used
/// by eviction claim/candidate selection, including the active-lease gate.
///
/// # Errors
///
/// Returns [`DbError`] if the query fails.
pub fn eviction_exclusion_report(
    conn: &Connection,
    recency_floor_epoch: i64,
    now_epoch: i64,
    allow_undurable: bool,
    boot_id: &str,
    mono_now_ms: i64,
    limit: u32,
) -> Result<EvictionExclusionReport, DbError> {
    let capped_u32 = limit.min(MAX_LIST_ROWS);
    let capped = usize::try_from(capped_u32).unwrap_or(MAX_LIST_ROWS as usize);
    let sql = format!(
            "WITH classified AS (
                 SELECT ai.id,
                        MAX(CASE WHEN ai.size_bytes >= 0 THEN ai.size_bytes ELSE 0 END) AS size_bytes,
                        CASE
                          WHEN EXISTS (
                                 SELECT 1 FROM leases l
                                  WHERE l.archive_item_id = ai.id
                                    AND l.boot_id = ?4
                                    AND l.expires_mono_ms > ?5
                               ) THEN 'lease_active'
                          WHEN ai.delete_state <> 'LIVE' THEN 'not_live'
                          WHEN ai.pinned <> 0 THEN 'pinned'
                          WHEN ai.folder_class <> 'RecentClips' THEN 'item_not_recentclips'
                          WHEN ai.suppress_until IS NOT NULL AND ai.suppress_until >= ?2 THEN 'suppressed'
                          WHEN (?3 = 0 AND NOT ({PROVEN_DURABLE_PROOF_SQL})) THEN 'not_durable'
                          WHEN COUNT(c.id) = 0 THEN 'no_linked_clip'
                          WHEN MIN(CASE WHEN c.folder_class = 'RecentClips' THEN 1 ELSE 0 END) = 0 THEN 'linked_not_recentclips'
                          WHEN MAX(c.is_sentry) = 1 THEN 'linked_sentry'
                          WHEN MIN(CASE WHEN c.started_at > 0 THEN 1 ELSE 0 END) = 0 THEN 'missing_started_at'
                          WHEN MAX(c.started_at) >= ?1 THEN 'too_recent'
                          ELSE 'eligible'
                        END AS reason,
                        MIN(COALESCE(NULLIF(c.started_at, 0), 9223372036854775807)) AS oldest_started_at
                   FROM archive_items ai
                   LEFT JOIN archive_item_clips aic ON aic.archive_item_id = ai.id
                   LEFT JOIN clips c ON c.id = aic.clip_id
                  GROUP BY ai.id
            )
            SELECT id, reason, size_bytes
              FROM classified
             WHERE reason <> 'eligible'
             ORDER BY
               CASE reason
                 WHEN 'lease_active' THEN 0
                 WHEN 'not_live' THEN 1
                 WHEN 'pinned' THEN 2
                 WHEN 'item_not_recentclips' THEN 3
                 WHEN 'suppressed' THEN 4
                 WHEN 'not_durable' THEN 5
                 WHEN 'no_linked_clip' THEN 6
                 WHEN 'linked_not_recentclips' THEN 7
                 WHEN 'linked_sentry' THEN 8
                 WHEN 'missing_started_at' THEN 9
                 WHEN 'too_recent' THEN 10
                 ELSE 99
               END ASC,
               oldest_started_at ASC,
               id ASC
             LIMIT ?6"
        );
    let mut stmt = conn.prepare(&sql)?;
    let sample_plus_one = i64::from(capped_u32.saturating_add(1));
    let rows = stmt.query_map(
        params![
            recency_floor_epoch,
            now_epoch,
            i64::from(allow_undurable),
            boot_id,
            mono_now_ms,
            sample_plus_one
        ],
        |row| Ok((row.get::<_, String>(1)?, row.get::<_, i64>(2)?)),
    )?;
    let mut sampled = Vec::new();
    for row in rows {
        sampled.push(row?);
    }
    let sample_truncated = sampled.len() > capped;
    sampled.truncate(capped);
    let sample_size = i64::try_from(sampled.len()).unwrap_or(i64::MAX);

    let mut grouped: HashMap<String, EvictionExclusionReason> = HashMap::new();
    for (reason, size_bytes) in sampled {
        let entry = grouped
            .entry(reason.clone())
            .or_insert(EvictionExclusionReason {
                reason,
                count: 0,
                size_bytes: 0,
            });
        entry.count = entry.count.saturating_add(1);
        entry.size_bytes = entry.size_bytes.saturating_add(size_bytes.max(0));
    }

    let stable_order = [
        "lease_active",
        "not_live",
        "pinned",
        "item_not_recentclips",
        "suppressed",
        "not_durable",
        "no_linked_clip",
        "linked_not_recentclips",
        "linked_sentry",
        "missing_started_at",
        "too_recent",
    ];
    let mut reasons = Vec::new();
    for reason in stable_order {
        if let Some(item) = grouped.remove(reason) {
            reasons.push(item);
        }
    }
    let mut remaining = grouped.into_values().collect::<Vec<_>>();
    remaining.sort_by(|a, b| a.reason.cmp(&b.reason));
    reasons.extend(remaining);

    Ok(EvictionExclusionReport {
        sample_size,
        sample_truncated,
        reasons,
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::expect_used, clippy::unwrap_used, clippy::panic)]

    use rusqlite::{Connection, params};

    use super::{eviction_exclusion_report, list_eviction_candidates, list_recovery_rows};
    use crate::db::open_in_memory;

    #[derive(Debug, Clone)]
    struct ArchiveSeed<'a> {
        folder_class: &'a str,
        path: &'a str,
        size_bytes: i64,
        archived_at: i64,
        delete_state: &'a str,
        durable: i64,
        pinned: i64,
        suppress_until: Option<i64>,
        delete_gen: Option<&'a str>,
    }

    #[derive(Debug, Clone)]
    struct ProvingUploadSetFixture {
        upload_set_id: String,
        digest: String,
        destination_id: String,
        remote_key: String,
        child_key: String,
    }

    fn insert_archive_item_unlinked(conn: &Connection, seed: &ArchiveSeed<'_>) -> i64 {
        conn.execute(
            "INSERT INTO archive_items
                (folder_class, path, size_bytes, file_count, archived_at, delete_state,
                 durable, pinned, suppress_until, delete_gen, created_at, updated_at)
             VALUES (?1, ?2, ?3, 1, ?4, ?5, ?6, ?7, ?8, ?9, 0, 0)",
            params![
                seed.folder_class,
                seed.path,
                seed.size_bytes,
                seed.archived_at,
                seed.delete_state,
                seed.durable,
                seed.pinned,
                seed.suppress_until,
                seed.delete_gen
            ],
        )
        .expect("insert archive item");
        let archive_item_id = conn.last_insert_rowid();
        if seed.durable == 1 {
            seed_proving_upload_set(conn, archive_item_id);
        }
        archive_item_id
    }

    fn insert_linked_clip(
        conn: &Connection,
        archive_item_id: i64,
        canonical_key: &str,
        started_at: i64,
        folder_class: &str,
    ) -> i64 {
        conn.execute(
            "INSERT INTO clips (canonical_key, started_at, partition, folder_class, created_at, updated_at)
             VALUES (?1, ?2, 'p', ?3, 0, 0)",
            params![canonical_key, started_at, folder_class],
        )
        .expect("insert clip");
        let clip_id = conn.last_insert_rowid();
        conn.execute(
            "INSERT INTO archive_item_clips (archive_item_id, clip_id) VALUES (?1, ?2)",
            params![archive_item_id, clip_id],
        )
        .expect("insert archive-item clip link");
        clip_id
    }

    fn proving_upload_set_fixture(archive_item_id: i64) -> ProvingUploadSetFixture {
        ProvingUploadSetFixture {
            upload_set_id: format!("{archive_item_id:032x}"),
            digest: format!("{:032x}", archive_item_id + 0x1000),
            destination_id: format!("dest-{archive_item_id}"),
            remote_key: format!("remote-{archive_item_id}"),
            child_key: format!("child-{archive_item_id}"),
        }
    }

    fn seed_proving_upload_set(conn: &Connection, archive_item_id: i64) -> ProvingUploadSetFixture {
        let fixture = proving_upload_set_fixture(archive_item_id);
        let request_digest = format!("{archive_item_id:064x}");
        let content_sha256 = "a".repeat(64);
        conn.execute(
            "UPDATE archive_items SET manifest_digest = ?2 WHERE id = ?1",
            params![archive_item_id, fixture.digest.as_str()],
        )
        .expect("set manifest digest");
        conn.execute(
            "INSERT INTO cloud_parent_upload_sets
                (upload_set_id, archive_item_id, destination_id, source_manifest_digest, request_digest,
                 expected_child_count, created_at, finalized_at, superseded_at)
             VALUES (?1, ?2, ?3, ?4, ?5, 1, 0, 1, NULL)",
            params![
                fixture.upload_set_id.as_str(),
                archive_item_id,
                fixture.destination_id.as_str(),
                fixture.digest.as_str(),
                request_digest
            ],
        )
        .expect("insert upload set");
        conn.execute(
            "INSERT INTO cloud_parent_upload_set_children
                (upload_set_id, child_key, destination_id, remote_key, category, seq, total_bytes,
                 manifest_mtime_ms, content_sha256, expected_hash, verify_alg)
             VALUES (?1, ?2, ?3, ?4, 'bulk', 0, 4096, 0, ?5, 'expected-hash', 'sha256')",
            params![
                fixture.upload_set_id.as_str(),
                fixture.child_key.as_str(),
                fixture.destination_id.as_str(),
                fixture.remote_key.as_str(),
                content_sha256
            ],
        )
        .expect("insert upload child");
        conn.execute(
            "INSERT INTO cloud_upload_queue
                (archive_item_id, child_key, destination_id, remote_key, category, seq, total_bytes,
                 bytes_uploaded, expected_hash, verify_alg, content_sha256, state, attempts, upload_set_id)
             VALUES (?1, ?2, ?3, ?4, 'bulk', 0, 4096, 4096, 'expected-hash', 'sha256', ?5, 'done', 0, ?6)",
            params![
                archive_item_id,
                fixture.child_key.as_str(),
                fixture.destination_id.as_str(),
                fixture.remote_key.as_str(),
                content_sha256,
                fixture.upload_set_id.as_str()
            ],
        )
        .expect("insert upload queue row");
        fixture
    }

    fn insert_extra_queue_row_for_set(
        conn: &Connection,
        archive_item_id: i64,
        fixture: &ProvingUploadSetFixture,
    ) {
        let extra_child_key = format!("{}-extra", fixture.child_key);
        let extra_destination_id = format!("{}-extra", fixture.destination_id);
        let extra_remote_key = format!("{}-extra", fixture.remote_key);
        let extra_content_sha256 = "b".repeat(64);
        conn.execute(
            "INSERT INTO cloud_upload_queue
                (archive_item_id, child_key, destination_id, remote_key, category, seq, total_bytes,
                 bytes_uploaded, expected_hash, verify_alg, content_sha256, state, attempts, upload_set_id)
             VALUES (?1, ?2, ?3, ?4, 'bulk', 99, 1, 0, 'extra-expected-hash', 'sha256', ?5, 'done', 0, ?6)",
            params![
                archive_item_id,
                extra_child_key,
                extra_destination_id,
                extra_remote_key,
                extra_content_sha256,
                fixture.upload_set_id.as_str()
            ],
        )
        .expect("insert extra queue row");
    }

    fn break_arm_a(conn: &Connection, archive_item_id: i64, _: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE archive_items SET durable = 0 WHERE id = ?1",
            params![archive_item_id],
        )
        .expect("break arm a");
    }

    fn break_arm_f(conn: &Connection, archive_item_id: i64, _: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE archive_items SET manifest_digest = NULL WHERE id = ?1",
            params![archive_item_id],
        )
        .expect("break arm f");
    }

    fn break_arm_g(conn: &Connection, archive_item_id: i64, _: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE archive_items
                SET manifest_digest = 'ffffffffffffffffffffffffffffffff'
              WHERE id = ?1",
            params![archive_item_id],
        )
        .expect("break arm g");
    }

    fn break_arm_i(conn: &Connection, archive_item_id: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_parent_upload_sets
                SET expected_child_count = 2
              WHERE upload_set_id = ?1",
            params![fixture.upload_set_id.as_str()],
        )
        .expect("break arm i expected count");
        insert_extra_queue_row_for_set(conn, archive_item_id, fixture);
    }

    fn break_arm_j(conn: &Connection, archive_item_id: i64, fixture: &ProvingUploadSetFixture) {
        insert_extra_queue_row_for_set(conn, archive_item_id, fixture);
    }

    fn break_arm_k(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        let mismatched_remote_key = format!("{}-mismatch", fixture.remote_key);
        conn.execute(
            "UPDATE cloud_upload_queue
                SET remote_key = ?3
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![
                fixture.upload_set_id.as_str(),
                fixture.child_key.as_str(),
                mismatched_remote_key
            ],
        )
        .expect("break arm k");
    }

    fn break_arm_n(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET verify_alg = 'md5'
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .expect("break arm n");
    }

    fn break_arm_o_null(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET expected_hash = NULL
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .expect("break arm o null");
    }

    fn break_arm_o_differs(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET expected_hash = 'different-hash'
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .expect("break arm o differs");
    }

    fn break_arm_p(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET child_key = 'other-child'
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .expect("break arm p");
    }

    fn break_arm_q(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET category = 'trip'
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .expect("break arm q");
    }

    fn break_arm_r(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET seq = 1
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .expect("break arm r");
    }

    fn break_arm_s(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET total_bytes = 8192
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .expect("break arm s");
    }

    fn insert_archive_item(conn: &Connection, seed: &ArchiveSeed<'_>) -> i64 {
        let archive_item_id = insert_archive_item_unlinked(conn, seed);
        let _ = insert_linked_clip(
            conn,
            archive_item_id,
            &format!("clip:{}", seed.path),
            seed.archived_at,
            seed.folder_class,
        );
        archive_item_id
    }

    fn insert_front_parse_attempt(conn: &Connection, canonical_key: &str, parse_state: &str) {
        conn.execute(
            "INSERT INTO front_parse_attempts
                (canonical_key, parse_state, parse_fingerprint, parser_version,
                 attempt_count, next_retry_at, attempted_at, updated_at)
             VALUES (?1, ?2, NULL, NULL, 1, NULL, 0, 0)",
            params![canonical_key, parse_state],
        )
        .expect("insert front parse attempt");
    }

    fn insert_event_row(conn: &Connection, id: i64, clip_id: i64, t: i64) {
        conn.execute(
            "INSERT INTO events
                (id, trip_id, clip_id, type, severity, t, lat, lon,
                 front_frame_offset, front_frame_index, description, created_at)
             VALUES (?1, NULL, ?2, 'sharp_turn', 2, ?3, NULL, NULL, NULL, NULL, 'test event', 0)",
            params![id, clip_id, t],
        )
        .expect("insert event");
    }

    fn seed_eviction_candidate_mix(conn: &Connection) -> (i64, i64) {
        let old_a = insert_archive_item(
            conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/eligible-old-a",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let old_b = insert_archive_item(
            conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/eligible-old-b",
                size_bytes: 2_000,
                archived_at: 200,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        for seed in [
            ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/excluded-pinned",
                size_bytes: 3_000,
                archived_at: 50,
                delete_state: "LIVE",
                durable: 1,
                pinned: 1,
                suppress_until: None,
                delete_gen: None,
            },
            ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/excluded-nondurable",
                size_bytes: 3_000,
                archived_at: 60,
                delete_state: "LIVE",
                durable: 0,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
            ArchiveSeed {
                folder_class: "SentryClips",
                path: "archive/excluded-sentry",
                size_bytes: 3_000,
                archived_at: 70,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
            ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/excluded-suppressed",
                size_bytes: 3_000,
                archived_at: 80,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: Some(10_000),
                delete_gen: None,
            },
            ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/excluded-too-recent",
                size_bytes: 3_000,
                archived_at: 9_500,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
            ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/excluded-not-live",
                size_bytes: 3_000,
                archived_at: 90,
                delete_state: "DELETE_CLAIMED",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: Some("deadbeef"),
            },
        ] {
            insert_archive_item(conn, &seed);
        }
        (old_a, old_b)
    }

    #[test]
    fn list_eviction_candidates_returns_only_safe_oldest_recent_durable_live_rows() {
        let conn = open_in_memory().expect("open db");
        let (old_a, old_b) = seed_eviction_candidate_mix(&conn);

        let all =
            list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query candidates");
        let ids: Vec<i64> = all.iter().map(|row| row.id).collect();
        assert_eq!(ids, vec![old_a, old_b]);
        assert_eq!(
            all.first().map(|row| row.folder_class.as_str()),
            Some("RecentClips")
        );

        let limited =
            list_eviction_candidates(&conn, 1_000, 1_000, false, 1).expect("limited query");
        assert_eq!(limited.first().map(|row| row.id), Some(old_a));
    }

    #[test]
    fn list_eviction_candidates_opt_in_includes_undurable_but_still_filters() {
        let conn = open_in_memory().expect("open db");
        let (old_a, old_b) = seed_eviction_candidate_mix(&conn);
        let rows = list_eviction_candidates(&conn, 1_000, 1_000, true, 100).expect("query");
        let paths: Vec<&str> = rows.iter().map(|r| r.path.as_str()).collect();
        assert!(paths.contains(&"archive/excluded-nondurable"));
        assert!(!paths.iter().any(|p| p.contains("pinned")));
        assert!(!paths.iter().any(|p| p.contains("sentry")));
        assert!(!paths.iter().any(|p| p.contains("suppressed")));
        assert!(!paths.iter().any(|p| p.contains("too-recent")));
        assert!(!paths.iter().any(|p| p.contains("not-live")));
        assert!(rows.iter().any(|r| r.id == old_a));
        assert!(rows.iter().any(|r| r.id == old_b));
    }

    #[test]
    fn list_eviction_candidates_rejects_stale_durable_without_proving_set() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/stale-durable-no-set",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        insert_linked_clip(&conn, item, "clip:stale-durable-no-set", 100, "RecentClips");
        conn.execute(
            "DELETE FROM cloud_upload_queue WHERE archive_item_id = ?1",
            params![item],
        )
        .expect("delete queue rows");
        conn.execute(
            "DELETE FROM cloud_parent_upload_set_children WHERE upload_set_id = ?1",
            params![format!("{item:032x}")],
        )
        .expect("delete child rows");
        conn.execute(
            "DELETE FROM cloud_parent_upload_sets WHERE archive_item_id = ?1",
            params![item],
        )
        .expect("delete set rows");

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert!(!rows.iter().any(|row| row.id == item));
    }

    #[test]
    fn list_eviction_candidates_rejects_superseded_proving_set() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/superseded-set",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        insert_linked_clip(&conn, item, "clip:superseded-set", 100, "RecentClips");
        conn.execute(
            "UPDATE cloud_parent_upload_sets SET superseded_at = 2 WHERE archive_item_id = ?1",
            params![item],
        )
        .expect("supersede set");

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert!(!rows.iter().any(|row| row.id == item));
    }

    #[test]
    fn list_eviction_candidates_rejects_unfinalized_proving_set() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/unfinalized-set",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        insert_linked_clip(&conn, item, "clip:unfinalized-set", 100, "RecentClips");
        conn.execute(
            "UPDATE cloud_parent_upload_sets SET finalized_at = NULL WHERE archive_item_id = ?1",
            params![item],
        )
        .expect("clear finalized_at");

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert!(!rows.iter().any(|row| row.id == item));
    }

    #[test]
    fn list_eviction_candidates_rejects_incomplete_proving_set_when_queue_state_not_done() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/incomplete-state",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        insert_linked_clip(&conn, item, "clip:incomplete-state", 100, "RecentClips");
        conn.execute(
            "UPDATE cloud_upload_queue SET state = 'queued' WHERE archive_item_id = ?1",
            params![item],
        )
        .expect("mark queue not done");

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert!(!rows.iter().any(|row| row.id == item));
    }

    #[test]
    fn list_eviction_candidates_rejects_incomplete_proving_set_when_content_sha_differs() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/incomplete-sha",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        insert_linked_clip(&conn, item, "clip:incomplete-sha", 100, "RecentClips");
        conn.execute(
            "UPDATE cloud_upload_queue
                SET content_sha256 = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
              WHERE archive_item_id = ?1",
            params![item],
        )
        .expect("mismatch queue hash");

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert!(!rows.iter().any(|row| row.id == item));
    }

    #[test]
    fn list_eviction_candidates_allows_fully_proven_item_without_allow_undurable() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/proven",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        insert_linked_clip(&conn, item, "clip:proven", 100, "RecentClips");

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert!(rows.iter().any(|row| row.id == item));
    }

    #[test]
    fn list_eviction_candidates_allow_undurable_bypasses_proven_durable_gate() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/bypass-unproven",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        insert_linked_clip(&conn, item, "clip:bypass-unproven", 100, "RecentClips");
        conn.execute(
            "DELETE FROM cloud_upload_queue WHERE archive_item_id = ?1",
            params![item],
        )
        .expect("delete queue rows");
        conn.execute(
            "DELETE FROM cloud_parent_upload_set_children WHERE upload_set_id = ?1",
            params![format!("{item:032x}")],
        )
        .expect("delete child rows");
        conn.execute(
            "DELETE FROM cloud_parent_upload_sets WHERE archive_item_id = ?1",
            params![item],
        )
        .expect("delete set rows");

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, true, 100).expect("query");
        assert!(rows.iter().any(|row| row.id == item));
    }

    #[test]
    #[allow(clippy::too_many_lines)]
    fn list_eviction_candidates_rejects_each_proven_durable_arm_break() {
        type ArmMutator = fn(&Connection, i64, &ProvingUploadSetFixture);
        let cases: [(&str, &str, ArmMutator); 13] = [
            ("arm_a_durable_bit", "ai.durable = 1", break_arm_a),
            (
                "arm_f_manifest_digest_non_null",
                "ai.manifest_digest IS NOT NULL",
                break_arm_f,
            ),
            (
                "arm_g_manifest_digest_matches_parent",
                "ai.manifest_digest = pus.source_manifest_digest",
                break_arm_g,
            ),
            (
                "arm_i_expected_equals_child_count",
                "SELECT COUNT(*) FROM cloud_parent_upload_set_children pusc",
                break_arm_i,
            ),
            (
                "arm_j_expected_equals_queue_count",
                "SELECT COUNT(*) FROM cloud_upload_queue puq",
                break_arm_j,
            ),
            (
                "arm_k_joined_queue_row_must_exist",
                "pq.upload_set_id IS NULL",
                break_arm_k,
            ),
            (
                "arm_n_verify_alg_matches",
                "pq.verify_alg <> pm.verify_alg",
                break_arm_n,
            ),
            (
                "arm_o_expected_hash_null",
                "COALESCE(pq.expected_hash, '') <> pm.expected_hash",
                break_arm_o_null,
            ),
            (
                "arm_o_expected_hash_differs",
                "COALESCE(pq.expected_hash, '') <> pm.expected_hash",
                break_arm_o_differs,
            ),
            (
                "arm_p_child_key_matches",
                "pq.child_key <> pm.child_key",
                break_arm_p,
            ),
            (
                "arm_q_category_matches",
                "pq.category <> pm.category",
                break_arm_q,
            ),
            ("arm_r_seq_matches", "pq.seq <> pm.seq", break_arm_r),
            (
                "arm_s_total_bytes_matches",
                "pq.total_bytes <> pm.total_bytes",
                break_arm_s,
            ),
        ];
        // Arm h (expected_child_count > 0) is schema-enforced by
        // cloud_parent_upload_sets.expected_child_count CHECK(expected_child_count > 0).
        let mut unexpectedly_listed = Vec::new();
        for (case_name, required_clause, mutate) in cases {
            assert!(
                crate::db::PROVEN_DURABLE_PROOF_SQL.contains(required_clause),
                "case {case_name}: required proof clause missing from PROVEN_DURABLE_PROOF_SQL"
            );
            let conn = open_in_memory().expect("open db");
            let item = insert_archive_item_unlinked(
                &conn,
                &ArchiveSeed {
                    folder_class: "RecentClips",
                    path: "archive/proven-arm-break",
                    size_bytes: 1_000,
                    archived_at: 100,
                    delete_state: "LIVE",
                    durable: 1,
                    pinned: 0,
                    suppress_until: None,
                    delete_gen: None,
                },
            );
            insert_linked_clip(&conn, item, "clip:proven-arm-break", 100, "RecentClips");
            let baseline =
                list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("baseline query");
            assert!(
                baseline.iter().any(|row| row.id == item),
                "case {case_name}: baseline proven seed must be listed before mutation"
            );
            let fixture = proving_upload_set_fixture(item);
            mutate(&conn, item, &fixture);
            let rows =
                list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query mutated");
            if rows.iter().any(|row| row.id == item) {
                unexpectedly_listed.push(case_name);
            }
        }
        assert!(
            unexpectedly_listed.is_empty(),
            "list_eviction_candidates unexpectedly included broken-proof cases: {unexpectedly_listed:?}"
        );
    }

    #[test]
    fn list_eviction_candidates_excludes_item_with_no_linked_clip() {
        let conn = open_in_memory().expect("open db");
        insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/unlinked-old",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert!(rows.is_empty());
    }

    #[test]
    fn list_eviction_candidates_excludes_item_with_zero_started_at() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/zero-started-at",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        insert_linked_clip(&conn, item, "clip:zero-started-at", 0, "RecentClips");

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert!(rows.is_empty());
    }

    #[test]
    fn list_eviction_candidates_excludes_when_linked_clip_not_recentclips() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/mismatched-folder-class",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        insert_linked_clip(
            &conn,
            item,
            "clip:mismatched-folder-class",
            100,
            "SentryClips",
        );

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert!(rows.is_empty());
    }

    #[test]
    fn list_eviction_candidates_excludes_stale_archived_at_but_fresh_started_at() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/stale-archived-fresh-started",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        insert_linked_clip(
            &conn,
            item,
            "clip:stale-archived-fresh-started",
            9_500,
            "RecentClips",
        );

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert!(rows.is_empty());
    }

    #[test]
    fn list_eviction_candidates_deletes_when_started_at_old_even_if_archived_at_fresh() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/fresh-archived-old-started",
                size_bytes: 1_000,
                archived_at: 9_500,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        insert_linked_clip(
            &conn,
            item,
            "clip:fresh-archived-old-started",
            100,
            "RecentClips",
        );

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert_eq!(rows.len(), 1);
        assert_eq!(rows.first().map(|row| row.id), Some(item));
    }

    fn insert_linked_recent_clip_sentry_flagged(
        conn: &Connection,
        archive_item_id: i64,
        canonical_key: &str,
        started_at: i64,
    ) {
        conn.execute(
            "INSERT INTO clips
                (canonical_key, started_at, partition, folder_class, is_sentry, created_at, updated_at)
             VALUES (?1, ?2, 'p', 'RecentClips', 1, 0, 0)",
            params![canonical_key, started_at],
        )
        .expect("insert sentry-flagged clip");
        let clip_id = conn.last_insert_rowid();
        conn.execute(
            "INSERT INTO archive_item_clips (archive_item_id, clip_id) VALUES (?1, ?2)",
            params![archive_item_id, clip_id],
        )
        .expect("insert archive-item clip link");
    }

    #[test]
    fn list_eviction_candidates_excludes_multiclip_item_when_any_clip_is_fresh() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/multiclip-one-fresh",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        // One old segment and one fresh segment on the same item: MAX(started_at)
        // must protect it (a MIN would wrongly delete footage recorded moments ago).
        insert_linked_clip(&conn, item, "clip:multiclip-old", 100, "RecentClips");
        insert_linked_clip(&conn, item, "clip:multiclip-fresh", 9_500, "RecentClips");

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert!(rows.is_empty());
    }

    #[test]
    fn list_eviction_candidates_excludes_multiclip_item_when_any_clip_has_zero_started_at() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/multiclip-one-zero",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        insert_linked_clip(&conn, item, "clip:multiclip-known", 100, "RecentClips");
        insert_linked_clip(&conn, item, "clip:multiclip-unknown", 0, "RecentClips");

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert!(rows.is_empty());
    }

    #[test]
    fn list_eviction_candidates_excludes_item_with_sentry_flagged_recent_clip() {
        let conn = open_in_memory().expect("open db");
        let item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/sentry-flagged-recent",
                size_bytes: 1_000,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        // Defense-in-depth: a clip whose folder_class is RecentClips but is_sentry=1
        // (a would-be ingest inconsistency) must never be selected.
        insert_linked_recent_clip_sentry_flagged(&conn, item, "clip:sentry-flagged", 100);

        let rows = list_eviction_candidates(&conn, 1_000, 1_000, false, 100).expect("query");
        assert!(rows.is_empty());
    }

    #[test]
    fn orders_confirmed_parked_before_sei_before_event() {
        let conn = open_in_memory().expect("open db");

        let item_a = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/tier0-newest",
                size_bytes: 1_000,
                archived_at: 9_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let key_a = "clip:tier0-newest";
        let _ = insert_linked_clip(&conn, item_a, key_a, 9_000, "RecentClips");
        insert_front_parse_attempt(&conn, key_a, "no_waypoints");

        let item_b = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/tier1-middle",
                size_bytes: 1_000,
                archived_at: 5_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let key_b = "clip:tier1-middle";
        let _ = insert_linked_clip(&conn, item_b, key_b, 5_000, "RecentClips");
        insert_front_parse_attempt(&conn, key_b, "parsed_with_waypoints");

        let item_c = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/tier2-oldest",
                size_bytes: 1_000,
                archived_at: 1_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let key_c = "clip:tier2-oldest";
        let clip_c = insert_linked_clip(&conn, item_c, key_c, 1_000, "RecentClips");
        insert_front_parse_attempt(&conn, key_c, "parsed_with_waypoints");
        insert_event_row(&conn, 1, clip_c, 1_000);

        let rows = list_eviction_candidates(&conn, 10_000, 10_000, false, 100).expect("query");
        let ids: Vec<i64> = rows.iter().map(|row| row.id).collect();
        assert_eq!(ids, vec![item_a, item_b, item_c]);
    }

    #[test]
    fn within_tier_oldest_first() {
        let conn = open_in_memory().expect("open db");
        let old_item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/tier0-old",
                size_bytes: 1_000,
                archived_at: 1_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let new_item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/tier0-new",
                size_bytes: 1_000,
                archived_at: 2_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let key_old = "clip:tier0-old";
        let key_new = "clip:tier0-new";
        let _ = insert_linked_clip(&conn, old_item, key_old, 1_000, "RecentClips");
        let _ = insert_linked_clip(&conn, new_item, key_new, 2_000, "RecentClips");
        insert_front_parse_attempt(&conn, key_old, "no_waypoints");
        insert_front_parse_attempt(&conn, key_new, "no_waypoints");

        let rows = list_eviction_candidates(&conn, 10_000, 10_000, false, 100).expect("query");
        let ids: Vec<i64> = rows.iter().map(|row| row.id).collect();
        assert_eq!(ids, vec![old_item, new_item]);
    }

    #[test]
    fn parsed_with_waypoints_never_tier0() {
        let conn = open_in_memory().expect("open db");
        let tier1_old = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/tier1-old",
                size_bytes: 1_000,
                archived_at: 1_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let tier0_new = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/tier0-newer",
                size_bytes: 1_000,
                archived_at: 9_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let key_tier1 = "clip:tier1-old";
        let key_tier0 = "clip:tier0-newer";
        let _ = insert_linked_clip(&conn, tier1_old, key_tier1, 1_000, "RecentClips");
        let _ = insert_linked_clip(&conn, tier0_new, key_tier0, 9_000, "RecentClips");
        insert_front_parse_attempt(&conn, key_tier1, "parsed_with_waypoints");
        insert_front_parse_attempt(&conn, key_tier0, "no_waypoints");

        let rows = list_eviction_candidates(&conn, 10_000, 10_000, false, 100).expect("query");
        let ids: Vec<i64> = rows.iter().map(|row| row.id).collect();
        assert_eq!(ids, vec![tier0_new, tier1_old]);
    }

    #[test]
    fn unparsed_clip_not_treated_as_parked() {
        let conn = open_in_memory().expect("open db");
        let unparsed = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/unparsed",
                size_bytes: 1_000,
                archived_at: 1_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let parked = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/parked",
                size_bytes: 1_000,
                archived_at: 9_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let _ = insert_linked_clip(&conn, unparsed, "clip:unparsed", 1_000, "RecentClips");
        let _ = insert_linked_clip(&conn, parked, "clip:parked", 9_000, "RecentClips");
        insert_front_parse_attempt(&conn, "clip:parked", "no_waypoints");

        let rows = list_eviction_candidates(&conn, 10_000, 10_000, false, 100).expect("query");
        let ids: Vec<i64> = rows.iter().map(|row| row.id).collect();
        assert_eq!(ids, vec![parked, unparsed]);
    }

    #[test]
    fn left_join_does_not_change_candidate_set() {
        let conn = open_in_memory().expect("open db");
        let first = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/candidate-first",
                size_bytes: 1_000,
                archived_at: 1_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let second = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/candidate-second",
                size_bytes: 1_000,
                archived_at: 2_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let third = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/candidate-third",
                size_bytes: 1_000,
                archived_at: 3_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let _ = insert_linked_clip(&conn, first, "clip:candidate-first", 1_000, "RecentClips");
        let _ = insert_linked_clip(&conn, second, "clip:candidate-second", 2_000, "RecentClips");
        let _ = insert_linked_clip(&conn, third, "clip:candidate-third", 3_000, "RecentClips");

        let before = list_eviction_candidates(&conn, 10_000, 10_000, false, 100).expect("query");
        let mut before_ids: Vec<i64> = before.iter().map(|row| row.id).collect();
        before_ids.sort_unstable();

        insert_front_parse_attempt(&conn, "clip:candidate-first", "no_waypoints");
        insert_front_parse_attempt(&conn, "clip:candidate-second", "parsed_with_waypoints");
        insert_front_parse_attempt(&conn, "clip:candidate-third", "legacy_unknown");

        let after = list_eviction_candidates(&conn, 10_000, 10_000, false, 100).expect("query");
        let mut after_ids: Vec<i64> = after.iter().map(|row| row.id).collect();
        after_ids.sort_unstable();

        assert_eq!(before_ids.len(), after_ids.len());
        assert_eq!(before_ids, after_ids);
    }

    #[test]
    fn multiclip_mixed_item_not_tier0() {
        let conn = open_in_memory().expect("open db");
        let mixed_item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/mixed-tier1",
                size_bytes: 1_000,
                archived_at: 2_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let mixed_a = "clip:mixed-no-waypoints";
        let mixed_b = "clip:mixed-parsed";
        let _ = insert_linked_clip(&conn, mixed_item, mixed_a, 2_000, "RecentClips");
        let _ = insert_linked_clip(&conn, mixed_item, mixed_b, 2_100, "RecentClips");
        insert_front_parse_attempt(&conn, mixed_a, "no_waypoints");
        insert_front_parse_attempt(&conn, mixed_b, "parsed_with_waypoints");

        let event_item = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/event-tier2",
                size_bytes: 1_000,
                archived_at: 1_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let event_key = "clip:event-tier2";
        let event_clip = insert_linked_clip(&conn, event_item, event_key, 1_000, "RecentClips");
        insert_front_parse_attempt(&conn, event_key, "parsed_with_waypoints");
        insert_event_row(&conn, 1, event_clip, 1_000);

        let rows = list_eviction_candidates(&conn, 10_000, 10_000, false, 100).expect("query");
        let ids: Vec<i64> = rows.iter().map(|row| row.id).collect();
        assert_eq!(ids, vec![mixed_item, event_item]);
    }

    #[test]
    fn event_tier_dominates_no_waypoints() {
        // An item whose clip is `no_waypoints` but ALSO carries an event must
        // sort as tier 2 (event wins), never tier 0. Guards the CASE priority
        // (event checked before no_waypoints). This clip state cannot occur in
        // production (events are SEI-derived) but locks the ordering rule.
        let conn = open_in_memory().expect("open db");
        let junk = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/junk-newer",
                size_bytes: 1_000,
                archived_at: 9_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let _ = insert_linked_clip(&conn, junk, "clip:junk-newer", 9_000, "RecentClips");
        insert_front_parse_attempt(&conn, "clip:junk-newer", "no_waypoints");

        let event_nw = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/event-no-waypoints-older",
                size_bytes: 1_000,
                archived_at: 1_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let event_clip =
            insert_linked_clip(&conn, event_nw, "clip:event-nw-older", 1_000, "RecentClips");
        insert_front_parse_attempt(&conn, "clip:event-nw-older", "no_waypoints");
        insert_event_row(&conn, 1, event_clip, 1_000);

        let rows = list_eviction_candidates(&conn, 10_000, 10_000, false, 100).expect("query");
        let ids: Vec<i64> = rows.iter().map(|row| row.id).collect();
        // junk is tier 0 (deleted first) despite being newer; the event item is
        // tier 2 (deleted last) even though its clip is also no_waypoints.
        assert_eq!(ids, vec![junk, event_nw]);
    }

    #[test]
    fn mixed_item_not_tier0_with_pure_sentinel() {
        // A multi-clip item mixing a no_waypoints clip with a parsed clip is
        // tier 1 (MIN==all), so a genuine pure-tier0 item — even if NEWER — must
        // be deleted before it. Fails if the tier used MAX (any) instead of MIN
        // (all) for the no_waypoints test.
        let conn = open_in_memory().expect("open db");
        let pure_junk = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/pure-junk-newest",
                size_bytes: 1_000,
                archived_at: 9_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let _ = insert_linked_clip(
            &conn,
            pure_junk,
            "clip:pure-junk-newest",
            9_000,
            "RecentClips",
        );
        insert_front_parse_attempt(&conn, "clip:pure-junk-newest", "no_waypoints");

        let mixed = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/mixed-middle",
                size_bytes: 1_000,
                archived_at: 2_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let _ = insert_linked_clip(&conn, mixed, "clip:mixed-nw", 2_000, "RecentClips");
        let _ = insert_linked_clip(&conn, mixed, "clip:mixed-parsed", 2_100, "RecentClips");
        insert_front_parse_attempt(&conn, "clip:mixed-nw", "no_waypoints");
        insert_front_parse_attempt(&conn, "clip:mixed-parsed", "parsed_with_waypoints");

        let event = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/event-oldest",
                size_bytes: 1_000,
                archived_at: 1_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let event_clip =
            insert_linked_clip(&conn, event, "clip:event-oldest", 1_000, "RecentClips");
        insert_front_parse_attempt(&conn, "clip:event-oldest", "parsed_with_waypoints");
        insert_event_row(&conn, 1, event_clip, 1_000);

        let rows = list_eviction_candidates(&conn, 10_000, 10_000, false, 100).expect("query");
        let ids: Vec<i64> = rows.iter().map(|row| row.id).collect();
        assert_eq!(ids, vec![pure_junk, mixed, event]);
    }

    #[test]
    fn equal_tier_equal_start_orders_by_ai_id() {
        // Two tier-0 items with identical started_at fall back to ai.id ASC for
        // deterministic paging/claim ordering.
        let conn = open_in_memory().expect("open db");
        let lower = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/tie-lower",
                size_bytes: 1_000,
                archived_at: 5_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let higher = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/tie-higher",
                size_bytes: 1_000,
                archived_at: 5_000,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let _ = insert_linked_clip(&conn, lower, "clip:tie-lower", 5_000, "RecentClips");
        let _ = insert_linked_clip(&conn, higher, "clip:tie-higher", 5_000, "RecentClips");
        insert_front_parse_attempt(&conn, "clip:tie-lower", "no_waypoints");
        insert_front_parse_attempt(&conn, "clip:tie-higher", "no_waypoints");

        let rows = list_eviction_candidates(&conn, 10_000, 10_000, false, 100).expect("query");
        let ids: Vec<i64> = rows.iter().map(|row| row.id).collect();
        assert!(lower < higher, "insertion order should yield ascending ids");
        assert_eq!(ids, vec![lower, higher]);
    }

    #[test]
    fn expanded_grace_allows_recent_but_not_within_grace() {
        let conn = open_in_memory().expect("open db");
        let now = 20_000;
        let recency_floor = now - 3_600;

        let outside_grace = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/outside-grace",
                size_bytes: 1_000,
                archived_at: now - 7_200,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let inside_grace = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/inside-grace",
                size_bytes: 1_000,
                archived_at: now - 1_800,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );

        let _ = insert_linked_clip(
            &conn,
            outside_grace,
            "clip:outside-grace",
            now - 7_200,
            "RecentClips",
        );
        let _ = insert_linked_clip(
            &conn,
            inside_grace,
            "clip:inside-grace",
            now - 1_800,
            "RecentClips",
        );

        let rows = list_eviction_candidates(&conn, recency_floor, now, false, 100).expect("query");
        let ids: Vec<i64> = rows.iter().map(|row| row.id).collect();
        assert_eq!(ids, vec![outside_grace]);
    }

    #[test]
    fn eviction_exclusion_report_includes_lease_gate_and_stable_reasons() {
        let conn = open_in_memory().expect("open db");
        let lease_active = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/lease-active",
                size_bytes: 100,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let _ = insert_linked_clip(&conn, lease_active, "clip:lease-active", 100, "RecentClips");
        conn.execute(
            "INSERT INTO leases
                (archive_item_id, kind, holder, gen, boot_id, expires_mono_ms)
             VALUES (?1, 'playback', 'webd:test', 'lease-gen', ?2, ?3)",
            params![lease_active, "boot-lease", 5_000i64],
        )
        .expect("insert active lease");

        let not_live = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/not-live",
                size_bytes: 200,
                archived_at: 100,
                delete_state: "DELETE_CLAIMED",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: Some("g"),
            },
        );
        let _ = insert_linked_clip(&conn, not_live, "clip:not-live", 100, "RecentClips");

        let pinned = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/pinned",
                size_bytes: 300,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 1,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let _ = insert_linked_clip(&conn, pinned, "clip:pinned", 100, "RecentClips");

        let too_recent = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/too-recent",
                size_bytes: 400,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let _ = insert_linked_clip(&conn, too_recent, "clip:too-recent", 950, "RecentClips");

        let report = eviction_exclusion_report(&conn, 900, 1_000, false, "boot-lease", 100, 256)
            .expect("exclusion report");
        assert_eq!(report.sample_size, 4);
        assert!(!report.sample_truncated);
        assert_eq!(
            report
                .reasons
                .iter()
                .map(|reason| reason.reason.as_str())
                .collect::<Vec<_>>(),
            vec!["lease_active", "not_live", "pinned", "too_recent"]
        );
        assert_eq!(
            report
                .reasons
                .iter()
                .map(|reason| (reason.count, reason.size_bytes))
                .collect::<Vec<_>>(),
            vec![(1, 100), (1, 200), (1, 300), (1, 400)]
        );
    }

    #[test]
    fn eviction_exclusion_report_respects_allow_undurable_gate() {
        let conn = open_in_memory().expect("open db");
        let undurable = insert_archive_item_unlinked(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/undurable",
                size_bytes: 777,
                archived_at: 100,
                delete_state: "LIVE",
                durable: 0,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        let _ = insert_linked_clip(&conn, undurable, "clip:undurable", 100, "RecentClips");

        let denied = eviction_exclusion_report(&conn, 900, 1_000, false, "boot-none", 0, 256)
            .expect("denied report");
        assert_eq!(denied.sample_size, 1);
        assert_eq!(denied.reasons.len(), 1);
        assert_eq!(denied.reasons[0].reason, "not_durable");
        assert_eq!(denied.reasons[0].count, 1);
        assert_eq!(denied.reasons[0].size_bytes, 777);

        let allowed = eviction_exclusion_report(&conn, 900, 1_000, true, "boot-none", 0, 256)
            .expect("allowed report");
        assert_eq!(allowed.sample_size, 0);
        assert!(allowed.reasons.is_empty());
    }

    #[test]
    fn list_recovery_rows_excludes_live_and_deleted() {
        let conn = open_in_memory().expect("open db");
        insert_archive_item(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/live",
                size_bytes: 1,
                archived_at: 1,
                delete_state: "LIVE",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );
        insert_archive_item(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/deleted",
                size_bytes: 2,
                archived_at: 2,
                delete_state: "DELETED",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: Some("done"),
            },
        );
        let claimed = insert_archive_item(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/claimed",
                size_bytes: 3,
                archived_at: 3,
                delete_state: "DELETE_CLAIMED",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: Some("g1"),
            },
        );
        let deleting = insert_archive_item(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/deleting",
                size_bytes: 4,
                archived_at: 4,
                delete_state: "DELETING",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: Some("g2"),
            },
        );
        let failed = insert_archive_item(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/failed",
                size_bytes: 5,
                archived_at: 5,
                delete_state: "DELETE_FAILED",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: Some("g3"),
            },
        );
        let quarantined = insert_archive_item(
            &conn,
            &ArchiveSeed {
                folder_class: "RecentClips",
                path: "archive/quarantined",
                size_bytes: 6,
                archived_at: 6,
                delete_state: "QUARANTINED",
                durable: 1,
                pinned: 0,
                suppress_until: None,
                delete_gen: None,
            },
        );

        let rows = list_recovery_rows(&conn).expect("query recovery rows");
        let ids: Vec<i64> = rows.iter().map(|row| row.id).collect();
        assert_eq!(ids, vec![claimed, deleting, failed, quarantined]);
        assert_eq!(
            rows.first().map(|row| row.delete_state.as_str()),
            Some("DELETE_CLAIMED")
        );
        assert_eq!(
            rows.first().and_then(|row| row.delete_gen.as_deref()),
            Some("g1")
        );
    }
}
