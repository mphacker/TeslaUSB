//! Crate-local `retentiond → indexd` delete-path transport.
//!
//! The wire contract mirrors `indexd::proto` but remains crate-local so
//! `retentiond` and `indexd` stay decoupled.
#![allow(dead_code)]

use std::cell::Cell;
use std::io;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[cfg(unix)]
use crate::register_client::IO_TIMEOUT_SECS;
use crate::register_client::{MAX_REQUEST_FRAME, RegisterError, read_frame, write_frame};

/// Delete-path request wire mirror for `indexd::proto::Request`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum DeleteWireRequest {
    /// Claim an eviction candidate atomically.
    ClaimEvictionCandidate {
        /// Archive item id.
        id: i64,
        /// Items at/after this floor are ineligible.
        recency_floor_epoch: i64,
        /// Opt-in: allow rows that are not cloud-durable.
        #[serde(default)]
        allow_undurable: bool,
    },
    /// Mark an item deleting.
    MarkArchiveDeleting {
        /// Archive item id.
        id: i64,
    },
    /// Mark an item deleted.
    MarkArchiveDeleted {
        /// Archive item id.
        id: i64,
        /// Bytes freed by deletion.
        bytes_freed: i64,
    },
    /// Release an item claim.
    ReleaseArchiveDeleteClaim {
        /// Archive item id.
        id: i64,
    },
    /// Clear a consumed terminal delete generation token.
    ClearArchiveDeleteGeneration {
        /// Archive item id.
        id: i64,
        /// Expected token to consume.
        delete_gen: String,
    },
    /// Quarantine an item.
    QuarantineArchiveItem {
        /// Archive item id.
        id: i64,
        /// Human-readable reason.
        reason: String,
    },
    /// List oldest-first eviction candidates.
    ListEvictionCandidates {
        /// Items newer than or equal to this floor are ineligible.
        recency_floor_epoch: i64,
        /// Opt-in: allow rows that are not cloud-durable.
        #[serde(default)]
        allow_undurable: bool,
        /// Maximum number of rows requested.
        limit: u32,
    },
    /// List rows needing crash recovery.
    ListRecoveryRows {},
    /// Claim the oldest claimable queued internal archive-delete request.
    ArchiveDeleteClaimNext {},
    /// Mark one claimed internal archive-delete request done.
    ArchiveDeleteComplete {
        /// Stable durable job id.
        job_id: String,
        /// Expected owned delete generation token.
        owned_delete_gen: String,
    },
    /// Mark one claimed internal archive-delete request failed or requeued.
    ArchiveDeleteFail {
        /// Stable durable job id.
        job_id: String,
        /// Expected owned delete generation token.
        owned_delete_gen: String,
        /// Human-readable failure detail.
        detail: String,
        /// Explicitly requeue instead of terminal-failing.
        #[serde(default)]
        requeue: bool,
    },
}

/// Delete-path response wire mirror for `indexd::proto::Response`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum DeleteWireResponse {
    /// Claim succeeded.
    Claimed {
        /// Persisted delete generation token for trash naming.
        delete_gen: String,
    },
    /// Claim denied.
    ClaimDenied {
        /// Human-readable reason.
        reason: String,
    },
    /// No row found for the requested id.
    NotFound {},
    /// Generic write acknowledgement.
    Acked {},
    /// Eviction candidate list payload.
    EvictionCandidates {
        /// Candidate rows.
        items: Vec<EvictionCandidateWire>,
    },
    /// Recovery row list payload.
    RecoveryRows {
        /// Recovery rows.
        rows: Vec<RecoveryRowWire>,
    },
    /// Claimed internal archive-delete work item.
    ArchiveDeleteClaimed {
        /// Stable durable job id.
        job_id: String,
        /// Logical request id.
        request_id: String,
        /// Target archive item id.
        target_archive_item_id: i64,
        /// Target archive relative path fence.
        target_archive_path: String,
        /// Target archive size fence.
        target_archive_size_bytes: i64,
        /// Persisted delete generation token from claim.
        delete_gen: String,
    },
    /// Operational/transient error.
    Error {
        /// Human-readable message.
        message: String,
    },
    /// Deterministic request rejection.
    Rejected {
        /// Human-readable message.
        message: String,
    },
}

