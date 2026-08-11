//! Cloud-sync persistence and RPC-facing DB operations for indexd.
//!
//! This module owns migration-v6 tables and behavior: queue paging/upsert,
//! upload commit/fail idempotency, derived stats, and non-secret config.
#![allow(
    clippy::missing_errors_doc,
    clippy::single_match_else,
    clippy::too_many_lines
)]

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{Connection, OptionalExtension, Transaction, params};
use serde::{Deserialize, Serialize};

use crate::db::mutations::BootContext;
use crate::db::{DbError, now_epoch_s};

const MAX_PAGE_LIMIT: u32 = 16;
const HASH_LEN: usize = 64;
const RESPONSE_ITEM_BUDGET: usize = 56 * 1024;

/// Stable paginated page.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CloudPage<T> {
    /// Returned items.
    pub items: Vec<T>,
    /// Opaque keyset cursor for the next page.
    pub next_cursor: Option<String>,
}

/// Queue primary key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CloudQueuePk {
    /// Remote destination identity.
    pub destination_id: String,
    /// Canonical remote key (byte-exact identity, see `normalize_remote_key`).
    pub remote_key: String,
}

/// One queue upsert payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CloudQueueUpsertItem {
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Child discriminator within the parent event.
    pub child_key: String,
    /// Destination identity.
    pub destination_id: String,
    /// Destination object key.
    pub remote_key: String,
    /// Upload category.
    pub category: String,
    /// FIFO tie-breaker.
    pub seq: i64,
    /// Total bytes of this child.
    pub total_bytes: i64,
    /// Source file identity hash.
    pub content_sha256: String,
    /// Backend verification value, if already known.
    pub expected_hash: Option<String>,
    /// Backend verification algorithm.
    pub verify_alg: String,
}

/// One `cloud_candidates` item.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudCandidateRow {
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Child key.
    pub child_key: String,
    /// Source archive-root-relative path.
    pub source_rel: String,
    /// Destination id.
    pub destination_id: String,
    /// Destination key.
    pub remote_key: String,
    /// Size in bytes.
    pub size_bytes: i64,
    /// Local identity hash.
    pub content_sha256: String,
    /// Queue state.
    pub state: String,
    /// Category.
    pub category: String,
    /// FIFO sequence.
    pub seq: i64,
}

/// One `cloud_discover` item.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudDiscoverRow {
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Source folder class.
    pub folder_class: String,
    /// Source archive-root-relative path.
    pub path: String,
    /// Parent manifest digest, when available.
    pub manifest_digest: Option<String>,
    /// Upload category.
    pub category: String,
}

/// One `cloud_queue_load` item.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudQueueRow {
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Child key.
    pub child_key: String,
    /// Source archive-root-relative path.
    pub source_rel: String,
    /// Destination id.
    pub destination_id: String,
    /// Destination key.
    pub remote_key: String,
    /// Category.
    pub category: String,
    /// FIFO sequence.
    pub seq: i64,
    /// Total bytes.
    pub total_bytes: i64,
    /// Uploaded bytes.
    pub bytes_uploaded: i64,
    /// Expected backend verification value.
    pub expected_hash: Option<String>,
    /// Expected backend verification algorithm.
    pub verify_alg: String,
    /// Local hash.
    pub content_sha256: String,
    /// State.
    pub state: String,
    /// Attempts.
    pub attempts: i64,
    /// Not-before unix timestamp.
    pub not_before: Option<i64>,
    /// Last error class/message (sanitized).
    pub last_error: Option<String>,
    /// Sealed upload set id, when row membership has been finalized.
    pub upload_set_id: Option<String>,
}

/// One `cloud_pending_upload_sets_load` item.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudPendingUploadSet {
    /// Upload set id.
    pub upload_set_id: String,
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Destination id.
    pub destination_id: String,
    /// Source manifest digest.
    pub source_manifest_digest: String,
    /// Expected child count.
    pub expected_child_count: i64,
}

/// `cloud_queue_retry` conflict resolution mode.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case")]
pub enum CloudQueueRetryResolution {
    /// Keep remote object; drop local conflicting offer as completed.
    KeepExisting,
    /// Requeue with a different remote key.
    Rekey {
        /// New canonical key.
        remote_key: String,
    },
    /// Requeue and explicitly allow replacing existing remote contents.
    Replace,
}

/// Upload lease acquire response.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UploadLeaseAcquireResult {
    /// Whether lease was granted.
    pub granted: bool,
    /// Lease token (`id:gen`) when granted.
    pub token: Option<String>,
    /// Boot id when granted.
    pub boot_id: Option<String>,
    /// Monotonic deadline when granted.
    pub expires_mono_ms: Option<i64>,
}

/// Upload lease renew response.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UploadLeaseRenewResult {
    /// Whether renew succeeded.
    pub ok: bool,
    /// New deadline when renewed.
    pub expires_mono_ms: Option<i64>,
}

/// `cloud_upload_commit` result.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CloudUploadCommitResult {
    /// Operation success.
    pub ok: bool,
    /// Whether parent became fully durable.
    pub durable_parent: bool,
}

/// `cloud_upload_fail` result.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CloudUploadFailResult {
    /// Operation success.
    pub ok: bool,
    /// Resulting queue state.
    pub state: String,
}

/// Derived cloud-sync counters.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudStats {
    /// Uploaded item count since baseline.
    pub synced_count: i64,
    /// Uploaded bytes since baseline.
    pub synced_bytes: i64,
    /// Baseline timestamp.
    pub since_at: i64,
}

/// Non-secret provider config.
#[allow(clippy::struct_excessive_bools)]
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudConfig {
    /// Sentry folder enabled.
    pub sentry_enabled: bool,
    /// Saved folder enabled.
    pub saved_enabled: bool,
    /// Recent folder enabled.
    pub recent_enabled: bool,
    /// Sentry priority (lower = earlier).
    pub sentry_priority: i64,
    /// Saved priority (lower = earlier).
    pub saved_priority: i64,
    /// Recent priority (lower = earlier).
    pub recent_priority: i64,
    /// Remote free-space reserve in GiB.
    pub reserve_gb: i64,
    /// Retry policy max attempts.
    pub max_attempts: i64,
    /// Retry policy base backoff seconds.
    pub base_backoff_secs: i64,
    /// Keep local files until backed up.
    pub keep_until_backed_up: bool,
    /// Auto sync enabled.
    pub auto_sync: bool,
}

/// One `cloud_history_load` row.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudHistoryRow {
    /// Row id.
    pub id: i64,
    /// Monotonic completion sequence.
    pub completion_seq: i64,
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Child key.
    pub child_key: String,
    /// Destination id.
    pub destination_id: String,
    /// Outcome.
    pub outcome: String,
    /// Bytes.
    pub size_bytes: i64,
    /// Unix timestamp.
    pub at: i64,
    /// Sanitized error class on failure.
    pub error_class: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct QueueCursor {
    seq: i64,
    destination_id: String,
    remote_key: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct PendingSetsCursor {
    created_at: i64,
    upload_set_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct DiscoverCursor {
    archive_item_id: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct HistoryCursor {
    completion_seq: i64,
    id: i64,
}

static TOKEN_COUNTER: AtomicU64 = AtomicU64::new(0);

fn invalid_input(message: &str) -> DbError {
    DbError::Sqlite(rusqlite::Error::InvalidParameterName(message.to_owned()))
}

fn validate_non_empty_len(value: &str, field: &str, max: usize) -> Result<(), DbError> {
    if value.is_empty() || value.len() > max {
        return Err(invalid_input(&format!("{field} must be 1..={max} bytes")));
    }
    if value.bytes().any(|byte| byte == 0) {
        return Err(invalid_input(&format!(
            "{field} must not contain NUL bytes"
        )));
    }
    Ok(())
}

fn validate_sha256(value: &str, field: &str) -> Result<(), DbError> {
    if value.len() != HASH_LEN || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(invalid_input(&format!(
            "{field} must be a {HASH_LEN}-char lowercase hex hash"
        )));
    }
    if value != value.to_ascii_lowercase() {
        return Err(invalid_input(&format!("{field} must be lowercase hex")));
    }
    Ok(())
}

fn validate_lower_hex_len(value: &str, field: &str, len: usize) -> Result<(), DbError> {
    if value.len() != len || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(invalid_input(&format!(
            "{field} must be a {len}-char lowercase hex hash"
        )));
    }
    if value != value.to_ascii_lowercase() {
        return Err(invalid_input(&format!("{field} must be lowercase hex")));
    }
    Ok(())
}

fn validate_optional_upload_set_id(upload_set_id: Option<&str>) -> Result<(), DbError> {
    if let Some(id) = upload_set_id {
        validate_lower_hex_len(id, "upload_set_id", 32)?;
    }
    Ok(())
}

fn enforce_upload_set_fence(
    tx: &Connection,
    row_upload_set_id: Option<&str>,
    requested: Option<&str>,
) -> Result<(), DbError> {
    match row_upload_set_id {
        None => {
            if requested.is_some() {
                return Err(invalid_input(
                    "upload_set_id supplied for an unsealed queue row",
                ));
            }
            Ok(())
        }
        Some(row_id) => match requested {
            Some(req) if req == row_id => {
                let live: Option<i64> = tx
                    .query_row(
                        "SELECT 1 FROM cloud_parent_upload_sets
                          WHERE upload_set_id = ?1 AND superseded_at IS NULL",
                        params![row_id],
                        |r| r.get(0),
                    )
                    .optional()?;
                if live.is_none() {
                    return Err(invalid_input("upload_set_id is superseded or unknown"));
                }
                Ok(())
            }
            _ => Err(invalid_input(
                "upload_set_id does not match sealed queue row",
            )),
        },
    }
}

fn validate_category(value: &str) -> Result<(), DbError> {
    if matches!(value, "event_sentry" | "trip" | "bulk") {
        return Ok(());
    }
    Err(invalid_input(
        "category must be one of event_sentry|trip|bulk",
    ))
}

fn validate_queue_state(value: &str) -> Result<(), DbError> {
    if matches!(
        value,
        "queued" | "in_progress" | "done" | "failed" | "parked"
    ) {
        return Ok(());
    }
    Err(invalid_input(
        "state must be one of queued|in_progress|done|failed|parked",
    ))
}

fn validate_verify_alg(value: &str) -> Result<(), DbError> {
    if matches!(
        value,
        "sha256" | "md5" | "crc32c" | "sha1" | "quickxor" | "dropbox" | "none"
    ) {
        return Ok(());
    }
    Err(invalid_input(
        "verify_alg must be one of sha256|md5|crc32c|sha1|quickxor|dropbox|none",
    ))
}

fn validate_backend_hash<'a>(hash: &'a str, hash_alg: &str) -> Result<Option<&'a str>, DbError> {
    match hash_alg {
        "sha256" => {
            validate_lower_hex_len(hash, "hash", 64)?;
            Ok(Some(hash))
        }
        "sha1" => {
            validate_lower_hex_len(hash, "hash", 40)?;
            Ok(Some(hash))
        }
        "md5" => {
            validate_lower_hex_len(hash, "hash", 32)?;
            Ok(Some(hash))
        }
        "crc32c" => {
            validate_lower_hex_len(hash, "hash", 8)?;
            Ok(Some(hash))
        }
        "quickxor" | "dropbox" => {
            validate_non_empty_len(hash, "hash", 256)?;
            Ok(Some(hash))
        }
        "none" => {
            if !hash.is_empty() {
                return Err(invalid_input("hash must be empty when hash_alg is none"));
            }
            Ok(None)
        }
        _ => Err(invalid_input(
            "verify_alg must be one of sha256|md5|crc32c|sha1|quickxor|dropbox|none",
        )),
    }
}

fn paginate_with_budget<T>(
    queried: Vec<T>,
    page_size: usize,
    estimate_size: impl Fn(&T) -> usize,
) -> (Vec<T>, bool) {
    let total = queried.len();
    let mut items = Vec::with_capacity(page_size.min(total));
    let mut used = 0usize;
    let mut has_more = false;
    for (index, item) in queried.into_iter().enumerate() {
        if items.len() >= page_size {
            has_more = true;
            break;
        }
        let estimated = estimate_size(&item);
        if !items.is_empty() && used.saturating_add(estimated) > RESPONSE_ITEM_BUDGET {
            has_more = true;
            break;
        }
        used = used.saturating_add(estimated);
        items.push(item);
        if index + 1 < total && items.len() >= page_size {
            has_more = true;
            break;
        }
    }
    if !has_more && items.len() < total && !items.is_empty() {
        has_more = true;
    }
    (items, has_more)
}

/// Upper bound on the bytes `s` occupies inside a `serde_json` string body (excluding
/// the surrounding quotes). ASCII text without quotes, backslashes, or control chars —
/// e.g. Tesla paths (`/`, `-`, `_`, `:`, `.`) — is unchanged, so real data pays nothing;
/// pathological free-text (rclone error strings) is budgeted at its escaped length so a
/// built page never serializes past [`MAX_REQUEST_FRAME`](crate::proto::MAX_REQUEST_FRAME).
/// Matches `serde_json`'s default escaping (only `"`, `\`, and control chars < 0x20).
fn json_escaped_len(s: &str) -> usize {
    s.chars()
        .map(|c| match c {
            '"' | '\\' | '\n' | '\r' | '\t' | '\u{08}' | '\u{0c}' => 2,
            c if (c as u32) < 0x20 => 6,
            c => c.len_utf8(),
        })
        .sum()
}

// Per-row size estimators. Each sums `json_escaped_len` over the row's string values
// and adds a fixed constant that upper-bounds the rest of the serialized JSON envelope:
// braces, commas, every `"key":`, the string-value quotes, and every numeric value at
// full i64 width (20 bytes). Kept a true upper bound so a page built to
// `RESPONSE_ITEM_BUDGET` never serializes past `MAX_REQUEST_FRAME` regardless of
// `MAX_PAGE_LIMIT` (proven by `estimated_row_sizes_upper_bound_serialized_json`).
fn cloud_candidate_row_estimated_size(row: &CloudCandidateRow) -> usize {
    json_escaped_len(&row.child_key)
        + json_escaped_len(&row.source_rel)
        + json_escaped_len(&row.destination_id)
        + json_escaped_len(&row.remote_key)
        + json_escaped_len(&row.content_sha256)
        + json_escaped_len(&row.state)
        + json_escaped_len(&row.category)
        + 240
}

fn cloud_discover_row_estimated_size(row: &CloudDiscoverRow) -> usize {
    json_escaped_len(&row.folder_class)
        + json_escaped_len(&row.path)
        + row.manifest_digest.as_deref().map_or(0, json_escaped_len)
        + json_escaped_len(&row.category)
        + 128
}

fn cloud_queue_row_estimated_size(row: &CloudQueueRow) -> usize {
    json_escaped_len(&row.child_key)
        + json_escaped_len(&row.source_rel)
        + json_escaped_len(&row.destination_id)
        + json_escaped_len(&row.remote_key)
        + json_escaped_len(&row.category)
        + row.expected_hash.as_deref().map_or(0, json_escaped_len)
        + json_escaped_len(&row.verify_alg)
        + json_escaped_len(&row.content_sha256)
        + json_escaped_len(&row.state)
        + row.not_before.map_or(0, |_| 16)
        + row.last_error.as_deref().map_or(0, json_escaped_len)
        + row.upload_set_id.as_deref().map_or(0, json_escaped_len)
        + 400
}

