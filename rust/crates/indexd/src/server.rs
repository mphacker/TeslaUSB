//! Unix-socket RPC server for `retentiond → indexd` archive registration.

use std::collections::{HashMap, HashSet};
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use rusqlite::{Connection, OptionalExtension, params};
use sha2::{Digest, Sha256};
use teslausb_core::manifest_digest::{ManifestDigestEntry, manifest_digest_v1_hex};

use crate::db::cloud::{
    CloudConfig, CloudQueuePk, CloudQueueRetryResolution, CloudQueueUpsertItem, cloud_candidates,
    cloud_config_get, cloud_config_put, cloud_discover, cloud_history_load,
    cloud_pending_upload_sets_load, cloud_queue_load, cloud_queue_retry, cloud_queue_upsert,
    cloud_stats_get, cloud_stats_reset, cloud_upload_commit, cloud_upload_fail, upload_lease_acquire,
    upload_lease_release, upload_lease_renew,
};
use crate::db::ingest::{
    AngleFacts, ArchiveAngleRegistration, ArchiveRegistration, ArchiveUnitRegistration, ClipFacts,
    register_archived_clip, register_quarantined_clip, upsert_angle_force_archive, upsert_clip,
};
use crate::db::mutations::{
    BootContext, has_unexpired_lease, mark_deleted, mark_deleting, quarantine,
    release_delete_claim, set_pref,
};
use crate::db::reads::{list_eviction_candidates, list_recovery_rows};
use crate::db::{DbError, now_epoch_s};
use crate::model::FolderClass;
use crate::proto::{
    CloudCandidateWire, CloudConfigWire, CloudDiscoverWire, CloudHistoryRowWire,
    CloudFinalizeParentUploadRequest, CloudFinalizeParentUploadResponse,
    CloudPendingUploadSetWire,
    CloudPrepareParentUploadChildWire, CloudPrepareParentUploadRequest, CloudPrepareParentUploadResponse,
    CloudQueueRetryResolutionWire, CloudQueueRowWire, CloudQueueUpsertWire, EvictionCandidateWire,
    FinalizeEventArchiveRequest, FinalizeEventArchiveResponse, RecoveryRowWire, RegisterArchivedClip,
    Request, Response, MAX_REQUEST_FRAME, read_request, write_response,
};

/// Start the indexd registration server thread.
///
/// # Errors
///
/// Returns an error if socket setup/bind fails.
pub fn spawn(
    conn: &Arc<Mutex<Connection>>,
    boot: &Arc<BootContext>,
    socket_path: &Path,
    io_timeout: Duration,
) -> io::Result<thread::JoinHandle<()>> {
    let listener = bind_listener(socket_path)?;
    let conn = Arc::clone(conn);
    let boot = Arc::clone(boot);
    thread::Builder::new()
        .name("indexd-rpc".to_owned())
        .spawn(move || serve(&listener, &conn, &boot, io_timeout))
}

fn bind_listener(socket_path: &Path) -> io::Result<UnixListener> {
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o750))?;
    }
    match std::fs::remove_file(socket_path) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    let listener = UnixListener::bind(socket_path)?;
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o660))?;
    Ok(listener)
}

fn serve(
    listener: &UnixListener,
    conn: &Arc<Mutex<Connection>>,
    boot: &Arc<BootContext>,
    io_timeout: Duration,
) {
    for incoming in listener.incoming() {
        match incoming {
            Ok(stream) => {
                let _ = handle_connection(stream, conn, boot, io_timeout);
            }
            Err(_) => continue,
        }
    }
}

#[allow(clippy::cognitive_complexity, clippy::too_many_lines)]
fn handle_connection(
    mut stream: UnixStream,
    conn: &Arc<Mutex<Connection>>,
    boot: &Arc<BootContext>,
    io_timeout: Duration,
) -> io::Result<()> {
    stream.set_read_timeout(Some(io_timeout))?;
    stream.set_write_timeout(Some(io_timeout))?;

    let response = match read_request(&mut stream) {
        Ok(request) => match request {
            Request::RegisterArchivedClip(payload) => {
                match handle_register_archived_clip(conn, &payload) {
                    Ok((clip_id, archive_item_id)) => Response::Ok {
                        clip_id,
                        archive_item_id,
                    },
                    Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                    Err(HandlerError::Internal(message)) => Response::Error { message },
                }
            }
            Request::RegisterQuarantinedArchive(payload) => {
                match handle_register_quarantined_clip(conn, &payload) {
                    Ok((clip_id, archive_item_id)) => Response::Ok {
                        clip_id,
                        archive_item_id,
                    },
                    Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                    Err(HandlerError::Internal(message)) => Response::Error { message },
                }
            }
            Request::SetPref { key, value } => match handle_set_pref(conn, &key, &value) {
                Ok(()) => Response::PrefSet { key },
                Err(message) => Response::Error { message },
            },
            Request::ClaimEvictionCandidate {
                id,
                recency_floor_epoch,
                allow_undurable,
            } => match handle_claim_eviction_candidate(
                conn,
                boot,
                id,
                recency_floor_epoch,
                allow_undurable,
            ) {
                Ok(response) => response,
                Err(message) => Response::Error { message },
            },
            Request::MarkArchiveDeleting { id } => match handle_mark_archive_deleting(conn, id) {
                Ok(()) => Response::Acked {},
                Err(message) => Response::Error { message },
            },
            Request::MarkArchiveDeleted { id, bytes_freed } => {
                match handle_mark_archive_deleted(conn, id, bytes_freed) {
                    Ok(response) => response,
                    Err(message) => Response::Error { message },
                }
            }
            Request::ReleaseArchiveDeleteClaim { id } => {
                match handle_release_archive_delete_claim(conn, id) {
                    Ok(()) => Response::Acked {},
                    Err(message) => Response::Error { message },
                }
            }
            Request::QuarantineArchiveItem { id, reason } => {
                match handle_quarantine_archive_item(conn, id, &reason) {
                    Ok(()) => Response::Acked {},
                    Err(message) => Response::Error { message },
                }
            }
            Request::ListEvictionCandidates {
                recency_floor_epoch,
                allow_undurable,
                limit,
            } => match handle_list_eviction_candidates(
                conn,
                recency_floor_epoch,
                allow_undurable,
                limit,
            ) {
                Ok(items) => Response::EvictionCandidates { items },
                Err(message) => Response::Error { message },
            },
            Request::ListRecoveryRows {} => match handle_list_recovery_rows(conn) {
                Ok(rows) => Response::RecoveryRows { rows },
                Err(message) => Response::Error { message },
            },
            Request::CloudCandidates {
                folders,
                after_cursor,
                limit,
            } => match handle_cloud_candidates(conn, &folders, after_cursor.as_deref(), limit) {
                Ok((items, next_cursor)) => Response::CloudCandidates { items, next_cursor },
                Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(HandlerError::Internal(message)) => Response::Error { message },
            },
            Request::CloudDiscover {
                after_cursor,
                limit,
            } => match handle_cloud_discover(conn, after_cursor.as_deref(), limit) {
                Ok((items, next_cursor)) => Response::CloudDiscoverPage { items, next_cursor },
                Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(HandlerError::Internal(message)) => Response::Error { message },
            },
            Request::CloudQueueLoad {
                after_cursor,
                limit,
                upload_set_id,
            } => match handle_cloud_queue_load(
                conn,
                after_cursor.as_deref(),
                limit,
                upload_set_id.as_deref(),
            ) {
                Ok((items, next_cursor)) => Response::CloudQueuePage { items, next_cursor },
                Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(HandlerError::Internal(message)) => Response::Error { message },
            },
            Request::CloudPendingUploadSetsLoad { after_cursor, limit } => {
                match handle_cloud_pending_upload_sets_load(conn, after_cursor.as_deref(), limit) {
                    Ok((items, next_cursor)) => {
                        Response::CloudPendingUploadSetsPage { items, next_cursor }
                    }
                    Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                    Err(HandlerError::Internal(message)) => Response::Error { message },
                }
            }
            Request::CloudQueueUpsert { item } => match handle_cloud_queue_upsert(conn, &item) {
                Ok(state) => Response::CloudQueueState { state },
                Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(HandlerError::Internal(message)) => Response::Error { message },
            },
            Request::CloudQueueRetry {
                archive_item_id,
                child_key,
                upload_set_id,
                resolution,
            } => match handle_cloud_queue_retry(
                conn,
                archive_item_id,
                child_key.as_deref(),
                upload_set_id.as_deref(),
                &resolution,
            ) {
                Ok(state) => Response::CloudQueueState { state },
                Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(HandlerError::Internal(message)) => Response::Error { message },
            },
            Request::UploadLeaseAcquire {
                archive_item_id,
                ttl_ms,
            } => match handle_upload_lease_acquire(conn, boot, archive_item_id, ttl_ms) {
                Ok((granted, token, boot_id, expires_mono_ms)) => Response::UploadLeaseAcquired {
                    granted,
                    token,
                    boot_id,
                    expires_mono_ms,
                },
                Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(HandlerError::Internal(message)) => Response::Error { message },
            },
            Request::UploadLeaseRenew { token, ttl_ms } => {
                match handle_upload_lease_renew(conn, boot, &token, ttl_ms) {
                    Ok((ok, expires_mono_ms)) => Response::UploadLeaseRenewed {
                        ok,
                        expires_mono_ms,
                    },
                    Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                    Err(HandlerError::Internal(message)) => Response::Error { message },
                }
            }
            Request::UploadLeaseRelease { token } => {
                match handle_upload_lease_release(conn, boot, &token) {
                    Ok(ok) => Response::UploadLeaseReleased { ok },
                    Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                    Err(HandlerError::Internal(message)) => Response::Error { message },
                }
            }
            Request::CloudUploadCommit {
                queue_pk,
                attempt_id,
                upload_set_id,
                hash,
                hash_alg,
                size,
            } => match handle_cloud_upload_commit(
                conn,
                &queue_pk.destination_id,
                &queue_pk.remote_key,
                &attempt_id,
                upload_set_id.as_deref(),
                &hash,
                &hash_alg,
                size,
            ) {
                Ok((ok, durable_parent)) => Response::CloudUploadCommitted { ok, durable_parent },
                Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(HandlerError::Internal(message)) => Response::Error { message },
            },
            Request::CloudUploadFail {
                queue_pk,
                attempt_id,
                upload_set_id,
                error_class,
                not_before,
                terminal,
            } => match handle_cloud_upload_fail(
                conn,
                &queue_pk.destination_id,
                &queue_pk.remote_key,
                &attempt_id,
                upload_set_id.as_deref(),
                &error_class,
                not_before,
                terminal,
            ) {
                Ok((ok, state)) => Response::CloudUploadFailed { ok, state },
                Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(HandlerError::Internal(message)) => Response::Error { message },
            },
            Request::CloudStatsGet {} => match handle_cloud_stats_get(conn) {
                Ok((synced_count, synced_bytes, since_at)) => Response::CloudStats {
                    synced_count,
                    synced_bytes,
                    since_at,
                },
                Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(HandlerError::Internal(message)) => Response::Error { message },
            },
            Request::CloudStatsReset {} => match handle_cloud_stats_reset(conn) {
                Ok(baseline_seq) => Response::CloudStatsReset {
                    ok: true,
                    baseline_seq,
                },
                Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(HandlerError::Internal(message)) => Response::Error { message },
            },
            Request::CloudConfigGet {} => match handle_cloud_config_get(conn) {
                Ok(config) => Response::CloudConfig { config },
                Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(HandlerError::Internal(message)) => Response::Error { message },
            },
            Request::CloudConfigPut { config } => match handle_cloud_config_put(conn, &config) {
                Ok(config) => Response::CloudConfig { config },
                Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(HandlerError::Internal(message)) => Response::Error { message },
            },
            Request::CloudHistoryLoad {
                after_cursor,
                limit,
            } => match handle_cloud_history_load(conn, after_cursor.as_deref(), limit) {
                Ok((items, next_cursor)) => Response::CloudHistoryPage { items, next_cursor },
                Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(HandlerError::Internal(message)) => Response::Error { message },
            },
            Request::FinalizeEventArchive(payload) => {
                match handle_finalize_event_archive(conn, boot, &payload) {
                    Ok(result) => Response::FinalizeEventArchive(result),
                    Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                    Err(HandlerError::Internal(message)) => Response::Error { message },
                }
            }
            Request::CloudPrepareParentUpload(payload) => {
                match handle_cloud_prepare_parent_upload(conn, &payload) {
                    Ok(response) => Response::CloudPrepareParentUpload(response),
                    Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                    Err(HandlerError::Internal(message)) => Response::Error { message },
                }
            }
            Request::CloudFinalizeParentUpload(payload) => {
                match handle_cloud_finalize_parent_upload(conn, &payload) {
                    Ok(response) => Response::CloudFinalizeParentUpload(response),
                    Err(HandlerError::Rejected(message)) => Response::Rejected { message },
                    Err(HandlerError::Internal(message)) => Response::Error { message },
                }
            }
        },
        Err(e) => Response::Error {
            message: format!("invalid request: {e}"),
        },
    };
    let _ = write_response(&mut stream, &response);
    Ok(())
}

/// Classifies a register-handler failure for wire-response mapping.
enum HandlerError {
    /// Deterministic: payload invalid, retry is futile -> `Response::Rejected`.
    Rejected(String),
    /// Operational/transient: DB or lock failure -> `Response::Error`.
    Internal(String),
}

fn handle_register_archived_clip(
    conn: &Arc<Mutex<Connection>>,
    payload: &RegisterArchivedClip,
) -> Result<(i64, i64), HandlerError> {
    let registration = build_registration(payload).map_err(HandlerError::Rejected)?;
    let mut locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    register_archived_clip(&mut locked, &registration)
        .map_err(|e| HandlerError::Internal(e.to_string()))
}

fn handle_register_quarantined_clip(
    conn: &Arc<Mutex<Connection>>,
    payload: &RegisterArchivedClip,
) -> Result<(i64, i64), HandlerError> {
    let registration = build_registration(payload).map_err(HandlerError::Rejected)?;
    let mut locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    register_quarantined_clip(&mut locked, &registration)
        .map_err(|e| HandlerError::Internal(e.to_string()))
}

fn handle_set_pref(conn: &Arc<Mutex<Connection>>, key: &str, value: &str) -> Result<(), String> {
    let locked = conn
        .lock()
        .map_err(|_| "index database mutex is poisoned".to_owned())?;
    set_pref(&locked, key, value).map_err(|e| e.to_string())
}

fn handle_claim_eviction_candidate(
    conn: &Arc<Mutex<Connection>>,
    boot: &Arc<BootContext>,
    id: i64,
    recency_floor_epoch: i64,
    allow_undurable: bool,
) -> Result<Response, String> {
    let mut locked = conn
        .lock()
        .map_err(|_| "index database mutex is poisoned".to_owned())?;
    let exists: i64 = locked
        .query_row(
            "SELECT COUNT(*) FROM archive_items WHERE id = ?1",
            params![id],
            |row| row.get(0),
        )
        .map_err(|e| e.to_string())?;
    if exists == 0 {
        return Ok(Response::NotFound {});
    }

    let claimed = boot
        .claim_eviction_candidate(&mut locked, id, recency_floor_epoch, allow_undurable)
        .map_err(|e| e.to_string())?;
    if claimed.is_some() {
        return Ok(Response::Claimed {});
    }

    let leased = has_unexpired_lease(&locked, boot.boot_id(), boot.mono_now_ms(), id)
        .map_err(|e| e.to_string())?;
    if leased {
        Ok(Response::ClaimDenied {
            reason: "unexpired lease".to_owned(),
        })
    } else {
        Ok(Response::ClaimDenied {
            reason: "ineligible".to_owned(),
        })
    }
}

fn handle_mark_archive_deleting(conn: &Arc<Mutex<Connection>>, id: i64) -> Result<(), String> {
    let locked = conn
        .lock()
        .map_err(|_| "index database mutex is poisoned".to_owned())?;
    mark_deleting(&locked, id).map_err(|e| e.to_string())
}

fn handle_mark_archive_deleted(
    conn: &Arc<Mutex<Connection>>,
    id: i64,
    bytes_freed: i64,
) -> Result<Response, String> {
    if bytes_freed < 0 {
        return Ok(Response::Rejected {
            message: "bytes_freed must be >= 0".to_owned(),
        });
    }
    let locked = conn
        .lock()
        .map_err(|_| "index database mutex is poisoned".to_owned())?;
    mark_deleted(&locked, id, bytes_freed).map_err(|e| e.to_string())?;
    Ok(Response::Acked {})
}

fn handle_release_archive_delete_claim(
    conn: &Arc<Mutex<Connection>>,
    id: i64,
) -> Result<(), String> {
    let locked = conn
        .lock()
        .map_err(|_| "index database mutex is poisoned".to_owned())?;
    release_delete_claim(&locked, id).map_err(|e| e.to_string())
}

fn handle_quarantine_archive_item(
    conn: &Arc<Mutex<Connection>>,
    id: i64,
    reason: &str,
) -> Result<(), String> {
    let locked = conn
        .lock()
        .map_err(|_| "index database mutex is poisoned".to_owned())?;
    quarantine(&locked, id, reason).map_err(|e| e.to_string())
}

fn handle_list_eviction_candidates(
    conn: &Arc<Mutex<Connection>>,
    recency_floor_epoch: i64,
    allow_undurable: bool,
    limit: u32,
) -> Result<Vec<EvictionCandidateWire>, String> {
    let now_epoch = now_epoch_s();
    let locked = conn
        .lock()
        .map_err(|_| "index database mutex is poisoned".to_owned())?;
    let rows = list_eviction_candidates(
        &locked,
        recency_floor_epoch,
        now_epoch,
        allow_undurable,
        limit,
    )
    .map_err(|e| e.to_string())?;
    Ok(rows
        .into_iter()
        .map(|row| EvictionCandidateWire {
            id: row.id,
            path: row.path,
            size_bytes: row.size_bytes,
            archived_at: row.archived_at,
            folder_class: row.folder_class,
        })
        .collect())
}

fn handle_list_recovery_rows(
    conn: &Arc<Mutex<Connection>>,
) -> Result<Vec<RecoveryRowWire>, String> {
    let locked = conn
        .lock()
        .map_err(|_| "index database mutex is poisoned".to_owned())?;
    let rows = list_recovery_rows(&locked).map_err(|e| e.to_string())?;
    Ok(rows
        .into_iter()
        .map(|row| RecoveryRowWire {
            id: row.id,
            delete_state: row.delete_state,
            path: row.path,
            size_bytes: row.size_bytes,
            delete_gen: row.delete_gen,
        })
        .collect())
}

fn map_db_error(error: DbError) -> HandlerError {
    match error {
        DbError::Sqlite(rusqlite::Error::InvalidParameterName(message)) => {
            HandlerError::Rejected(message)
        }
        other => HandlerError::Internal(other.to_string()),
    }
}

fn to_config_wire(config: &CloudConfig) -> CloudConfigWire {
    CloudConfigWire {
        sentry_enabled: config.sentry_enabled,
        saved_enabled: config.saved_enabled,
        recent_enabled: config.recent_enabled,
        sentry_priority: config.sentry_priority,
        saved_priority: config.saved_priority,
        recent_priority: config.recent_priority,
        reserve_gb: config.reserve_gb,
        max_attempts: config.max_attempts,
        base_backoff_secs: config.base_backoff_secs,
        keep_until_backed_up: config.keep_until_backed_up,
        auto_sync: config.auto_sync,
    }
}

fn from_config_wire(config: &CloudConfigWire) -> CloudConfig {
    CloudConfig {
        sentry_enabled: config.sentry_enabled,
        saved_enabled: config.saved_enabled,
        recent_enabled: config.recent_enabled,
        sentry_priority: config.sentry_priority,
        saved_priority: config.saved_priority,
        recent_priority: config.recent_priority,
        reserve_gb: config.reserve_gb,
        max_attempts: config.max_attempts,
        base_backoff_secs: config.base_backoff_secs,
        keep_until_backed_up: config.keep_until_backed_up,
        auto_sync: config.auto_sync,
    }
}

fn from_retry_resolution_wire(
    resolution: &CloudQueueRetryResolutionWire,
) -> CloudQueueRetryResolution {
    match resolution {
        CloudQueueRetryResolutionWire::KeepExisting => CloudQueueRetryResolution::KeepExisting,
        CloudQueueRetryResolutionWire::Rekey { remote_key } => CloudQueueRetryResolution::Rekey {
            remote_key: remote_key.clone(),
        },
        CloudQueueRetryResolutionWire::Replace => CloudQueueRetryResolution::Replace,
    }
}

fn handle_cloud_candidates(
    conn: &Arc<Mutex<Connection>>,
    folders: &[String],
    after_cursor: Option<&str>,
    limit: u32,
) -> Result<(Vec<CloudCandidateWire>, Option<String>), HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    let page = cloud_candidates(&locked, folders, after_cursor, limit).map_err(map_db_error)?;
    Ok((
        page.items
            .into_iter()
            .map(|row| CloudCandidateWire {
                archive_item_id: row.archive_item_id,
                child_key: row.child_key,
                source_rel: row.source_rel,
                destination_id: row.destination_id,
                remote_key: row.remote_key,
                size_bytes: row.size_bytes,
                content_sha256: row.content_sha256,
                state: row.state,
                category: row.category,
                seq: row.seq,
            })
            .collect(),
        page.next_cursor,
    ))
}