/// One eviction candidate row over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvictionCandidateWire {
    /// Archive item id.
    pub id: i64,
    /// Archive-root-relative item path.
    pub path: String,
    /// Archive bytes.
    pub size_bytes: i64,
    /// Archive completion epoch seconds.
    pub archived_at: i64,
    /// Source folder class.
    pub folder_class: String,
}

/// One recovery row over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecoveryRowWire {
    /// Archive item id.
    pub id: i64,
    /// Current delete-state string.
    pub delete_state: String,
    /// Archive-root-relative item path.
    pub path: String,
    /// Archive bytes.
    pub size_bytes: i64,
    /// Delete generation token (hex) when present.
    pub delete_gen: Option<String>,
}

/// One claimed internal archive-delete work item over the wire.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArchiveDeleteClaimedWire {
    /// Stable durable job id.
    pub job_id: String,
    /// Logical request id.
    pub request_id: String,
    /// Target archive item id.
    pub target_archive_item_id: i64,
    /// Target archive relative path fence.
    pub target_archive_path: String,
    /// Target archive size fence.
    pub target_archive_size_bytes: i64,
    /// Persisted delete generation token from claim.
    pub delete_gen: String,
}

/// Unix-socket transport for retentiond delete-path RPC verbs.
pub struct IndexDeleteClient {
    socket_path: PathBuf,
    recency_floor_epoch: Cell<i64>,
    allow_undurable: Cell<bool>,
}

impl IndexDeleteClient {
    /// Build a client with fail-closed defaults.
    #[must_use]
    pub fn new(socket_path: impl Into<PathBuf>) -> Self {
        Self {
            socket_path: socket_path.into(),
            recency_floor_epoch: Cell::new(i64::MAX),
            allow_undurable: Cell::new(false),
        }
    }

    /// Set one delete-cycle context shared by list + claim requests.
    pub fn set_cycle_context(&self, recency_floor_epoch: i64, allow_undurable: bool) {
        self.recency_floor_epoch.set(recency_floor_epoch);
        self.allow_undurable.set(allow_undurable);
    }

    /// Current cycle recency floor.
    #[must_use]
    pub fn recency_floor_epoch(&self) -> i64 {
        self.recency_floor_epoch.get()
    }

    /// Current cycle allow-undurable flag.
    #[must_use]
    pub fn allow_undurable(&self) -> bool {
        self.allow_undurable.get()
    }

    fn send(&self, req: &DeleteWireRequest) -> io::Result<DeleteWireResponse> {
        let payload = serde_json::to_vec(req).map_err(io::Error::other)?;
        let mut stream = connect_indexd(&self.socket_path)?;
        write_frame(&mut stream, &payload, MAX_REQUEST_FRAME)
            .map_err(|err| register_error_to_io(&err))?;
        let response_payload =
            read_frame(&mut stream, MAX_REQUEST_FRAME).map_err(|err| register_error_to_io(&err))?;
        serde_json::from_slice(&response_payload).map_err(io::Error::other)
    }

    /// List oldest-first eviction candidates for the currently configured cycle.
    ///
    /// # Errors
    ///
    /// Returns an error on transport, decode, or unexpected response status.
    pub fn list_eviction_candidates(&self) -> io::Result<Vec<EvictionCandidateWire>> {
        let req = DeleteWireRequest::ListEvictionCandidates {
            recency_floor_epoch: self.recency_floor_epoch.get(),
            allow_undurable: self.allow_undurable.get(),
            limit: 256,
        };
        match self.send(&req)? {
            DeleteWireResponse::EvictionCandidates { items } => Ok(items),
            DeleteWireResponse::Error { message } => Err(io::Error::other(format!(
                "indexd list_eviction_candidates error: {message}"
            ))),
            DeleteWireResponse::Rejected { message } => Err(io::Error::other(format!(
                "indexd list_eviction_candidates rejected: {message}"
            ))),
            other => Err(io::Error::other(format!(
                "unexpected list_eviction_candidates response: {other:?}"
            ))),
        }
    }