fn cloud_pending_upload_set_estimated_size(row: &CloudPendingUploadSet) -> usize {
    json_escaped_len(&row.upload_set_id)
        + json_escaped_len(&row.destination_id)
        + json_escaped_len(&row.source_manifest_digest)
        + 256
}

fn cloud_history_row_estimated_size(row: &CloudHistoryRow) -> usize {
    json_escaped_len(&row.child_key)
        + json_escaped_len(&row.destination_id)
        + json_escaped_len(&row.outcome)
        + row.error_class.as_deref().map_or(0, json_escaped_len)
        + 256
}

fn normalize_remote_key(raw: &str) -> Result<String, DbError> {
    // Canonicalization is byte-exact identity: once validated, we store exactly
    // the caller-provided bytes so every lane dedups on identical keys.
    validate_non_empty_len(raw, "remote_key", 1024)?;
    Ok(raw.to_owned())
}

fn page_limit(limit: u32) -> Result<usize, DbError> {
    if limit == 0 {
        return Err(invalid_input("limit must be >= 1"));
    }
    Ok(usize::try_from(limit.min(MAX_PAGE_LIMIT)).unwrap_or(MAX_PAGE_LIMIT as usize))
}

fn encode_hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

fn decode_hex(value: &str) -> Result<Vec<u8>, DbError> {
    if value.len() % 2 != 0 {
        return Err(invalid_input("cursor hex payload length must be even"));
    }
    let mut out = Vec::with_capacity(value.len() / 2);
    for pair in value.as_bytes().chunks_exact(2) {
        let parsed = std::str::from_utf8(pair)
            .map_err(|_| invalid_input("cursor payload is not valid utf8 hex"))?;
        let byte = u8::from_str_radix(parsed, 16)
            .map_err(|_| invalid_input("cursor payload is not hex"))?;
        out.push(byte);
    }
    Ok(out)
}

fn encode_cursor<T: Serialize>(tag: &str, payload: &T) -> Result<String, DbError> {
    let encoded = serde_json::to_vec(payload).map_err(|e| invalid_input(&e.to_string()))?;
    Ok(format!("{tag}:{}", encode_hex(&encoded)))
}

fn decode_cursor<T: for<'de> Deserialize<'de>>(
    expected_tag: &str,
    cursor: &str,
) -> Result<T, DbError> {
    let (tag, payload) = cursor
        .split_once(':')
        .ok_or_else(|| invalid_input("cursor must contain tag and payload"))?;
    if tag != expected_tag {
        return Err(invalid_input("cursor tag mismatch"));
    }
    let decoded = decode_hex(payload)?;
    serde_json::from_slice(&decoded).map_err(|e| invalid_input(&format!("invalid cursor: {e}")))
}

fn folders_to_categories(folders: &[String]) -> Result<(bool, bool, bool), DbError> {
    if folders.is_empty() {
        return Ok((true, true, true));
    }
    let mut event_sentry = false;
    let mut trip = false;
    let mut bulk = false;
    for folder in folders {
        let Some(category) = folder_class_to_category(folder) else {
            return Err(invalid_input(&format!(
                "unsupported folder class: {folder}"
            )));
        };
        match category {
            "event_sentry" => event_sentry = true,
            "trip" => trip = true,
            "bulk" => bulk = true,
            _ => return Err(invalid_input("unsupported folder class category mapping")),
        };
    }
    Ok((event_sentry, trip, bulk))
}

fn folder_class_to_category(folder_class: &str) -> Option<&'static str> {
    match folder_class {
        "SentryClips" | "SavedClips" => Some("event_sentry"),
        "TeslaTrackMode" => Some("trip"),
        "RecentClips" => Some("bulk"),
        _ => None,
    }
}

fn lease_token_new(id: i64, generation: &str) -> String {
    format!("{id}:{generation}")
}

fn parse_lease_token(token: &str) -> Result<(i64, String), DbError> {
    let (id_raw, generation) = token
        .split_once(':')
        .ok_or_else(|| invalid_input("token must be id:gen"))?;
    let lease_id = id_raw
        .parse::<i64>()
        .map_err(|_| invalid_input("token lease id must be integer"))?;
    validate_non_empty_len(generation, "token generation", 64)?;
    Ok((lease_id, generation.to_owned()))
}

fn new_generation_token() -> String {
    #[allow(clippy::cast_possible_truncation)]
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0_u64, |d| d.as_nanos() as u64);
    let counter = TOKEN_COUNTER.fetch_add(1, Ordering::Relaxed);
    format!(
        "{:016x}{:016x}",
        nanos ^ counter.rotate_left(13),
        counter ^ nanos
    )
}

fn next_completion_seq(tx: &Transaction<'_>, now: i64) -> Result<i64, DbError> {
    let current: i64 = tx.query_row(
        "SELECT completion_seq FROM cloud_meta WHERE id = 1",
        [],
        |row| row.get(0),
    )?;
    let next = current.saturating_add(1);
    tx.execute(
        "UPDATE cloud_meta
            SET completion_seq = ?1,
                updated_at = ?2
          WHERE id = 1",
        params![next, now],
    )?;
    Ok(next)
}

/// Load cloud upload candidates (`state != done`) in stable keyset order.
///
/// Ordering key: `(seq, destination_id, remote_key)`.
pub fn cloud_candidates(
    conn: &Connection,
    folders: &[String],
    after_cursor: Option<&str>,
    limit: u32,
) -> Result<CloudPage<CloudCandidateRow>, DbError> {
    let page_size = page_limit(limit)?;
    let cursor = after_cursor
        .map(|value| decode_cursor::<QueueCursor>("cand-v1", value))
        .transpose()?;
    let (event_sentry, trip, bulk) = folders_to_categories(folders)?;
    if !(event_sentry || trip || bulk) {
        return Ok(CloudPage {
            items: Vec::new(),
            next_cursor: None,
        });
    }
    let (seq, destination_id, remote_key) = cursor.map_or((None, None, None), |value| {
        (
            Some(value.seq),
            Some(value.destination_id),
            Some(value.remote_key),
        )
    });

    let mut stmt = conn.prepare(
        "SELECT
            q.archive_item_id,
            q.child_key,
            CASE
                WHEN q.child_key = '.' THEN a.path
                ELSE a.path || '/' || q.child_key
            END AS source_rel,
            q.destination_id,
            q.remote_key,
            q.total_bytes,
            q.content_sha256,
            q.state,
            q.category,
            q.seq
         FROM cloud_upload_queue q
         JOIN archive_items a ON a.id = q.archive_item_id
         WHERE q.state != 'done'
           AND ((?1 = 1 AND q.category = 'event_sentry')
             OR (?2 = 1 AND q.category = 'trip')
             OR (?3 = 1 AND q.category = 'bulk'))
           AND (
             ?4 IS NULL
             OR q.seq > ?4
             OR (q.seq = ?4 AND q.destination_id > ?5)
             OR (q.seq = ?4 AND q.destination_id = ?5 AND q.remote_key > ?6)
           )
         ORDER BY q.seq ASC, q.destination_id ASC, q.remote_key ASC
         LIMIT ?7",
    )?;
    let rows = stmt.query_map(
        params![
            i64::from(event_sentry),
            i64::from(trip),
            i64::from(bulk),
            seq,
            destination_id,
            remote_key,
            i64::try_from(page_size + 1).unwrap_or(i64::MAX),
        ],
        |row| {
            Ok(CloudCandidateRow {
                archive_item_id: row.get(0)?,
                child_key: row.get(1)?,
                source_rel: row.get(2)?,
                destination_id: row.get(3)?,
                remote_key: row.get(4)?,
                size_bytes: row.get(5)?,
                content_sha256: row.get(6)?,
                state: row.get(7)?,
                category: row.get(8)?,
                seq: row.get(9)?,
            })
        },
    )?;
    let mut queried = Vec::new();
    for row in rows {
        queried.push(row?);
    }
    let (items, has_more) =
        paginate_with_budget(queried, page_size, cloud_candidate_row_estimated_size);
    let next_cursor = if has_more {
        items.last().map(|last| {
            encode_cursor(
                "cand-v1",
                &QueueCursor {
                    seq: last.seq,
                    destination_id: last.destination_id.clone(),
                    remote_key: last.remote_key.clone(),
                },
            )
        })
    } else {
        None
    }
    .transpose()?;
    Ok(CloudPage { items, next_cursor })
}

/// Discover non-durable, live parent archive items to seed the upload queue.
///
/// Ordering key: `archive_items.id`.
pub fn cloud_discover(
    conn: &Connection,
    after_cursor: Option<&str>,
    limit: u32,
) -> Result<CloudPage<CloudDiscoverRow>, DbError> {
    let page_size = page_limit(limit)?;
    let cursor = after_cursor
        .map(|value| decode_cursor::<DiscoverCursor>("discover-v1", value))
        .transpose()?;
    let after_id = cursor.map(|value| value.archive_item_id);
    let config = cloud_config_get(conn)?;
    if !(config.sentry_enabled || config.saved_enabled || config.recent_enabled) {
        return Ok(CloudPage {
            items: Vec::new(),
            next_cursor: None,
        });
    }

    let mut stmt = conn.prepare(
        "SELECT id, folder_class, path, manifest_digest
           FROM archive_items
          WHERE durable = 0
            AND delete_state = 'LIVE'
            AND ((?1 = 1 AND folder_class = 'SentryClips')
              OR (?2 = 1 AND folder_class = 'SavedClips')
              OR (?3 = 1 AND folder_class = 'RecentClips'))
            AND (?4 IS NULL OR id > ?4)
          ORDER BY id ASC
          LIMIT ?5",
    )?;
    let rows = stmt.query_map(
        params![
            i64::from(config.sentry_enabled),
            i64::from(config.saved_enabled),
            i64::from(config.recent_enabled),
            after_id,
            i64::try_from(page_size + 1).unwrap_or(i64::MAX),
        ],
        |row| {
            let folder_class: String = row.get(1)?;
            let category = folder_class_to_category(&folder_class)
                .ok_or_else(|| {
                    rusqlite::Error::InvalidParameterName(
                        "unsupported folder class in archive_items".to_owned(),
                    )
                })?
                .to_owned();
            Ok(CloudDiscoverRow {
                archive_item_id: row.get(0)?,
                folder_class,
                path: row.get(2)?,
                manifest_digest: row.get(3)?,
                category,
            })
        },
    )?;
    let mut queried = Vec::new();
    for row in rows {
        queried.push(row?);
    }
    let (items, has_more) =
        paginate_with_budget(queried, page_size, cloud_discover_row_estimated_size);
    let next_cursor = if has_more {
        items.last().map(|last| {
            encode_cursor(
                "discover-v1",
                &DiscoverCursor {
                    archive_item_id: last.archive_item_id,
                },
            )
        })
    } else {
        None
    }
    .transpose()?;
    Ok(CloudPage { items, next_cursor })
}

/// Load queue rows in stable keyset order.
///
/// Ordering key: `(seq, destination_id, remote_key)`.
pub fn cloud_queue_load(
    conn: &Connection,
    after_cursor: Option<&str>,
    limit: u32,
    upload_set_id: Option<&str>,
) -> Result<CloudPage<CloudQueueRow>, DbError> {
    let page_size = page_limit(limit)?;
    let cursor = after_cursor
        .map(|value| decode_cursor::<QueueCursor>("queue-v1", value))
        .transpose()?;
    let (seq, destination_id, remote_key) = cursor.map_or((None, None, None), |value| {
        (
            Some(value.seq),
            Some(value.destination_id),
            Some(value.remote_key),
        )
    });

    let mut stmt = conn.prepare(
        "SELECT q.archive_item_id,
               q.child_key,
               COALESCE(
                   CASE
                       WHEN q.child_key = '.' THEN a.path
                       ELSE a.path || '/' || q.child_key
                   END,
                   ''
               ) AS source_rel,
               q.destination_id,
               q.remote_key,
               q.category,
               q.seq,
               q.total_bytes,
               q.bytes_uploaded,
               q.expected_hash,
               q.verify_alg,
               q.content_sha256,
               q.state,
               q.attempts,
               q.not_before,
               q.last_error,
               q.upload_set_id
          FROM cloud_upload_queue q
      LEFT JOIN archive_items a ON a.id = q.archive_item_id
          WHERE (?1 IS NULL
                OR q.seq > ?1
                OR (q.seq = ?1 AND q.destination_id > ?2)
                OR (q.seq = ?1 AND q.destination_id = ?2 AND q.remote_key > ?3))
           AND (?4 IS NULL OR q.upload_set_id = ?4)
          ORDER BY q.seq ASC, q.destination_id ASC, q.remote_key ASC
          LIMIT ?5",
    )?;
    let rows = stmt.query_map(
        params![
            seq,
            destination_id,
            remote_key,
            upload_set_id,
            i64::try_from(page_size + 1).unwrap_or(i64::MAX),
        ],
        |row| {
            Ok(CloudQueueRow {
                archive_item_id: row.get(0)?,
                child_key: row.get(1)?,
                source_rel: row.get(2)?,
                destination_id: row.get(3)?,
                remote_key: row.get(4)?,
                category: row.get(5)?,
                seq: row.get(6)?,
                total_bytes: row.get(7)?,
                bytes_uploaded: row.get(8)?,
                expected_hash: row.get(9)?,
                verify_alg: row.get(10)?,
                content_sha256: row.get(11)?,
                state: row.get(12)?,
                attempts: row.get(13)?,
                not_before: row.get(14)?,
                last_error: row.get(15)?,
                upload_set_id: row.get(16)?,
            })
        },
    )?;
    let mut queried = Vec::new();
    for row in rows {
        queried.push(row?);
    }
    let (items, has_more) =
        paginate_with_budget(queried, page_size, cloud_queue_row_estimated_size);
    let next_cursor = if has_more {
        items.last().map(|last| {
            encode_cursor(
                "queue-v1",
                &QueueCursor {
                    seq: last.seq,
                    destination_id: last.destination_id.clone(),
                    remote_key: last.remote_key.clone(),
                },
            )
        })
    } else {
        None
    }
    .transpose()?;
    Ok(CloudPage { items, next_cursor })
}