fn handle_cloud_queue_load(
    conn: &Arc<Mutex<Connection>>,
    after_cursor: Option<&str>,
    limit: u32,
    upload_set_id: Option<&str>,
) -> Result<(Vec<CloudQueueRowWire>, Option<String>), HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    let page = cloud_queue_load(&locked, after_cursor, limit, upload_set_id).map_err(map_db_error)?;
    Ok((
        page.items
            .into_iter()
            .map(|row| CloudQueueRowWire {
                archive_item_id: row.archive_item_id,
                child_key: row.child_key,
                destination_id: row.destination_id,
                remote_key: row.remote_key,
                category: row.category,
                seq: row.seq,
                total_bytes: row.total_bytes,
                bytes_uploaded: row.bytes_uploaded,
                expected_hash: row.expected_hash,
                verify_alg: row.verify_alg,
                content_sha256: row.content_sha256,
                state: row.state,
                attempts: row.attempts,
                not_before: row.not_before,
                last_error: row.last_error,
                upload_set_id: row.upload_set_id,
            })
            .collect(),
        page.next_cursor,
    ))
}

fn handle_cloud_pending_upload_sets_load(
    conn: &Arc<Mutex<Connection>>,
    after_cursor: Option<&str>,
    limit: u32,
) -> Result<(Vec<CloudPendingUploadSetWire>, Option<String>), HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    let page = cloud_pending_upload_sets_load(&locked, after_cursor, limit).map_err(map_db_error)?;
    Ok((
        page.items
            .into_iter()
            .map(|row| CloudPendingUploadSetWire {
                upload_set_id: row.upload_set_id,
                archive_item_id: row.archive_item_id,
                destination_id: row.destination_id,
                source_manifest_digest: row.source_manifest_digest,
                expected_child_count: row.expected_child_count,
            })
            .collect(),
        page.next_cursor,
    ))
}

fn handle_cloud_discover(
    conn: &Arc<Mutex<Connection>>,
    after_cursor: Option<&str>,
    limit: u32,
) -> Result<(Vec<CloudDiscoverWire>, Option<String>), HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    let page = cloud_discover(&locked, after_cursor, limit).map_err(map_db_error)?;
    Ok((
        page.items
            .into_iter()
            .map(|row| CloudDiscoverWire {
                archive_item_id: row.archive_item_id,
                folder_class: row.folder_class,
                path: row.path,
                manifest_digest: row.manifest_digest,
                category: row.category,
            })
            .collect(),
        page.next_cursor,
    ))
}

fn handle_cloud_queue_upsert(
    conn: &Arc<Mutex<Connection>>,
    item: &CloudQueueUpsertWire,
) -> Result<String, HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    cloud_queue_upsert(
        &locked,
        &CloudQueueUpsertItem {
            archive_item_id: item.archive_item_id,
            child_key: item.child_key.clone(),
            destination_id: item.destination_id.clone(),
            remote_key: item.remote_key.clone(),
            category: item.category.clone(),
            seq: item.seq,
            total_bytes: item.total_bytes,
            content_sha256: item.content_sha256.clone(),
            expected_hash: item.expected_hash.clone(),
            verify_alg: item.verify_alg.clone(),
        },
    )
    .map_err(map_db_error)
}

fn handle_cloud_queue_retry(
    conn: &Arc<Mutex<Connection>>,
    archive_item_id: i64,
    child_key: Option<&str>,
    upload_set_id: Option<&str>,
    resolution: &CloudQueueRetryResolutionWire,
) -> Result<String, HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    cloud_queue_retry(
        &locked,
        archive_item_id,
        child_key,
        upload_set_id,
        &from_retry_resolution_wire(resolution),
    )
    .map_err(map_db_error)
}

fn handle_upload_lease_acquire(
    conn: &Arc<Mutex<Connection>>,
    boot: &Arc<BootContext>,
    archive_item_id: i64,
    ttl_ms: u32,
) -> Result<UploadLeaseAcquireWire, HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    let result =
        upload_lease_acquire(&locked, boot, archive_item_id, ttl_ms).map_err(map_db_error)?;
    Ok((
        result.granted,
        result.token,
        result.boot_id,
        result.expires_mono_ms,
    ))
}

type UploadLeaseAcquireWire = (bool, Option<String>, Option<String>, Option<i64>);

fn handle_upload_lease_renew(
    conn: &Arc<Mutex<Connection>>,
    boot: &Arc<BootContext>,
    token: &str,
    ttl_ms: u32,
) -> Result<(bool, Option<i64>), HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    let result = upload_lease_renew(&locked, boot, token, ttl_ms).map_err(map_db_error)?;
    Ok((result.ok, result.expires_mono_ms))
}

fn handle_upload_lease_release(
    conn: &Arc<Mutex<Connection>>,
    boot: &Arc<BootContext>,
    token: &str,
) -> Result<bool, HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    upload_lease_release(&locked, boot, token).map_err(map_db_error)
}

#[allow(clippy::too_many_arguments)]
fn handle_cloud_upload_commit(
    conn: &Arc<Mutex<Connection>>,
    destination_id: &str,
    remote_key: &str,
    attempt_id: &str,
    upload_set_id: Option<&str>,
    hash: &str,
    hash_alg: &str,
    size: i64,
) -> Result<(bool, bool), HandlerError> {
    let mut locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    let result = cloud_upload_commit(
        &mut locked,
        &CloudQueuePk {
            destination_id: destination_id.to_owned(),
            remote_key: remote_key.to_owned(),
        },
        attempt_id,
        hash,
        hash_alg,
        size,
        upload_set_id,
    )
    .map_err(map_db_error)?;
    Ok((result.ok, result.durable_parent))
}

#[allow(clippy::too_many_arguments)]
fn handle_cloud_upload_fail(
    conn: &Arc<Mutex<Connection>>,
    destination_id: &str,
    remote_key: &str,
    attempt_id: &str,
    upload_set_id: Option<&str>,
    error_class: &str,
    not_before: Option<i64>,
    terminal: bool,
) -> Result<(bool, String), HandlerError> {
    let mut locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    let result = cloud_upload_fail(
        &mut locked,
        &CloudQueuePk {
            destination_id: destination_id.to_owned(),
            remote_key: remote_key.to_owned(),
        },
        attempt_id,
        error_class,
        not_before,
        terminal,
        upload_set_id,
    )
    .map_err(map_db_error)?;
    Ok((result.ok, result.state))
}

fn handle_cloud_stats_get(conn: &Arc<Mutex<Connection>>) -> Result<(i64, i64, i64), HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    let stats = cloud_stats_get(&locked).map_err(map_db_error)?;
    Ok((stats.synced_count, stats.synced_bytes, stats.since_at))
}

fn handle_cloud_stats_reset(conn: &Arc<Mutex<Connection>>) -> Result<i64, HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    cloud_stats_reset(&locked).map_err(map_db_error)
}

fn handle_cloud_config_get(conn: &Arc<Mutex<Connection>>) -> Result<CloudConfigWire, HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    let config = cloud_config_get(&locked).map_err(map_db_error)?;
    Ok(to_config_wire(&config))
}

fn handle_cloud_config_put(
    conn: &Arc<Mutex<Connection>>,
    config: &CloudConfigWire,
) -> Result<CloudConfigWire, HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    let persisted = cloud_config_put(&locked, &from_config_wire(config)).map_err(map_db_error)?;
    Ok(to_config_wire(&persisted))
}

fn handle_cloud_history_load(
    conn: &Arc<Mutex<Connection>>,
    after_cursor: Option<&str>,
    limit: u32,
) -> Result<(Vec<CloudHistoryRowWire>, Option<String>), HandlerError> {
    let locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    let page = cloud_history_load(&locked, after_cursor, limit).map_err(map_db_error)?;
    Ok((
        page.items
            .into_iter()
            .map(|row| CloudHistoryRowWire {
                id: row.id,
                completion_seq: row.completion_seq,
                archive_item_id: row.archive_item_id,
                child_key: row.child_key,
                destination_id: row.destination_id,
                outcome: row.outcome,
                size_bytes: row.size_bytes,
                at: row.at,
                error_class: row.error_class,
            })
            .collect(),
        page.next_cursor,
    ))
}

const PREPARE_REQUEST_DIGEST_DOMAIN_TAG: &[u8] = b"teslausb.cloud_prepare_set.v1\0";
const PREPARE_UPLOAD_SET_ID_DOMAIN_TAG: &[u8] = b"teslausb.upload_set_id.v1\0";
const PREPARE_COLLISION_PARK_MESSAGE: &str = "hash collision on destination_id+remote_key";

#[derive(Debug, Clone)]
struct PreparedChild {
    wire: CloudPrepareParentUploadChildWire,
    size_u64: u64,
    content_sha256_bytes: [u8; 32],
}

fn handle_cloud_prepare_parent_upload(
    conn: &Arc<Mutex<Connection>>,
    payload: &CloudPrepareParentUploadRequest,
) -> Result<CloudPrepareParentUploadResponse, HandlerError> {
    let mut locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    prepare_parent_upload_tx(&mut locked, payload)
}

fn prepare_parent_upload_tx(
    conn: &mut Connection,
    payload: &CloudPrepareParentUploadRequest,
) -> Result<CloudPrepareParentUploadResponse, HandlerError> {
    let tx = conn
        .transaction()
        .map_err(|e| HandlerError::Internal(e.to_string()))?;
    let response = prepare_parent_upload_in_tx(&tx, payload)?;
    tx.commit().map_err(map_prepare_sqlite_error)?;
    Ok(response)
}

fn handle_cloud_finalize_parent_upload(
    conn: &Arc<Mutex<Connection>>,
    payload: &CloudFinalizeParentUploadRequest,
) -> Result<CloudFinalizeParentUploadResponse, HandlerError> {
    let mut locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    finalize_parent_upload_tx(&mut locked, payload)
}

fn finalize_parent_upload_tx(
    conn: &mut Connection,
    payload: &CloudFinalizeParentUploadRequest,
) -> Result<CloudFinalizeParentUploadResponse, HandlerError> {
    let tx = conn
        .transaction()
        .map_err(|e| HandlerError::Internal(e.to_string()))?;
    let response = finalize_parent_upload_in_tx(&tx, payload)?;
    tx.commit().map_err(map_prepare_sqlite_error)?;
    Ok(response)
}

#[allow(clippy::cognitive_complexity, clippy::too_many_lines)]
fn finalize_parent_upload_in_tx(
    tx: &rusqlite::Transaction<'_>,
    payload: &CloudFinalizeParentUploadRequest,
) -> Result<CloudFinalizeParentUploadResponse, HandlerError> {
    let set_row = tx
        .query_row(
            "SELECT archive_item_id, source_manifest_digest, expected_child_count, finalized_at, superseded_at
               FROM cloud_parent_upload_sets
              WHERE upload_set_id = ?1",
            params![payload.upload_set_id],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, Option<i64>>(3)?,
                    row.get::<_, Option<i64>>(4)?,
                ))
            },
        )
        .optional()
        .map_err(|e| HandlerError::Internal(e.to_string()))?;
    let Some((archive_item_id, source_manifest_digest, _expected_child_count, finalized_at, superseded_at)) =
        set_row
    else {
        return Err(HandlerError::Rejected(
            "finalize rejected: unknown upload set".to_owned(),
        ));
    };
    if superseded_at.is_some() {
        return Err(HandlerError::Rejected(
            "finalize rejected: upload set is superseded".to_owned(),
        ));
    }

    let parent_row = tx
        .query_row(
            "SELECT delete_state, durable, manifest_digest
               FROM archive_items
              WHERE id = ?1",
            params![archive_item_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, Option<String>>(2)?,
                ))
            },
        )
        .optional()
        .map_err(|e| HandlerError::Internal(e.to_string()))?;
    let Some((delete_state, durable, manifest_digest)) = parent_row else {
        return Err(HandlerError::Rejected(
            "finalize rejected: parent archive item does not exist".to_owned(),
        ));
    };
    if delete_state != "LIVE" {
        return Err(HandlerError::Rejected(
            "finalize rejected: parent archive item is not LIVE".to_owned(),
        ));
    }
    let Some(manifest_digest) = manifest_digest else {
        return Err(HandlerError::Rejected(
            "finalize rejected: manifest digest mismatch".to_owned(),
        ));
    };
    if manifest_digest != source_manifest_digest {
        return Err(HandlerError::Rejected(
            "finalize rejected: manifest digest mismatch".to_owned(),
        ));
    }
    if finalized_at.is_some() {
        return Ok(CloudFinalizeParentUploadResponse {
            ok: true,
            durable_parent: durable == 1,
            already_finalized: true,
        });
    }

    let complete: bool = tx
        .query_row(
            "SELECT EXISTS (
                 SELECT 1
                   FROM cloud_parent_upload_sets s
                  WHERE s.upload_set_id = ?1
                    AND s.expected_child_count > 0
                    AND s.expected_child_count = (
                        SELECT COUNT(*)
                          FROM cloud_parent_upload_set_children c
                         WHERE c.upload_set_id = s.upload_set_id
                    )
                    AND s.expected_child_count = (
                        SELECT COUNT(*)
                          FROM cloud_upload_queue q
                         WHERE q.upload_set_id = s.upload_set_id
                    )
                    AND NOT EXISTS (
                        SELECT 1
                          FROM cloud_parent_upload_set_children m
                          LEFT JOIN cloud_upload_queue q
                            ON q.upload_set_id = m.upload_set_id
                           AND q.destination_id = m.destination_id
                           AND q.remote_key = m.remote_key
                         WHERE m.upload_set_id = s.upload_set_id
                           AND (
                               q.upload_set_id IS NULL
                               OR q.state <> 'done'
                               OR q.content_sha256 <> m.content_sha256
                               OR q.verify_alg <> m.verify_alg
                               OR COALESCE(q.expected_hash, '') <> m.expected_hash
                               OR q.child_key <> m.child_key
                               OR q.category <> m.category
                               OR q.seq <> m.seq
                               OR q.total_bytes <> m.total_bytes
                           )
                    )
            )",
            params![payload.upload_set_id],
            |row| row.get(0),
        )
        .map_err(|e| HandlerError::Internal(e.to_string()))?;
    if !complete {
        return Ok(CloudFinalizeParentUploadResponse {
            ok: true,
            durable_parent: false,
            already_finalized: false,
        });
    }

    let now = now_epoch_s();
    tx.execute(
        "UPDATE cloud_parent_upload_sets
            SET finalized_at = ?2
          WHERE upload_set_id = ?1
            AND finalized_at IS NULL",
        params![payload.upload_set_id, now],
    )
    .map_err(map_prepare_sqlite_error)?;
    tx.execute(
        "UPDATE archive_items
            SET durable = 1, updated_at = ?2
          WHERE id = ?1
            AND durable = 0",
        params![archive_item_id, now],
    )
    .map_err(map_prepare_sqlite_error)?;

    Ok(CloudFinalizeParentUploadResponse {
        ok: true,
        durable_parent: true,
        already_finalized: false,
    })
}

#[allow(clippy::cognitive_complexity, clippy::too_many_lines)]
fn prepare_parent_upload_in_tx(
    tx: &rusqlite::Transaction<'_>,
    payload: &CloudPrepareParentUploadRequest,
) -> Result<CloudPrepareParentUploadResponse, HandlerError> {
    if payload.archive_item_id <= 0 {
        return Err(HandlerError::Rejected(
            "archive_item_id must be > 0".to_owned(),
        ));
    }
    validate_non_empty_without_nul(&payload.destination_id, "destination_id", 128)?;
    if !is_lower_hex(&payload.source_manifest_digest, 32) {
        return Err(HandlerError::Rejected(
            "source_manifest_digest must be 32 lowercase hex".to_owned(),
        ));
    }
    if payload.children.is_empty() {
        return Err(HandlerError::Rejected(
            "children must contain at least one child".to_owned(),
        ));
    }

    let mut prepared_children = Vec::with_capacity(payload.children.len());
    let mut seen_child_keys = HashSet::with_capacity(payload.children.len());
    let mut seen_destination_remote = HashSet::with_capacity(payload.children.len());
    for child in &payload.children {
        validate_prepare_child_key(&child.child_key)?;
        if !seen_child_keys.insert(child.child_key.clone()) {
            return Err(HandlerError::Rejected(format!(
                "duplicate child_key in prepare request: {}",
                child.child_key
            )));
        }
        validate_non_empty_without_nul(&child.destination_id, "children.destination_id", 128)?;
        if child.destination_id != payload.destination_id {
            return Err(HandlerError::Rejected(
                "children.destination_id must match request destination_id".to_owned(),
            ));
        }
        validate_non_empty_without_nul(&child.remote_key, "children.remote_key", 1024)?;
        validate_prepare_category(&child.category)?;
        if child.seq < 0 {
            return Err(HandlerError::Rejected(
                "children.seq must be >= 0".to_owned(),
            ));
        }
        let size_u64 = u64::try_from(child.total_bytes).map_err(|_| {
            HandlerError::Rejected("children.total_bytes must be >= 0".to_owned())
        })?;
        validate_non_empty_without_nul(&child.expected_hash, "children.expected_hash", 256)?;
        validate_prepare_verify_alg(&child.verify_alg)?;
        let content_sha256_bytes = decode_lower_hex_sha256(&child.content_sha256)?;
        if !seen_destination_remote.insert((child.destination_id.clone(), child.remote_key.clone()))
        {
            return Err(HandlerError::Rejected(
                "duplicate destination_id+remote_key in prepare children".to_owned(),
            ));
        }
        prepared_children.push(PreparedChild {
            wire: child.clone(),
            size_u64,
            content_sha256_bytes,
        });
    }

    let entries: Vec<ManifestDigestEntry<'_>> = prepared_children
        .iter()
        .map(|child| ManifestDigestEntry {
            rel_name: child.wire.child_key.as_str(),
            size: child.size_u64,
            mtime_ms: child.wire.manifest_mtime_ms,
            hash: child.content_sha256_bytes,
        })
        .collect();
    let reconstructed_manifest_digest = manifest_digest_v1_hex(&entries);
    let parent_row = tx
        .query_row(
            "SELECT delete_state, durable, manifest_digest
               FROM archive_items
              WHERE id = ?1",
            params![payload.archive_item_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, Option<String>>(2)?,
                ))
            },
        )
        .optional()
        .map_err(|e| HandlerError::Internal(e.to_string()))?;
    let Some((delete_state, durable, stored_manifest_digest)) = parent_row else {
        return Err(HandlerError::Rejected(
            "prepare rejected: parent archive item does not exist".to_owned(),
        ));
    };
    if delete_state != "LIVE" {
        return Err(HandlerError::Rejected(
            "prepare rejected: parent archive item is not LIVE".to_owned(),
        ));
    }
    let Some(stored_manifest_digest) = stored_manifest_digest else {
        return Err(HandlerError::Rejected(
            "prepare rejected: parent manifest_digest is NULL".to_owned(),
        ));
    };
    if reconstructed_manifest_digest != payload.source_manifest_digest
        || payload.source_manifest_digest != stored_manifest_digest
    {
        return Err(HandlerError::Rejected(
            "prepare rejected: manifest digest triple-equality mismatch".to_owned(),
        ));
    }

    let request_digest = compute_prepare_request_digest(payload.archive_item_id, payload, &prepared_children)?;
    let current_set = tx
        .query_row(
            "SELECT upload_set_id, request_digest
               FROM cloud_parent_upload_sets
              WHERE archive_item_id = ?1
                AND superseded_at IS NULL",
            params![payload.archive_item_id],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(|e| HandlerError::Internal(e.to_string()))?;
    if let Some((upload_set_id, existing_request_digest)) = &current_set {
        if existing_request_digest == &request_digest {
            return Ok(CloudPrepareParentUploadResponse {
                upload_set_id: upload_set_id.clone(),
                already_prepared: true,
            });
        }
    }

    if durable != 0 {
        return Err(HandlerError::Rejected(
            "prepare rejected: durable parent cannot be prepared".to_owned(),
        ));
    }
    for child in &prepared_children {
        let conflicting_parent = tx
            .query_row(
                "SELECT s.archive_item_id
                   FROM cloud_parent_upload_set_children c
                   JOIN cloud_parent_upload_sets s ON s.upload_set_id = c.upload_set_id
                  WHERE s.superseded_at IS NULL
                    AND s.archive_item_id != ?1
                    AND c.destination_id = ?2
                    AND c.remote_key = ?3
                  LIMIT 1",
                params![
                    payload.archive_item_id,
                    child.wire.destination_id,
                    child.wire.remote_key
                ],
                |row| row.get::<_, i64>(0),
            )
            .optional()
            .map_err(|e| HandlerError::Internal(e.to_string()))?;
        if conflicting_parent.is_some() {
            return Err(HandlerError::Rejected(
                "prepare rejected: remote_key already belongs to another parent current set"
                    .to_owned(),
            ));
        }
    }

    let now = now_epoch_s();
    if let Some((upload_set_id, _)) = current_set {
        tx.execute(
            "UPDATE cloud_parent_upload_sets
                SET superseded_at = ?2
              WHERE upload_set_id = ?1
                AND superseded_at IS NULL",
            params![upload_set_id, now],
        )
        .map_err(map_prepare_sqlite_error)?;
        tx.execute(
            "UPDATE cloud_upload_queue
                SET state = 'parked',
                    bytes_uploaded = 0,
                    attempts = 0,
                    not_before = NULL,
                    last_error = ?2
              WHERE upload_set_id = ?1
                AND state != 'done'",
            params![upload_set_id, PREPARE_COLLISION_PARK_MESSAGE],
        )
        .map_err(map_prepare_sqlite_error)?;
    }

    let upload_set_id = compute_prepare_upload_set_id(&request_digest, now, payload.archive_item_id)?;
    let expected_child_count = i64::try_from(prepared_children.len())
        .map_err(|_| HandlerError::Rejected("too many children".to_owned()))?;
    tx.execute(
        "INSERT INTO cloud_parent_upload_sets
            (upload_set_id, archive_item_id, destination_id, source_manifest_digest, request_digest,
             expected_child_count, created_at, finalized_at, superseded_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, NULL, NULL)",
        params![
            upload_set_id,
            payload.archive_item_id,
            payload.destination_id,
            payload.source_manifest_digest,
            request_digest,
            expected_child_count,
            now,
        ],
    )
    .map_err(map_prepare_sqlite_error)?;

    for child in &prepared_children {
        tx.execute(
            "INSERT INTO cloud_parent_upload_set_children
                (upload_set_id, child_key, destination_id, remote_key, category, seq, total_bytes,
                 manifest_mtime_ms, content_sha256, expected_hash, verify_alg)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            params![
                upload_set_id,
                child.wire.child_key,
                child.wire.destination_id,
                child.wire.remote_key,
                child.wire.category,
                child.wire.seq,
                child.wire.total_bytes,
                child.wire.manifest_mtime_ms,
                child.wire.content_sha256,
                child.wire.expected_hash,
                child.wire.verify_alg,
            ],
        )
        .map_err(map_prepare_sqlite_error)?;
        upsert_prepared_queue_row(tx, payload.archive_item_id, &upload_set_id, child)?;
    }

    Ok(CloudPrepareParentUploadResponse {
        upload_set_id,
        already_prepared: false,
    })
}