    /// List delete-state recovery rows.
    ///
    /// # Errors
    ///
    /// Returns an error on transport, decode, or unexpected response status.
    pub fn list_recovery_rows(&self) -> io::Result<Vec<RecoveryRowWire>> {
        match self.send(&DeleteWireRequest::ListRecoveryRows {})? {
            DeleteWireResponse::RecoveryRows { rows } => Ok(rows),
            DeleteWireResponse::Error { message } => Err(io::Error::other(format!(
                "indexd list_recovery_rows error: {message}"
            ))),
            DeleteWireResponse::Rejected { message } => Err(io::Error::other(format!(
                "indexd list_recovery_rows rejected: {message}"
            ))),
            other => Err(io::Error::other(format!(
                "unexpected list_recovery_rows response: {other:?}"
            ))),
        }
    }

    /// Claim the oldest claimable queued internal archive-delete request.
    ///
    /// # Errors
    ///
    /// Returns an error on transport, decode, or unexpected response status.
    pub fn claim_next_archive_delete(&self) -> io::Result<Option<ArchiveDeleteClaimedWire>> {
        match self.send(&DeleteWireRequest::ArchiveDeleteClaimNext {})? {
            DeleteWireResponse::ArchiveDeleteClaimed {
                job_id,
                request_id,
                target_archive_item_id,
                target_archive_path,
                target_archive_size_bytes,
                delete_gen,
            } => Ok(Some(ArchiveDeleteClaimedWire {
                job_id,
                request_id,
                target_archive_item_id,
                target_archive_path,
                target_archive_size_bytes,
                delete_gen,
            })),
            DeleteWireResponse::NotFound {} => Ok(None),
            DeleteWireResponse::Error { message } => Err(io::Error::other(format!(
                "indexd archive_delete_claim_next error: {message}"
            ))),
            DeleteWireResponse::Rejected { message } => Err(io::Error::other(format!(
                "indexd archive_delete_claim_next rejected: {message}"
            ))),
            other => Err(io::Error::other(format!(
                "unexpected archive_delete_claim_next response: {other:?}"
            ))),
        }
    }

    /// Mark one claimed internal archive-delete request done.
    ///
    /// # Errors
    ///
    /// Returns an error on transport, decode, or non-ack response.
    pub fn complete_archive_delete(&self, job_id: &str, owned_delete_gen: &str) -> io::Result<()> {
        match self.send(&DeleteWireRequest::ArchiveDeleteComplete {
            job_id: job_id.to_owned(),
            owned_delete_gen: owned_delete_gen.to_owned(),
        })? {
            DeleteWireResponse::Acked {} => Ok(()),
            DeleteWireResponse::Error { message } => Err(io::Error::other(format!(
                "indexd archive_delete_complete error: {message}"
            ))),
            DeleteWireResponse::Rejected { message } => Err(io::Error::other(format!(
                "indexd archive_delete_complete rejected: {message}"
            ))),
            other => Err(io::Error::other(format!(
                "unexpected archive_delete_complete response: {other:?}"
            ))),
        }
    }

    /// Mark one claimed internal archive-delete request failed/requeued.
    ///
    /// # Errors
    ///
    /// Returns an error on transport, decode, or non-ack response.
    pub fn fail_archive_delete(
        &self,
        job_id: &str,
        owned_delete_gen: &str,
        detail: &str,
        requeue: bool,
    ) -> io::Result<()> {
        match self.send(&DeleteWireRequest::ArchiveDeleteFail {
            job_id: job_id.to_owned(),
            owned_delete_gen: owned_delete_gen.to_owned(),
            detail: detail.to_owned(),
            requeue,
        })? {
            DeleteWireResponse::Acked {} => Ok(()),
            DeleteWireResponse::Error { message } => Err(io::Error::other(format!(
                "indexd archive_delete_fail error: {message}"
            ))),
            DeleteWireResponse::Rejected { message } => Err(io::Error::other(format!(
                "indexd archive_delete_fail rejected: {message}"
            ))),
            other => Err(io::Error::other(format!(
                "unexpected archive_delete_fail response: {other:?}"
            ))),
        }
    }