/// Load current prepared parent upload sets for post-reboot resume.
///
/// "Current" means `finalized_at IS NULL` and `superseded_at IS NULL`.
pub fn cloud_pending_upload_sets_load(
    conn: &Connection,
    after_cursor: Option<&str>,
    limit: u32,
) -> Result<CloudPage<CloudPendingUploadSet>, DbError> {
    let page_size = page_limit(limit)?;
    let cursor = after_cursor
        .map(|value| decode_cursor::<PendingSetsCursor>("pending-sets-v1", value))
        .transpose()?;
    let (created_at, upload_set_id) = cursor.map_or((None, None), |value| {
        (Some(value.created_at), Some(value.upload_set_id))
    });

    let mut stmt = conn.prepare(
        "SELECT upload_set_id, archive_item_id, destination_id, source_manifest_digest,
                expected_child_count, created_at
           FROM cloud_parent_upload_sets
          WHERE finalized_at IS NULL AND superseded_at IS NULL
            AND (?1 IS NULL OR created_at > ?1 OR (created_at = ?1 AND upload_set_id > ?2))
          ORDER BY created_at ASC, upload_set_id ASC
          LIMIT ?3",
    )?;
    let rows = stmt.query_map(
        params![
            created_at,
            upload_set_id,
            i64::try_from(page_size + 1).unwrap_or(i64::MAX),
        ],
        |row| {
            Ok((
                CloudPendingUploadSet {
                    upload_set_id: row.get(0)?,
                    archive_item_id: row.get(1)?,
                    destination_id: row.get(2)?,
                    source_manifest_digest: row.get(3)?,
                    expected_child_count: row.get(4)?,
                },
                row.get::<_, i64>(5)?,
            ))
        },
    )?;
    let mut queried = Vec::new();
    for row in rows {
        queried.push(row?);
    }
    let (items_with_created_at, has_more) = paginate_with_budget(queried, page_size, |(row, _)| {
        cloud_pending_upload_set_estimated_size(row)
    });
    let next_cursor = if has_more {
        items_with_created_at.last().map(|(last, created_at)| {
            encode_cursor(
                "pending-sets-v1",
                &PendingSetsCursor {
                    created_at: *created_at,
                    upload_set_id: last.upload_set_id.clone(),
                },
            )
        })
    } else {
        None
    }
    .transpose()?;
    let items = items_with_created_at
        .into_iter()
        .map(|(row, _)| row)
        .collect();
    Ok(CloudPage { items, next_cursor })
}

/// Idempotent queue upsert by `(destination_id, remote_key)`.
///
/// Returns resulting queue `state`.
pub fn cloud_queue_upsert(
    conn: &Connection,
    item: &CloudQueueUpsertItem,
) -> Result<String, DbError> {
    if item.archive_item_id <= 0 {
        return Err(invalid_input("archive_item_id must be > 0"));
    }
    validate_non_empty_len(&item.child_key, "child_key", 512)?;
    validate_non_empty_len(&item.destination_id, "destination_id", 128)?;
    let remote_key = normalize_remote_key(&item.remote_key)?;
    validate_category(&item.category)?;
    validate_verify_alg(&item.verify_alg)?;
    validate_sha256(&item.content_sha256, "content_sha256")?;
    if let Some(expected_hash) = item.expected_hash.as_deref() {
        validate_non_empty_len(expected_hash, "expected_hash", 256)?;
    }
    if item.seq < 0 || item.total_bytes < 0 {
        return Err(invalid_input("seq and total_bytes must be >= 0"));
    }

    let tx = conn.unchecked_transaction()?;

    let dedup_state = tx
        .query_row(
            "SELECT content_sha256, size_bytes
               FROM cloud_synced_files
              WHERE destination_id = ?1 AND remote_key = ?2",
            params![item.destination_id, remote_key],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)),
        )
        .optional()?
        .map_or("queued", |(hash, size)| {
            if hash == item.content_sha256 && size == item.total_bytes {
                "done"
            } else {
                "parked"
            }
        });

    let existing = tx
        .query_row(
            "SELECT content_sha256, total_bytes, state, upload_set_id
               FROM cloud_upload_queue
              WHERE destination_id = ?1 AND remote_key = ?2",
            params![item.destination_id, remote_key],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, Option<String>>(3)?,
                ))
            },
        )
        .optional()?;

    let state = match existing {
        Some((existing_hash, existing_size, existing_state, existing_upload_set_id)) => {
            if existing_upload_set_id.is_some() {
                return Err(invalid_input("cannot upsert a sealed queue row"));
            }
            if existing_hash != item.content_sha256 || existing_size != item.total_bytes {
                tx.commit()?;
                return Ok("parked".to_owned());
            }
            let next_state = if dedup_state == "done" {
                "done"
            } else {
                existing_state.as_str()
            };
            validate_queue_state(next_state)?;
            let (bytes_uploaded, attempts, not_before, last_error): (
                i64,
                i64,
                Option<i64>,
                Option<String>,
            ) = if next_state == "done" {
                (
                    item.total_bytes,
                    0_i64,
                    Option::<i64>::None,
                    Option::<String>::None,
                )
            } else if next_state == "parked" {
                (
                    0_i64,
                    0_i64,
                    None,
                    Some("hash collision on destination_id+remote_key".to_owned()),
                )
            } else {
                (0_i64, 0_i64, None, None)
            };
            tx.execute(
                "UPDATE cloud_upload_queue
                    SET archive_item_id = ?3,
                        child_key = ?4,
                        category = ?5,
                        seq = ?6,
                        total_bytes = ?7,
                        bytes_uploaded = ?8,
                        expected_hash = ?9,
                        verify_alg = ?10,
                        content_sha256 = ?11,
                        state = ?12,
                        attempts = ?13,
                        not_before = ?14,
                        last_error = ?15
                  WHERE destination_id = ?1 AND remote_key = ?2",
                params![
                    item.destination_id,
                    remote_key,
                    item.archive_item_id,
                    item.child_key,
                    item.category,
                    item.seq,
                    item.total_bytes,
                    bytes_uploaded,
                    item.expected_hash,
                    item.verify_alg,
                    item.content_sha256,
                    next_state,
                    attempts,
                    not_before,
                    last_error,
                ],
            )?;
            next_state.to_owned()
        }
        None => {
            validate_queue_state(dedup_state)?;
            let bytes_uploaded = if dedup_state == "done" {
                item.total_bytes
            } else {
                0_i64
            };
            let last_error = if dedup_state == "parked" {
                Some("hash collision on destination_id+remote_key".to_owned())
            } else {
                None
            };
            tx.execute(
                "INSERT INTO cloud_upload_queue
                    (archive_item_id, child_key, destination_id, remote_key, category, seq,
                     total_bytes, bytes_uploaded, expected_hash, verify_alg, content_sha256,
                     state, attempts, not_before, last_error)
                 VALUES
                    (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, 0, NULL, ?13)",
                params![
                    item.archive_item_id,
                    item.child_key,
                    item.destination_id,
                    remote_key,
                    item.category,
                    item.seq,
                    item.total_bytes,
                    bytes_uploaded,
                    item.expected_hash,
                    item.verify_alg,
                    item.content_sha256,
                    dedup_state,
                    last_error,
                ],
            )?;
            dedup_state.to_owned()
        }
    };
    tx.commit()?;
    Ok(state)
}

/// Manual retry / collision resolution for one queued child.
///
/// Returns resulting queue state.
pub fn cloud_queue_retry(
    conn: &Connection,
    archive_item_id: i64,
    child_key: Option<&str>,
    upload_set_id: Option<&str>,
    resolution: &CloudQueueRetryResolution,
) -> Result<String, DbError> {
    if archive_item_id <= 0 {
        return Err(invalid_input("archive_item_id must be > 0"));
    }
    validate_optional_upload_set_id(upload_set_id)?;
    let tx = conn.unchecked_transaction()?;
    let target = tx
        .query_row(
            "SELECT destination_id, remote_key, content_sha256, total_bytes,
                    verify_alg, expected_hash, upload_set_id
               FROM cloud_upload_queue
              WHERE archive_item_id = ?1
                AND (?2 IS NULL OR child_key = ?2)
                AND (?3 IS NULL OR upload_set_id = ?3)
              ORDER BY seq ASC, destination_id ASC, remote_key ASC
              LIMIT 1",
            params![archive_item_id, child_key, upload_set_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, Option<String>>(5)?,
                    row.get::<_, Option<String>>(6)?,
                ))
            },
        )
        .optional()?;
    let Some((
        destination_id,
        remote_key,
        content_sha256,
        total_bytes,
        row_verify_alg,
        row_expected_hash,
        row_upload_set_id,
    )) = target
    else {
        return Err(invalid_input("target queue row not found"));
    };
    enforce_upload_set_fence(&tx, row_upload_set_id.as_deref(), upload_set_id)?;

    let resulting_state = match resolution {
        CloudQueueRetryResolution::KeepExisting => {
            let evidence = tx
                .query_row(
                    "SELECT content_sha256, size_bytes, verify_alg, verify_value
                       FROM cloud_synced_files
                      WHERE destination_id = ?1 AND remote_key = ?2",
                    params![destination_id, remote_key],
                    |row| {
                        Ok((
                            row.get::<_, String>(0)?,
                            row.get::<_, i64>(1)?,
                            row.get::<_, String>(2)?,
                            row.get::<_, Option<String>>(3)?,
                        ))
                    },
                )
                .optional()?;
            let has_matching_evidence =
                evidence.is_some_and(|(hash, size, synced_alg, synced_value)| {
                    let base = hash == content_sha256 && size == total_bytes;
                    if row_upload_set_id.is_some() {
                        base && synced_alg == row_verify_alg
                            && synced_value.unwrap_or_default()
                                == row_expected_hash.clone().unwrap_or_default()
                    } else {
                        base
                    }
                });
            if has_matching_evidence {
                tx.execute(
                    "UPDATE cloud_upload_queue
                        SET state = 'done',
                            bytes_uploaded = total_bytes,
                            attempts = 0,
                            not_before = NULL,
                            last_error = NULL
                      WHERE destination_id = ?1
                        AND remote_key = ?2
                        AND upload_set_id IS ?3",
                    params![destination_id, remote_key, row_upload_set_id],
                )?;
                "done".to_owned()
            } else {
                tx.query_row(
                    "SELECT state FROM cloud_upload_queue
                      WHERE destination_id = ?1 AND remote_key = ?2",
                    params![destination_id, remote_key],
                    |r| r.get::<_, String>(0),
                )?
            }
        }
        CloudQueueRetryResolution::Replace => {
            tx.execute(
                "UPDATE cloud_upload_queue
                    SET state = 'queued',
                        bytes_uploaded = 0,
                        attempts = 0,
                        not_before = NULL,
                        last_error = NULL
                  WHERE destination_id = ?1 AND remote_key = ?2",
                params![destination_id, remote_key],
            )?;
            "queued".to_owned()
        }
        CloudQueueRetryResolution::Rekey {
            remote_key: new_key,
        } => {
            if row_upload_set_id.is_some() {
                return Err(invalid_input("cannot rekey a sealed queue row"));
            }
            let new_key = normalize_remote_key(new_key)?;
            let dedup_state = tx
                .query_row(
                    "SELECT content_sha256, size_bytes
                       FROM cloud_synced_files
                      WHERE destination_id = ?1 AND remote_key = ?2",
                    params![destination_id, new_key],
                    |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)),
                )
                .optional()?
                .map_or("queued", |(hash, size)| {
                    if hash == content_sha256 && size == total_bytes {
                        "done"
                    } else {
                        "parked"
                    }
                });
            let bytes_uploaded = if dedup_state == "done" {
                total_bytes
            } else {
                0
            };
            let last_error = if dedup_state == "parked" {
                Some("hash collision on destination_id+remote_key".to_owned())
            } else {
                None
            };
            tx.execute(
                "UPDATE cloud_upload_queue
                    SET remote_key = ?3,
                        state = ?4,
                        bytes_uploaded = ?5,
                        attempts = 0,
                        not_before = NULL,
                        last_error = ?6
                  WHERE destination_id = ?1 AND remote_key = ?2",
                params![
                    destination_id,
                    remote_key,
                    new_key,
                    dedup_state,
                    bytes_uploaded,
                    last_error,
                ],
            )?;
            dedup_state.to_owned()
        }
    };
    tx.commit()?;

    Ok(resulting_state)
}

/// Acquire an upload lease (`kind='upload'`) with boot-scoped monotonic expiry.
pub fn upload_lease_acquire(
    conn: &Connection,
    boot: &BootContext,
    archive_item_id: i64,
    ttl_ms: u32,
) -> Result<UploadLeaseAcquireResult, DbError> {
    if archive_item_id <= 0 {
        return Err(invalid_input("archive_item_id must be > 0"));
    }
    if ttl_ms == 0 {
        return Err(invalid_input("ttl_ms must be >= 1"));
    }
    let state = conn
        .query_row(
            "SELECT delete_state FROM archive_items WHERE id = ?1",
            params![archive_item_id],
            |row| row.get::<_, String>(0),
        )
        .optional()?;
    if state.as_deref() != Some("LIVE") {
        return Ok(UploadLeaseAcquireResult {
            granted: false,
            token: None,
            boot_id: None,
            expires_mono_ms: None,
        });
    }

    let now_mono = boot.mono_now_ms();
    let expires_mono_ms = now_mono.saturating_add(i64::from(ttl_ms));
    let generation = new_generation_token();
    conn.execute(
        "INSERT INTO leases
            (archive_item_id, kind, holder, gen, boot_id, acquired_wall, expires_mono_ms, preempt_req)
         VALUES (?1, 'upload', 'uploadd:cloud', ?2, ?3, ?4, ?5, 0)",
        params![
            archive_item_id,
            generation,
            boot.boot_id(),
            now_epoch_s(),
            expires_mono_ms
        ],
    )?;
    let lease_id = conn.last_insert_rowid();
    Ok(UploadLeaseAcquireResult {
        granted: true,
        token: Some(lease_token_new(lease_id, &generation)),
        boot_id: Some(boot.boot_id().to_owned()),
        expires_mono_ms: Some(expires_mono_ms),
    })
}

/// Renew an upload lease token.
pub fn upload_lease_renew(
    conn: &Connection,
    boot: &BootContext,
    token: &str,
    ttl_ms: u32,
) -> Result<UploadLeaseRenewResult, DbError> {
    if ttl_ms == 0 {
        return Err(invalid_input("ttl_ms must be >= 1"));
    }
    let (lease_id, generation) = parse_lease_token(token)?;
    let row = conn
        .query_row(
            "SELECT l.gen, l.boot_id, l.expires_mono_ms, l.archive_item_id, l.kind, a.delete_state
               FROM leases l
               JOIN archive_items a ON a.id = l.archive_item_id
              WHERE l.id = ?1",
            params![lease_id],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, i64>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                ))
            },
        )
        .optional()?;
    let Some((row_gen, row_boot, expires, _archive_item_id, kind, delete_state)) = row else {
        return Ok(UploadLeaseRenewResult {
            ok: false,
            expires_mono_ms: None,
        });
    };
    if row_gen != generation
        || row_boot != boot.boot_id()
        || kind != "upload"
        || delete_state != "LIVE"
        || expires <= boot.mono_now_ms()
    {
        return Ok(UploadLeaseRenewResult {
            ok: false,
            expires_mono_ms: None,
        });
    }
    let renewed = boot.mono_now_ms().saturating_add(i64::from(ttl_ms));
    conn.execute(
        "UPDATE leases SET expires_mono_ms = ?2 WHERE id = ?1",
        params![lease_id, renewed],
    )?;
    Ok(UploadLeaseRenewResult {
        ok: true,
        expires_mono_ms: Some(renewed),
    })
}

/// Release an upload lease token.
pub fn upload_lease_release(
    conn: &Connection,
    boot: &BootContext,
    token: &str,
) -> Result<bool, DbError> {
    let (lease_id, generation) = parse_lease_token(token)?;
    let rows = conn.execute(
        "DELETE FROM leases
          WHERE id = ?1
            AND gen = ?2
            AND boot_id = ?3
            AND kind = 'upload'",
        params![lease_id, generation, boot.boot_id()],
    )?;
    Ok(rows > 0)
}