fn upsert_prepared_queue_row(
    tx: &rusqlite::Transaction<'_>,
    archive_item_id: i64,
    upload_set_id: &str,
    child: &PreparedChild,
) -> Result<(), HandlerError> {
    let dedup_done = tx
        .query_row(
            "SELECT content_sha256, size_bytes, verify_alg, COALESCE(verify_value, '')
               FROM cloud_synced_files
              WHERE destination_id = ?1 AND remote_key = ?2",
            params![child.wire.destination_id, child.wire.remote_key],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            },
        )
        .optional()
        .map_err(|e| HandlerError::Internal(e.to_string()))?
        .is_some_and(|(content_sha256, size_bytes, verify_alg, verify_value)| {
            content_sha256 == child.wire.content_sha256
                && size_bytes == child.wire.total_bytes
                && verify_alg == child.wire.verify_alg
                && verify_value == child.wire.expected_hash
        });
    let state = if dedup_done { "done" } else { "queued" };
    let bytes_uploaded = if dedup_done { child.wire.total_bytes } else { 0 };
    tx.execute(
        "INSERT INTO cloud_upload_queue
            (archive_item_id, child_key, destination_id, remote_key, category, seq, total_bytes,
             bytes_uploaded, expected_hash, verify_alg, content_sha256, state, attempts,
             not_before, last_error, upload_set_id)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, 0, NULL, NULL, ?13)
         ON CONFLICT(destination_id, remote_key) DO UPDATE SET
            archive_item_id = excluded.archive_item_id,
            child_key = excluded.child_key,
            category = excluded.category,
            seq = excluded.seq,
            total_bytes = excluded.total_bytes,
            bytes_uploaded = excluded.bytes_uploaded,
            expected_hash = excluded.expected_hash,
            verify_alg = excluded.verify_alg,
            content_sha256 = excluded.content_sha256,
            state = excluded.state,
            attempts = 0,
            not_before = NULL,
            last_error = NULL,
            upload_set_id = excluded.upload_set_id",
        params![
            archive_item_id,
            child.wire.child_key,
            child.wire.destination_id,
            child.wire.remote_key,
            child.wire.category,
            child.wire.seq,
            child.wire.total_bytes,
            bytes_uploaded,
            child.wire.expected_hash,
            child.wire.verify_alg,
            child.wire.content_sha256,
            state,
            upload_set_id,
        ],
    )
    .map_err(map_prepare_sqlite_error)?;
    Ok(())
}