    /// Send one delete-path request.
    ///
    /// # Errors
    ///
    /// Returns transport/decode errors from the RPC exchange.
    pub fn send_delete_request(&self, req: &DeleteWireRequest) -> io::Result<DeleteWireResponse> {
        self.send(req)
    }
}

#[cfg(unix)]
fn connect_indexd(socket_path: &PathBuf) -> io::Result<std::os::unix::net::UnixStream> {
    use std::os::unix::net::UnixStream;
    use std::time::Duration;

    let stream = UnixStream::connect(socket_path)?;
    let timeout = Duration::from_secs(IO_TIMEOUT_SECS);
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    Ok(stream)
}

#[cfg(not(unix))]
fn connect_indexd(_socket_path: &PathBuf) -> io::Result<std::fs::File> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "indexd unix-socket transport requires unix",
    ))
}

fn register_error_to_io(err: &RegisterError) -> io::Error {
    io::Error::other(err.to_string())
}

#[cfg(all(test, unix))]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        clippy::panic,
        clippy::indexing_slicing
    )]

    use std::fs;
    use std::os::unix::net::UnixListener;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::thread;

    use super::{DeleteWireResponse, EvictionCandidateWire, IndexDeleteClient, RecoveryRowWire};
    use crate::register_client::{MAX_REQUEST_FRAME, read_frame, write_frame};

    static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn new_temp_dir() -> std::path::PathBuf {
        let unique = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let name = format!("retentiond-index-delete-{}-{unique}", std::process::id());
        let dir = std::env::temp_dir().join(name);
        fs::create_dir_all(&dir).expect("create temp dir");
        dir
    }

    #[test]
    fn list_eviction_candidates_sends_golden_json() {
        let temp_dir = new_temp_dir();
        let socket_path = temp_dir.join("indexd.sock");
        let listener = UnixListener::bind(&socket_path).expect("bind listener");

        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
            let request_json = String::from_utf8(payload).expect("utf8 request");
            assert_eq!(
                request_json,
                "{\"cmd\":\"list_eviction_candidates\",\"recency_floor_epoch\":12345,\"allow_undurable\":true,\"limit\":256}"
            );

            let payload = serde_json::to_vec(&DeleteWireResponse::EvictionCandidates {
                items: vec![EvictionCandidateWire {
                    id: 7,
                    path: "RecentClips/2026-07-01/clip".to_owned(),
                    size_bytes: 100,
                    archived_at: 200,
                    folder_class: "RecentClips".to_owned(),
                }],
            })
            .expect("encode response");
            write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
        });

        let client = IndexDeleteClient::new(socket_path);
        client.set_cycle_context(12345, true);
        let rows = client
            .list_eviction_candidates()
            .expect("list eviction candidates");
        assert_eq!(rows.len(), 1);
        assert_eq!(rows.first().expect("row exists").id, 7);
        server.join().expect("server join");
        let _ = fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn list_recovery_rows_sends_golden_json() {
        let temp_dir = new_temp_dir();
        let socket_path = temp_dir.join("indexd.sock");
        let listener = UnixListener::bind(&socket_path).expect("bind listener");

        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
            let request_json = String::from_utf8(payload).expect("utf8 request");
            assert_eq!(request_json, "{\"cmd\":\"list_recovery_rows\"}");

            let payload = serde_json::to_vec(&DeleteWireResponse::RecoveryRows {
                rows: vec![RecoveryRowWire {
                    id: 9,
                    delete_state: "DELETING".to_owned(),
                    path: "RecentClips/2026-07-01/clip".to_owned(),
                    size_bytes: 33,
                    delete_gen: Some("0000000000000000000000000000000f".to_owned()),
                }],
            })
            .expect("encode response");
            write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
        });

        let client = IndexDeleteClient::new(socket_path);
        let rows = client.list_recovery_rows().expect("list recovery rows");
        assert_eq!(rows.len(), 1);
        assert_eq!(rows.first().expect("row exists").id, 9);
        server.join().expect("server join");
        let _ = fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn claim_next_archive_delete_sends_golden_json_and_maps_payload() {
        let temp_dir = new_temp_dir();
        let socket_path = temp_dir.join("indexd.sock");
        let listener = UnixListener::bind(&socket_path).expect("bind listener");

        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
            let request_json = String::from_utf8(payload).expect("utf8 request");
            assert_eq!(request_json, "{\"cmd\":\"archive_delete_claim_next\"}");

            let payload = serde_json::to_vec(&DeleteWireResponse::ArchiveDeleteClaimed {
                job_id: "m-9001".to_owned(),
                request_id: "req-9001".to_owned(),
                target_archive_item_id: 9,
                target_archive_path: "archive/manual-delete/item-a".to_owned(),
                target_archive_size_bytes: 123,
                delete_gen: "abcdefabcdefabcdefabcdefabcdefab".to_owned(),
            })
            .expect("encode response");
            write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
        });

        let client = IndexDeleteClient::new(socket_path);
        let claimed = client
            .claim_next_archive_delete()
            .expect("claim next")
            .expect("work item");
        assert_eq!(claimed.job_id, "m-9001");
        assert_eq!(claimed.request_id, "req-9001");
        assert_eq!(claimed.target_archive_item_id, 9);
        assert_eq!(claimed.target_archive_path, "archive/manual-delete/item-a");
        assert_eq!(claimed.target_archive_size_bytes, 123);
        assert_eq!(claimed.delete_gen, "abcdefabcdefabcdefabcdefabcdefab");
        server.join().expect("server join");
        let _ = fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn claim_next_archive_delete_maps_not_found_to_none() {
        let temp_dir = new_temp_dir();
        let socket_path = temp_dir.join("indexd.sock");
        let listener = UnixListener::bind(&socket_path).expect("bind listener");

        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let _payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
            let payload = serde_json::to_vec(&DeleteWireResponse::NotFound {}).expect("encode");
            write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
        });

        let client = IndexDeleteClient::new(socket_path);
        let claimed = client.claim_next_archive_delete().expect("claim next");
        assert!(claimed.is_none());
        server.join().expect("server join");
        let _ = fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn complete_archive_delete_sends_golden_json() {
        let temp_dir = new_temp_dir();
        let socket_path = temp_dir.join("indexd.sock");
        let listener = UnixListener::bind(&socket_path).expect("bind listener");

        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
            let request_json = String::from_utf8(payload).expect("utf8 request");
            assert_eq!(
                request_json,
                "{\"cmd\":\"archive_delete_complete\",\"job_id\":\"m-9002\",\"owned_delete_gen\":\"abcdefabcdefabcdefabcdefabcdefab\"}"
            );

            let payload = serde_json::to_vec(&DeleteWireResponse::Acked {}).expect("encode");
            write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
        });

        let client = IndexDeleteClient::new(socket_path);
        client
            .complete_archive_delete("m-9002", "abcdefabcdefabcdefabcdefabcdefab")
            .expect("complete archive delete");
        server.join().expect("server join");
        let _ = fs::remove_dir_all(temp_dir);
    }

    #[test]
    fn fail_archive_delete_sends_golden_json() {
        let temp_dir = new_temp_dir();
        let socket_path = temp_dir.join("indexd.sock");
        let listener = UnixListener::bind(&socket_path).expect("bind listener");

        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
            let request_json = String::from_utf8(payload).expect("utf8 request");
            assert_eq!(
                request_json,
                "{\"cmd\":\"archive_delete_fail\",\"job_id\":\"m-9003\",\"owned_delete_gen\":\"abcdefabcdefabcdefabcdefabcdefab\",\"detail\":\"fsync source parent failed\",\"requeue\":false}"
            );

            let payload = serde_json::to_vec(&DeleteWireResponse::Acked {}).expect("encode");
            write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
        });

        let client = IndexDeleteClient::new(socket_path);
        client
            .fail_archive_delete(
                "m-9003",
                "abcdefabcdefabcdefabcdefabcdefab",
                "fsync source parent failed",
                false,
            )
            .expect("fail archive delete");
        server.join().expect("server join");
        let _ = fs::remove_dir_all(temp_dir);
    }
}