/// Commit one uploaded queue item atomically and idempotently on `attempt_id`.
pub fn cloud_upload_commit(
    conn: &mut Connection,
    queue_pk: &CloudQueuePk,
    attempt_id: &str,
    hash: &str,
    hash_alg: &str,
    size: i64,
    upload_set_id: Option<&str>,
) -> Result<CloudUploadCommitResult, DbError> {
    validate_non_empty_len(&queue_pk.destination_id, "destination_id", 128)?;
    let remote_key = normalize_remote_key(&queue_pk.remote_key)?;
    validate_non_empty_len(attempt_id, "attempt_id", 128)?;
    validate_verify_alg(hash_alg)?;
    let verify_value = validate_backend_hash(hash, hash_alg)?;
    validate_optional_upload_set_id(upload_set_id)?;
    if size < 0 {
        return Err(invalid_input("size must be >= 0"));
    }

    let now = now_epoch_s();
    let tx = conn.transaction()?;
    let previous = tx
        .query_row(
            "SELECT destination_id, remote_key, outcome, hash, size_bytes, upload_set_id
               FROM cloud_upload_attempts
              WHERE attempt_id = ?1",
            params![attempt_id],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, i64>(4)?,
                    r.get::<_, Option<String>>(5)?,
                ))
            },
        )
        .optional()?;
    if let Some((dest, key, outcome, prior_hash, prior_size, prior_upload_set_id)) = previous {
        if dest != queue_pk.destination_id || key != remote_key {
            return Err(invalid_input(
                "attempt_id already used with a different queue primary key",
            ));
        }
        if prior_upload_set_id.as_deref() != upload_set_id {
            return Err(invalid_input(
                "attempt_id already used with a different upload_set_id",
            ));
        }
        let prior_verify = tx
            .query_row(
                "SELECT verify_alg, verify_value, content_sha256
                   FROM cloud_synced_files
                  WHERE destination_id = ?1 AND remote_key = ?2",
                params![queue_pk.destination_id, remote_key],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, Option<String>>(1)?,
                        r.get::<_, String>(2)?,
                    ))
                },
            )
            .optional()?;
        let Some((prior_alg, prior_verify_value, prior_content_sha256)) = prior_verify else {
            return Err(invalid_input(
                "attempt_id already used with a different upload outcome",
            ));
        };
        let prior_hash_matches = if prior_alg == "none" {
            hash.is_empty()
        } else {
            prior_verify_value.as_deref() == Some(hash)
        };
        if outcome != "uploaded"
            || prior_size != size
            || prior_hash != prior_content_sha256
            || prior_alg != hash_alg
            || !prior_hash_matches
        {
            return Err(invalid_input(
                "attempt_id already used with a different upload outcome",
            ));
        }
        // Deprecated wire field: durability is decided solely by
        // `cloud_finalize_parent_upload` (contract §7.5), so commit always reports false.
        return Ok(CloudUploadCommitResult {
            ok: true,
            durable_parent: false,
        });
    }

    let row = tx
        .query_row(
            "SELECT archive_item_id, child_key, total_bytes, expected_hash, verify_alg,
                    content_sha256, state, upload_set_id
               FROM cloud_upload_queue
              WHERE destination_id = ?1 AND remote_key = ?2",
            params![queue_pk.destination_id, remote_key],
            |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, Option<String>>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                    r.get::<_, String>(6)?,
                    r.get::<_, Option<String>>(7)?,
                ))
            },
        )
        .optional()?;
    let Some((
        archive_item_id,
        child_key,
        total_bytes,
        expected_hash,
        verify_alg,
        content_sha256,
        state,
        row_upload_set_id,
    )) = row
    else {
        return Err(invalid_input("queue row does not exist"));
    };
    enforce_upload_set_fence(&tx, row_upload_set_id.as_deref(), upload_set_id)?;
    if state == "parked" {
        return Err(invalid_input("cannot commit a parked queue row"));
    }
    if verify_alg == "none" {
        if !hash.is_empty() {
            return Err(invalid_input(
                "hash must be empty when queue verify_alg is none",
            ));
        }
    } else {
        if hash_alg != verify_alg {
            return Err(invalid_input("hash_alg must match queue verify_alg"));
        }
        if let Some(expected_hash) = expected_hash.as_deref() {
            if hash != expected_hash {
                return Err(invalid_input("hash must match queue expected_hash"));
            }
        }
    }
    if total_bytes != size {
        return Err(invalid_input("size must match queue total_bytes"));
    }
    let existing_synced = tx
        .query_row(
            "SELECT verify_alg, verify_value, content_sha256, size_bytes, completion_seq
               FROM cloud_synced_files
              WHERE destination_id = ?1 AND remote_key = ?2",
            params![queue_pk.destination_id, remote_key],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, i64>(3)?,
                    r.get::<_, i64>(4)?,
                ))
            },
        )
        .optional()?;
    if let Some((_, _, synced_content_sha256, _, _)) = existing_synced.as_ref() {
        if synced_content_sha256.as_str() != content_sha256.as_str() {
            return Err(invalid_input(
                "destination key already has a different synced content hash",
            ));
        }
    }
    if state == "done" {
        // The child is already committed. A re-commit arriving with a fresh attempt_id
        // (e.g. a lost-ack retry) must be idempotent: return ok WITHOUT allocating a new
        // completion_seq or appending a duplicate cloud_sync_history row — a duplicate
        // history row would inflate the DERIVED sync stats. Fail closed unless this
        // commit asserts the identical content proof as the recorded synced record,
        // mirroring the attempt-idempotency consistency check above. Never flips durable
        // (contract §7.5).
        let Some((synced_alg, synced_value, _, synced_size, synced_completion_seq)) =
            existing_synced
        else {
            return Err(invalid_input("queue row is done but has no synced record"));
        };
        let hash_matches = if synced_alg == "none" {
            hash.is_empty()
        } else {
            synced_value.as_deref() == Some(hash)
        };
        if synced_alg.as_str() != hash_alg || synced_size != size || !hash_matches {
            return Err(invalid_input(
                "queue row is done with a different upload outcome",
            ));
        }
        tx.execute(
            "INSERT INTO cloud_upload_attempts
                (attempt_id, destination_id, remote_key, outcome, durable_parent, completion_seq,
                 state_after, hash, size_bytes, created_at, upload_set_id)
             VALUES (?1, ?2, ?3, 'uploaded', 0, ?4, 'done', ?5, ?6, ?7, ?8)",
            params![
                attempt_id,
                queue_pk.destination_id,
                remote_key,
                synced_completion_seq,
                content_sha256,
                size,
                now,
                upload_set_id,
            ],
        )?;
        tx.commit()?;
        return Ok(CloudUploadCommitResult {
            ok: true,
            durable_parent: false,
        });
    }

    let completion_seq = next_completion_seq(&tx, now)?;
    tx.execute(
        "INSERT INTO cloud_synced_files
            (destination_id, remote_key, archive_item_id, child_key, content_sha256, verify_alg,
             verify_value, size_bytes, synced_at, completion_seq)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
         ON CONFLICT(destination_id, remote_key) DO UPDATE SET
            archive_item_id = excluded.archive_item_id,
            child_key = excluded.child_key,
            content_sha256 = excluded.content_sha256,
            verify_alg = excluded.verify_alg,
            verify_value = excluded.verify_value,
            size_bytes = excluded.size_bytes,
            synced_at = excluded.synced_at,
            completion_seq = excluded.completion_seq",
        params![
            queue_pk.destination_id,
            remote_key,
            archive_item_id,
            child_key,
            content_sha256,
            hash_alg,
            verify_value,
            size,
            now,
            completion_seq
        ],
    )?;
    tx.execute(
        "INSERT INTO cloud_sync_history
            (completion_seq, archive_item_id, child_key, destination_id, outcome, size_bytes, at, error_class)
         VALUES (?1, ?2, ?3, ?4, 'uploaded', ?5, ?6, NULL)",
        params![
            completion_seq,
            archive_item_id,
            child_key,
            queue_pk.destination_id,
            size,
            now
        ],
    )?;
    tx.execute(
        "UPDATE cloud_upload_queue
            SET state = 'done',
                bytes_uploaded = total_bytes,
                not_before = NULL,
                last_error = NULL
          WHERE destination_id = ?1
            AND remote_key = ?2
            AND upload_set_id IS ?3",
        params![queue_pk.destination_id, remote_key, upload_set_id],
    )?;
    let durable_parent = false;
    tx.execute(
        "INSERT INTO cloud_upload_attempts
            (attempt_id, destination_id, remote_key, outcome, durable_parent, completion_seq,
             state_after, hash, size_bytes, created_at, upload_set_id)
         VALUES (?1, ?2, ?3, 'uploaded', ?4, ?5, 'done', ?6, ?7, ?8, ?9)",
        params![
            attempt_id,
            queue_pk.destination_id,
            remote_key,
            i64::from(durable_parent),
            completion_seq,
            content_sha256,
            size,
            now,
            upload_set_id,
        ],
    )?;
    tx.commit()?;
    Ok(CloudUploadCommitResult {
        ok: true,
        durable_parent,
    })
}

/// Record one failed upload idempotently on `attempt_id`.
pub fn cloud_upload_fail(
    conn: &mut Connection,
    queue_pk: &CloudQueuePk,
    attempt_id: &str,
    error_class: &str,
    not_before: Option<i64>,
    terminal: bool,
    upload_set_id: Option<&str>,
) -> Result<CloudUploadFailResult, DbError> {
    validate_non_empty_len(&queue_pk.destination_id, "destination_id", 128)?;
    let remote_key = normalize_remote_key(&queue_pk.remote_key)?;
    validate_non_empty_len(attempt_id, "attempt_id", 128)?;
    validate_non_empty_len(error_class, "error_class", 128)?;
    validate_optional_upload_set_id(upload_set_id)?;
    if let Some(value) = not_before {
        if value < 0 {
            return Err(invalid_input("not_before must be >= 0"));
        }
    }

    let now = now_epoch_s();
    let tx = conn.transaction()?;
    let previous = tx
        .query_row(
            "SELECT destination_id, remote_key, outcome, state_after, upload_set_id
               FROM cloud_upload_attempts
              WHERE attempt_id = ?1",
            params![attempt_id],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, Option<String>>(4)?,
                ))
            },
        )
        .optional()?;
    if let Some((dest, key, outcome, state_after, prior_upload_set_id)) = previous {
        if dest != queue_pk.destination_id || key != remote_key {
            return Err(invalid_input(
                "attempt_id already used with a different queue primary key",
            ));
        }
        if prior_upload_set_id.as_deref() != upload_set_id {
            return Err(invalid_input(
                "attempt_id already used with a different upload_set_id",
            ));
        }
        if outcome != "failed" {
            return Err(invalid_input(
                "attempt_id already used by cloud_upload_commit",
            ));
        }
        return Ok(CloudUploadFailResult {
            ok: true,
            state: state_after,
        });
    }

    let row = tx
        .query_row(
            "SELECT archive_item_id, child_key, total_bytes, upload_set_id
               FROM cloud_upload_queue
              WHERE destination_id = ?1 AND remote_key = ?2",
            params![queue_pk.destination_id, remote_key],
            |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, Option<String>>(3)?,
                ))
            },
        )
        .optional()?;
    let Some((archive_item_id, child_key, total_bytes, row_upload_set_id)) = row else {
        return Err(invalid_input("queue row does not exist"));
    };
    enforce_upload_set_fence(&tx, row_upload_set_id.as_deref(), upload_set_id)?;
    let state = if terminal { "parked" } else { "failed" };
    let completion_seq = next_completion_seq(&tx, now)?;
    tx.execute(
        "UPDATE cloud_upload_queue
            SET state = ?3,
                attempts = attempts + 1,
                not_before = ?4,
                last_error = ?5
          WHERE destination_id = ?1 AND remote_key = ?2",
        params![
            queue_pk.destination_id,
            remote_key,
            state,
            not_before,
            error_class
        ],
    )?;
    tx.execute(
        "INSERT INTO cloud_sync_history
            (completion_seq, archive_item_id, child_key, destination_id, outcome, size_bytes, at, error_class)
         VALUES (?1, ?2, ?3, ?4, 'failed', ?5, ?6, ?7)",
        params![
            completion_seq,
            archive_item_id,
            child_key,
            queue_pk.destination_id,
            total_bytes,
            now,
            error_class
        ],
    )?;
    tx.execute(
        "INSERT INTO cloud_upload_attempts
            (attempt_id, destination_id, remote_key, outcome, durable_parent, completion_seq,
             state_after, hash, size_bytes, created_at, upload_set_id)
         VALUES (?1, ?2, ?3, 'failed', 0, ?4, ?5, '', ?6, ?7, ?8)",
        params![
            attempt_id,
            queue_pk.destination_id,
            remote_key,
            completion_seq,
            state,
            total_bytes,
            now,
            upload_set_id,
        ],
    )?;
    tx.commit()?;
    Ok(CloudUploadFailResult {
        ok: true,
        state: state.to_owned(),
    })
}