fn compute_prepare_request_digest(
    archive_item_id: i64,
    payload: &CloudPrepareParentUploadRequest,
    children: &[PreparedChild],
) -> Result<String, HandlerError> {
    let mut sorted = children.to_vec();
    sorted.sort_by(|left, right| left.wire.child_key.cmp(&right.wire.child_key));
    let child_count = i64::try_from(sorted.len())
        .map_err(|_| HandlerError::Rejected("too many children".to_owned()))?;
    let mut hasher = Sha256::new();
    hasher.update(PREPARE_REQUEST_DIGEST_DOMAIN_TAG);
    hasher.update(archive_item_id.to_le_bytes());
    hash_len_prefixed(&mut hasher, payload.source_manifest_digest.as_bytes())
        .map_err(HandlerError::Rejected)?;
    hash_len_prefixed(&mut hasher, payload.destination_id.as_bytes()).map_err(HandlerError::Rejected)?;
    hasher.update(child_count.to_le_bytes());
    for child in &sorted {
        hash_len_prefixed(&mut hasher, child.wire.child_key.as_bytes()).map_err(HandlerError::Rejected)?;
        hash_len_prefixed(&mut hasher, child.wire.remote_key.as_bytes()).map_err(HandlerError::Rejected)?;
        hash_len_prefixed(&mut hasher, child.wire.content_sha256.as_bytes()).map_err(HandlerError::Rejected)?;
        hash_len_prefixed(&mut hasher, child.wire.verify_alg.as_bytes()).map_err(HandlerError::Rejected)?;
        hash_len_prefixed(&mut hasher, child.wire.expected_hash.as_bytes()).map_err(HandlerError::Rejected)?;
        hash_len_prefixed(&mut hasher, child.wire.category.as_bytes()).map_err(HandlerError::Rejected)?;
        hash_len_prefixed(&mut hasher, child.wire.destination_id.as_bytes())
            .map_err(HandlerError::Rejected)?;
        hasher.update(child.wire.seq.to_le_bytes());
        hasher.update(child.wire.total_bytes.to_le_bytes());
        hasher.update(child.wire.manifest_mtime_ms.to_le_bytes());
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn compute_prepare_upload_set_id(
    request_digest: &str,
    created_at: i64,
    archive_item_id: i64,
) -> Result<String, HandlerError> {
    let request_digest_bytes =
        decode_lower_hex_fixed(request_digest, 64, "request_digest must be 64 lowercase hex")?;
    let mut hasher = Sha256::new();
    hasher.update(PREPARE_UPLOAD_SET_ID_DOMAIN_TAG);
    hasher.update(&request_digest_bytes);
    hasher.update(created_at.to_le_bytes());
    hasher.update(archive_item_id.to_le_bytes());
    let digest = hasher.finalize();
    let mut upload_set_id = String::with_capacity(32);
    for byte in digest.iter().take(16) {
        use std::fmt::Write as _;
        write!(&mut upload_set_id, "{byte:02x}")
            .map_err(|e| HandlerError::Internal(e.to_string()))?;
    }
    Ok(upload_set_id)
}

fn validate_non_empty_without_nul(value: &str, field: &str, max: usize) -> Result<(), HandlerError> {
    if value.is_empty() || value.len() > max {
        return Err(HandlerError::Rejected(format!(
            "{field} must be 1..={max} bytes"
        )));
    }
    if value.as_bytes().contains(&0) {
        return Err(HandlerError::Rejected(format!(
            "{field} must not contain NUL bytes"
        )));
    }
    Ok(())
}

fn validate_prepare_child_key(child_key: &str) -> Result<(), HandlerError> {
    validate_non_empty_without_nul(child_key, "children.child_key", 512)?;
    if child_key.starts_with('/') {
        return Err(HandlerError::Rejected(
            "children.child_key must be relative".to_owned(),
        ));
    }
    if child_key.contains('\\') {
        return Err(HandlerError::Rejected(
            "children.child_key must not contain backslash".to_owned(),
        ));
    }
    if child_key
        .split('/')
        .any(|component| component.is_empty() || component == "." || component == "..")
    {
        return Err(HandlerError::Rejected(
            "children.child_key must not contain empty/dot path components".to_owned(),
        ));
    }
    Ok(())
}

fn validate_prepare_category(category: &str) -> Result<(), HandlerError> {
    if matches!(category, "event_sentry" | "trip" | "bulk") {
        return Ok(());
    }
    Err(HandlerError::Rejected(
        "children.category must be one of event_sentry|trip|bulk".to_owned(),
    ))
}

fn validate_prepare_verify_alg(verify_alg: &str) -> Result<(), HandlerError> {
    if matches!(
        verify_alg,
        "sha256" | "md5" | "crc32c" | "sha1" | "quickxor" | "dropbox"
    ) {
        return Ok(());
    }
    Err(HandlerError::Rejected(
        "children.verify_alg must be one of sha256|md5|crc32c|sha1|quickxor|dropbox"
            .to_owned(),
    ))
}

fn decode_lower_hex_fixed(
    value: &str,
    expected_len: usize,
    error_message: &str,
) -> Result<Vec<u8>, HandlerError> {
    if !is_lower_hex(value, expected_len) {
        return Err(HandlerError::Rejected(error_message.to_owned()));
    }
    let mut out = Vec::with_capacity(expected_len / 2);
    for pair in value.as_bytes().chunks_exact(2) {
        let hex = std::str::from_utf8(pair).map_err(|_| {
            HandlerError::Rejected(error_message.to_owned())
        })?;
        let byte = u8::from_str_radix(hex, 16)
            .map_err(|_| HandlerError::Rejected(error_message.to_owned()))?;
        out.push(byte);
    }
    Ok(out)
}

fn decode_lower_hex_sha256(value: &str) -> Result<[u8; 32], HandlerError> {
    let bytes = decode_lower_hex_fixed(
        value,
        64,
        "children.content_sha256 must be exactly 64 lowercase hex chars",
    )?;
    bytes.try_into().map_err(|_| {
        HandlerError::Rejected("children.content_sha256 must decode to 32 bytes".to_owned())
    })
}

fn map_prepare_sqlite_error(error: rusqlite::Error) -> HandlerError {
    match error {
        rusqlite::Error::SqliteFailure(_, Some(message))
            if message.contains("UNIQUE constraint failed")
                || message.contains("CHECK constraint failed")
                || message.contains("FOREIGN KEY constraint failed")
                || message.contains("NOT NULL constraint failed") =>
        {
            HandlerError::Rejected(message)
        }
        other => HandlerError::Internal(other.to_string()),
    }
}

const FINALIZE_EVENT_TOO_LARGE_MESSAGE: &str = "event too large — chunked staging not implemented";
const FINALIZE_CONFLICT_SAME_DIGEST_MESSAGE: &str =
    "finalize conflict: digest matches but content differs";
const FINALIZE_CAS_STALE_MESSAGE: &str = "finalize CAS stale";
const FINALIZE_SUPERSEDE_PARK_MESSAGE: &str = "parked: superseded by newer event generation";
const SEGMENT_SET_DIGEST_DOMAIN_TAG: &[u8] = b"teslausb.segment_set.v1\0";
const FINALIZE_METADATA_DIGEST_DOMAIN_TAG: &[u8] = b"teslausb.finalize_metadata.v1\0";

#[derive(Debug, Clone)]
struct FinalizeValidated {
    request: FinalizeEventArchiveRequest,
    folder_class: FolderClass,
    clip_cameras: HashMap<String, HashSet<String>>,
}

#[derive(Debug)]
struct ExistingArchiveItem {
    id: i64,
    path: String,
    file_count: i64,
    size_bytes: i64,
    delete_state: String,
    manifest_digest: Option<String>,
    verified_pass_id: Option<String>,
    source_generation: Option<String>,
    source_event_key: Option<String>,
    source_volume_id: Option<String>,
    segment_set_digest: Option<String>,
    metadata_digest: Option<String>,
}

fn handle_finalize_event_archive(
    conn: &Arc<Mutex<Connection>>,
    boot: &Arc<BootContext>,
    payload: &FinalizeEventArchiveRequest,
) -> Result<FinalizeEventArchiveResponse, HandlerError> {
    let validated = validate_finalize_event_archive(payload).map_err(HandlerError::Rejected)?;
    let mut locked = conn
        .lock()
        .map_err(|_| HandlerError::Internal("index database mutex is poisoned".to_owned()))?;
    finalize_event_archive_tx(&mut locked, boot, &validated)
}

fn finalize_event_archive_tx(
    conn: &mut Connection,
    boot: &Arc<BootContext>,
    validated: &FinalizeValidated,
) -> Result<FinalizeEventArchiveResponse, HandlerError> {
    let tx = conn
        .transaction()
        .map_err(|e| HandlerError::Internal(e.to_string()))?;
    let existing = lookup_existing_archive_item(
        &tx,
        &validated.request.source_event_key,
        validated.request.source_volume_id.as_deref(),
    )?;
    let result = match existing {
        None => finalize_insert_new(&tx, validated)?,
        Some(existing_row) => finalize_existing(&tx, boot, validated, &existing_row)?,
    };
    tx.commit().map_err(map_finalize_sqlite_error)?;
    Ok(result)
}

fn finalize_insert_new(
    tx: &rusqlite::Transaction<'_>,
    validated: &FinalizeValidated,
) -> Result<FinalizeEventArchiveResponse, HandlerError> {
    if validated.request.expected_prior_manifest_digest.is_some() {
        return Err(HandlerError::Rejected(FINALIZE_CAS_STALE_MESSAGE.to_owned()));
    }

    let now = now_epoch_s();
    let clip_map = upsert_finalize_clips_and_angles(tx, validated)?;
    let clip_id = clip_map.values().min().copied();
    let metadata_digest =
        compute_metadata_digest(&validated.request).map_err(HandlerError::Internal)?;
    let archive_item_id: i64 = tx
        .query_row(
            "INSERT INTO archive_items
                (folder_class, path, clip_id, size_bytes, file_count, archived_at, delete_state, durable,
                 manifest_digest, verified_pass_id, source_generation, source_event_key, source_volume_id,
                 segment_set_digest, metadata_digest, created_at, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'LIVE', 0, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?14)
             RETURNING id",
            params![
                validated.folder_class.as_db_str(),
                validated.request.generation_dir_path,
                clip_id,
                validated.request.size_bytes,
                validated.request.file_count,
                validated.request.archived_at,
                validated.request.manifest_digest,
                validated.request.pass_id,
                validated.request.source_generation,
                validated.request.source_event_key,
                validated.request.source_volume_id,
                validated.request.segment_set_digest,
                metadata_digest,
                now,
            ],
            |row| row.get(0),
        )
        .map_err(map_finalize_sqlite_error)?;
    replace_archive_item_links(tx, archive_item_id, &clip_map, &validated.clip_cameras)
        .map_err(map_finalize_sqlite_error)?;
    Ok(FinalizeEventArchiveResponse {
        archive_item_id,
        already_finalized: false,
    })
}

fn finalize_existing(
    tx: &rusqlite::Transaction<'_>,
    boot: &Arc<BootContext>,
    validated: &FinalizeValidated,
    existing: &ExistingArchiveItem,
) -> Result<FinalizeEventArchiveResponse, HandlerError> {
    let request = &validated.request;
    if existing.manifest_digest.as_deref() == Some(request.manifest_digest.as_str()) {
        if is_exact_finalize_replay(tx, request, existing)? {
            return Ok(FinalizeEventArchiveResponse {
                archive_item_id: existing.id,
                already_finalized: true,
            });
        }
        return Err(HandlerError::Rejected(
            FINALIZE_CONFLICT_SAME_DIGEST_MESSAGE.to_owned(),
        ));
    }

    let expected_prior = request.expected_prior_manifest_digest.as_deref();
    if expected_prior != existing.manifest_digest.as_deref() {
        return Err(HandlerError::Rejected(FINALIZE_CAS_STALE_MESSAGE.to_owned()));
    }
    if existing.delete_state != "LIVE" {
        return Err(HandlerError::Rejected(
            "finalize rejected: archive item is not LIVE".to_owned(),
        ));
    }
    let lease_active = has_unexpired_lease(tx, boot.boot_id(), boot.mono_now_ms(), existing.id)
        .map_err(|e| HandlerError::Internal(e.to_string()))?;
    if lease_active {
        return Err(HandlerError::Rejected(
            "finalize rejected: active upload lease".to_owned(),
        ));
    }
    let clip_map = upsert_finalize_clips_and_angles(tx, validated)?;
    supersede_current_parent_upload_set(tx, existing.id, request.archived_at)?;
    replace_archive_item_links(tx, existing.id, &clip_map, &validated.clip_cameras)
        .map_err(map_finalize_sqlite_error)?;

    let now = now_epoch_s();
    let clip_id = clip_map.values().min().copied();
    let metadata_digest = compute_metadata_digest(request).map_err(HandlerError::Internal)?;
    tx.execute(
        "UPDATE archive_items
            SET path = ?2,
                clip_id = ?3,
                folder_class = ?4,
                file_count = ?5,
                size_bytes = ?6,
                archived_at = ?7,
                durable = 0,
                manifest_digest = ?8,
                verified_pass_id = ?9,
                source_generation = ?10,
                source_event_key = ?11,
                source_volume_id = ?12,
                segment_set_digest = ?13,
                metadata_digest = ?15,
                updated_at = ?14
          WHERE id = ?1",
        params![
            existing.id,
            request.generation_dir_path,
            clip_id,
            validated.folder_class.as_db_str(),
            request.file_count,
            request.size_bytes,
            request.archived_at,
            request.manifest_digest,
            request.pass_id,
            request.source_generation,
            request.source_event_key,
            request.source_volume_id,
            request.segment_set_digest,
            now,
            metadata_digest,
        ],
    )
    .map_err(map_finalize_sqlite_error)?;

    Ok(FinalizeEventArchiveResponse {
        archive_item_id: existing.id,
        already_finalized: false,
    })
}

fn lookup_existing_archive_item(
    conn: &Connection,
    source_event_key: &str,
    source_volume_id: Option<&str>,
) -> Result<Option<ExistingArchiveItem>, HandlerError> {
    conn.query_row(
        "SELECT id, path, file_count, size_bytes, delete_state, manifest_digest,
                verified_pass_id, source_generation, source_event_key, source_volume_id, segment_set_digest,
                metadata_digest
           FROM archive_items
          WHERE source_event_key = ?1
            AND ((source_volume_id = ?2) OR (source_volume_id IS NULL AND ?2 IS NULL))
          LIMIT 1",
        params![source_event_key, source_volume_id],
        |row| {
            Ok(ExistingArchiveItem {
                id: row.get(0)?,
                path: row.get(1)?,
                file_count: row.get(2)?,
                size_bytes: row.get(3)?,
                delete_state: row.get(4)?,
                manifest_digest: row.get(5)?,
                verified_pass_id: row.get(6)?,
                source_generation: row.get(7)?,
                source_event_key: row.get(8)?,
                source_volume_id: row.get(9)?,
                segment_set_digest: row.get(10)?,
                metadata_digest: row.get(11)?,
            })
        },
    )
    .optional()
    .map_err(|e| HandlerError::Internal(e.to_string()))
}

fn is_exact_finalize_replay(
    tx: &rusqlite::Transaction<'_>,
    request: &FinalizeEventArchiveRequest,
    existing: &ExistingArchiveItem,
) -> Result<bool, HandlerError> {
    let metadata_digest = compute_metadata_digest(request).map_err(HandlerError::Internal)?;
    if existing.path != request.generation_dir_path
        || existing.file_count != request.file_count
        || existing.size_bytes != request.size_bytes
        || existing.source_generation.as_deref() != Some(request.source_generation.as_str())
        || existing.source_event_key.as_deref() != Some(request.source_event_key.as_str())
        || existing.source_volume_id != request.source_volume_id
        || existing.segment_set_digest.as_deref() != Some(request.segment_set_digest.as_str())
        || existing.verified_pass_id.as_deref() != Some(request.pass_id.as_str())
        || existing.metadata_digest.as_deref() != Some(metadata_digest.as_str())
    {
        return Ok(false);
    }
    let linked = linked_clip_keys(tx, existing.id).map_err(map_finalize_sqlite_error)?;
    let mut expected: Vec<String> = request.clips.iter().map(|clip| clip.canonical_key.clone()).collect();
    expected.sort();
    expected.dedup();
    Ok(linked == expected)
}

fn linked_clip_keys(
    conn: &Connection,
    archive_item_id: i64,
) -> Result<Vec<String>, rusqlite::Error> {
    let mut stmt = conn.prepare(
        "SELECT c.canonical_key
           FROM archive_item_clips aic
           JOIN clips c ON c.id = aic.clip_id
          WHERE aic.archive_item_id = ?1
          ORDER BY c.canonical_key",
    )?;
    let rows = stmt.query_map(params![archive_item_id], |row| row.get::<_, String>(0))?;
    rows.collect()
}

fn supersede_current_parent_upload_set(
    conn: &Connection,
    archive_item_id: i64,
    archived_at: i64,
) -> Result<(), HandlerError> {
    let mut stmt = conn
        .prepare(
            "SELECT upload_set_id
               FROM cloud_parent_upload_sets
              WHERE archive_item_id = ?1
                AND superseded_at IS NULL",
        )
        .map_err(map_finalize_sqlite_error)?;
    let rows = stmt
        .query_map(params![archive_item_id], |row| row.get::<_, String>(0))
        .map_err(map_finalize_sqlite_error)?;
    let mut superseded_set_ids = Vec::new();
    for row in rows {
        superseded_set_ids.push(row.map_err(map_finalize_sqlite_error)?);
    }
    drop(stmt);

    let superseded_at = archived_at.max(now_epoch_s());
    conn.execute(
        "UPDATE cloud_parent_upload_sets
            SET superseded_at = ?2
          WHERE archive_item_id = ?1
            AND superseded_at IS NULL",
        params![archive_item_id, superseded_at],
    )
    .map_err(map_finalize_sqlite_error)?;
    for superseded_set_id in superseded_set_ids {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET state = 'parked',
                    bytes_uploaded = 0,
                    attempts = 0,
                    not_before = NULL,
                    last_error = ?2
              WHERE upload_set_id = ?1
                AND state <> 'done'",
            params![superseded_set_id, FINALIZE_SUPERSEDE_PARK_MESSAGE],
        )
        .map_err(map_finalize_sqlite_error)?;
    }
    Ok(())
}

fn upsert_finalize_clips_and_angles(
    conn: &Connection,
    validated: &FinalizeValidated,
) -> Result<HashMap<String, i64>, HandlerError> {
    let mut clip_map = HashMap::with_capacity(validated.request.clips.len());
    for clip in &validated.request.clips {
        let clip_id = upsert_clip(
            conn,
            &ClipFacts {
                canonical_key: clip.canonical_key.clone(),
                started_at: clip.started_at,
                ended_at: Some(clip.ended_at),
                partition: clip.partition.clone(),
                folder_class: validated.folder_class,
                duration_s: None,
            },
        )
        .map_err(|e| HandlerError::Internal(e.to_string()))?;
        clip_map.insert(clip.canonical_key.clone(), clip_id);
    }
    for angle in &validated.request.angles {
        let Some(&clip_id) = clip_map.get(&angle.canonical_key) else {
            return Err(HandlerError::Rejected(format!(
                "angles reference unknown clip: {}",
                angle.canonical_key
            )));
        };
        upsert_angle_force_archive(
            conn,
            clip_id,
            &AngleFacts {
                camera: angle.camera.clone(),
                file_ref: angle.file_ref.clone(),
                view_kind: "archive".to_owned(),
                offset_ms: angle.offset_ms,
                duration_s: seconds_opt_to_f64(angle.duration_s, "angles.duration_s")
                    .map_err(HandlerError::Rejected)?,
                size_bytes: Some(angle.size_bytes),
            },
        )
        .map_err(|e| HandlerError::Internal(e.to_string()))?;
    }
    Ok(clip_map)
}

fn replace_archive_item_links(
    conn: &Connection,
    archive_item_id: i64,
    clip_map: &HashMap<String, i64>,
    clip_cameras: &HashMap<String, HashSet<String>>,
) -> Result<(), rusqlite::Error> {
    let old_clip_ids = linked_clip_ids(conn, archive_item_id)?;
    conn.execute(
        "DELETE FROM archive_item_clips WHERE archive_item_id = ?1",
        params![archive_item_id],
    )?;
    for clip_id in clip_map.values() {
        conn.execute(
            "INSERT OR IGNORE INTO archive_item_clips (archive_item_id, clip_id) VALUES (?1, ?2)",
            params![archive_item_id, clip_id],
        )?;
    }
    let new_clip_ids: HashSet<i64> = clip_map.values().copied().collect();
    for old_clip_id in old_clip_ids {
        if !new_clip_ids.contains(&old_clip_id) {
            conn.execute(
                "DELETE FROM angles
                  WHERE clip_id = ?1
                    AND view_kind = 'archive'
                    AND NOT EXISTS (
                        SELECT 1 FROM archive_item_clips WHERE clip_id = ?1
                    )",
                params![old_clip_id],
            )?;
        }
    }
    for (canonical_key, cameras) in clip_cameras {
        let Some(&clip_id) = clip_map.get(canonical_key) else {
            continue;
        };
        prune_archive_cameras(conn, clip_id, cameras)?;
    }
    Ok(())
}

fn linked_clip_ids(conn: &Connection, archive_item_id: i64) -> Result<HashSet<i64>, rusqlite::Error> {
    let mut stmt = conn.prepare(
        "SELECT clip_id FROM archive_item_clips WHERE archive_item_id = ?1",
    )?;
    let rows = stmt.query_map(params![archive_item_id], |row| row.get::<_, i64>(0))?;
    rows.collect()
}

fn prune_archive_cameras(
    conn: &Connection,
    clip_id: i64,
    desired_cameras: &HashSet<String>,
) -> Result<(), rusqlite::Error> {
    let mut stmt =
        conn.prepare("SELECT camera FROM angles WHERE clip_id = ?1 AND view_kind = 'archive'")?;
    let rows = stmt.query_map(params![clip_id], |row| row.get::<_, String>(0))?;
    for camera in rows {
        let camera = camera?;
        if !desired_cameras.contains(camera.as_str()) {
            conn.execute(
                "DELETE FROM angles WHERE clip_id = ?1 AND camera = ?2 AND view_kind = 'archive'",
                params![clip_id, camera],
            )?;
        }
    }
    Ok(())
}

fn map_finalize_sqlite_error(error: rusqlite::Error) -> HandlerError {
    match error {
        rusqlite::Error::SqliteFailure(_, Some(message))
            if message.contains("conflicting source event identity")
                || message.contains("UNIQUE constraint failed")
                || message.contains("CHECK constraint failed")
                || message.contains("durable requires finalized complete upload set") =>
        {
            HandlerError::Rejected(message)
        }
        other => HandlerError::Internal(other.to_string()),
    }
}

#[allow(clippy::cognitive_complexity, clippy::too_many_lines)]
fn validate_finalize_event_archive(payload: &FinalizeEventArchiveRequest) -> Result<FinalizeValidated, String> {
    if estimate_finalize_payload_bytes(payload) > MAX_REQUEST_FRAME as usize {
        return Err(FINALIZE_EVENT_TOO_LARGE_MESSAGE.to_owned());
    }
    let segment_count = i64::try_from(payload.segments.len())
        .map_err(|_| "too many segments".to_owned())?;
    if segment_count != payload.expected_segment_count {
        return Err("expected_segment_count does not match segments".to_owned());
    }
    validate_rel_path(&payload.generation_dir_path, "generation_dir_path")?;
    validate_event_key(&payload.source_event_key, "source_event_key")?;
    if payload.source_generation.is_empty() {
        return Err("source_generation must be non-empty".to_owned());
    }
    if payload.archived_at < 0 {
        return Err("archived_at must be >= 0".to_owned());
    }
    if payload.file_count < 0 {
        return Err("file_count must be >= 0".to_owned());
    }
    if payload.size_bytes < 0 {
        return Err("size_bytes must be >= 0".to_owned());
    }
    if !is_lower_hex(&payload.pass_id, 32) {
        return Err("pass_id must be 32 lowercase hex".to_owned());
    }
    if !is_lower_hex(&payload.manifest_digest, 32) {
        return Err("manifest_digest must be 32 lowercase hex".to_owned());
    }
    if !is_lower_hex(&payload.segment_set_digest, 64) {
        return Err("segment_set_digest must be 64 lowercase hex".to_owned());
    }
    if let Some(expected_prior) = &payload.expected_prior_manifest_digest {
        if !is_lower_hex(expected_prior, 32) {
            return Err("expected_prior_manifest_digest must be 32 lowercase hex".to_owned());
        }
    }
    let folder_class = parse_folder_class(&payload.folder_class)?;

    let mut segment_keys = HashSet::with_capacity(payload.segments.len());
    let mut segment_bytes_sum = 0_i64;
    for segment in &payload.segments {
        validate_prefixed_event_key(&segment.segment_key, &payload.source_event_key, "segments.segment_key")?;
        if segment.size_bytes < 0 {
            return Err("segments.size_bytes must be >= 0".to_owned());
        }
        if segment.mtime_ms < 0 {
            return Err("segments.mtime_ms must be >= 0".to_owned());
        }
        if !is_lower_hex(&segment.content_sha256, 64) {
            return Err("segments.content_sha256 must be 64 lowercase hex".to_owned());
        }
        if !segment_keys.insert(segment.segment_key.as_str()) {
            return Err(format!("duplicate segment_key: {}", segment.segment_key));
        }
        segment_bytes_sum = segment_bytes_sum
            .checked_add(segment.size_bytes)
            .ok_or_else(|| "segments size sum overflow".to_owned())?;
    }
    if payload.file_count < segment_count {
        return Err("file_count must be >= segments.len()".to_owned());
    }
    if payload.size_bytes < segment_bytes_sum {
        return Err("size_bytes must be >= total segment bytes".to_owned());
    }

    let mut clip_keys = HashSet::with_capacity(payload.clips.len());
    for clip in &payload.clips {
        validate_prefixed_event_key(&clip.canonical_key, &payload.source_event_key, "clips.canonical_key")?;
        if clip.partition != payload.partition {
            return Err("clips.partition must match request partition".to_owned());
        }
        if clip.folder_class != payload.folder_class {
            return Err("clips.folder_class must match request folder_class".to_owned());
        }
        if clip.ended_at < clip.started_at {
            return Err("clips.ended_at must be >= started_at".to_owned());
        }
        if !clip_keys.insert(clip.canonical_key.as_str()) {
            return Err(format!("duplicate clip canonical_key: {}", clip.canonical_key));
        }
    }

    let mut clip_cameras: HashMap<String, HashSet<String>> = HashMap::new();
    for angle in &payload.angles {
        validate_prefixed_event_key(&angle.canonical_key, &payload.source_event_key, "angles.canonical_key")?;
        validate_rel_path(&angle.file_ref, "angles.file_ref")?;
        if !clip_keys.contains(angle.canonical_key.as_str()) {
            return Err(format!(
                "angles reference unknown clip: {}",
                angle.canonical_key
            ));
        }
        if !is_allowed_camera(&angle.camera) {
            return Err(format!("invalid camera: {}", angle.camera));
        }
        if angle.offset_ms < 0 {
            return Err("angles.offset_ms must be >= 0".to_owned());
        }
        if angle.size_bytes < 0 {
            return Err("angles.size_bytes must be >= 0".to_owned());
        }
        if let Some(duration_s) = angle.duration_s {
            if duration_s < 0 {
                return Err("angles.duration_s must be >= 0".to_owned());
            }
        }
        let cameras = clip_cameras.entry(angle.canonical_key.clone()).or_default();
        if !cameras.insert(angle.camera.clone()) {
            return Err(format!(
                "duplicate camera for clip {}: {}",
                angle.canonical_key, angle.camera
            ));
        }
    }

    let recomputed = compute_segment_set_digest(&payload.segments)?;
    if recomputed != payload.segment_set_digest {
        return Err("segment_set_digest mismatch".to_owned());
    }

    Ok(FinalizeValidated {
        request: payload.clone(),
        folder_class,
        clip_cameras,
    })
}

fn validate_event_key(key: &str, field: &str) -> Result<(), String> {
    if key.is_empty() {
        return Err(format!("{field} must be non-empty"));
    }
    if key.starts_with('/') || key.starts_with('\\') {
        return Err(format!("{field} must be relative"));
    }
    if key
        .split(['/', '\\'])
        .any(|segment| segment.is_empty() || segment == "." || segment == "..")
    {
        return Err(format!("{field} must not contain empty/dot path segments"));
    }
    Ok(())
}

fn validate_prefixed_event_key(key: &str, source_event_key: &str, field: &str) -> Result<(), String> {
    validate_event_key(key, field)?;
    if !has_event_prefix(key, source_event_key) {
        return Err(format!("{field} must use source_event_key prefix"));
    }
    Ok(())
}

fn has_event_prefix(value: &str, prefix: &str) -> bool {
    if value == prefix {
        return true;
    }
    value
        .strip_prefix(prefix)
        .is_some_and(|suffix| suffix.starts_with('/') || suffix.starts_with('\\'))
}

/// Canonical segment-set digest for `finalize_event_archive`.
///
/// The hash is `sha256` over a domain-separation tag (`teslausb.segment_set.v1\0`)
/// followed by each segment sorted by `segment_key` byte order, encoding every field as:
/// `u32_le(len(bytes)) || bytes` for strings, and `i64_le` for numeric values.
/// Output is a 64-char lowercase hex digest.
fn compute_segment_set_digest(
    segments: &[crate::proto::FinalizeEventArchiveSegmentWire],
) -> Result<String, String> {
    let mut sorted = segments.to_vec();
    sorted.sort_by(|left, right| left.segment_key.cmp(&right.segment_key));
    let mut hasher = Sha256::new();
    hasher.update(SEGMENT_SET_DIGEST_DOMAIN_TAG);
    for segment in &sorted {
        hash_len_prefixed(&mut hasher, segment.segment_key.as_bytes())?;
        hasher.update(segment.size_bytes.to_le_bytes());
        hasher.update(segment.mtime_ms.to_le_bytes());
        hash_len_prefixed(&mut hasher, segment.content_sha256.as_bytes())?;
    }
    Ok(format!("{:x}", hasher.finalize()))
}

/// Server-computed digest over the finalize request's derived clip/angle metadata
/// that neither `manifest_digest` nor `segment_set_digest` covers. Persisted on the
/// archive item at finalize time so an idempotent replay can prove the request's
/// semantic payload is unchanged without re-reading the shared (mutable) clip/angle
/// rows. Per-clip `folder_class`/`partition` are omitted because validation forces
/// them equal to the top-level values.
fn compute_metadata_digest(request: &FinalizeEventArchiveRequest) -> Result<String, String> {
    let mut hasher = Sha256::new();
    hasher.update(FINALIZE_METADATA_DIGEST_DOMAIN_TAG);
    hash_len_prefixed(&mut hasher, request.folder_class.as_bytes())?;
    hash_len_prefixed(&mut hasher, request.partition.as_bytes())?;

    let mut clips = request.clips.clone();
    clips.sort_by(|left, right| left.canonical_key.cmp(&right.canonical_key));
    let clip_count: u32 = clips
        .len()
        .try_into()
        .map_err(|_| "too many clips".to_owned())?;
    hasher.update(clip_count.to_le_bytes());
    for clip in &clips {
        hash_len_prefixed(&mut hasher, clip.canonical_key.as_bytes())?;
        hasher.update(clip.started_at.to_le_bytes());
        hasher.update(clip.ended_at.to_le_bytes());
    }

    let mut angles = request.angles.clone();
    angles.sort_by(|left, right| {
        left.canonical_key
            .cmp(&right.canonical_key)
            .then_with(|| left.camera.cmp(&right.camera))
    });
    let angle_count: u32 = angles
        .len()
        .try_into()
        .map_err(|_| "too many angles".to_owned())?;
    hasher.update(angle_count.to_le_bytes());
    for angle in &angles {
        hash_len_prefixed(&mut hasher, angle.canonical_key.as_bytes())?;
        hash_len_prefixed(&mut hasher, angle.camera.as_bytes())?;
        hash_len_prefixed(&mut hasher, angle.file_ref.as_bytes())?;
        hasher.update(angle.offset_ms.to_le_bytes());
        match angle.duration_s {
            None => hasher.update([0_u8]),
            Some(duration_s) => {
                hasher.update([1_u8]);
                hasher.update(duration_s.to_le_bytes());
            }
        }
        hasher.update(angle.size_bytes.to_le_bytes());
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn hash_len_prefixed(hasher: &mut Sha256, bytes: &[u8]) -> Result<(), String> {
    let length: u32 = bytes
        .len()
        .try_into()
        .map_err(|_| "segment field too large".to_owned())?;
    hasher.update(length.to_le_bytes());
    hasher.update(bytes);
    Ok(())
}

fn is_lower_hex(value: &str, expected_len: usize) -> bool {
    value.len() == expected_len
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()))
}

fn estimate_finalize_payload_bytes(payload: &FinalizeEventArchiveRequest) -> usize {
    let mut total = payload.pass_id.len()
        + payload.source_event_key.len()
        + payload.source_generation.len()
        + payload.manifest_digest.len()
        + payload.segment_set_digest.len()
        + payload.generation_dir_path.len()
        + payload.folder_class.len()
        + payload.partition.len()
        + payload.source_volume_id.as_ref().map_or(0, String::len)
        + payload
            .expected_prior_manifest_digest
            .as_ref()
            .map_or(0, String::len);
    total = total
        .saturating_add(payload.segments.iter().map(|segment| {
            segment.segment_key.len() + segment.content_sha256.len() + 32
        }).sum::<usize>())
        .saturating_add(payload.clips.iter().map(|clip| {
            clip.canonical_key.len() + clip.folder_class.len() + clip.partition.len() + 24
        }).sum::<usize>())
        .saturating_add(payload.angles.iter().map(|angle| {
            angle.canonical_key.len() + angle.camera.len() + angle.file_ref.len() + 24
        }).sum::<usize>());
    total
}

fn build_registration(payload: &RegisterArchivedClip) -> Result<ArchiveRegistration, String> {
    validate_payload(payload)?;
    let folder_class = parse_folder_class(&payload.folder_class)?;
    Ok(ArchiveRegistration {
        canonical_key: payload.canonical_key.clone(),
        folder_class,
        partition: payload.partition.clone(),
        started_at: payload.started_at,
        ended_at: payload.ended_at,
        duration_s: seconds_opt_to_f64(payload.duration_s, "duration_s")?,
        archive: ArchiveUnitRegistration {
            path: payload.archive.path.clone(),
            size_bytes: payload.archive.size_bytes,
            file_count: payload.archive.file_count,
            archived_at: payload.archive.archived_at,
        },
        angles: payload
            .angles
            .iter()
            .map(|a| {
                Ok(ArchiveAngleRegistration {
                    camera: a.camera.clone(),
                    file_ref: a.file_ref.clone(),
                    offset_ms: a.offset_ms,
                    duration_s: seconds_opt_to_f64(a.duration_s, "angles.duration_s")?,
                    size_bytes: a.size_bytes,
                })
            })
            .collect::<Result<Vec<_>, String>>()?,
    })
}

fn parse_folder_class(raw: &str) -> Result<FolderClass, String> {
    match raw {
        "RecentClips" => Ok(FolderClass::RecentClips),
        "SavedClips" => Ok(FolderClass::SavedClips),
        "SentryClips" => Ok(FolderClass::SentryClips),
        "TeslaTrackMode" => Ok(FolderClass::TeslaTrackMode),
        other => Err(format!("invalid folder_class: {other}")),
    }
}

fn validate_payload(payload: &RegisterArchivedClip) -> Result<(), String> {
    if payload.canonical_key.is_empty() {
        return Err("canonical_key must be non-empty".to_owned());
    }
    if payload.partition.is_empty() {
        return Err("partition must be non-empty".to_owned());
    }
    if let Some(duration_s) = payload.duration_s {
        if duration_s < 0 {
            return Err("duration_s must be >= 0".to_owned());
        }
    }
    validate_rel_path(&payload.archive.path, "archive.path")?;
    if payload.archive.size_bytes < 0 {
        return Err("archive.size_bytes must be >= 0".to_owned());
    }
    if payload.archive.file_count < 1 {
        return Err("archive.file_count must be >= 1".to_owned());
    }
    if payload.angles.is_empty() {
        return Err("register_archived_clip requires at least one angle".to_owned());
    }

    let mut seen_cameras: HashSet<&str> = HashSet::new();
    for angle in &payload.angles {
        if !is_allowed_camera(&angle.camera) {
            return Err(format!("invalid camera: {}", angle.camera));
        }
        if !seen_cameras.insert(angle.camera.as_str()) {
            return Err(format!("duplicate camera: {}", angle.camera));
        }
        validate_rel_path(&angle.file_ref, "angles.file_ref")?;
        if angle.offset_ms < 0 {
            return Err("angles.offset_ms must be >= 0".to_owned());
        }
        if angle.size_bytes < 0 {
            return Err("angles.size_bytes must be >= 0".to_owned());
        }
        if let Some(duration_s) = angle.duration_s {
            if duration_s < 0 {
                return Err("angles.duration_s must be >= 0".to_owned());
            }
        }
    }
    Ok(())
}

fn is_allowed_camera(camera: &str) -> bool {
    matches!(
        camera,
        "front" | "back" | "left_repeater" | "right_repeater" | "left_pillar" | "right_pillar"
    )
}

fn validate_rel_path(path: &str, field: &str) -> Result<(), String> {
    if path.is_empty() {
        return Err(format!("{field} must be non-empty"));
    }
    if path.starts_with('/') || path.starts_with('\\') {
        return Err(format!("{field} must be archive-root-relative"));
    }
    if path
        .split(['/', '\\'])
        .any(|segment| segment.is_empty() || segment == "." || segment == "..")
    {
        return Err(format!("{field} must not contain empty/dot path segments"));
    }
    Ok(())
}

fn seconds_opt_to_f64(value: Option<i64>, field: &str) -> Result<Option<f64>, String> {
    value
        .map(|seconds| seconds_to_f64(seconds, field))
        .transpose()
}

fn seconds_to_f64(value: i64, field: &str) -> Result<f64, String> {
    const MAX_EXACT_INT_IN_F64: u64 = 9_007_199_254_740_992;
    if value.unsigned_abs() > MAX_EXACT_INT_IN_F64 {
        return Err(format!("{field} exceeds exact f64 integer range"));
    }
    value
        .to_string()
        .parse::<f64>()
        .map_err(|e| format!("failed to convert {field}: {e}"))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::expect_used, clippy::unwrap_used)]

    use std::os::unix::net::UnixStream;
    use std::path::{Path, PathBuf};
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use rusqlite::{Connection, params};

    use super::{
        FINALIZE_CAS_STALE_MESSAGE, FINALIZE_CONFLICT_SAME_DIGEST_MESSAGE,
        FINALIZE_EVENT_TOO_LARGE_MESSAGE, FINALIZE_SUPERSEDE_PARK_MESSAGE,
        compute_segment_set_digest,
        handle_cloud_finalize_parent_upload, handle_cloud_prepare_parent_upload,
        handle_finalize_event_archive, parse_folder_class, spawn, validate_payload,
    };
    use crate::db::cloud::{CloudQueuePk, cloud_upload_commit, cloud_upload_fail};
    use crate::db::mutations::BootContext;
    use crate::db::open_in_memory;
    use crate::proto::{
        ArchiveAngle, ArchiveUnit, CloudConfigWire, CloudQueuePkWire,
        CloudFinalizeParentUploadRequest,
        CloudPrepareParentUploadChildWire, CloudPrepareParentUploadRequest,
        CloudQueueRetryResolutionWire, CloudQueueUpsertWire, FinalizeEventArchiveAngleWire,
        FinalizeEventArchiveClipWire, FinalizeEventArchiveRequest, FinalizeEventArchiveSegmentWire,
        MAX_REQUEST_FRAME, RegisterArchivedClip, Request, Response, read_frame, write_frame,
    };
    use teslausb_core::manifest_digest::{ManifestDigestEntry, manifest_digest_v1_hex};

    fn payload() -> RegisterArchivedClip {
        RegisterArchivedClip {
            canonical_key: "slot0:TeslaCam/RecentClips/2026-06-19/clip-a".to_owned(),
            folder_class: "RecentClips".to_owned(),
            partition: "slot0".to_owned(),
            started_at: 1_718_805_600,
            ended_at: 1_718_805_660,
            duration_s: Some(60),
            archive: ArchiveUnit {
                path: "archive/2026-06-19/clip-a".to_owned(),
                size_bytes: 4096,
                file_count: 4,
                archived_at: 1_718_805_700,
            },
            angles: vec![ArchiveAngle {
                camera: "front".to_owned(),
                file_ref: "archive/2026-06-19/clip-a/front.mp4".to_owned(),
                offset_ms: 0,
                duration_s: Some(60),
                size_bytes: 1024,
            }],
        }
    }

    fn finalize_payload() -> FinalizeEventArchiveRequest {
        let source_event_key = "slot0:TeslaCam/SentryClips/2026-06-19_10-00-00".to_owned();
        let segments = vec![
            FinalizeEventArchiveSegmentWire {
                segment_key: format!("{source_event_key}/2026-06-19_10-00-00-front.mp4"),
                size_bytes: 1_024,
                mtime_ms: 1_718_805_700_000,
                content_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    .to_owned(),
            },
            FinalizeEventArchiveSegmentWire {
                segment_key: format!("{source_event_key}/2026-06-19_10-01-00-front.mp4"),
                size_bytes: 2_048,
                mtime_ms: 1_718_805_760_000,
                content_sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                    .to_owned(),
            },
        ];
        let segment_set_digest =
            compute_segment_set_digest(&segments).expect("compute segment_set_digest");
        let clips = vec![
            FinalizeEventArchiveClipWire {
                canonical_key: format!("{source_event_key}/2026-06-19_10-00-00"),
                started_at: 1_718_805_600,
                ended_at: 1_718_805_660,
                folder_class: "SentryClips".to_owned(),
                partition: "slot0".to_owned(),
            },
            FinalizeEventArchiveClipWire {
                canonical_key: format!("{source_event_key}/2026-06-19_10-01-00"),
                started_at: 1_718_805_660,
                ended_at: 1_718_805_720,
                folder_class: "SentryClips".to_owned(),
                partition: "slot0".to_owned(),
            },
        ];
        let angles = vec![
            FinalizeEventArchiveAngleWire {
                canonical_key: clips[0].canonical_key.clone(),
                camera: "front".to_owned(),
                file_ref: "archive/events/e1/front-00.mp4".to_owned(),
                offset_ms: 0,
                duration_s: Some(60),
                size_bytes: 1_024,
            },
            FinalizeEventArchiveAngleWire {
                canonical_key: clips[1].canonical_key.clone(),
                camera: "front".to_owned(),
                file_ref: "archive/events/e1/front-01.mp4".to_owned(),
                offset_ms: 0,
                duration_s: Some(60),
                size_bytes: 2_048,
            },
        ];
        FinalizeEventArchiveRequest {
            pass_id: "11111111111111111111111111111111".to_owned(),
            source_event_key,
            source_volume_id: Some("vol-a".to_owned()),
            source_generation: "boot1:scan17".to_owned(),
            expected_prior_manifest_digest: None,
            manifest_digest: "22222222222222222222222222222222".to_owned(),
            segment_set_digest,
            expected_segment_count: segments.len() as i64,
            size_bytes: 4_096,
            file_count: 4,
            archived_at: 1_718_805_780,
            generation_dir_path: "archive/events/e1-gen1".to_owned(),
            folder_class: "SentryClips".to_owned(),
            partition: "slot0".to_owned(),
            segments,
            clips,
            angles,
        }
    }

        fn finalize_payload_generation_two() -> FinalizeEventArchiveRequest {
            let mut request = finalize_payload();
            request.pass_id = "33333333333333333333333333333333".to_owned();
            request.expected_prior_manifest_digest = Some(request.manifest_digest.clone());
            request.manifest_digest = "44444444444444444444444444444444".to_owned();
            request.source_generation = "boot1:scan18".to_owned();
            request.generation_dir_path = "archive/events/e1-gen2".to_owned();
            request.segments = vec![FinalizeEventArchiveSegmentWire {
                segment_key: format!("{}/2026-06-19_10-02-00-front.mp4", request.source_event_key),
                size_bytes: 3_072,
                mtime_ms: 1_718_805_820_000,
                content_sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
                    .to_owned(),
            }];
            request.clips = vec![FinalizeEventArchiveClipWire {
                canonical_key: format!("{}/2026-06-19_10-02-00", request.source_event_key),
                started_at: 1_718_805_720,
                ended_at: 1_718_805_780,
                folder_class: "SentryClips".to_owned(),
                partition: "slot0".to_owned(),
            }];
            request.angles = vec![FinalizeEventArchiveAngleWire {
                canonical_key: request.clips[0].canonical_key.clone(),
                camera: "front".to_owned(),
                file_ref: "archive/events/e2/front-02.mp4".to_owned(),
                offset_ms: 0,
                duration_s: Some(60),
                size_bytes: 3_072,
            }];
            request.segment_set_digest =
                compute_segment_set_digest(&request.segments).expect("compute segment_set_digest");
            request.expected_segment_count = 1;
            request.size_bytes = 5_000;
            request.file_count = 3;
            request
        }

        fn call_finalize(
            conn: &Arc<Mutex<Connection>>,
            boot: &Arc<BootContext>,
            request: &FinalizeEventArchiveRequest,
        ) -> Response {
            match handle_finalize_event_archive(conn, boot, request) {
                Ok(response) => Response::FinalizeEventArchive(response),
                Err(super::HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(super::HandlerError::Internal(message)) => Response::Error { message },
            }
        }

        macro_rules! prepare_child {
            (
                $child_key:expr,
                $remote_key:expr,
                $seq:expr,
                $total_bytes:expr,
                $manifest_mtime_ms:expr,
                $content_sha256:expr,
                $expected_hash:expr,
                $verify_alg:expr
                $(,)?
            ) => {
                CloudPrepareParentUploadChildWire {
                    child_key: $child_key.to_owned(),
                    destination_id: "dest".to_owned(),
                    remote_key: $remote_key.to_owned(),
                    category: "bulk".to_owned(),
                    seq: $seq,
                    total_bytes: $total_bytes,
                    manifest_mtime_ms: $manifest_mtime_ms,
                    content_sha256: $content_sha256.to_owned(),
                    expected_hash: $expected_hash.to_owned(),
                    verify_alg: $verify_alg.to_owned(),
                }
            };
        }

        fn manifest_digest_for_prepare_children(children: &[CloudPrepareParentUploadChildWire]) -> String {
            let entries: Vec<ManifestDigestEntry<'_>> = children
                .iter()
                .map(|child| ManifestDigestEntry {
                    rel_name: child.child_key.as_str(),
                    size: u64::try_from(child.total_bytes).expect("child bytes must be non-negative"),
                    mtime_ms: child.manifest_mtime_ms,
                    hash: match super::decode_lower_hex_sha256(&child.content_sha256) {
                        Ok(value) => value,
                        Err(_) => panic!("decode child sha256"),
                    },
                })
                .collect();
            manifest_digest_v1_hex(&entries)
        }

        fn insert_prepare_parent(
            conn: &Connection,
            path: &str,
            delete_state: &str,
            durable: i64,
            manifest_digest: Option<&str>,
        ) -> i64 {
            conn.execute(
                "INSERT INTO archive_items
                    (folder_class, path, size_bytes, file_count, archived_at, durable, delete_state, manifest_digest, created_at, updated_at)
                 VALUES ('RecentClips', ?1, 4_096, 2, 100, ?2, ?3, ?4, 0, 0)",
                params![path, durable, delete_state, manifest_digest],
            )
            .expect("insert prepare parent");
            conn.last_insert_rowid()
        }

        fn call_prepare(
            conn: &Arc<Mutex<Connection>>,
            request: &CloudPrepareParentUploadRequest,
        ) -> Response {
            match handle_cloud_prepare_parent_upload(conn, request) {
                Ok(response) => Response::CloudPrepareParentUpload(response),
                Err(super::HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(super::HandlerError::Internal(message)) => Response::Error { message },
            }
        }

        fn call_finalize_parent(
            conn: &Arc<Mutex<Connection>>,
            request: &CloudFinalizeParentUploadRequest,
        ) -> Response {
            match handle_cloud_finalize_parent_upload(conn, request) {
                Ok(response) => Response::CloudFinalizeParentUpload(response),
                Err(super::HandlerError::Rejected(message)) => Response::Rejected { message },
                Err(super::HandlerError::Internal(message)) => Response::Error { message },
            }
        }

        fn prepare_two_child_upload_set(
            conn: &Arc<Mutex<Connection>>,
            path: &str,
        ) -> (i64, String, String) {
            let children = vec![
                prepare_child!(
                    "front.mp4",
                    "rk/front",
                    1,
                    123,
                    1_718_805_700_000,
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "etag-front",
                    "md5",
                ),
                prepare_child!(
                    "back.mp4",
                    "rk/back",
                    2,
                    456,
                    1_718_805_701_000,
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "etag-back",
                    "md5",
                ),
            ];
            let manifest_digest = manifest_digest_for_prepare_children(&children);
            let archive_item_id = {
                let locked = conn.lock().expect("lock db");
                insert_prepare_parent(&locked, path, "LIVE", 0, Some(manifest_digest.as_str()))
            };
            let request = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: manifest_digest.clone(),
                children,
            };
            let Response::CloudPrepareParentUpload(result) = call_prepare(conn, &request) else {
                panic!("expected prepare response");
            };
            (archive_item_id, manifest_digest, result.upload_set_id)
        }

        #[test]
        fn prepare_parent_upload_happy_path_seals_set_children_and_queue() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let children = vec![
                prepare_child!(
                    "front.mp4",
                    "rk/front",
                    1,
                    123,
                    1_718_805_700_000,
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "etag-front",
                    "md5",
                ),
                prepare_child!(
                    "back.mp4",
                    "rk/back",
                    2,
                    456,
                    1_718_805_701_000,
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "etag-back",
                    "md5",
                ),
            ];
            let manifest_digest = manifest_digest_for_prepare_children(&children);
            let archive_item_id = {
                let locked = conn.lock().expect("lock db");
                insert_prepare_parent(
                    &locked,
                    "archive/events/prepare-happy",
                    "LIVE",
                    0,
                    Some(manifest_digest.as_str()),
                )
            };
            let request = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: manifest_digest.clone(),
                children: children.clone(),
            };

            let response = call_prepare(&conn, &request);
            let Response::CloudPrepareParentUpload(result) = response else {
                panic!("expected prepare response");
            };
            assert!(!result.already_prepared);
            assert!(super::is_lower_hex(&result.upload_set_id, 32));

            let locked = conn.lock().expect("lock db");
            let row: (String, i64, Option<i64>) = locked
                .query_row(
                    "SELECT request_digest, expected_child_count, superseded_at
                       FROM cloud_parent_upload_sets
                      WHERE upload_set_id = ?1",
                    params![result.upload_set_id],
                    |record| Ok((record.get(0)?, record.get(1)?, record.get(2)?)),
                )
                .expect("read upload set");
            assert!(super::is_lower_hex(&row.0, 64));
            assert_eq!(row.1, i64::try_from(children.len()).expect("children count"));
            assert_eq!(row.2, None);

            let child_count: i64 = locked
                .query_row(
                    "SELECT COUNT(*) FROM cloud_parent_upload_set_children WHERE upload_set_id = ?1",
                    params![result.upload_set_id],
                    |record| record.get(0),
                )
                .expect("count set children");
            assert_eq!(child_count, i64::try_from(children.len()).expect("children count"));

            let queue_count: i64 = locked
                .query_row(
                    "SELECT COUNT(*) FROM cloud_upload_queue WHERE upload_set_id = ?1",
                    params![result.upload_set_id],
                    |record| record.get(0),
                )
                .expect("count tagged queue rows");
            assert_eq!(queue_count, i64::try_from(children.len()).expect("children count"));
        }

        #[test]
        fn prepare_parent_upload_rejects_manifest_digest_omission_and_substitution() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let full_children = vec![
                prepare_child!(
                    "front.mp4",
                    "rk/front",
                    1,
                    123,
                    1_718_805_700_000,
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "etag-front",
                    "md5",
                ),
                prepare_child!(
                    "back.mp4",
                    "rk/back",
                    2,
                    456,
                    1_718_805_701_000,
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "etag-back",
                    "md5",
                ),
            ];
            let stored_digest = manifest_digest_for_prepare_children(&full_children);
            let archive_item_id = {
                let locked = conn.lock().expect("lock db");
                insert_prepare_parent(
                    &locked,
                    "archive/events/prepare-digest",
                    "LIVE",
                    0,
                    Some(stored_digest.as_str()),
                )
            };

            let omitted_request = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: stored_digest.clone(),
                children: vec![full_children[0].clone()],
            };
            assert!(matches!(
                call_prepare(&conn, &omitted_request),
                Response::Rejected { .. }
            ));

            let mut substituted_children = full_children.clone();
            substituted_children[1].content_sha256 =
                "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc".to_owned();
            let substituted_request = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: stored_digest,
                children: substituted_children,
            };
            assert!(matches!(
                call_prepare(&conn, &substituted_request),
                Response::Rejected { .. }
            ));

            let set_count: i64 = conn
                .lock()
                .expect("lock db")
                .query_row("SELECT COUNT(*) FROM cloud_parent_upload_sets", [], |row| row.get(0))
                .expect("count upload sets");
            assert_eq!(set_count, 0);
        }

        #[test]
        fn prepare_parent_upload_is_idempotent_replay() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let children = vec![
                prepare_child!(
                    "front.mp4",
                    "rk/front",
                    1,
                    123,
                    1_718_805_700_000,
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "etag-front",
                    "md5",
                ),
                prepare_child!(
                    "back.mp4",
                    "rk/back",
                    2,
                    456,
                    1_718_805_701_000,
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "etag-back",
                    "md5",
                ),
            ];
            let manifest_digest = manifest_digest_for_prepare_children(&children);
            let archive_item_id = {
                let locked = conn.lock().expect("lock db");
                insert_prepare_parent(
                    &locked,
                    "archive/events/prepare-idempotent",
                    "LIVE",
                    0,
                    Some(manifest_digest.as_str()),
                )
            };
            let request = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: manifest_digest,
                children,
            };
            let first = call_prepare(&conn, &request);
            let Response::CloudPrepareParentUpload(first_result) = first else {
                panic!("expected prepare response");
            };
            assert!(!first_result.already_prepared);

            let second = call_prepare(&conn, &request);
            let Response::CloudPrepareParentUpload(second_result) = second else {
                panic!("expected idempotent prepare response");
            };
            assert!(second_result.already_prepared);
            assert_eq!(second_result.upload_set_id, first_result.upload_set_id);

            let count: i64 = conn
                .lock()
                .expect("lock db")
                .query_row("SELECT COUNT(*) FROM cloud_parent_upload_sets", [], |row| row.get(0))
                .expect("count upload sets");
            assert_eq!(count, 1);
        }

        #[test]
        fn prepare_parent_upload_supersedes_prior_set_and_parks_unfinished_rows() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let children_a = vec![
                prepare_child!(
                    "front-a.mp4",
                    "rk/a/front",
                    1,
                    111,
                    1_718_805_700_000,
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "etag-a-front",
                    "md5",
                ),
                prepare_child!(
                    "back-a.mp4",
                    "rk/a/back",
                    2,
                    222,
                    1_718_805_701_000,
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "etag-a-back",
                    "md5",
                ),
            ];
            let digest_a = manifest_digest_for_prepare_children(&children_a);
            let archive_item_id = {
                let locked = conn.lock().expect("lock db");
                insert_prepare_parent(
                    &locked,
                    "archive/events/prepare-supersede",
                    "LIVE",
                    0,
                    Some(digest_a.as_str()),
                )
            };
            let prepare_a = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: digest_a.clone(),
                children: children_a.clone(),
            };
            let Response::CloudPrepareParentUpload(first_result) = call_prepare(&conn, &prepare_a) else {
                panic!("expected first prepare response");
            };

            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "UPDATE archive_items SET manifest_digest = ?2 WHERE id = ?1",
                        params![
                            archive_item_id,
                            manifest_digest_for_prepare_children(&[
                                prepare_child!(
                                    "front-b.mp4",
                                    "rk/b/front",
                                    1,
                                    333,
                                    1_718_805_702_000,
                                    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                                    "etag-b-front",
                                    "md5",
                                ),
                                prepare_child!(
                                    "back-b.mp4",
                                    "rk/b/back",
                                    2,
                                    444,
                                    1_718_805_703_000,
                                    "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                                    "etag-b-back",
                                    "md5",
                                ),
                            ])
                        ],
                    )
                    .expect("update parent digest");
            }

            let children_b = vec![
                prepare_child!(
                    "front-b.mp4",
                    "rk/b/front",
                    1,
                    333,
                    1_718_805_702_000,
                    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                    "etag-b-front",
                    "md5",
                ),
                prepare_child!(
                    "back-b.mp4",
                    "rk/b/back",
                    2,
                    444,
                    1_718_805_703_000,
                    "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "etag-b-back",
                    "md5",
                ),
            ];
            let digest_b = manifest_digest_for_prepare_children(&children_b);
            let prepare_b = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: digest_b,
                children: children_b,
            };
            let Response::CloudPrepareParentUpload(second_result) = call_prepare(&conn, &prepare_b) else {
                panic!("expected second prepare response");
            };
            assert_ne!(first_result.upload_set_id, second_result.upload_set_id);

            let mut locked = conn.lock().expect("lock db");
            let superseded_at: Option<i64> = locked
                .query_row(
                    "SELECT superseded_at
                       FROM cloud_parent_upload_sets
                      WHERE upload_set_id = ?1",
                    params![first_result.upload_set_id],
                    |row| row.get(0),
                )
                .expect("read superseded_at");
            assert!(superseded_at.is_some());
            let parked_rows: i64 = locked
                .query_row(
                    "SELECT COUNT(*)
                       FROM cloud_upload_queue
                      WHERE upload_set_id = ?1
                        AND state = 'parked'",
                    params![first_result.upload_set_id],
                    |row| row.get(0),
                )
                .expect("count parked rows");
            assert_eq!(parked_rows, 2);

            let commit = cloud_upload_commit(
                &mut locked,
                &CloudQueuePk {
                    destination_id: "dest".to_owned(),
                    remote_key: "rk/a/front".to_owned(),
                },
                "attempt-superseded",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "sha256",
                111,
                None,
            );
            assert!(commit.is_err());

            let current_set: String = locked
                .query_row(
                    "SELECT upload_set_id
                       FROM cloud_parent_upload_sets
                      WHERE archive_item_id = ?1
                        AND superseded_at IS NULL",
                    params![archive_item_id],
                    |row| row.get(0),
                )
                .expect("read current set");
            assert_eq!(current_set, second_result.upload_set_id);
        }

        #[test]
        fn cloud_prepare_retag_then_late_prior_set_commit_rejected() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let children_a = vec![prepare_child!(
                "front.mp4",
                "rk/shared",
                1,
                111,
                1_718_805_700_000,
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "11111111111111111111111111111111",
                "md5",
            )];
            let digest_a = manifest_digest_for_prepare_children(&children_a);
            let archive_item_id = {
                let locked = conn.lock().expect("lock db");
                insert_prepare_parent(
                    &locked,
                    "archive/events/prepare-retag",
                    "LIVE",
                    0,
                    Some(digest_a.as_str()),
                )
            };
            let prepare_a = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: digest_a,
                children: children_a,
            };
            let Response::CloudPrepareParentUpload(first_result) = call_prepare(&conn, &prepare_a) else {
                panic!("expected first prepare response");
            };

            let children_b = vec![prepare_child!(
                "front.mp4",
                "rk/shared",
                1,
                222,
                1_718_805_701_000,
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "22222222222222222222222222222222",
                "md5",
            )];
            let digest_b = manifest_digest_for_prepare_children(&children_b);
            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "UPDATE archive_items SET manifest_digest = ?2 WHERE id = ?1",
                        params![archive_item_id, digest_b],
                    )
                    .expect("update parent digest");
            }
            let prepare_b = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: manifest_digest_for_prepare_children(&children_b),
                children: children_b,
            };
            let Response::CloudPrepareParentUpload(second_result) = call_prepare(&conn, &prepare_b) else {
                panic!("expected second prepare response");
            };
            assert_ne!(first_result.upload_set_id, second_result.upload_set_id);

            let mut locked = conn.lock().expect("lock db");
            let queue_set_id: Option<String> = locked
                .query_row(
                    "SELECT upload_set_id
                       FROM cloud_upload_queue
                      WHERE destination_id = 'dest' AND remote_key = 'rk/shared'",
                    [],
                    |row| row.get(0),
                )
                .expect("read queue upload_set_id");
            assert_eq!(queue_set_id.as_deref(), Some(second_result.upload_set_id.as_str()));
            let attempts_before: i64 = locked
                .query_row("SELECT COUNT(*) FROM cloud_upload_attempts", [], |row| row.get(0))
                .expect("count attempts before");
            let history_before: i64 = locked
                .query_row("SELECT COUNT(*) FROM cloud_sync_history", [], |row| row.get(0))
                .expect("count history before");

            let late_commit = cloud_upload_commit(
                &mut locked,
                &CloudQueuePk {
                    destination_id: "dest".to_owned(),
                    remote_key: "rk/shared".to_owned(),
                },
                "attempt-late-commit",
                "22222222222222222222222222222222",
                "md5",
                222,
                Some(first_result.upload_set_id.as_str()),
            );
            assert!(late_commit.is_err());
            let late_fail = cloud_upload_fail(
                &mut locked,
                &CloudQueuePk {
                    destination_id: "dest".to_owned(),
                    remote_key: "rk/shared".to_owned(),
                },
                "attempt-late-fail",
                "timeout",
                Some(1234),
                false,
                Some(first_result.upload_set_id.as_str()),
            );
            assert!(late_fail.is_err());

            let attempts_after: i64 = locked
                .query_row("SELECT COUNT(*) FROM cloud_upload_attempts", [], |row| row.get(0))
                .expect("count attempts after");
            let history_after: i64 = locked
                .query_row("SELECT COUNT(*) FROM cloud_sync_history", [], |row| row.get(0))
                .expect("count history after");
            let queue_state: String = locked
                .query_row(
                    "SELECT state
                       FROM cloud_upload_queue
                      WHERE destination_id = 'dest' AND remote_key = 'rk/shared'",
                    [],
                    |row| row.get(0),
                )
                .expect("read queue state");
            assert_eq!(attempts_after, attempts_before);
            assert_eq!(history_after, history_before);
            assert_eq!(queue_state, "queued");
        }

        #[test]
        fn prepare_parent_upload_rejects_verify_alg_none() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let children = vec![prepare_child!(
                "front.mp4",
                "rk/front",
                1,
                100,
                1_718_805_700_000,
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "etag-front",
                "none",
            )];
            let digest = manifest_digest_for_prepare_children(&children);
            let archive_item_id = {
                let locked = conn.lock().expect("lock db");
                insert_prepare_parent(
                    &locked,
                    "archive/events/prepare-none",
                    "LIVE",
                    0,
                    Some(digest.as_str()),
                )
            };
            let request = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: digest,
                children,
            };
            assert!(matches!(
                call_prepare(&conn, &request),
                Response::Rejected { .. }
            ));
        }

        #[test]
        fn prepare_parent_upload_rejects_remote_key_owned_by_another_parent_current_set() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let children_a = vec![prepare_child!(
                "front-a.mp4",
                "rk/shared",
                1,
                100,
                1_718_805_700_000,
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "etag-a",
                "md5",
            )];
            let digest_a = manifest_digest_for_prepare_children(&children_a);
            let parent_a = {
                let locked = conn.lock().expect("lock db");
                insert_prepare_parent(
                    &locked,
                    "archive/events/prepare-remote-a",
                    "LIVE",
                    0,
                    Some(digest_a.as_str()),
                )
            };
            let request_a = CloudPrepareParentUploadRequest {
                archive_item_id: parent_a,
                destination_id: "dest".to_owned(),
                source_manifest_digest: digest_a,
                children: children_a,
            };
            assert!(matches!(
                call_prepare(&conn, &request_a),
                Response::CloudPrepareParentUpload(_)
            ));

            let children_b = vec![prepare_child!(
                "front-b.mp4",
                "rk/shared",
                1,
                101,
                1_718_805_701_000,
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "etag-b",
                "md5",
            )];
            let digest_b = manifest_digest_for_prepare_children(&children_b);
            let parent_b = {
                let locked = conn.lock().expect("lock db");
                insert_prepare_parent(
                    &locked,
                    "archive/events/prepare-remote-b",
                    "LIVE",
                    0,
                    Some(digest_b.as_str()),
                )
            };
            let request_b = CloudPrepareParentUploadRequest {
                archive_item_id: parent_b,
                destination_id: "dest".to_owned(),
                source_manifest_digest: digest_b,
                children: children_b,
            };
            assert!(matches!(
                call_prepare(&conn, &request_b),
                Response::Rejected { .. }
            ));
        }

        #[test]
        fn prepare_parent_upload_rejects_non_live_or_durable_parent() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let live_children = vec![prepare_child!(
                "front.mp4",
                "rk/front",
                1,
                100,
                1_718_805_700_000,
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "etag-front",
                "md5",
            )];
            let live_digest = manifest_digest_for_prepare_children(&live_children);
            let (non_live_parent, durable_parent) = {
                let locked = conn.lock().expect("lock db");
                (
                    insert_prepare_parent(
                        &locked,
                        "archive/events/prepare-non-live",
                        "DELETE_CLAIMED",
                        0,
                        Some(live_digest.as_str()),
                    ),
                    insert_prepare_parent(
                        &locked,
                        "archive/events/prepare-durable",
                        "LIVE",
                        1,
                        Some(live_digest.as_str()),
                    ),
                )
            };

            let non_live_request = CloudPrepareParentUploadRequest {
                archive_item_id: non_live_parent,
                destination_id: "dest".to_owned(),
                source_manifest_digest: live_digest.clone(),
                children: live_children.clone(),
            };
            assert!(matches!(
                call_prepare(&conn, &non_live_request),
                Response::Rejected { .. }
            ));

            let durable_request = CloudPrepareParentUploadRequest {
                archive_item_id: durable_parent,
                destination_id: "dest".to_owned(),
                source_manifest_digest: live_digest,
                children: live_children,
            };
            assert!(matches!(
                call_prepare(&conn, &durable_request),
                Response::Rejected { .. }
            ));
        }

        #[test]
        fn prepare_parent_upload_rejects_negative_bytes_bad_hash_bad_child_keys_and_duplicates() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let baseline_children = vec![prepare_child!(
                "front.mp4",
                "rk/front",
                1,
                100,
                1_718_805_700_000,
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "etag-front",
                "md5",
            )];
            let baseline_digest = manifest_digest_for_prepare_children(&baseline_children);
            let archive_item_id = {
                let locked = conn.lock().expect("lock db");
                insert_prepare_parent(
                    &locked,
                    "archive/events/prepare-byte-exactness",
                    "LIVE",
                    0,
                    Some(baseline_digest.as_str()),
                )
            };

            let mut negative_bytes_children = baseline_children.clone();
            negative_bytes_children[0].total_bytes = -1;
            let negative_request = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: baseline_digest.clone(),
                children: negative_bytes_children,
            };
            assert!(matches!(
                call_prepare(&conn, &negative_request),
                Response::Rejected { .. }
            ));

            let mut bad_hash_children = baseline_children.clone();
            bad_hash_children[0].content_sha256 = "AA".repeat(32);
            let bad_hash_request = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: baseline_digest.clone(),
                children: bad_hash_children,
            };
            assert!(matches!(
                call_prepare(&conn, &bad_hash_request),
                Response::Rejected { .. }
            ));

            let mut dotdot_children = baseline_children.clone();
            dotdot_children[0].child_key = "../front.mp4".to_owned();
            let dotdot_request = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: baseline_digest.clone(),
                children: dotdot_children,
            };
            assert!(matches!(
                call_prepare(&conn, &dotdot_request),
                Response::Rejected { .. }
            ));

            let mut backslash_children = baseline_children.clone();
            backslash_children[0].child_key = "front\\mp4".to_owned();
            let backslash_request = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: baseline_digest.clone(),
                children: backslash_children,
            };
            assert!(matches!(
                call_prepare(&conn, &backslash_request),
                Response::Rejected { .. }
            ));

            let duplicate_children = vec![
                baseline_children[0].clone(),
                prepare_child!(
                    "front.mp4",
                    "rk/other",
                    2,
                    100,
                    1_718_805_700_001,
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "etag-other",
                    "md5",
                ),
            ];
            let duplicate_request = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: baseline_digest,
                children: duplicate_children,
            };
            assert!(matches!(
                call_prepare(&conn, &duplicate_request),
                Response::Rejected { .. }
            ));

            let set_count: i64 = conn
                .lock()
                .expect("lock db")
                .query_row("SELECT COUNT(*) FROM cloud_parent_upload_sets", [], |row| row.get(0))
                .expect("count upload sets");
            assert_eq!(set_count, 0);
        }

        #[test]
        fn finalize_parent_upload_complete_set_flips_durable() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let (archive_item_id, _manifest_digest, upload_set_id) =
                prepare_two_child_upload_set(&conn, "archive/events/finalize-parent-complete");
            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "UPDATE cloud_upload_queue
                            SET state = 'done', bytes_uploaded = total_bytes
                          WHERE upload_set_id = ?1",
                        params![upload_set_id],
                    )
                    .expect("mark queue done");
            }
            let response = call_finalize_parent(
                &conn,
                &CloudFinalizeParentUploadRequest {
                    upload_set_id: upload_set_id.clone(),
                },
            );
            let Response::CloudFinalizeParentUpload(result) = response else {
                panic!("expected finalize parent response");
            };
            assert!(result.ok);
            assert!(result.durable_parent);
            assert!(!result.already_finalized);
            let locked = conn.lock().expect("lock db");
            let finalized_at: Option<i64> = locked
                .query_row(
                    "SELECT finalized_at
                       FROM cloud_parent_upload_sets
                      WHERE upload_set_id = ?1",
                    params![upload_set_id],
                    |row| row.get(0),
                )
                .expect("read finalized_at");
            assert!(finalized_at.is_some());
            let durable: i64 = locked
                .query_row(
                    "SELECT durable FROM archive_items WHERE id = ?1",
                    params![archive_item_id],
                    |row| row.get(0),
                )
                .expect("read durable");
            assert_eq!(durable, 1);
        }

        #[test]
        fn finalize_parent_upload_idempotent_replay_after_durable() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let (archive_item_id, _manifest_digest, upload_set_id) =
                prepare_two_child_upload_set(&conn, "archive/events/finalize-parent-replay");
            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "UPDATE cloud_upload_queue
                            SET state = 'done', bytes_uploaded = total_bytes
                          WHERE upload_set_id = ?1",
                        params![upload_set_id],
                    )
                    .expect("mark queue done");
            }
            let first = call_finalize_parent(
                &conn,
                &CloudFinalizeParentUploadRequest {
                    upload_set_id: upload_set_id.clone(),
                },
            );
            let Response::CloudFinalizeParentUpload(first_result) = first else {
                panic!("expected finalize parent response");
            };
            assert!(first_result.durable_parent);
            assert!(!first_result.already_finalized);
            let finalized_before: i64 = conn
                .lock()
                .expect("lock db")
                .query_row(
                    "SELECT finalized_at
                       FROM cloud_parent_upload_sets
                      WHERE upload_set_id = ?1",
                    params![upload_set_id],
                    |row| row.get(0),
                )
                .expect("read finalized_at before replay");

            let second = call_finalize_parent(
                &conn,
                &CloudFinalizeParentUploadRequest {
                    upload_set_id: upload_set_id.clone(),
                },
            );
            let Response::CloudFinalizeParentUpload(second_result) = second else {
                panic!("expected finalize parent replay response");
            };
            assert!(second_result.ok);
            assert!(second_result.durable_parent);
            assert!(second_result.already_finalized);

            let locked = conn.lock().expect("lock db");
            let finalized_after: i64 = locked
                .query_row(
                    "SELECT finalized_at
                       FROM cloud_parent_upload_sets
                      WHERE upload_set_id = ?1",
                    params![upload_set_id],
                    |row| row.get(0),
                )
                .expect("read finalized_at after replay");
            assert_eq!(finalized_after, finalized_before);
            let durable: i64 = locked
                .query_row(
                    "SELECT durable FROM archive_items WHERE id = ?1",
                    params![archive_item_id],
                    |row| row.get(0),
                )
                .expect("read durable");
            assert_eq!(durable, 1);
        }

        #[test]
        fn finalize_parent_upload_incomplete_does_not_flip() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let (archive_item_id, _manifest_digest, upload_set_id) =
                prepare_two_child_upload_set(&conn, "archive/events/finalize-parent-incomplete");
            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "UPDATE cloud_upload_queue
                            SET state = 'done', bytes_uploaded = total_bytes
                          WHERE upload_set_id = ?1
                            AND child_key = 'front.mp4'",
                        params![upload_set_id],
                    )
                    .expect("mark only one queue row done");
            }
            let response = call_finalize_parent(
                &conn,
                &CloudFinalizeParentUploadRequest {
                    upload_set_id: upload_set_id.clone(),
                },
            );
            let Response::CloudFinalizeParentUpload(result) = response else {
                panic!("expected finalize parent response");
            };
            assert!(result.ok);
            assert!(!result.durable_parent);
            assert!(!result.already_finalized);

            let locked = conn.lock().expect("lock db");
            let durable: i64 = locked
                .query_row(
                    "SELECT durable FROM archive_items WHERE id = ?1",
                    params![archive_item_id],
                    |row| row.get(0),
                )
                .expect("read durable");
            assert_eq!(durable, 0);
            let finalized_at: Option<i64> = locked
                .query_row(
                    "SELECT finalized_at
                       FROM cloud_parent_upload_sets
                      WHERE upload_set_id = ?1",
                    params![upload_set_id],
                    |row| row.get(0),
                )
                .expect("read finalized_at");
            assert_eq!(finalized_at, None);
        }

        #[test]
        fn finalize_parent_upload_member_mismatch_does_not_flip() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let (archive_item_id, _manifest_digest, upload_set_id) =
                prepare_two_child_upload_set(&conn, "archive/events/finalize-parent-mismatch");
            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "UPDATE cloud_upload_queue
                            SET state = 'done', bytes_uploaded = total_bytes
                          WHERE upload_set_id = ?1",
                        params![upload_set_id],
                    )
                    .expect("mark queue done");
                locked
                    .execute(
                        "UPDATE cloud_upload_queue
                            SET content_sha256 = ?2
                          WHERE upload_set_id = ?1
                            AND child_key = 'front.mp4'",
                        params![
                            upload_set_id,
                            "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
                        ],
                    )
                    .expect("corrupt one queue row hash");
            }
            let response = call_finalize_parent(
                &conn,
                &CloudFinalizeParentUploadRequest {
                    upload_set_id: upload_set_id.clone(),
                },
            );
            let Response::CloudFinalizeParentUpload(result) = response else {
                panic!("expected finalize parent response");
            };
            assert!(result.ok);
            assert!(!result.durable_parent);
            assert!(!result.already_finalized);

            let locked = conn.lock().expect("lock db");
            let durable: i64 = locked
                .query_row(
                    "SELECT durable FROM archive_items WHERE id = ?1",
                    params![archive_item_id],
                    |row| row.get(0),
                )
                .expect("read durable");
            assert_eq!(durable, 0);
            let finalized_at: Option<i64> = locked
                .query_row(
                    "SELECT finalized_at
                       FROM cloud_parent_upload_sets
                      WHERE upload_set_id = ?1",
                    params![upload_set_id],
                    |row| row.get(0),
                )
                .expect("read finalized_at");
            assert_eq!(finalized_at, None);
        }

        #[test]
        fn finalize_parent_upload_missing_queue_row_does_not_flip() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let (archive_item_id, _manifest_digest, upload_set_id) =
                prepare_two_child_upload_set(&conn, "archive/events/finalize-parent-missing-row");
            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "UPDATE cloud_upload_queue
                            SET state = 'done', bytes_uploaded = total_bytes
                          WHERE upload_set_id = ?1",
                        params![upload_set_id],
                    )
                    .expect("mark queue done");
                locked
                    .execute(
                        "DELETE FROM cloud_upload_queue
                          WHERE upload_set_id = ?1
                            AND child_key = 'back.mp4'",
                        params![upload_set_id],
                    )
                    .expect("delete one queue row");
            }
            let response = call_finalize_parent(
                &conn,
                &CloudFinalizeParentUploadRequest {
                    upload_set_id: upload_set_id.clone(),
                },
            );
            let Response::CloudFinalizeParentUpload(result) = response else {
                panic!("expected finalize parent response");
            };
            assert!(result.ok);
            assert!(!result.durable_parent);
            assert!(!result.already_finalized);
            let locked = conn.lock().expect("lock db");
            let durable: i64 = locked
                .query_row(
                    "SELECT durable FROM archive_items WHERE id = ?1",
                    params![archive_item_id],
                    |row| row.get(0),
                )
                .expect("read durable");
            assert_eq!(durable, 0);
        }

        #[test]
        fn finalize_parent_upload_rejects_superseded_set() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let children_a = vec![
                prepare_child!(
                    "front-a.mp4",
                    "rk/a/front",
                    1,
                    111,
                    1_718_805_700_000,
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "etag-a-front",
                    "md5",
                ),
                prepare_child!(
                    "back-a.mp4",
                    "rk/a/back",
                    2,
                    222,
                    1_718_805_701_000,
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "etag-a-back",
                    "md5",
                ),
            ];
            let digest_a = manifest_digest_for_prepare_children(&children_a);
            let archive_item_id = {
                let locked = conn.lock().expect("lock db");
                insert_prepare_parent(
                    &locked,
                    "archive/events/finalize-parent-superseded",
                    "LIVE",
                    0,
                    Some(digest_a.as_str()),
                )
            };
            let prepare_a = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: digest_a,
                children: children_a,
            };
            let Response::CloudPrepareParentUpload(first_result) = call_prepare(&conn, &prepare_a) else {
                panic!("expected first prepare response");
            };

            let children_b = vec![
                prepare_child!(
                    "front-b.mp4",
                    "rk/b/front",
                    1,
                    333,
                    1_718_805_702_000,
                    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                    "etag-b-front",
                    "md5",
                ),
                prepare_child!(
                    "back-b.mp4",
                    "rk/b/back",
                    2,
                    444,
                    1_718_805_703_000,
                    "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "etag-b-back",
                    "md5",
                ),
            ];
            let digest_b = manifest_digest_for_prepare_children(&children_b);
            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "UPDATE archive_items SET manifest_digest = ?2 WHERE id = ?1",
                        params![archive_item_id, digest_b],
                    )
                    .expect("update parent digest");
            }
            let prepare_b = CloudPrepareParentUploadRequest {
                archive_item_id,
                destination_id: "dest".to_owned(),
                source_manifest_digest: digest_b,
                children: children_b,
            };
            let second = call_prepare(&conn, &prepare_b);
            assert!(matches!(second, Response::CloudPrepareParentUpload(_)));

            let response = call_finalize_parent(
                &conn,
                &CloudFinalizeParentUploadRequest {
                    upload_set_id: first_result.upload_set_id,
                },
            );
            assert_eq!(
                response,
                Response::Rejected {
                    message: "finalize rejected: upload set is superseded".to_owned(),
                }
            );
            let durable: i64 = conn
                .lock()
                .expect("lock db")
                .query_row(
                    "SELECT durable FROM archive_items WHERE id = ?1",
                    params![archive_item_id],
                    |row| row.get(0),
                )
                .expect("read durable");
            assert_eq!(durable, 0);
        }

        #[test]
        fn finalize_parent_upload_rejects_unknown_set() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let response = call_finalize_parent(
                &conn,
                &CloudFinalizeParentUploadRequest {
                    upload_set_id: "ffffffffffffffffffffffffffffffff".to_owned(),
                },
            );
            assert_eq!(
                response,
                Response::Rejected {
                    message: "finalize rejected: unknown upload set".to_owned(),
                }
            );
        }

        #[test]
        fn finalize_parent_upload_rejects_non_live_parent() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let (archive_item_id, _manifest_digest, upload_set_id) =
                prepare_two_child_upload_set(&conn, "archive/events/finalize-parent-non-live");
            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "UPDATE archive_items
                            SET delete_state = 'DELETE_CLAIMED'
                          WHERE id = ?1",
                        params![archive_item_id],
                    )
                    .expect("set non-live state");
            }
            let response = call_finalize_parent(
                &conn,
                &CloudFinalizeParentUploadRequest { upload_set_id },
            );
            assert_eq!(
                response,
                Response::Rejected {
                    message: "finalize rejected: parent archive item is not LIVE".to_owned(),
                }
            );
            let durable: i64 = conn
                .lock()
                .expect("lock db")
                .query_row(
                    "SELECT durable FROM archive_items WHERE id = ?1",
                    params![archive_item_id],
                    |row| row.get(0),
                )
                .expect("read durable");
            assert_eq!(durable, 0);
        }

        #[test]
        fn finalize_parent_upload_rejects_digest_mismatch() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let (archive_item_id, _manifest_digest, upload_set_id) =
                prepare_two_child_upload_set(&conn, "archive/events/finalize-parent-digest-mismatch");
            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "UPDATE cloud_upload_queue
                            SET state = 'done', bytes_uploaded = total_bytes
                          WHERE upload_set_id = ?1",
                        params![upload_set_id],
                    )
                    .expect("mark queue done");
                locked
                    .execute(
                        "UPDATE archive_items
                            SET manifest_digest = ?2
                          WHERE id = ?1",
                        params![archive_item_id, "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"],
                    )
                    .expect("replace digest");
            }
            let response = call_finalize_parent(
                &conn,
                &CloudFinalizeParentUploadRequest { upload_set_id },
            );
            assert_eq!(
                response,
                Response::Rejected {
                    message: "finalize rejected: manifest digest mismatch".to_owned(),
                }
            );
            let durable: i64 = conn
                .lock()
                .expect("lock db")
                .query_row(
                    "SELECT durable FROM archive_items WHERE id = ?1",
                    params![archive_item_id],
                    |row| row.get(0),
                )
                .expect("read durable");
            assert_eq!(durable, 0);
        }

        #[test]
        fn finalize_parent_upload_legacy_null_upload_set_id_never_completes() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let (archive_item_id, _manifest_digest, upload_set_id) =
                prepare_two_child_upload_set(&conn, "archive/events/finalize-parent-legacy-null");
            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "UPDATE cloud_upload_queue
                            SET state = 'done', bytes_uploaded = total_bytes
                          WHERE upload_set_id = ?1",
                        params![upload_set_id],
                    )
                    .expect("mark queue done");
                locked
                    .execute(
                        "UPDATE cloud_upload_queue
                            SET upload_set_id = NULL
                          WHERE archive_item_id = ?1",
                        params![archive_item_id],
                    )
                    .expect("detach queue rows from upload set");
            }
            let response = call_finalize_parent(
                &conn,
                &CloudFinalizeParentUploadRequest {
                    upload_set_id: upload_set_id.clone(),
                },
            );
            let Response::CloudFinalizeParentUpload(result) = response else {
                panic!("expected finalize parent response");
            };
            assert!(result.ok);
            assert!(!result.durable_parent);
            assert!(!result.already_finalized);
            let locked = conn.lock().expect("lock db");
            let durable: i64 = locked
                .query_row(
                    "SELECT durable FROM archive_items WHERE id = ?1",
                    params![archive_item_id],
                    |row| row.get(0),
                )
                .expect("read durable");
            assert_eq!(durable, 0);
            let finalized_at: Option<i64> = locked
                .query_row(
                    "SELECT finalized_at
                       FROM cloud_parent_upload_sets
                      WHERE upload_set_id = ?1",
                    params![upload_set_id],
                    |row| row.get(0),
                )
                .expect("read finalized_at");
            assert_eq!(finalized_at, None);
        }

        fn set_durable_via_complete_upload_set(
            conn: &Connection,
            archive_item_id: i64,
            manifest_digest: &str,
            upload_set_id: &str,
        ) {
            conn.execute(
                "INSERT INTO cloud_parent_upload_sets
                    (upload_set_id, archive_item_id, destination_id, source_manifest_digest, request_digest,
                     expected_child_count, created_at, finalized_at)
                 VALUES (?1, ?2, 'dest', ?3, ?4, 1, 100, 101)",
                params![
                    upload_set_id,
                    archive_item_id,
                    manifest_digest,
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                ],
            )
            .expect("insert upload set");
            conn.execute(
                "INSERT INTO cloud_parent_upload_set_children
                    (upload_set_id, child_key, destination_id, remote_key, category, seq, total_bytes, manifest_mtime_ms,
                     content_sha256, expected_hash, verify_alg)
                 VALUES (?1, 'child-1', 'dest', 'rk/1', 'event_sentry', 1, 10, 1000, ?2, ?3, 'sha256')",
                params![
                    upload_set_id,
                    "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                ],
            )
            .expect("insert upload set child");
            conn.execute(
                "INSERT INTO cloud_upload_queue
                    (archive_item_id, child_key, destination_id, remote_key, category, seq, total_bytes, bytes_uploaded,
                     expected_hash, verify_alg, content_sha256, state, attempts, upload_set_id)
                 VALUES (?1, 'child-1', 'dest', 'rk/1', 'event_sentry', 1, 10, 10, ?2, 'sha256', ?3, 'done', 1, ?4)",
                params![
                    archive_item_id,
                    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                    "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    upload_set_id,
                ],
            )
            .expect("insert queue row");
            conn.execute(
                "UPDATE archive_items SET durable = 1 WHERE id = ?1",
                params![archive_item_id],
            )
            .expect("set durable");
        }

        #[test]
        fn finalize_event_archive_absent_creates_row_and_links() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let boot = Arc::new(BootContext::new());
            let request = finalize_payload();

            let response = call_finalize(&conn, &boot, &request);
            let Response::FinalizeEventArchive(result) = response else {
                panic!("expected finalize response");
            };
            assert!(!result.already_finalized);

            let locked = conn.lock().expect("lock db");
            let row: (String, i64, String) = locked
                .query_row(
                    "SELECT delete_state, durable, path FROM archive_items WHERE id = ?1",
                    params![result.archive_item_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .expect("read archive row");
            assert_eq!(row.0, "LIVE");
            assert_eq!(row.1, 0);
            assert_eq!(row.2, request.generation_dir_path);
            let link_count: i64 = locked
                .query_row(
                    "SELECT COUNT(*) FROM archive_item_clips WHERE archive_item_id = ?1",
                    params![result.archive_item_id],
                    |row| row.get(0),
                )
                .expect("read link count");
            assert_eq!(link_count, request.clips.len() as i64);
        }

        #[test]
        fn finalize_event_archive_exact_replay_preserves_durable() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let boot = Arc::new(BootContext::new());
            let request = finalize_payload();
            let first = call_finalize(&conn, &boot, &request);
            let Response::FinalizeEventArchive(first_result) = first else {
                panic!("expected finalize response");
            };

            {
                let locked = conn.lock().expect("lock db");
                set_durable_via_complete_upload_set(
                    &locked,
                    first_result.archive_item_id,
                    &request.manifest_digest,
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                );
            }

            let replay = call_finalize(&conn, &boot, &request);
            let Response::FinalizeEventArchive(replay_result) = replay else {
                panic!("expected finalize replay response");
            };
            assert!(replay_result.already_finalized);
            assert_eq!(replay_result.archive_item_id, first_result.archive_item_id);
            let durable: i64 = conn
                .lock()
                .expect("lock db")
                .query_row(
                    "SELECT durable FROM archive_items WHERE id = ?1",
                    params![first_result.archive_item_id],
                    |row| row.get(0),
                )
                .expect("read durable");
            assert_eq!(durable, 1);
        }

        #[test]
        fn finalize_event_archive_conflict_same_digest_rejected_without_mutation() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let boot = Arc::new(BootContext::new());
            let request = finalize_payload();
            let created = call_finalize(&conn, &boot, &request);
            let Response::FinalizeEventArchive(created_result) = created else {
                panic!("expected finalize response");
            };

            let mut conflicting = request.clone();
            conflicting.size_bytes += 1;
            let response = call_finalize(&conn, &boot, &conflicting);
            assert_eq!(
                response,
                Response::Rejected {
                    message: FINALIZE_CONFLICT_SAME_DIGEST_MESSAGE.to_owned()
                }
            );
            let locked = conn.lock().expect("lock db");
            let row: (i64, String) = locked
                .query_row(
                    "SELECT size_bytes, path FROM archive_items WHERE id = ?1",
                    params![created_result.archive_item_id],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .expect("read archive row");
            assert_eq!(row.0, request.size_bytes);
            assert_eq!(row.1, request.generation_dir_path);
        }

        #[test]
        fn finalize_event_archive_same_digest_changed_clip_metadata_rejected_without_mutation() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let boot = Arc::new(BootContext::new());
            let request = finalize_payload();
            let created = call_finalize(&conn, &boot, &request);
            let Response::FinalizeEventArchive(created_result) = created else {
                panic!("expected finalize response");
            };

            // Identical manifest_digest + segment_set_digest + path + counts + clip-key
            // set, but a clip's timing differs. The file/segment bytes match, yet the
            // derived clip metadata (uncovered by either digest) diverges, so finalize
            // must fail closed as a same-digest conflict rather than treat it as an
            // idempotent replay that silently preserves stale metadata.
            let mut conflicting = request.clone();
            conflicting.clips[0].started_at -= 1;
            let response = call_finalize(&conn, &boot, &conflicting);
            assert_eq!(
                response,
                Response::Rejected {
                    message: FINALIZE_CONFLICT_SAME_DIGEST_MESSAGE.to_owned()
                }
            );

            let locked = conn.lock().expect("lock db");
            let started_at: i64 = locked
                .query_row(
                    "SELECT c.started_at
                       FROM archive_item_clips aic
                       JOIN clips c ON c.id = aic.clip_id
                      WHERE aic.archive_item_id = ?1
                      ORDER BY c.canonical_key
                      LIMIT 1",
                    params![created_result.archive_item_id],
                    |row| row.get(0),
                )
                .expect("read clip started_at");
            assert_eq!(started_at, request.clips[0].started_at);
        }

        #[test]
        fn finalize_event_archive_same_digest_changed_angle_rejected_without_mutation() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let boot = Arc::new(BootContext::new());
            let request = finalize_payload();
            let created = call_finalize(&conn, &boot, &request);
            let Response::FinalizeEventArchive(created_result) = created else {
                panic!("expected finalize response");
            };

            // Same digests/counts, but an angle's file_ref (camera-to-file mapping)
            // differs — this is not covered by either digest and can make archived
            // footage undiscoverable, so it must be a conflict, not a replay.
            let mut conflicting = request.clone();
            conflicting.angles[0].file_ref = "archive/events/e1/front-00-alt.mp4".to_owned();
            let response = call_finalize(&conn, &boot, &conflicting);
            assert_eq!(
                response,
                Response::Rejected {
                    message: FINALIZE_CONFLICT_SAME_DIGEST_MESSAGE.to_owned()
                }
            );

            let locked = conn.lock().expect("lock db");
            let file_ref: String = locked
                .query_row(
                    "SELECT a.file_ref
                       FROM archive_item_clips aic
                       JOIN clips c ON c.id = aic.clip_id
                       JOIN angles a ON a.clip_id = c.id AND a.view_kind = 'archive'
                      WHERE aic.archive_item_id = ?1
                      ORDER BY c.canonical_key, a.camera
                      LIMIT 1",
                    params![created_result.archive_item_id],
                    |row| row.get(0),
                )
                .expect("read angle file_ref");
            assert_eq!(file_ref, request.angles[0].file_ref);
        }

        #[test]
        fn finalize_event_archive_changed_generation_supersedes_and_replaces_links() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let boot = Arc::new(BootContext::new());
            let request = finalize_payload();
            let Response::FinalizeEventArchive(initial) = call_finalize(&conn, &boot, &request) else {
                panic!("expected finalize response");
            };

            {
                let locked = conn.lock().expect("lock db");
                set_durable_via_complete_upload_set(
                    &locked,
                    initial.archive_item_id,
                    &request.manifest_digest,
                    "cccccccccccccccccccccccccccccccc",
                );
            }

            let next = finalize_payload_generation_two();
            let response = call_finalize(&conn, &boot, &next);
            let Response::FinalizeEventArchive(result) = response else {
                panic!("expected finalize response");
            };
            assert!(!result.already_finalized);
            assert_eq!(result.archive_item_id, initial.archive_item_id);

            let locked = conn.lock().expect("lock db");
            let row: (String, i64, String) = locked
                .query_row(
                    "SELECT path, durable, manifest_digest FROM archive_items WHERE id = ?1",
                    params![result.archive_item_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .expect("read archive row");
            assert_eq!(row.0, next.generation_dir_path);
            assert_eq!(row.1, 0);
            assert_eq!(row.2, next.manifest_digest);

            let superseded: Option<i64> = locked
                .query_row(
                    "SELECT superseded_at FROM cloud_parent_upload_sets WHERE upload_set_id = ?1",
                    params!["cccccccccccccccccccccccccccccccc"],
                    |row| row.get(0),
                )
                .expect("read superseded_at");
            assert!(superseded.is_some());

            let linked: Vec<String> = {
                let mut stmt = locked
                    .prepare(
                        "SELECT c.canonical_key
                           FROM archive_item_clips aic
                           JOIN clips c ON c.id = aic.clip_id
                          WHERE aic.archive_item_id = ?1
                          ORDER BY c.canonical_key",
                    )
                    .expect("prepare linked clip query");
                let rows = stmt
                    .query_map(params![result.archive_item_id], |row| row.get::<_, String>(0))
                    .expect("query linked clips");
                rows.collect::<Result<Vec<_>, _>>().expect("collect linked clips")
            };
            assert_eq!(linked, vec![next.clips[0].canonical_key.clone()]);
        }

        #[test]
        fn finalize_changed_generation_supersedes_and_parks_unfinished_rows() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let boot = Arc::new(BootContext::new());
            let request = finalize_payload();
            let Response::FinalizeEventArchive(initial) = call_finalize(&conn, &boot, &request) else {
                panic!("expected finalize response");
            };
            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "INSERT INTO cloud_parent_upload_sets
                            (upload_set_id, archive_item_id, destination_id, source_manifest_digest, request_digest,
                             expected_child_count, created_at, finalized_at, superseded_at)
                         VALUES (?1, ?2, 'dest', ?3, ?4, 2, 100, NULL, NULL)",
                        params![
                            "abababababababababababababababab",
                            initial.archive_item_id,
                            request.manifest_digest,
                            "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd",
                        ],
                    )
                    .expect("insert active upload set");
                locked
                    .execute(
                        "INSERT INTO cloud_upload_queue
                            (archive_item_id, child_key, destination_id, remote_key, category, seq, total_bytes, bytes_uploaded,
                             expected_hash, verify_alg, content_sha256, state, attempts, not_before, last_error, upload_set_id)
                         VALUES (?1, 'child-done', 'dest', 'rk/done', 'event_sentry', 1, 10, 10,
                                 'etag-done', 'md5',
                                 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                                 'done', 1, NULL, NULL, ?2)",
                        params![initial.archive_item_id, "abababababababababababababababab"],
                    )
                    .expect("insert done queue row");
                locked
                    .execute(
                        "INSERT INTO cloud_upload_queue
                            (archive_item_id, child_key, destination_id, remote_key, category, seq, total_bytes, bytes_uploaded,
                             expected_hash, verify_alg, content_sha256, state, attempts, not_before, last_error, upload_set_id)
                         VALUES (?1, 'child-queued', 'dest', 'rk/queued', 'event_sentry', 2, 20, 5,
                                 'etag-queued', 'md5',
                                 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                                 'queued', 3, 1234, 'transient', ?2)",
                        params![initial.archive_item_id, "abababababababababababababababab"],
                    )
                    .expect("insert queued row");
            }

            let next = finalize_payload_generation_two();
            let response = call_finalize(&conn, &boot, &next);
            let Response::FinalizeEventArchive(result) = response else {
                panic!("expected finalize response");
            };
            assert!(!result.already_finalized);
            assert_eq!(result.archive_item_id, initial.archive_item_id);

            let locked = conn.lock().expect("lock db");
            let superseded_at: Option<i64> = locked
                .query_row(
                    "SELECT superseded_at
                       FROM cloud_parent_upload_sets
                      WHERE upload_set_id = 'abababababababababababababababab'",
                    [],
                    |row| row.get(0),
                )
                .expect("read superseded_at");
            assert!(superseded_at.is_some());
            let done_row: (String, i64, i64, Option<i64>, Option<String>) = locked
                .query_row(
                    "SELECT state, bytes_uploaded, attempts, not_before, last_error
                       FROM cloud_upload_queue
                      WHERE destination_id='dest' AND remote_key='rk/done'",
                    [],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
                )
                .expect("read done row");
            assert_eq!(done_row.0, "done");
            assert_eq!(done_row.1, 10);
            assert_eq!(done_row.2, 1);
            assert_eq!(done_row.3, None);
            assert_eq!(done_row.4, None);
            let queued_row: (String, i64, i64, Option<i64>, Option<String>) = locked
                .query_row(
                    "SELECT state, bytes_uploaded, attempts, not_before, last_error
                       FROM cloud_upload_queue
                      WHERE destination_id='dest' AND remote_key='rk/queued'",
                    [],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
                )
                .expect("read queued row");
            assert_eq!(queued_row.0, "parked");
            assert_eq!(queued_row.1, 0);
            assert_eq!(queued_row.2, 0);
            assert_eq!(queued_row.3, None);
            assert_eq!(
                queued_row.4.as_deref(),
                Some(FINALIZE_SUPERSEDE_PARK_MESSAGE)
            );
            let durable: i64 = locked
                .query_row(
                    "SELECT durable FROM archive_items WHERE id = ?1",
                    params![initial.archive_item_id],
                    |row| row.get(0),
                )
                .expect("read durable");
            assert_eq!(durable, 0);
        }

        #[test]
        fn finalize_event_archive_cas_stale_rejected_without_mutation() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let boot = Arc::new(BootContext::new());
            let request = finalize_payload();
            let Response::FinalizeEventArchive(initial) = call_finalize(&conn, &boot, &request) else {
                panic!("expected finalize response");
            };
            let mut stale = finalize_payload_generation_two();
            stale.expected_prior_manifest_digest = None;
            let response = call_finalize(&conn, &boot, &stale);
            assert_eq!(
                response,
                Response::Rejected {
                    message: FINALIZE_CAS_STALE_MESSAGE.to_owned()
                }
            );
            let digest: String = conn
                .lock()
                .expect("lock db")
                .query_row(
                    "SELECT manifest_digest FROM archive_items WHERE id = ?1",
                    params![initial.archive_item_id],
                    |row| row.get(0),
                )
                .expect("read manifest_digest");
            assert_eq!(digest, request.manifest_digest);
        }

        #[test]
        fn finalize_event_archive_changed_generation_rejected_when_upload_lease_active() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let boot = Arc::new(BootContext::new());
            let request = finalize_payload();
            let Response::FinalizeEventArchive(initial) = call_finalize(&conn, &boot, &request) else {
                panic!("expected finalize response");
            };
            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "INSERT INTO leases
                            (archive_item_id, kind, holder, gen, boot_id, expires_mono_ms)
                         VALUES (?1, 'upload', 'uploadd:test', 'lease-gen', ?2, ?3)",
                        params![
                            initial.archive_item_id,
                            boot.boot_id(),
                            boot.mono_now_ms() + 60_000
                        ],
                    )
                    .expect("insert lease");
            }
            let next = finalize_payload_generation_two();
            let response = call_finalize(&conn, &boot, &next);
            assert_eq!(
                response,
                Response::Rejected {
                    message: "finalize rejected: active upload lease".to_owned()
                }
            );
        }

        #[test]
        fn finalize_event_archive_oversize_rejected_without_write() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let boot = Arc::new(BootContext::new());
            let mut request = finalize_payload();
            request.source_event_key = "x".repeat(MAX_REQUEST_FRAME as usize + 1);
            let response = call_finalize(&conn, &boot, &request);
            assert_eq!(
                response,
                Response::Rejected {
                    message: FINALIZE_EVENT_TOO_LARGE_MESSAGE.to_owned()
                }
            );
            let count: i64 = conn
                .lock()
                .expect("lock db")
                .query_row("SELECT COUNT(*) FROM archive_items", [], |row| row.get(0))
                .expect("count archive items");
            assert_eq!(count, 0);
        }

        #[test]
        fn finalize_event_archive_self_consistency_failures_reject_without_write() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let boot = Arc::new(BootContext::new());

            let mut bad_count = finalize_payload();
            bad_count.expected_segment_count += 1;
            assert!(matches!(
                call_finalize(&conn, &boot, &bad_count),
                Response::Rejected { .. }
            ));

            let mut bad_digest = finalize_payload();
            bad_digest.segment_set_digest =
                "9999999999999999999999999999999999999999999999999999999999999999".to_owned();
            assert!(matches!(
                call_finalize(&conn, &boot, &bad_digest),
                Response::Rejected { .. }
            ));

            let mut traversal = finalize_payload();
            traversal.segments[0].segment_key = "../escape/front.mp4".to_owned();
            traversal.segment_set_digest = compute_segment_set_digest(&traversal.segments)
                .expect("compute segment_set_digest");
            assert!(matches!(
                call_finalize(&conn, &boot, &traversal),
                Response::Rejected { .. }
            ));

            let mut duplicate_camera = finalize_payload();
            duplicate_camera
                .angles
                .push(duplicate_camera.angles[0].clone());
            assert!(matches!(
                call_finalize(&conn, &boot, &duplicate_camera),
                Response::Rejected { .. }
            ));

            let count: i64 = conn
                .lock()
                .expect("lock db")
                .query_row("SELECT COUNT(*) FROM archive_items", [], |row| row.get(0))
                .expect("count archive items");
            assert_eq!(count, 0);
        }

        #[test]
        fn finalize_event_archive_source_identity_trigger_conflict_is_non_panicking() {
            let conn = Arc::new(Mutex::new(open_in_memory().expect("open db")));
            let boot = Arc::new(BootContext::new());
            let request = finalize_payload();
            {
                let locked = conn.lock().expect("lock db");
                locked
                    .execute(
                        "INSERT INTO archive_items
                            (folder_class, path, size_bytes, file_count, archived_at, durable, delete_state,
                             manifest_digest, verified_pass_id, source_generation, source_event_key, source_volume_id,
                             segment_set_digest, created_at, updated_at)
                         VALUES ('SentryClips', 'archive/events/preseed', 100, 1, 1, 0, 'LIVE',
                                 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                                 'boot1:scan1', ?1, 'vol-a',
                                 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc', 0, 0)",
                        params![request.source_event_key],
                    )
                    .expect("insert seed row");
            }
            let mut conflicting = request.clone();
            conflicting.source_volume_id = None;
            let response = call_finalize(&conn, &boot, &conflicting);
            assert!(matches!(
                response,
                Response::Rejected { .. } | Response::Error { .. }
            ));
            let count: i64 = conn
                .lock()
                .expect("lock db")
                .query_row("SELECT COUNT(*) FROM archive_items", [], |row| row.get(0))
                .expect("count archive items");
            assert_eq!(count, 1);
        }
    #[test]
    fn archived_clips_folder_class_is_rejected() {
        assert!(parse_folder_class("ArchivedClips").is_err());
    }

    #[test]
    fn validation_rejects_duplicate_camera() {
        let mut request = payload();
        let angle = request.angles.first().cloned().unwrap();
        request.angles.push(angle);
        assert!(validate_payload(&request).is_err());
    }

    #[test]
    fn validation_accepts_valid_payload() {
        assert!(validate_payload(&payload()).is_ok());
    }

    #[test]
    fn validation_accepts_real_six_camera_recent_clip_payload() {
        let mut request = payload();
        request.archive.file_count = 6;
        request.angles.push(ArchiveAngle {
            camera: "back".to_owned(),
            file_ref: "archive/2026-06-19/clip-a/back.mp4".to_owned(),
            offset_ms: 0,
            duration_s: Some(60),
            size_bytes: 1024,
        });
        request.angles.push(ArchiveAngle {
            camera: "left_repeater".to_owned(),
            file_ref: "archive/2026-06-19/clip-a/left_repeater.mp4".to_owned(),
            offset_ms: 0,
            duration_s: Some(60),
            size_bytes: 1024,
        });
        request.angles.push(ArchiveAngle {
            camera: "right_repeater".to_owned(),
            file_ref: "archive/2026-06-19/clip-a/right_repeater.mp4".to_owned(),
            offset_ms: 0,
            duration_s: Some(60),
            size_bytes: 1024,
        });
        request.angles.push(ArchiveAngle {
            camera: "left_pillar".to_owned(),
            file_ref: "archive/2026-06-19/clip-a/left_pillar.mp4".to_owned(),
            offset_ms: 0,
            duration_s: Some(60),
            size_bytes: 1024,
        });
        request.angles.push(ArchiveAngle {
            camera: "right_pillar".to_owned(),
            file_ref: "archive/2026-06-19/clip-a/right_pillar.mp4".to_owned(),
            offset_ms: 0,
            duration_s: Some(60),
            size_bytes: 1024,
        });

        assert!(validate_payload(&request).is_ok());
    }

    #[test]
    fn validation_accepts_left_pillar_single_angle_payload() {
        let mut request = payload();
        let angle = request.angles.first_mut().expect("payload has one angle");
        angle.camera = "left_pillar".to_owned();
        angle.file_ref = "archive/2026-06-19/clip-a/left_pillar.mp4".to_owned();
        assert!(validate_payload(&request).is_ok());
    }

    #[test]
    fn validation_rejects_bogus_left_camera_label() {
        let mut request = payload();
        let angle = request.angles.first_mut().expect("payload has one angle");
        angle.camera = "left".to_owned();
        angle.file_ref = "archive/2026-06-19/clip-a/left.mp4".to_owned();
        assert!(validate_payload(&request).is_err());
    }

    fn new_temp_dir() -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock before epoch")
            .as_nanos();
        let name = format!("indexd-server-test-{}-{nanos}", std::process::id());
        let dir = std::env::temp_dir().join(name);
        std::fs::create_dir_all(&dir).expect("create temp dir");
        dir
    }

    #[derive(Debug, Clone)]
    struct ProvingUploadSetFixture {
        upload_set_id: String,
        digest: String,
        destination_id: String,
        remote_key: String,
        child_key: String,
    }

    fn insert_archive_item(
        conn: &Connection,
        path: &str,
        folder_class: &str,
        archived_at: i64,
        durable: i64,
        pinned: i64,
    ) -> i64 {
        conn.execute(
            "INSERT INTO archive_items
                (folder_class, path, size_bytes, file_count, archived_at, durable, pinned, created_at, updated_at)
             VALUES (?1, ?2, 4096, 1, ?3, ?4, ?5, 0, 0)",
            params![folder_class, path, archived_at, durable, pinned],
        )
        .expect("insert archive item");
        let archive_item_id = conn.last_insert_rowid();
        if durable == 1 {
            seed_proving_upload_set(conn, archive_item_id);
        }
        // The recency gate now keys on clips.started_at, so link a clip whose
        // recording instant mirrors archived_at to preserve the intended age.
        conn.execute(
            "INSERT INTO clips
                (canonical_key, started_at, partition, folder_class, created_at, updated_at)
             VALUES (?1, ?2, 'p', ?3, 0, 0)",
            params![path, archived_at, folder_class],
        )
        .expect("insert clip");
        let clip_id = conn.last_insert_rowid();
        conn.execute(
            "INSERT INTO archive_item_clips (archive_item_id, clip_id) VALUES (?1, ?2)",
            params![archive_item_id, clip_id],
        )
        .expect("link archive item to clip");
        archive_item_id
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

    fn send(socket_path: &Path, request: &Request) -> Response {
        let mut stream = UnixStream::connect(socket_path).expect("connect indexd server");
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .expect("set read timeout");
        stream
            .set_write_timeout(Some(Duration::from_secs(2)))
            .expect("set write timeout");
        let payload = serde_json::to_vec(request).expect("encode request");
        write_frame(&mut stream, &payload).expect("write request frame");
        let response_payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read response");
        serde_json::from_slice(&response_payload).expect("decode response")
    }

    #[test]
    #[allow(clippy::too_many_lines)]
    fn socket_delete_lifecycle_and_lease_denial_roundtrip() {
        let conn = open_in_memory().expect("open db");
        let candidate_id =
            insert_archive_item(&conn, "archive/recent/old-1", "RecentClips", 100, 1, 0);
        let leased_id =
            insert_archive_item(&conn, "archive/recent/leased", "RecentClips", 90, 1, 0);
        let conn = Arc::new(Mutex::new(conn));
        let boot = Arc::new(BootContext::new());
        {
            let locked = conn.lock().expect("lock db");
            locked
                .execute(
                    "INSERT INTO leases
                        (archive_item_id, kind, holder, gen, boot_id, expires_mono_ms)
                     VALUES (?1, 'playback', 'webd:test', 'lease-gen', ?2, ?3)",
                    params![leased_id, boot.boot_id(), boot.mono_now_ms() + 120_000],
                )
                .expect("insert lease");
        }

        let dir = new_temp_dir();
        let socket_path = dir.join("indexd.sock");
        let _server =
            spawn(&conn, &boot, &socket_path, Duration::from_secs(2)).expect("spawn indexd server");
        let candidates = send(
            &socket_path,
            &Request::ListEvictionCandidates {
                recency_floor_epoch: 500,
                allow_undurable: false,
                limit: 8,
            },
        );
        assert!(matches!(candidates, Response::EvictionCandidates { .. }));
        let Response::EvictionCandidates { items } = candidates else {
            unreachable!();
        };
        assert!(items.iter().any(|item| item.id == candidate_id));
        assert_eq!(
            send(
                &socket_path,
                &Request::ClaimEvictionCandidate {
                    id: candidate_id,
                    recency_floor_epoch: 500,
                    allow_undurable: false,
                }
            ),
            Response::Claimed {}
        );
        assert_eq!(
            send(
                &socket_path,
                &Request::MarkArchiveDeleting { id: candidate_id }
            ),
            Response::Acked {}
        );
        assert_eq!(
            send(
                &socket_path,
                &Request::MarkArchiveDeleted {
                    id: candidate_id,
                    bytes_freed: 4096
                }
            ),
            Response::Acked {}
        );
        let second_claim = send(
            &socket_path,
            &Request::ClaimEvictionCandidate {
                id: candidate_id,
                recency_floor_epoch: 500,
                allow_undurable: false,
            },
        );
        assert!(matches!(
            second_claim,
            Response::ClaimDenied { .. } | Response::NotFound {}
        ));
        assert_eq!(
            send(
                &socket_path,
                &Request::ClaimEvictionCandidate {
                    id: leased_id,
                    recency_floor_epoch: 500,
                    allow_undurable: false,
                }
            ),
            Response::ClaimDenied {
                reason: "unexpired lease".to_owned()
            }
        );
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn socket_undurable_opt_in_roundtrip() {
        let conn = open_in_memory().expect("open db");
        let undurable_id = insert_archive_item(
            &conn,
            "archive/recent/old-undurable",
            "RecentClips",
            80,
            0,
            0,
        );
        let conn = Arc::new(Mutex::new(conn));
        let boot = Arc::new(BootContext::new());
        let dir = new_temp_dir();
        let socket_path = dir.join("indexd.sock");
        let _server =
            spawn(&conn, &boot, &socket_path, Duration::from_secs(2)).expect("spawn indexd server");

        let denied_list = send(
            &socket_path,
            &Request::ListEvictionCandidates {
                recency_floor_epoch: 500,
                allow_undurable: false,
                limit: 8,
            },
        );
        let Response::EvictionCandidates { items } = denied_list else {
            unreachable!();
        };
        assert!(!items.iter().any(|item| item.id == undurable_id));

        let allowed_list = send(
            &socket_path,
            &Request::ListEvictionCandidates {
                recency_floor_epoch: 500,
                allow_undurable: true,
                limit: 8,
            },
        );
        let Response::EvictionCandidates { items } = allowed_list else {
            unreachable!();
        };
        assert!(items.iter().any(|item| item.id == undurable_id));

        assert_eq!(
            send(
                &socket_path,
                &Request::ClaimEvictionCandidate {
                    id: undurable_id,
                    recency_floor_epoch: 500,
                    allow_undurable: false,
                }
            ),
            Response::ClaimDenied {
                reason: "ineligible".to_owned()
            }
        );
        assert_eq!(
            send(
                &socket_path,
                &Request::ClaimEvictionCandidate {
                    id: undurable_id,
                    recency_floor_epoch: 500,
                    allow_undurable: true,
                }
            ),
            Response::Claimed {}
        );

        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    #[allow(clippy::too_many_lines, clippy::panic, clippy::single_match_else)]
    fn socket_cloud_rpc_roundtrip() {
        let conn = open_in_memory().expect("open db");
        conn.execute(
            "INSERT INTO archive_items
                (id, folder_class, path, size_bytes, file_count, archived_at, created_at, updated_at)
             VALUES (101, 'RecentClips', 'archive/cloud/101', 30, 2, 100, 0, 0)",
            [],
        )
        .expect("insert archive item");
        let conn = Arc::new(Mutex::new(conn));
        let boot = Arc::new(BootContext::new());
        let dir = new_temp_dir();
        let socket_path = dir.join("indexd.sock");
        let _server =
            spawn(&conn, &boot, &socket_path, Duration::from_secs(2)).expect("spawn indexd server");

        let hash_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let hash_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

        let queued = send(
            &socket_path,
            &Request::CloudQueueUpsert {
                item: CloudQueueUpsertWire {
                    archive_item_id: 101,
                    child_key: "child-a".to_owned(),
                    destination_id: "dest".to_owned(),
                    remote_key: "rk/a".to_owned(),
                    category: "bulk".to_owned(),
                    seq: 1,
                    total_bytes: 10,
                    content_sha256: hash_a.to_owned(),
                    expected_hash: Some(hash_a.to_owned()),
                    verify_alg: "sha256".to_owned(),
                },
            },
        );
        assert_eq!(
            queued,
            Response::CloudQueueState {
                state: "queued".to_owned()
            }
        );
        let _ = send(
            &socket_path,
            &Request::CloudQueueUpsert {
                item: CloudQueueUpsertWire {
                    archive_item_id: 101,
                    child_key: "child-b".to_owned(),
                    destination_id: "dest".to_owned(),
                    remote_key: "rk/b".to_owned(),
                    category: "bulk".to_owned(),
                    seq: 2,
                    total_bytes: 20,
                    content_sha256: hash_b.to_owned(),
                    expected_hash: Some(hash_b.to_owned()),
                    verify_alg: "sha256".to_owned(),
                },
            },
        );

        let lease = send(
            &socket_path,
            &Request::UploadLeaseAcquire {
                archive_item_id: 101,
                ttl_ms: 1_000,
            },
        );
        let Response::UploadLeaseAcquired {
            granted: true,
            token: Some(token),
            boot_id: Some(_),
            expires_mono_ms: Some(_),
        } = lease
        else {
            panic!("unexpected lease response: {lease:?}");
        };
        let renewed = send(
            &socket_path,
            &Request::UploadLeaseRenew {
                token: token.clone(),
                ttl_ms: 2_000,
            },
        );
        assert!(matches!(
            renewed,
            Response::UploadLeaseRenewed {
                ok: true,
                expires_mono_ms: Some(_)
            }
        ));
        assert_eq!(
            send(
                &socket_path,
                &Request::UploadLeaseRelease {
                    token: token.clone()
                }
            ),
            Response::UploadLeaseReleased { ok: true }
        );

        let first_commit = send(
            &socket_path,
            &Request::CloudUploadCommit {
                queue_pk: CloudQueuePkWire {
                    destination_id: "dest".to_owned(),
                    remote_key: "rk/a".to_owned(),
                },
                attempt_id: "attempt-a".to_owned(),
                upload_set_id: None,
                hash: hash_a.to_owned(),
                hash_alg: "sha256".to_owned(),
                size: 10,
            },
        );
        assert_eq!(
            first_commit,
            Response::CloudUploadCommitted {
                ok: true,
                durable_parent: false
            }
        );
        let second_commit = send(
            &socket_path,
            &Request::CloudUploadCommit {
                queue_pk: CloudQueuePkWire {
                    destination_id: "dest".to_owned(),
                    remote_key: "rk/b".to_owned(),
                },
                attempt_id: "attempt-b".to_owned(),
                upload_set_id: None,
                hash: hash_b.to_owned(),
                hash_alg: "sha256".to_owned(),
                size: 20,
            },
        );
        assert_eq!(
            second_commit,
            Response::CloudUploadCommitted {
                ok: true,
                durable_parent: false
            }
        );

        let fail_state = send(
            &socket_path,
            &Request::CloudQueueUpsert {
                item: CloudQueueUpsertWire {
                    archive_item_id: 101,
                    child_key: "child-c".to_owned(),
                    destination_id: "dest".to_owned(),
                    remote_key: "rk/c".to_owned(),
                    category: "bulk".to_owned(),
                    seq: 3,
                    total_bytes: 5,
                    content_sha256:
                        "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
                            .to_owned(),
                    expected_hash: None,
                    verify_alg: "none".to_owned(),
                },
            },
        );
        assert_eq!(
            fail_state,
            Response::CloudQueueState {
                state: "queued".to_owned()
            }
        );
        let failed = send(
            &socket_path,
            &Request::CloudUploadFail {
                queue_pk: CloudQueuePkWire {
                    destination_id: "dest".to_owned(),
                    remote_key: "rk/c".to_owned(),
                },
                attempt_id: "attempt-c".to_owned(),
                upload_set_id: None,
                error_class: "timeout".to_owned(),
                not_before: Some(1234),
                terminal: false,
            },
        );
        assert_eq!(
            failed,
            Response::CloudUploadFailed {
                ok: true,
                state: "failed".to_owned()
            }
        );

        let candidates = send(
            &socket_path,
            &Request::CloudCandidates {
                folders: vec!["RecentClips".to_owned()],
                after_cursor: None,
                limit: 10,
            },
        );
        assert!(matches!(candidates, Response::CloudCandidates { .. }));
        let discover = send(
            &socket_path,
            &Request::CloudDiscover {
                after_cursor: None,
                limit: 10,
            },
        );
        assert!(matches!(discover, Response::CloudDiscoverPage { .. }));
        let queue = send(
            &socket_path,
            &Request::CloudQueueLoad {
                after_cursor: None,
                limit: 10,
                upload_set_id: None,
            },
        );
        assert!(matches!(queue, Response::CloudQueuePage { .. }));
        let history = send(
            &socket_path,
            &Request::CloudHistoryLoad {
                after_cursor: None,
                limit: 10,
            },
        );
        assert!(matches!(history, Response::CloudHistoryPage { .. }));
        let stats = send(&socket_path, &Request::CloudStatsGet {});
        assert!(matches!(stats, Response::CloudStats { .. }));
        let reset = send(&socket_path, &Request::CloudStatsReset {});
        assert!(matches!(reset, Response::CloudStatsReset { ok: true, .. }));
        let config = send(&socket_path, &Request::CloudConfigGet {});
        let Response::CloudConfig { config } = config else {
            panic!("expected cloud config response");
        };
        assert_eq!(config.max_attempts, 5);
        let updated = send(
            &socket_path,
            &Request::CloudConfigPut {
                config: CloudConfigWire {
                    auto_sync: false,
                    ..config
                },
            },
        );
        let Response::CloudConfig { config } = updated else {
            panic!("expected cloud config response");
        };
        assert!(!config.auto_sync);
        let retried = send(
            &socket_path,
            &Request::CloudQueueRetry {
                archive_item_id: 101,
                child_key: Some("child-c".to_owned()),
                upload_set_id: None,
                resolution: CloudQueueRetryResolutionWire::Replace,
            },
        );
        assert_eq!(
            retried,
            Response::CloudQueueState {
                state: "queued".to_owned()
            }
        );

        let _ = std::fs::remove_dir_all(dir);
    }
}