/// Return derived cloud counters from `cloud_sync_history` above baseline.
pub fn cloud_stats_get(conn: &Connection) -> Result<CloudStats, DbError> {
    let (baseline_seq, baseline_at): (i64, i64) = conn.query_row(
        "SELECT stats_baseline_seq, stats_baseline_at FROM cloud_meta WHERE id = 1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let (synced_count, synced_bytes): (i64, i64) = conn.query_row(
        "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0)
           FROM cloud_sync_history
          WHERE outcome = 'uploaded'
            AND completion_seq > ?1",
        params![baseline_seq],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    Ok(CloudStats {
        synced_count,
        synced_bytes,
        since_at: baseline_at,
    })
}

/// Reset derived counters baseline to current completion sequence.
pub fn cloud_stats_reset(conn: &Connection) -> Result<i64, DbError> {
    let now = now_epoch_s();
    let completion_seq: i64 = conn.query_row(
        "SELECT completion_seq FROM cloud_meta WHERE id = 1",
        [],
        |r| r.get(0),
    )?;
    conn.execute(
        "UPDATE cloud_meta
            SET stats_baseline_seq = ?1,
                stats_baseline_at = ?2,
                updated_at = ?2
          WHERE id = 1",
        params![completion_seq, now],
    )?;
    Ok(completion_seq)
}

/// Load typed non-secret cloud config.
pub fn cloud_config_get(conn: &Connection) -> Result<CloudConfig, DbError> {
    Ok(conn.query_row(
        "SELECT sentry_enabled, saved_enabled, recent_enabled, sentry_priority, saved_priority,
                recent_priority, reserve_gb, max_attempts, base_backoff_secs,
                keep_until_backed_up, auto_sync
           FROM cloud_provider_config
          WHERE id = 1",
        [],
        |r| {
            Ok(CloudConfig {
                sentry_enabled: r.get::<_, i64>(0)? == 1,
                saved_enabled: r.get::<_, i64>(1)? == 1,
                recent_enabled: r.get::<_, i64>(2)? == 1,
                sentry_priority: r.get(3)?,
                saved_priority: r.get(4)?,
                recent_priority: r.get(5)?,
                reserve_gb: r.get(6)?,
                max_attempts: r.get(7)?,
                base_backoff_secs: r.get(8)?,
                keep_until_backed_up: r.get::<_, i64>(9)? == 1,
                auto_sync: r.get::<_, i64>(10)? == 1,
            })
        },
    )?)
}

/// Validate and persist cloud config.
pub fn cloud_config_put(conn: &Connection, config: &CloudConfig) -> Result<CloudConfig, DbError> {
    if config.sentry_priority < 0 || config.saved_priority < 0 || config.recent_priority < 0 {
        return Err(invalid_input("folder priority must be >= 0"));
    }
    if config.reserve_gb < 0 {
        return Err(invalid_input("reserve_gb must be >= 0"));
    }
    if config.max_attempts < 1 {
        return Err(invalid_input("max_attempts must be >= 1"));
    }
    if config.base_backoff_secs < 0 {
        return Err(invalid_input("base_backoff_secs must be >= 0"));
    }
    conn.execute(
        "UPDATE cloud_provider_config
            SET sentry_enabled = ?1,
                saved_enabled = ?2,
                recent_enabled = ?3,
                sentry_priority = ?4,
                saved_priority = ?5,
                recent_priority = ?6,
                reserve_gb = ?7,
                max_attempts = ?8,
                base_backoff_secs = ?9,
                keep_until_backed_up = ?10,
                auto_sync = ?11,
                updated_at = ?12
          WHERE id = 1",
        params![
            i64::from(config.sentry_enabled),
            i64::from(config.saved_enabled),
            i64::from(config.recent_enabled),
            config.sentry_priority,
            config.saved_priority,
            config.recent_priority,
            config.reserve_gb,
            config.max_attempts,
            config.base_backoff_secs,
            i64::from(config.keep_until_backed_up),
            i64::from(config.auto_sync),
            now_epoch_s(),
        ],
    )?;
    cloud_config_get(conn)
}

/// Load history rows in stable keyset order.
///
/// Ordering key: `(completion_seq, id)`.
pub fn cloud_history_load(
    conn: &Connection,
    after_cursor: Option<&str>,
    limit: u32,
) -> Result<CloudPage<CloudHistoryRow>, DbError> {
    let page_size = page_limit(limit)?;
    let cursor = after_cursor
        .map(|value| decode_cursor::<HistoryCursor>("hist-v1", value))
        .transpose()?;
    let (completion_seq, id) = cursor.map_or((None, None), |value| {
        (Some(value.completion_seq), Some(value.id))
    });

    let mut stmt = conn.prepare(
        "SELECT id, completion_seq, archive_item_id, child_key, destination_id, outcome, size_bytes, at, error_class
           FROM cloud_sync_history
          WHERE (?1 IS NULL
                 OR completion_seq > ?1
                 OR (completion_seq = ?1 AND id > ?2))
          ORDER BY completion_seq ASC, id ASC
          LIMIT ?3",
    )?;
    let rows = stmt.query_map(
        params![
            completion_seq,
            id,
            i64::try_from(page_size + 1).unwrap_or(i64::MAX),
        ],
        |row| {
            Ok(CloudHistoryRow {
                id: row.get(0)?,
                completion_seq: row.get(1)?,
                archive_item_id: row.get(2)?,
                child_key: row.get(3)?,
                destination_id: row.get(4)?,
                outcome: row.get(5)?,
                size_bytes: row.get(6)?,
                at: row.get(7)?,
                error_class: row.get(8)?,
            })
        },
    )?;
    let mut queried = Vec::new();
    for row in rows {
        queried.push(row?);
    }
    let (items, has_more) =
        paginate_with_budget(queried, page_size, cloud_history_row_estimated_size);
    let next_cursor = if has_more {
        items.last().map(|last| {
            encode_cursor(
                "hist-v1",
                &HistoryCursor {
                    completion_seq: last.completion_seq,
                    id: last.id,
                },
            )
        })
    } else {
        None
    }
    .transpose()?;
    Ok(CloudPage { items, next_cursor })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use rusqlite::params;

    use super::{
        CloudCandidateRow, CloudDiscoverRow, CloudHistoryRow, CloudPendingUploadSet, CloudQueuePk,
        CloudQueueRetryResolution, CloudQueueRow, CloudQueueUpsertItem,
        cloud_candidate_row_estimated_size, cloud_candidates, cloud_config_get, cloud_config_put,
        cloud_discover, cloud_discover_row_estimated_size, cloud_history_load,
        cloud_history_row_estimated_size, cloud_pending_upload_set_estimated_size,
        cloud_pending_upload_sets_load, cloud_queue_load, cloud_queue_retry,
        cloud_queue_row_estimated_size, cloud_queue_upsert, cloud_stats_get, cloud_stats_reset,
        cloud_upload_commit, cloud_upload_fail, json_escaped_len, upload_lease_acquire,
        upload_lease_release, upload_lease_renew,
    };
    use crate::db::mutations::BootContext;
    use crate::db::open_in_memory;
    use crate::proto::MAX_REQUEST_FRAME;

    fn insert_archive_item(conn: &rusqlite::Connection, path: &str) -> i64 {
        conn.execute(
            "INSERT INTO archive_items
                (folder_class, path, size_bytes, file_count, archived_at, created_at, updated_at)
             VALUES ('RecentClips', ?1, 1024, 1, 100, 0, 0)",
            params![path],
        )
        .unwrap();
        conn.last_insert_rowid()
    }

    fn insert_archive_item_with_class(
        conn: &rusqlite::Connection,
        folder_class: &str,
        path: &str,
        durable: i64,
        delete_state: &str,
    ) -> i64 {
        conn.execute(
            "INSERT INTO archive_items
                (folder_class, path, size_bytes, file_count, archived_at, durable, delete_state, created_at, updated_at)
             VALUES (?1, ?2, 1024, 1, 100, ?3, ?4, 0, 0)",
            params![folder_class, path, durable, delete_state],
        )
        .unwrap();
        conn.last_insert_rowid()
    }

    #[allow(clippy::too_many_arguments)]
    fn upsert_item(
        conn: &rusqlite::Connection,
        archive_item_id: i64,
        destination_id: &str,
        remote_key: &str,
        child_key: &str,
        seq: i64,
        total_bytes: i64,
        content_sha256: &str,
    ) -> String {
        cloud_queue_upsert(
            conn,
            &CloudQueueUpsertItem {
                archive_item_id,
                child_key: child_key.to_owned(),
                destination_id: destination_id.to_owned(),
                remote_key: remote_key.to_owned(),
                category: "bulk".to_owned(),
                seq,
                total_bytes,
                content_sha256: content_sha256.to_owned(),
                expected_hash: Some(content_sha256.to_owned()),
                verify_alg: "sha256".to_owned(),
            },
        )
        .unwrap()
    }

    fn seal_queue_row(
        conn: &rusqlite::Connection,
        archive_item_id: i64,
        destination_id: &str,
        remote_key: &str,
        upload_set_id: &str,
        superseded_at: Option<i64>,
    ) {
        conn.execute(
            "INSERT INTO cloud_parent_upload_sets
                (upload_set_id, archive_item_id, destination_id, source_manifest_digest,
                 request_digest, expected_child_count, created_at, finalized_at, superseded_at)
             VALUES (?1, ?2, ?3,
                     '11111111111111111111111111111111',
                     '2222222222222222222222222222222222222222222222222222222222222222',
                     1, 0, NULL, ?4)",
            params![
                upload_set_id,
                archive_item_id,
                destination_id,
                superseded_at
            ],
        )
        .unwrap();
        conn.execute(
            "UPDATE cloud_upload_queue
                SET upload_set_id = ?1
              WHERE destination_id = ?2 AND remote_key = ?3",
            params![upload_set_id, destination_id, remote_key],
        )
        .unwrap();
    }

    #[test]
    fn commit_rejects_wrong_upload_set_id_on_sealed_row() {
        let mut conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/commit-sealed-fence");
        let hash = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let set_a = "11111111111111111111111111111111";
        let set_b = "22222222222222222222222222222222";
        upsert_item(
            &conn,
            parent,
            "dest",
            "rk/fence",
            "child-fence",
            1,
            10,
            hash,
        );
        seal_queue_row(&conn, parent, "dest", "rk/fence", set_a, None);
        let other_parent = insert_archive_item(&conn, "archive/commit-sealed-fence-other");
        conn.execute(
            "INSERT INTO cloud_parent_upload_sets
                (upload_set_id, archive_item_id, destination_id, source_manifest_digest,
                 request_digest, expected_child_count, created_at, finalized_at, superseded_at)
             VALUES (?1, ?2, 'dest',
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                     'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                     1, 0, NULL, NULL)",
            params![set_b, other_parent],
        )
        .unwrap();

        let wrong = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/fence".to_owned(),
            },
            "attempt-wrong-set",
            hash,
            "sha256",
            10,
            Some(set_b),
        );
        assert!(wrong.is_err());

        let ok = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/fence".to_owned(),
            },
            "attempt-right-set",
            hash,
            "sha256",
            10,
            Some(set_a),
        )
        .unwrap();
        assert!(ok.ok);
    }

    #[test]
    fn commit_rejects_superseded_upload_set() {
        let mut conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/commit-superseded-fence");
        let hash = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        let set_a = "33333333333333333333333333333333";
        upsert_item(
            &conn,
            parent,
            "dest",
            "rk/superseded",
            "child-superseded",
            1,
            10,
            hash,
        );
        seal_queue_row(&conn, parent, "dest", "rk/superseded", set_a, Some(5));

        let superseded = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/superseded".to_owned(),
            },
            "attempt-superseded",
            hash,
            "sha256",
            10,
            Some(set_a),
        );
        assert!(superseded.is_err());
    }

    #[test]
    fn commit_unsealed_row_rejects_supplied_set_id() {
        let mut conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/commit-unsealed-fence");
        let hash = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
        let set_a = "44444444444444444444444444444444";
        upsert_item(
            &conn,
            parent,
            "dest",
            "rk/unsealed",
            "child-unsealed",
            1,
            10,
            hash,
        );

        let wrong = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/unsealed".to_owned(),
            },
            "attempt-unsealed-wrong",
            hash,
            "sha256",
            10,
            Some(set_a),
        );
        assert!(wrong.is_err());

        let ok = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/unsealed".to_owned(),
            },
            "attempt-unsealed-ok",
            hash,
            "sha256",
            10,
            None,
        )
        .unwrap();
        assert!(ok.ok);
    }

    #[test]
    fn cloud_upload_commit_is_idempotent_and_never_flips_durable() {
        let mut conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/p1");
        let hash_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let hash_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        upsert_item(&conn, parent, "dest", "rk/a", "child-a", 1, 10, hash_a);
        upsert_item(&conn, parent, "dest", "rk/b", "child-b", 2, 20, hash_b);

        let first = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/a".to_owned(),
            },
            "attempt-a",
            hash_a,
            "sha256",
            10,
            None,
        )
        .unwrap();
        assert!(first.ok);
        assert!(!first.durable_parent);

        let replay = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/a".to_owned(),
            },
            "attempt-a",
            hash_a,
            "sha256",
            10,
            None,
        )
        .unwrap();
        assert_eq!(replay, first);
        let conflicting_replay = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/a".to_owned(),
            },
            "attempt-a",
            hash_b,
            "sha256",
            10,
            None,
        );
        assert!(conflicting_replay.is_err());
        let uploaded_rows: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM cloud_sync_history WHERE outcome='uploaded'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(uploaded_rows, 1);
        let durable: i64 = conn
            .query_row(
                "SELECT durable FROM archive_items WHERE id = ?1",
                params![parent],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(durable, 0);

        let second = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/b".to_owned(),
            },
            "attempt-b",
            hash_b,
            "sha256",
            20,
            None,
        )
        .unwrap();
        assert!(second.ok);
        // Commit never flips durable; only cloud_finalize_parent_upload does.
        assert!(!second.durable_parent);
        let durable: i64 = conn
            .query_row(
                "SELECT durable FROM archive_items WHERE id = ?1",
                params![parent],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(durable, 0);
    }

    #[test]
    fn commit_done_recommit_records_attempt_row() {
        let mut conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/done-recommit");
        let hash = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        upsert_item(&conn, parent, "dest", "rk/a", "child-a", 1, 10, hash);

        let first = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/a".to_owned(),
            },
            "attempt-1",
            hash,
            "sha256",
            10,
            None,
        )
        .unwrap();
        assert!(first.ok);

        let seq_after_first: i64 = conn
            .query_row(
                "SELECT completion_seq FROM cloud_meta WHERE id = 1",
                [],
                |r| r.get(0),
            )
            .unwrap();

        // A re-commit with a FRESH attempt_id (previous attempt row absent — e.g. a
        // lost-ack retry) on an already-done row must be idempotent, not a duplicate.
        let recommit = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/a".to_owned(),
            },
            "attempt-2-fresh",
            hash,
            "sha256",
            10,
            None,
        )
        .unwrap();
        assert!(recommit.ok);
        assert!(!recommit.durable_parent);

        // Exactly ONE uploaded history row — no duplicate inflating the derived stats.
        let uploaded_rows: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM cloud_sync_history WHERE outcome='uploaded'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(uploaded_rows, 1);
        // The completion_seq allocator was not advanced by the idempotent re-commit.
        let seq_after_recommit: i64 = conn
            .query_row(
                "SELECT completion_seq FROM cloud_meta WHERE id = 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(seq_after_recommit, seq_after_first);
        // The idempotent re-commit records an attempts ledger row using the existing
        // synced completion_seq, without appending cloud_sync_history.
        let attempts: i64 = conn
            .query_row("SELECT COUNT(*) FROM cloud_upload_attempts", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(attempts, 2);
        let (attempt_completion_seq, attempt_upload_set_id): (i64, Option<String>) = conn
            .query_row(
                "SELECT completion_seq, upload_set_id
                   FROM cloud_upload_attempts
                  WHERE attempt_id = 'attempt-2-fresh'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(attempt_completion_seq, seq_after_first);
        assert_eq!(attempt_upload_set_id, None);
    }

    #[test]
    fn cloud_upload_commit_done_recommit_fresh_attempt_id_different_content_rejected() {
        let mut conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/done-recommit-conflict");
        let content = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        // Enqueue WITHOUT a pre-declared expected_hash so the verify-after-upload guard
        // does not short-circuit the mismatch — the rejection must come from the done
        // re-commit consistency check itself.
        cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "child-a".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/a".to_owned(),
                category: "bulk".to_owned(),
                seq: 1,
                total_bytes: 10,
                content_sha256: content.to_owned(),
                expected_hash: None,
                verify_alg: "sha256".to_owned(),
            },
        )
        .unwrap();

        cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/a".to_owned(),
            },
            "attempt-1",
            content,
            "sha256",
            10,
            None,
        )
        .unwrap();

        let different = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        let conflict = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/a".to_owned(),
            },
            "attempt-2-fresh",
            different,
            "sha256",
            10,
            None,
        );
        assert!(conflict.is_err());
        // The rejected re-commit left the recorded verify proof and history untouched.
        let verify_value: Option<String> = conn
            .query_row(
                "SELECT verify_value FROM cloud_synced_files
                   WHERE destination_id='dest' AND remote_key='rk/a'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(verify_value.as_deref(), Some(content));
        let uploaded_rows: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM cloud_sync_history WHERE outcome='uploaded'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(uploaded_rows, 1);
    }

    #[test]
    fn json_escaped_len_matches_serde_json_encoding() {
        for s in [
            "plain",
            "TeslaCam/SentryClips/2026-06-19_10-00-00",
            "quote\"and\\backslash",
            "tab\tnl\nret\rbs\u{08}ff\u{0c}",
            "ctrl\u{01}\u{1f}",
            "unicode-é-🚗",
            "",
        ] {
            let encoded = serde_json::to_string(s).expect("encode");
            // serde_json wraps the body in quotes; the estimate excludes them, so the
            // helper is an exact match (hence a safe upper bound) for the serialized size.
            assert_eq!(
                json_escaped_len(s) + 2,
                encoded.len(),
                "escaped-length mismatch for {s:?} (encoded={encoded:?})"
            );
        }
    }

    #[test]
    fn estimated_row_sizes_upper_bound_serialized_json() {
        // Each *_estimated_size must be a true upper bound on the row's own serialized
        // JSON so a page built to RESPONSE_ITEM_BUDGET never exceeds MAX_REQUEST_FRAME,
        // independent of MAX_PAGE_LIMIT. Worst case: 1 KiB strings + max-width i64s.
        let big = "x".repeat(1024);

        let candidate = CloudCandidateRow {
            archive_item_id: i64::MAX,
            child_key: big.clone(),
            source_rel: big.clone(),
            destination_id: big.clone(),
            remote_key: big.clone(),
            size_bytes: i64::MAX,
            content_sha256: big.clone(),
            state: big.clone(),
            category: big.clone(),
            seq: i64::MIN,
        };
        assert!(
            cloud_candidate_row_estimated_size(&candidate)
                >= serde_json::to_string(&candidate).unwrap().len()
        );

        let discover = CloudDiscoverRow {
            archive_item_id: i64::MIN,
            folder_class: big.clone(),
            path: big.clone(),
            manifest_digest: Some(big.clone()),
            category: big.clone(),
        };
        assert!(
            cloud_discover_row_estimated_size(&discover)
                >= serde_json::to_string(&discover).unwrap().len()
        );

        let queue = CloudQueueRow {
            archive_item_id: i64::MAX,
            child_key: big.clone(),
            source_rel: big.clone(),
            destination_id: big.clone(),
            remote_key: big.clone(),
            category: big.clone(),
            seq: i64::MIN,
            total_bytes: i64::MAX,
            bytes_uploaded: i64::MAX,
            expected_hash: Some(big.clone()),
            verify_alg: big.clone(),
            content_sha256: big.clone(),
            state: big.clone(),
            attempts: i64::MAX,
            not_before: Some(i64::MIN),
            last_error: Some(big.clone()),
            upload_set_id: Some(big.clone()),
        };
        assert!(
            cloud_queue_row_estimated_size(&queue) >= serde_json::to_string(&queue).unwrap().len()
        );

        let pending_set = CloudPendingUploadSet {
            upload_set_id: big.clone(),
            archive_item_id: i64::MAX,
            destination_id: big.clone(),
            source_manifest_digest: big.clone(),
            expected_child_count: i64::MAX,
        };
        assert!(
            cloud_pending_upload_set_estimated_size(&pending_set)
                >= serde_json::to_string(&pending_set).unwrap().len()
        );

        let history = CloudHistoryRow {
            id: i64::MAX,
            completion_seq: i64::MAX,
            archive_item_id: i64::MAX,
            child_key: big.clone(),
            destination_id: big.clone(),
            outcome: big.clone(),
            size_bytes: i64::MAX,
            at: i64::MIN,
            error_class: Some(big.clone()),
        };
        assert!(
            cloud_history_row_estimated_size(&history)
                >= serde_json::to_string(&history).unwrap().len()
        );
    }

    #[test]
    fn fail_rejects_wrong_upload_set_id_on_sealed_row() {
        let mut conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/fail-sealed-fence");
        let hash = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";
        let set_a = "55555555555555555555555555555555";
        let set_b = "66666666666666666666666666666666";
        upsert_item(
            &conn,
            parent,
            "dest",
            "rk/fail-fence",
            "child-fail-fence",
            1,
            10,
            hash,
        );
        seal_queue_row(&conn, parent, "dest", "rk/fail-fence", set_a, None);
        let other_parent = insert_archive_item(&conn, "archive/fail-sealed-fence-other");
        conn.execute(
            "INSERT INTO cloud_parent_upload_sets
                (upload_set_id, archive_item_id, destination_id, source_manifest_digest,
                 request_digest, expected_child_count, created_at, finalized_at, superseded_at)
             VALUES (?1, ?2, 'dest',
                     'cccccccccccccccccccccccccccccccc',
                     'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
                     1, 0, NULL, NULL)",
            params![set_b, other_parent],
        )
        .unwrap();

        let wrong = cloud_upload_fail(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/fail-fence".to_owned(),
            },
            "attempt-fail-wrong-set",
            "timeout",
            Some(1234),
            false,
            Some(set_b),
        );
        assert!(wrong.is_err());

        let ok = cloud_upload_fail(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/fail-fence".to_owned(),
            },
            "attempt-fail-right-set",
            "timeout",
            Some(1234),
            false,
            Some(set_a),
        )
        .unwrap();
        assert!(ok.ok);
        assert_eq!(ok.state, "failed");
    }

    #[test]
    fn cloud_upload_fail_is_idempotent_on_attempt_id() {
        let mut conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/fail");
        let hash = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
        upsert_item(&conn, parent, "dest", "rk/f", "child-f", 1, 10, hash);

        let first = cloud_upload_fail(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/f".to_owned(),
            },
            "attempt-f",
            "timeout",
            Some(1234),
            false,
            None,
        )
        .unwrap();
        let replay = cloud_upload_fail(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/f".to_owned(),
            },
            "attempt-f",
            "timeout",
            Some(9999),
            true,
            None,
        )
        .unwrap();
        assert_eq!(replay, first);
        let attempts: i64 = conn
            .query_row(
                "SELECT attempts FROM cloud_upload_queue WHERE destination_id='dest' AND remote_key='rk/f'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(attempts, 1);
        let failed_rows: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM cloud_sync_history WHERE outcome='failed'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(failed_rows, 1);
    }

    #[test]
    fn queue_upsert_dedup_match_and_collision_park() {
        let conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/dedup");
        let hash = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";
        conn.execute(
            "INSERT INTO cloud_synced_files
                (destination_id, remote_key, archive_item_id, child_key, content_sha256, verify_alg,
                 verify_value, size_bytes, synced_at, completion_seq)
             VALUES ('dest','rk/d',?1,'child-d',?2,'sha256',?2,100,0,1)",
            params![parent, hash],
        )
        .unwrap();
        let state_done = upsert_item(&conn, parent, "dest", "rk/d", "child-d", 1, 100, hash);
        assert_eq!(state_done, "done");
        let durable_after_done: i64 = conn
            .query_row(
                "SELECT durable FROM archive_items WHERE id = ?1",
                params![parent],
                |r| r.get(0),
            )
            .unwrap();
        // Dedup-match marks the child done but never flips the parent durable.
        assert_eq!(durable_after_done, 0);
        let original: (i64, String, String, String) = conn
            .query_row(
                "SELECT archive_item_id, child_key, content_sha256, state
                   FROM cloud_upload_queue
                  WHERE destination_id='dest' AND remote_key='rk/d'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .unwrap();
        let collision_hash = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
        let parent_two = insert_archive_item(&conn, "archive/dedup-new");
        let state_parked = upsert_item(
            &conn,
            parent_two,
            "dest",
            "rk/d",
            "child-new",
            2,
            100,
            collision_hash,
        );
        assert_eq!(state_parked, "parked");
        let after: (i64, String, String, String) = conn
            .query_row(
                "SELECT archive_item_id, child_key, content_sha256, state
                   FROM cloud_upload_queue
                  WHERE destination_id='dest' AND remote_key='rk/d'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .unwrap();
        assert_eq!(after, original);
    }

    #[test]
    fn queue_pagination_cursor_is_stable_with_concurrent_insert() {
        let conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/page");
        let h1 = "1111111111111111111111111111111111111111111111111111111111111111";
        let h2 = "2222222222222222222222222222222222222222222222222222222222222222";
        let h3 = "3333333333333333333333333333333333333333333333333333333333333333";
        let h4 = "4444444444444444444444444444444444444444444444444444444444444444";
        upsert_item(&conn, parent, "dest", "k1", "c1", 1, 10, h1);
        upsert_item(&conn, parent, "dest", "k2", "c2", 2, 10, h2);
        upsert_item(&conn, parent, "dest", "k3", "c3", 3, 10, h3);

        let first = cloud_queue_load(&conn, None, 2, None).unwrap();
        assert_eq!(first.items.len(), 2);
        let cursor = first.next_cursor.clone().expect("next cursor");
        upsert_item(&conn, parent, "dest", "k0", "c0", 0, 10, h4);
        let second = cloud_queue_load(&conn, Some(&cursor), 10, None).unwrap();
        let keys: Vec<String> = second.items.into_iter().map(|row| row.remote_key).collect();
        assert_eq!(keys, vec!["k3".to_owned()]);
    }

    #[test]
    fn cloud_queue_load_includes_expected_hash_and_verify_alg() {
        let conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/queue-roundtrip");
        let hash_a = "1010101010101010101010101010101010101010101010101010101010101010";
        let hash_b = "2020202020202020202020202020202020202020202020202020202020202020";
        cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "child-a".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/with-expected".to_owned(),
                category: "bulk".to_owned(),
                seq: 1,
                total_bytes: 10,
                content_sha256: hash_a.to_owned(),
                expected_hash: Some("0123456789abcdef0123456789abcdef".to_owned()),
                verify_alg: "md5".to_owned(),
            },
        )
        .unwrap();
        cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "child-b".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/no-expected".to_owned(),
                category: "bulk".to_owned(),
                seq: 2,
                total_bytes: 10,
                content_sha256: hash_b.to_owned(),
                expected_hash: None,
                verify_alg: "none".to_owned(),
            },
        )
        .unwrap();

        let page = cloud_queue_load(&conn, None, 10, None).unwrap();
        assert_eq!(page.items.len(), 2);
        let with_expected = page
            .items
            .iter()
            .find(|row| row.remote_key == "rk/with-expected")
            .unwrap();
        assert_eq!(
            with_expected.expected_hash.as_deref(),
            Some("0123456789abcdef0123456789abcdef")
        );
        assert_eq!(with_expected.verify_alg, "md5");
        let no_expected = page
            .items
            .iter()
            .find(|row| row.remote_key == "rk/no-expected")
            .unwrap();
        assert_eq!(no_expected.expected_hash, None);
        assert_eq!(no_expected.verify_alg, "none");
    }

    #[test]
    fn cloud_queue_load_source_rel_matches_cloud_candidates_for_child_and_parent_rows() {
        let conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/source-rel");
        let hash_child = "3030303030303030303030303030303030303030303030303030303030303030";
        let hash_parent = "4040404040404040404040404040404040404040404040404040404040404040";
        upsert_item(
            &conn,
            parent,
            "dest",
            "rk/child",
            "segment-a",
            1,
            10,
            hash_child,
        );
        upsert_item(&conn, parent, "dest", "rk/parent", ".", 2, 10, hash_parent);

        let candidates = cloud_candidates(&conn, &["RecentClips".to_owned()], None, 10).unwrap();
        let queue = cloud_queue_load(&conn, None, 10, None).unwrap();
        assert_eq!(candidates.items.len(), 2);
        assert_eq!(queue.items.len(), 2);

        let child_queue = queue
            .items
            .iter()
            .find(|row| row.remote_key == "rk/child")
            .unwrap();
        let child_candidate = candidates
            .items
            .iter()
            .find(|row| row.remote_key == "rk/child")
            .unwrap();
        assert_eq!(child_queue.source_rel, "archive/source-rel/segment-a");
        assert_eq!(child_queue.source_rel, child_candidate.source_rel);

        let parent_queue = queue
            .items
            .iter()
            .find(|row| row.remote_key == "rk/parent")
            .unwrap();
        let parent_candidate = candidates
            .items
            .iter()
            .find(|row| row.remote_key == "rk/parent")
            .unwrap();
        assert_eq!(parent_queue.source_rel, "archive/source-rel");
        assert_eq!(parent_queue.source_rel, parent_candidate.source_rel);
    }

    #[test]
    fn pending_upload_sets_load_returns_current_unfinalized_only() {
        let conn = open_in_memory().unwrap();
        let parent_current = insert_archive_item(&conn, "archive/pending-current");
        let parent_final = insert_archive_item(&conn, "archive/pending-final");
        let parent_superseded = insert_archive_item(&conn, "archive/pending-superseded");

        conn.execute(
            "INSERT INTO cloud_parent_upload_sets
                (upload_set_id, archive_item_id, destination_id, source_manifest_digest,
                 request_digest, expected_child_count, created_at, finalized_at, superseded_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                parent_current,
                "dest-current",
                "11111111111111111111111111111111",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                3_i64,
                10_i64,
                Option::<i64>::None,
                Option::<i64>::None
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO cloud_parent_upload_sets
                (upload_set_id, archive_item_id, destination_id, source_manifest_digest,
                 request_digest, expected_child_count, created_at, finalized_at, superseded_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                parent_final,
                "dest-final",
                "22222222222222222222222222222222",
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                4_i64,
                20_i64,
                Some(5_i64),
                Option::<i64>::None
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO cloud_parent_upload_sets
                (upload_set_id, archive_item_id, destination_id, source_manifest_digest,
                 request_digest, expected_child_count, created_at, finalized_at, superseded_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![
                "cccccccccccccccccccccccccccccccc",
                parent_superseded,
                "dest-superseded",
                "33333333333333333333333333333333",
                "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                5_i64,
                30_i64,
                Option::<i64>::None,
                Some(7_i64)
            ],
        )
        .unwrap();

        let page = cloud_pending_upload_sets_load(&conn, None, 100).unwrap();
        assert_eq!(
            page.items,
            vec![CloudPendingUploadSet {
                upload_set_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_owned(),
                archive_item_id: parent_current,
                destination_id: "dest-current".to_owned(),
                source_manifest_digest: "11111111111111111111111111111111".to_owned(),
                expected_child_count: 3,
            }]
        );
        assert_eq!(page.next_cursor, None);
    }

    #[test]
    fn pending_upload_sets_load_paginates_keyset_stable() {
        let conn = open_in_memory().unwrap();
        let parent_a = insert_archive_item(&conn, "archive/pending-page-a");
        let parent_b = insert_archive_item(&conn, "archive/pending-page-b");
        let parent_c = insert_archive_item(&conn, "archive/pending-page-c");
        let rows = [
            (
                "11111111111111111111111111111111",
                parent_a,
                "dest-a",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                2_i64,
                10_i64,
            ),
            (
                "22222222222222222222222222222220",
                parent_b,
                "dest-b",
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                3_i64,
                20_i64,
            ),
            (
                "2222222222222222222222222222222f",
                parent_c,
                "dest-c",
                "cccccccccccccccccccccccccccccccc",
                "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                4_i64,
                20_i64,
            ),
        ];
        for row in rows {
            conn.execute(
                "INSERT INTO cloud_parent_upload_sets
                    (upload_set_id, archive_item_id, destination_id, source_manifest_digest,
                     request_digest, expected_child_count, created_at, finalized_at, superseded_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, NULL, NULL)",
                params![row.0, row.1, row.2, row.3, row.4, row.5, row.6],
            )
            .unwrap();
        }

        let first = cloud_pending_upload_sets_load(&conn, None, 2).unwrap();
        assert_eq!(first.items.len(), 2);
        let first_ids: Vec<String> = first
            .items
            .iter()
            .map(|item| item.upload_set_id.clone())
            .collect();
        assert_eq!(
            first_ids,
            vec![
                "11111111111111111111111111111111".to_owned(),
                "22222222222222222222222222222220".to_owned()
            ]
        );
        let cursor = first.next_cursor.clone().expect("next cursor");
        let second = cloud_pending_upload_sets_load(&conn, Some(&cursor), 2).unwrap();
        assert_eq!(second.items.len(), 1);
        assert_eq!(second.next_cursor, None);
        let second_id = second.items.first().expect("one row").upload_set_id.clone();
        assert_eq!(second_id, "2222222222222222222222222222222f");
        assert!(!first_ids.contains(&second_id));
    }

    #[test]
    fn queue_load_set_filter_returns_only_that_set_with_upload_set_id_populated() {
        let conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/queue-set-filter");
        let h1 = "1010101010101010101010101010101010101010101010101010101010101010";
        let h2 = "2020202020202020202020202020202020202020202020202020202020202020";
        let h3 = "3030303030303030303030303030303030303030303030303030303030303030";
        let h4 = "4040404040404040404040404040404040404040404040404040404040404040";
        let set_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let set_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        upsert_item(&conn, parent, "dest", "rk/a", "child-a", 1, 10, h1);
        upsert_item(&conn, parent, "dest", "rk/b", "child-b", 2, 10, h2);
        upsert_item(&conn, parent, "dest", "rk/c", "child-c", 3, 10, h3);
        upsert_item(&conn, parent, "dest", "rk/d", "child-d", 4, 10, h4);
        seal_queue_row(&conn, parent, "dest", "rk/a", set_a, None);
        conn.execute(
            "UPDATE cloud_upload_queue
                SET upload_set_id = ?1
              WHERE destination_id = 'dest' AND remote_key = 'rk/b'",
            params![set_a],
        )
        .unwrap();
        seal_queue_row(&conn, parent, "dest", "rk/c", set_b, Some(9));

        let filtered = cloud_queue_load(&conn, None, 100, Some(set_a)).unwrap();
        assert_eq!(filtered.items.len(), 2);
        for row in &filtered.items {
            assert_eq!(row.upload_set_id.as_deref(), Some(set_a));
        }
        let filtered_keys: Vec<String> = filtered
            .items
            .into_iter()
            .map(|row| row.remote_key)
            .collect();
        assert_eq!(filtered_keys, vec!["rk/a".to_owned(), "rk/b".to_owned()]);

        let unfiltered = cloud_queue_load(&conn, None, 100, None).unwrap();
        assert_eq!(unfiltered.items.len(), 4);
        let a_row = unfiltered
            .items
            .iter()
            .find(|row| row.remote_key == "rk/a")
            .unwrap();
        assert_eq!(a_row.upload_set_id.as_deref(), Some(set_a));
        let c_row = unfiltered
            .items
            .iter()
            .find(|row| row.remote_key == "rk/c")
            .unwrap();
        assert_eq!(c_row.upload_set_id.as_deref(), Some(set_b));
        let d_row = unfiltered
            .items
            .iter()
            .find(|row| row.remote_key == "rk/d")
            .unwrap();
        assert_eq!(d_row.upload_set_id, None);
    }

    #[test]
    fn stats_are_derived_and_reset_rebases_baseline() {
        let mut conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/stats");
        let h1 = "abababababababababababababababababababababababababababababababab";
        let h2 = "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd";
        let h3 = "efefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefef";
        upsert_item(&conn, parent, "dest", "s1", "c1", 1, 10, h1);
        upsert_item(&conn, parent, "dest", "s2", "c2", 2, 20, h2);
        cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "s1".to_owned(),
            },
            "st1",
            h1,
            "sha256",
            10,
            None,
        )
        .unwrap();
        cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "s2".to_owned(),
            },
            "st2",
            h2,
            "sha256",
            20,
            None,
        )
        .unwrap();
        let before_reset = cloud_stats_get(&conn).unwrap();
        assert_eq!(before_reset.synced_count, 2);
        assert_eq!(before_reset.synced_bytes, 30);
        let _ = cloud_stats_reset(&conn).unwrap();
        let after_reset = cloud_stats_get(&conn).unwrap();
        assert_eq!(after_reset.synced_count, 0);
        assert_eq!(after_reset.synced_bytes, 0);
        upsert_item(&conn, parent, "dest", "s3", "c3", 3, 40, h3);
        cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "s3".to_owned(),
            },
            "st3",
            h3,
            "sha256",
            40,
            None,
        )
        .unwrap();
        let after_new_upload = cloud_stats_get(&conn).unwrap();
        assert_eq!(after_new_upload.synced_count, 1);
        assert_eq!(after_new_upload.synced_bytes, 40);
    }

    #[test]
    fn queue_retry_resolution_modes_work() {
        let conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/retry");
        let h1 = "9999999999999999999999999999999999999999999999999999999999999999";
        upsert_item(&conn, parent, "dest", "rk/r", "child-r", 1, 10, h1);
        conn.execute(
            "UPDATE cloud_upload_queue SET state='parked', attempts=3 WHERE destination_id='dest' AND remote_key='rk/r'",
            [],
        )
        .unwrap();
        let state = cloud_queue_retry(
            &conn,
            parent,
            Some("child-r"),
            None,
            &CloudQueueRetryResolution::Replace,
        )
        .unwrap();
        assert_eq!(state, "queued");
        let attempts: i64 = conn
            .query_row(
                "SELECT attempts FROM cloud_upload_queue WHERE destination_id='dest' AND remote_key='rk/r'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(attempts, 0);
    }

    #[test]
    fn queue_retry_keep_existing_requires_synced_evidence_and_never_flips_durable() {
        let conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/retry-keep");
        let hash = "ababcdcdababcdcdababcdcdababcdcdababcdcdababcdcdababcdcdababcdcd";
        upsert_item(&conn, parent, "dest", "rk/k", "child-k", 1, 10, hash);
        conn.execute(
            "UPDATE cloud_upload_queue
                SET state='parked',
                    attempts=2
              WHERE destination_id='dest' AND remote_key='rk/k'",
            [],
        )
        .unwrap();
        let state = cloud_queue_retry(
            &conn,
            parent,
            Some("child-k"),
            None,
            &CloudQueueRetryResolution::KeepExisting,
        )
        .unwrap();
        assert_eq!(state, "parked");
        let durable: i64 = conn
            .query_row(
                "SELECT durable FROM archive_items WHERE id = ?1",
                params![parent],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(durable, 0);

        conn.execute(
            "INSERT INTO cloud_synced_files
                (destination_id, remote_key, archive_item_id, child_key, content_sha256, verify_alg,
                 verify_value, size_bytes, synced_at, completion_seq)
             VALUES (?1, ?2, ?3, ?4, ?5, 'sha256', ?5, ?6, 0, 21)",
            params!["dest", "rk/k", parent, "child-k", hash, 10_i64],
        )
        .unwrap();
        let state = cloud_queue_retry(
            &conn,
            parent,
            Some("child-k"),
            None,
            &CloudQueueRetryResolution::KeepExisting,
        )
        .unwrap();
        assert_eq!(state, "done");
        let durable: i64 = conn
            .query_row(
                "SELECT durable FROM archive_items WHERE id = ?1",
                params![parent],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(durable, 0);
    }

    #[test]
    fn queue_retry_keep_existing_requires_full_verify_tuple() {
        let conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/retry-keep-sealed-verify");
        let hash = "efefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefef";
        let set_id = "77777777777777777777777777777777";
        cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "child-sealed".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/sealed".to_owned(),
                category: "bulk".to_owned(),
                seq: 1,
                total_bytes: 10,
                content_sha256: hash.to_owned(),
                expected_hash: Some("etag-good".to_owned()),
                verify_alg: "md5".to_owned(),
            },
        )
        .unwrap();
        conn.execute(
            "UPDATE cloud_upload_queue
                SET state='parked', attempts=2
              WHERE destination_id='dest' AND remote_key='rk/sealed'",
            [],
        )
        .unwrap();
        seal_queue_row(&conn, parent, "dest", "rk/sealed", set_id, None);
        conn.execute(
            "INSERT INTO cloud_synced_files
                (destination_id, remote_key, archive_item_id, child_key, content_sha256, verify_alg,
                 verify_value, size_bytes, synced_at, completion_seq)
             VALUES ('dest', 'rk/sealed', ?1, 'child-sealed', ?2, 'md5', 'etag-wrong', 10, 0, 31)",
            params![parent, hash],
        )
        .unwrap();

        let wrong_verify = cloud_queue_retry(
            &conn,
            parent,
            Some("child-sealed"),
            Some(set_id),
            &CloudQueueRetryResolution::KeepExisting,
        )
        .unwrap();
        assert_eq!(wrong_verify, "parked");

        conn.execute(
            "UPDATE cloud_synced_files
                SET verify_value = 'etag-good'
              WHERE destination_id = 'dest' AND remote_key = 'rk/sealed'",
            [],
        )
        .unwrap();
        let matched = cloud_queue_retry(
            &conn,
            parent,
            Some("child-sealed"),
            Some(set_id),
            &CloudQueueRetryResolution::KeepExisting,
        )
        .unwrap();
        assert_eq!(matched, "done");
    }

    #[test]
    fn queue_retry_keep_existing_unsealed_uses_hash_size_only() {
        let conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/retry-keep-unsealed");
        let hash = "1212121212121212121212121212121212121212121212121212121212121212";
        cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "child-unsealed".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/unsealed".to_owned(),
                category: "bulk".to_owned(),
                seq: 1,
                total_bytes: 10,
                content_sha256: hash.to_owned(),
                expected_hash: None,
                verify_alg: "none".to_owned(),
            },
        )
        .unwrap();
        conn.execute(
            "UPDATE cloud_upload_queue
                SET state='parked', attempts=2
              WHERE destination_id='dest' AND remote_key='rk/unsealed'",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO cloud_synced_files
                (destination_id, remote_key, archive_item_id, child_key, content_sha256, verify_alg,
                 verify_value, size_bytes, synced_at, completion_seq)
             VALUES ('dest', 'rk/unsealed', ?1, 'child-unsealed', ?2, 'md5', 'other-proof', 10, 0, 32)",
            params![parent, hash],
        )
        .unwrap();

        let state = cloud_queue_retry(
            &conn,
            parent,
            Some("child-unsealed"),
            None,
            &CloudQueueRetryResolution::KeepExisting,
        )
        .unwrap();
        assert_eq!(state, "done");
    }

    #[test]
    fn queue_retry_selects_requested_generation_not_superseded() {
        let conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/retry-generation-filter");
        let hash_a = "2323232323232323232323232323232323232323232323232323232323232323";
        let hash_b = "3434343434343434343434343434343434343434343434343434343434343434";
        let set_a = "88888888888888888888888888888888";
        let set_b = "99999999999999999999999999999999";
        cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "child-shared".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/gen-a".to_owned(),
                category: "bulk".to_owned(),
                seq: 1,
                total_bytes: 10,
                content_sha256: hash_a.to_owned(),
                expected_hash: Some("etag-a".to_owned()),
                verify_alg: "md5".to_owned(),
            },
        )
        .unwrap();
        cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "child-shared".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/gen-b".to_owned(),
                category: "bulk".to_owned(),
                seq: 2,
                total_bytes: 10,
                content_sha256: hash_b.to_owned(),
                expected_hash: Some("etag-b".to_owned()),
                verify_alg: "md5".to_owned(),
            },
        )
        .unwrap();
        conn.execute(
            "UPDATE cloud_upload_queue SET state='parked', attempts=1
              WHERE destination_id='dest' AND remote_key IN ('rk/gen-a', 'rk/gen-b')",
            [],
        )
        .unwrap();
        seal_queue_row(&conn, parent, "dest", "rk/gen-a", set_a, Some(11));
        seal_queue_row(&conn, parent, "dest", "rk/gen-b", set_b, None);
        conn.execute(
            "INSERT INTO cloud_synced_files
                (destination_id, remote_key, archive_item_id, child_key, content_sha256, verify_alg,
                 verify_value, size_bytes, synced_at, completion_seq)
             VALUES ('dest', 'rk/gen-b', ?1, 'child-shared', ?2, 'md5', 'etag-b', 10, 0, 33)",
            params![parent, hash_b],
        )
        .unwrap();

        let state = cloud_queue_retry(
            &conn,
            parent,
            Some("child-shared"),
            Some(set_b),
            &CloudQueueRetryResolution::KeepExisting,
        )
        .unwrap();
        assert_eq!(state, "done");
        let done_a: String = conn
            .query_row(
                "SELECT state
                   FROM cloud_upload_queue
                  WHERE destination_id='dest' AND remote_key='rk/gen-a'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let done_b: String = conn
            .query_row(
                "SELECT state
                   FROM cloud_upload_queue
                  WHERE destination_id='dest' AND remote_key='rk/gen-b'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(done_a, "parked");
        assert_eq!(done_b, "done");
    }

    #[test]
    fn cloud_queue_retry_rekey_sealed_rejected() {
        let conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/retry-rekey-sealed");
        let hash = "4545454545454545454545454545454545454545454545454545454545454545";
        let set_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab";
        upsert_item(
            &conn,
            parent,
            "dest",
            "rk/rekey",
            "child-rekey",
            1,
            10,
            hash,
        );
        seal_queue_row(&conn, parent, "dest", "rk/rekey", set_a, None);

        let result = cloud_queue_retry(
            &conn,
            parent,
            Some("child-rekey"),
            Some(set_a),
            &CloudQueueRetryResolution::Rekey {
                remote_key: "rk/new".to_owned(),
            },
        );
        assert!(result.is_err());
    }

    #[test]
    fn cloud_queue_upsert_sealed_rejected() {
        let conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/upsert-sealed");
        let hash = "5656565656565656565656565656565656565656565656565656565656565656";
        let set_a = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbc";
        upsert_item(
            &conn,
            parent,
            "dest",
            "rk/upsert-sealed",
            "child-upsert",
            1,
            10,
            hash,
        );
        seal_queue_row(&conn, parent, "dest", "rk/upsert-sealed", set_a, None);

        let result = cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "child-upsert".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/upsert-sealed".to_owned(),
                category: "bulk".to_owned(),
                seq: 1,
                total_bytes: 10,
                content_sha256: hash.to_owned(),
                expected_hash: Some(hash.to_owned()),
                verify_alg: "sha256".to_owned(),
            },
        );
        assert!(result.is_err());
    }

    #[test]
    fn cloud_upload_commit_rejects_parked_and_synced_hash_collision() {
        let mut conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/commit-reject");
        let hash = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        upsert_item(&conn, parent, "dest", "rk/p", "child-p", 1, 10, hash);
        conn.execute(
            "UPDATE cloud_upload_queue SET state='parked' WHERE destination_id='dest' AND remote_key='rk/p'",
            [],
        )
        .unwrap();
        let parked = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/p".to_owned(),
            },
            "attempt-parked",
            hash,
            "sha256",
            10,
            None,
        );
        assert!(parked.is_err());

        let hash_q = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        upsert_item(&conn, parent, "dest", "rk/q", "child-q", 2, 10, hash_q);
        conn.execute(
            "INSERT INTO cloud_synced_files
                (destination_id, remote_key, archive_item_id, child_key, content_sha256, verify_alg,
                 verify_value, size_bytes, synced_at, completion_seq)
             VALUES ('dest','rk/q',?1,'oracle',
                     'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                     'sha256',
                     'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                     10, 0, 11)",
            params![parent],
        )
        .unwrap();
        let collision = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/q".to_owned(),
            },
            "attempt-collision",
            hash_q,
            "sha256",
            10,
            None,
        );
        assert!(collision.is_err());
        let oracle_hash: String = conn
            .query_row(
                "SELECT content_sha256 FROM cloud_synced_files
                  WHERE destination_id='dest' AND remote_key='rk/q'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            oracle_hash,
            "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
        );
    }

    #[test]
    fn cloud_upload_commit_supports_backend_hash_algorithms() {
        let mut conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/hash-alg");

        cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "child-md5".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/md5".to_owned(),
                category: "bulk".to_owned(),
                seq: 1,
                total_bytes: 10,
                content_sha256: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
                    .to_owned(),
                expected_hash: Some("0123456789abcdef0123456789abcdef".to_owned()),
                verify_alg: "md5".to_owned(),
            },
        )
        .unwrap();
        let md5 = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/md5".to_owned(),
            },
            "attempt-md5",
            "0123456789abcdef0123456789abcdef",
            "md5",
            10,
            None,
        )
        .unwrap();
        assert!(md5.ok);
        let md5_replay = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/md5".to_owned(),
            },
            "attempt-md5",
            "0123456789abcdef0123456789abcdef",
            "md5",
            10,
            None,
        )
        .unwrap();
        assert_eq!(md5_replay, md5);
        let md5_conflict = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/md5".to_owned(),
            },
            "attempt-md5",
            "ffffffffffffffffffffffffffffffff",
            "md5",
            10,
            None,
        );
        assert!(md5_conflict.is_err());
        let (verify_alg, verify_value): (String, Option<String>) = conn
            .query_row(
                "SELECT verify_alg, verify_value
                   FROM cloud_synced_files
                  WHERE destination_id='dest' AND remote_key='rk/md5'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(verify_alg, "md5");
        assert_eq!(
            verify_value,
            Some("0123456789abcdef0123456789abcdef".to_owned())
        );

        cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "child-md5-no-expected".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/md5-no-expected".to_owned(),
                category: "bulk".to_owned(),
                seq: 2,
                total_bytes: 10,
                content_sha256: "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
                    .to_owned(),
                expected_hash: None,
                verify_alg: "md5".to_owned(),
            },
        )
        .unwrap();
        cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/md5-no-expected".to_owned(),
            },
            "attempt-md5-no-expected",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "md5",
            10,
            None,
        )
        .unwrap();

        cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "child-md5-bad-hash".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/md5-bad-hash".to_owned(),
                category: "bulk".to_owned(),
                seq: 3,
                total_bytes: 10,
                content_sha256: "abababababababababababababababababababababababababababababababab"
                    .to_owned(),
                expected_hash: Some("11111111111111111111111111111111".to_owned()),
                verify_alg: "md5".to_owned(),
            },
        )
        .unwrap();
        let native_mismatch = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/md5-bad-hash".to_owned(),
            },
            "attempt-md5-bad-hash",
            "22222222222222222222222222222222",
            "md5",
            10,
            None,
        );
        assert!(native_mismatch.is_err());

        cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "child-md5-wrong-alg".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/md5-wrong-alg".to_owned(),
                category: "bulk".to_owned(),
                seq: 4,
                total_bytes: 10,
                content_sha256: "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
                    .to_owned(),
                expected_hash: Some("33333333333333333333333333333333".to_owned()),
                verify_alg: "md5".to_owned(),
            },
        )
        .unwrap();
        let wrong_alg = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/md5-wrong-alg".to_owned(),
            },
            "attempt-md5-wrong-alg",
            "4444444444444444444444444444444444444444",
            "sha1",
            10,
            None,
        );
        assert!(wrong_alg.is_err());

        cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "child-none".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "rk/none".to_owned(),
                category: "bulk".to_owned(),
                seq: 5,
                total_bytes: 10,
                content_sha256: "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
                    .to_owned(),
                expected_hash: None,
                verify_alg: "none".to_owned(),
            },
        )
        .unwrap();
        let none_requires_empty = cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/none".to_owned(),
            },
            "attempt-none-bad",
            "55555555555555555555555555555555",
            "md5",
            10,
            None,
        );
        assert!(none_requires_empty.is_err());
        cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "rk/none".to_owned(),
            },
            "attempt-none",
            "",
            "none",
            10,
            None,
        )
        .unwrap();
        let verify_none: Option<String> = conn
            .query_row(
                "SELECT verify_value
                   FROM cloud_synced_files
                  WHERE destination_id='dest' AND remote_key='rk/none'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(verify_none, None);
    }

    #[test]
    fn candidates_pagination_obeys_byte_budget_and_cursor_resumes_without_gaps() {
        let conn = open_in_memory().unwrap();
        let mut expected_keys = Vec::new();
        for index in 0_i64..6 {
            let parent = insert_archive_item(
                &conn,
                &format!(
                    "archive/very-long-{index}/{}",
                    "segment".repeat(2_800_usize)
                ),
            );
            let remote_key = format!("rk/{index:02}");
            let child_key = format!("child-{index}");
            let hash = format!("{index:064x}");
            upsert_item(
                &conn,
                parent,
                "dest",
                &remote_key,
                &child_key,
                index + 1,
                10,
                &hash,
            );
            expected_keys.push(remote_key);
        }
        let mut cursor: Option<String> = None;
        let mut seen = Vec::new();
        let mut pages = 0_u32;
        loop {
            let page = cloud_candidates(&conn, &["RecentClips".to_owned()], cursor.as_deref(), 16)
                .unwrap();
            if page.items.is_empty() {
                break;
            }
            pages = pages.saturating_add(1);
            let payload = serde_json::to_vec(&page.items).unwrap();
            assert!(payload.len() <= MAX_REQUEST_FRAME as usize);
            for item in &page.items {
                seen.push(item.remote_key.clone());
            }
            cursor = page.next_cursor.clone();
            if cursor.is_none() {
                break;
            }
        }
        assert!(pages > 1);
        assert_eq!(seen, expected_keys);
    }

    #[test]
    fn lease_acquire_renew_release_roundtrip() {
        let conn = open_in_memory().unwrap();
        let item = insert_archive_item(&conn, "archive/lease");
        let boot = BootContext::new();
        let acquired = upload_lease_acquire(&conn, &boot, item, 1_000).unwrap();
        assert!(acquired.granted);
        let token = acquired.token.expect("token");
        let renewed = upload_lease_renew(&conn, &boot, &token, 1_000).unwrap();
        assert!(renewed.ok);
        let released = upload_lease_release(&conn, &boot, &token).unwrap();
        assert!(released);
        let second_release = upload_lease_release(&conn, &boot, &token).unwrap();
        assert!(!second_release);
    }

    #[test]
    fn config_get_put_roundtrip() {
        let conn = open_in_memory().unwrap();
        let mut config = cloud_config_get(&conn).unwrap();
        config.auto_sync = false;
        config.max_attempts = 7;
        let updated = cloud_config_put(&conn, &config).unwrap();
        assert_eq!(updated.auto_sync, config.auto_sync);
        assert_eq!(updated.max_attempts, 7);
    }

    #[test]
    fn cloud_discover_filters_enabled_live_nondurable_and_pages_by_id() {
        let conn = open_in_memory().unwrap();
        let recent_a =
            insert_archive_item_with_class(&conn, "RecentClips", "archive/recent/a", 0, "LIVE");
        let sentry_a =
            insert_archive_item_with_class(&conn, "SentryClips", "archive/sentry/a", 0, "LIVE");
        let _saved_disabled =
            insert_archive_item_with_class(&conn, "SavedClips", "archive/saved/a", 0, "LIVE");
        let _recent_durable = insert_archive_item_with_class(
            &conn,
            "RecentClips",
            "archive/recent/durable",
            1,
            "LIVE",
        );
        let _recent_deleting = insert_archive_item_with_class(
            &conn,
            "RecentClips",
            "archive/recent/deleting",
            0,
            "DELETE_CLAIMED",
        );

        let mut config = cloud_config_get(&conn).unwrap();
        config.sentry_enabled = true;
        config.saved_enabled = false;
        config.recent_enabled = true;
        cloud_config_put(&conn, &config).unwrap();

        let first = cloud_discover(&conn, None, 1).unwrap();
        assert_eq!(first.items.len(), 1);
        let first_item = first.items.first().unwrap();
        assert_eq!(first_item.archive_item_id, recent_a);
        assert_eq!(first_item.category, "bulk");
        let cursor = first.next_cursor.clone().unwrap();

        let recent_b =
            insert_archive_item_with_class(&conn, "RecentClips", "archive/recent/b", 0, "LIVE");
        let second = cloud_discover(&conn, Some(&cursor), 10).unwrap();
        let ids: Vec<i64> = second.items.iter().map(|row| row.archive_item_id).collect();
        assert_eq!(ids, vec![sentry_a, recent_b]);
        let categories: Vec<String> = second
            .items
            .iter()
            .map(|row| row.category.clone())
            .collect();
        assert_eq!(
            categories,
            vec!["event_sentry".to_owned(), "bulk".to_owned()]
        );
    }

    #[test]
    fn cloud_discover_returns_empty_when_all_folders_disabled() {
        let conn = open_in_memory().unwrap();
        let _ = insert_archive_item_with_class(&conn, "RecentClips", "archive/recent/x", 0, "LIVE");
        let mut config = cloud_config_get(&conn).unwrap();
        config.sentry_enabled = false;
        config.saved_enabled = false;
        config.recent_enabled = false;
        cloud_config_put(&conn, &config).unwrap();

        let page = cloud_discover(&conn, None, 10).unwrap();
        assert!(page.items.is_empty());
        assert_eq!(page.next_cursor, None);
    }

    #[test]
    fn cloud_discover_respects_each_folder_toggle() {
        let conn = open_in_memory().unwrap();
        let recent = insert_archive_item_with_class(
            &conn,
            "RecentClips",
            "archive/recent/toggle",
            0,
            "LIVE",
        );
        let sentry = insert_archive_item_with_class(
            &conn,
            "SentryClips",
            "archive/sentry/toggle",
            0,
            "LIVE",
        );
        let saved =
            insert_archive_item_with_class(&conn, "SavedClips", "archive/saved/toggle", 0, "LIVE");

        let mut config = cloud_config_get(&conn).unwrap();

        config.sentry_enabled = false;
        config.saved_enabled = false;
        config.recent_enabled = true;
        cloud_config_put(&conn, &config).unwrap();
        let recent_only = cloud_discover(&conn, None, 10).unwrap();
        assert_eq!(
            recent_only
                .items
                .iter()
                .map(|row| row.archive_item_id)
                .collect::<Vec<i64>>(),
            vec![recent]
        );

        config.sentry_enabled = true;
        config.saved_enabled = false;
        config.recent_enabled = false;
        cloud_config_put(&conn, &config).unwrap();
        let sentry_only = cloud_discover(&conn, None, 10).unwrap();
        assert_eq!(
            sentry_only
                .items
                .iter()
                .map(|row| row.archive_item_id)
                .collect::<Vec<i64>>(),
            vec![sentry]
        );

        config.sentry_enabled = false;
        config.saved_enabled = true;
        config.recent_enabled = false;
        cloud_config_put(&conn, &config).unwrap();
        let saved_only = cloud_discover(&conn, None, 10).unwrap();
        assert_eq!(
            saved_only
                .items
                .iter()
                .map(|row| row.archive_item_id)
                .collect::<Vec<i64>>(),
            vec![saved]
        );
    }

    #[test]
    fn cloud_discover_includes_manifest_digest_and_path_when_present() {
        let conn = open_in_memory().unwrap();
        conn.execute(
            "INSERT INTO archive_items
                (folder_class, path, size_bytes, file_count, archived_at, durable, delete_state, manifest_digest, created_at, updated_at)
             VALUES ('RecentClips', 'archive/recent/with-digest', 1024, 1, 100, 0, 'LIVE',
                     '12341234123412341234123412341234', 0, 0)",
            [],
        )
        .unwrap();
        let mut config = cloud_config_get(&conn).unwrap();
        config.recent_enabled = true;
        config.sentry_enabled = false;
        config.saved_enabled = false;
        cloud_config_put(&conn, &config).unwrap();

        let page = cloud_discover(&conn, None, 10).unwrap();
        assert_eq!(page.items.len(), 1);
        assert_eq!(page.items[0].path, "archive/recent/with-digest");
        assert_eq!(
            page.items[0].manifest_digest.as_deref(),
            Some("12341234123412341234123412341234")
        );
    }

    #[test]
    fn history_load_pages() {
        let mut conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/history");
        let h1 = "1212121212121212121212121212121212121212121212121212121212121212";
        let h2 = "3434343434343434343434343434343434343434343434343434343434343434";
        upsert_item(&conn, parent, "dest", "h1", "c1", 1, 10, h1);
        upsert_item(&conn, parent, "dest", "h2", "c2", 2, 10, h2);
        cloud_upload_commit(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "h1".to_owned(),
            },
            "h-attempt-1",
            h1,
            "sha256",
            10,
            None,
        )
        .unwrap();
        cloud_upload_fail(
            &mut conn,
            &CloudQueuePk {
                destination_id: "dest".to_owned(),
                remote_key: "h2".to_owned(),
            },
            "h-attempt-2",
            "timeout",
            Some(3000),
            false,
            None,
        )
        .unwrap();
        let first = cloud_history_load(&conn, None, 1).unwrap();
        assert_eq!(first.items.len(), 1);
        let second = cloud_history_load(&conn, first.next_cursor.as_deref(), 10).unwrap();
        assert_eq!(second.items.len(), 1);
    }

    #[test]
    fn candidates_page_uses_folder_filter_and_cursor() {
        let conn = open_in_memory().unwrap();
        let parent = insert_archive_item(&conn, "archive/cands");
        let h1 = "5656565656565656565656565656565656565656565656565656565656565656";
        let h2 = "7878787878787878787878787878787878787878787878787878787878787878";
        upsert_item(&conn, parent, "dest", "c1", "a", 1, 10, h1);
        cloud_queue_upsert(
            &conn,
            &CloudQueueUpsertItem {
                archive_item_id: parent,
                child_key: "b".to_owned(),
                destination_id: "dest".to_owned(),
                remote_key: "c2".to_owned(),
                category: "event_sentry".to_owned(),
                seq: 2,
                total_bytes: 10,
                content_sha256: h2.to_owned(),
                expected_hash: None,
                verify_alg: "none".to_owned(),
            },
        )
        .unwrap();
        let page = cloud_candidates(&conn, &["RecentClips".to_owned()], None, 1).unwrap();
        assert_eq!(page.items.len(), 1);
        assert!(page.next_cursor.is_none());
        let event_page = cloud_candidates(&conn, &["SavedClips".to_owned()], None, 10).unwrap();
        assert_eq!(event_page.items.len(), 1);
    }
}
