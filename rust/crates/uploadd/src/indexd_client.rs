//! `uploadd` client-side transport for `indexd` cloud RPCs.
//!
//! The wire contract mirrors `indexd::proto` but remains crate-local so
//! `uploadd` and `indexd` stay decoupled.

use std::io::{self, Read, Write};

use serde::{Deserialize, Serialize};

/// `indexd` cloud-control Unix socket path.
pub const INDEXD_SOCKET_PATH: &str = "/run/teslausb/indexd.sock";

/// Maximum accepted request/response frame length in bytes.
pub const MAX_REQUEST_FRAME: u32 = 64 * 1024;

/// Stable paginated page result.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Page<T> {
    /// Returned rows.
    pub items: Vec<T>,
    /// Opaque cursor for the next page.
    pub next_cursor: Option<String>,
}

/// One `cloud_queue_upsert` payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
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

/// Queue primary key.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudQueuePk {
    /// Remote destination identity.
    pub destination_id: String,
    /// Canonical remote key.
    pub remote_key: String,
}

/// One `cloud_upload_commit` payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudQueueCommitRequest {
    /// Queue primary key.
    pub queue_pk: CloudQueuePk,
    /// Idempotency key for this transfer attempt.
    pub attempt_id: String,
    /// Optional sealed upload-set fence.
    #[serde(default)]
    pub upload_set_id: Option<String>,
    /// Backend verification hash.
    pub hash: String,
    /// Hash algorithm.
    pub hash_alg: String,
    /// Uploaded bytes.
    pub size: i64,
}

/// One `cloud_upload_commit` response payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudQueueCommitResult {
    /// Commit success.
    pub ok: bool,
    /// Parent became durable.
    pub durable_parent: bool,
}

/// One `upload_lease_acquire` response payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UploadLeaseAcquireResult {
    /// Lease granted.
    pub granted: bool,
    /// Lease token.
    pub token: Option<String>,
    /// Lease boot id.
    pub boot_id: Option<String>,
    /// Monotonic expiry.
    pub expires_mono_ms: Option<i64>,
}

/// One `upload_lease_renew` response payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UploadLeaseRenewResult {
    /// Renew success.
    pub ok: bool,
    /// New expiry.
    pub expires_mono_ms: Option<i64>,
}

/// One `upload_lease_release` response payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UploadLeaseReleaseResult {
    /// Release success.
    pub ok: bool,
}

/// One `cloud_upload_fail` payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudQueueFailRequest {
    /// Queue primary key.
    pub queue_pk: CloudQueuePk,
    /// Idempotency key for this transfer attempt.
    pub attempt_id: String,
    /// Optional sealed upload-set fence.
    #[serde(default)]
    pub upload_set_id: Option<String>,
    /// Sanitized error class.
    pub error_class: String,
    /// Retry gate (unix seconds), null = immediate retry.
    pub not_before: Option<i64>,
    /// Terminal failure marker.
    pub terminal: bool,
}

/// One `cloud_upload_fail` response payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudQueueFailResult {
    /// Fail record success.
    pub ok: bool,
    /// Resulting queue state.
    pub state: String,
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

/// One `cloud_queue_retry` payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloudQueueRetryRequest {
    /// Parent archive item id.
    pub archive_item_id: i64,
    /// Optional child discriminator.
    pub child_key: Option<String>,
    /// Optional sealed upload-set fence.
    #[serde(default)]
    pub upload_set_id: Option<String>,
    /// Resolution mode.
    pub resolution: CloudQueueRetryResolution,
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
    #[serde(default)]
    pub upload_set_id: Option<String>,
}

/// Failures while sending/receiving cloud RPCs.
#[derive(Debug, thiserror::Error)]
pub enum IndexdClientError {
    /// Transport/framing I/O failure.
    #[error("i/o error: {0}")]
    Io(#[from] io::Error),
    /// Server-reported command failure.
    #[error("indexd command failed: {message}")]
    Server {
        /// Human-readable server message.
        message: String,
    },
    /// Deterministic server rejection: retrying is futile.
    #[error("indexd rejected payload: {message}")]
    Rejected {
        /// Human-readable rejection reason.
        message: String,
    },
    /// Received or attempted frame exceeded max size.
    #[error("frame too large: {len} > {cap} bytes")]
    FrameTooLarge {
        /// Observed frame length in bytes.
        len: usize,
        /// Maximum accepted frame length in bytes.
        cap: usize,
    },
    /// JSON decode/encode or semantic parse error.
    #[error("decode error: {0}")]
    Decode(String),
}

/// `indexd` cloud RPC client seam for `uploadd`.
pub trait IndexdCloudClient {
    /// Request one discover page.
    ///
    /// # Errors
    ///
    /// Returns [`IndexdClientError`] on transport, framing, decode, or
    /// server-reported failures.
    fn cloud_discover(
        &self,
        after_cursor: Option<String>,
        limit: u32,
    ) -> Result<Page<CloudDiscoverRow>, IndexdClientError>;

    /// Upsert one queue row and return resulting state.
    ///
    /// # Errors
    ///
    /// Returns [`IndexdClientError`] on transport, framing, decode, or
    /// server-reported failures.
    fn cloud_queue_upsert(
        &self,
        item: &CloudQueueUpsertItem,
    ) -> Result<String, IndexdClientError>;

    /// Request one queue page.
    ///
    /// # Errors
    ///
    /// Returns [`IndexdClientError`] on transport, framing, decode, or
    /// server-reported failures.
    fn cloud_queue_load(
        &self,
        after_cursor: Option<String>,
        limit: u32,
        upload_set_id: Option<String>,
    ) -> Result<Page<CloudQueueRow>, IndexdClientError>;

    /// Commit one successful upload and return commit result.
    ///
    /// # Errors
    ///
    /// Returns [`IndexdClientError`] on transport, framing, decode, or
    /// server-reported failures.
    fn cloud_queue_commit(
        &self,
        request: &CloudQueueCommitRequest,
    ) -> Result<CloudQueueCommitResult, IndexdClientError>;

    /// Apply one queue retry operation and return resulting state.
    ///
    /// # Errors
    ///
    /// Returns [`IndexdClientError`] on transport, framing, decode, or
    /// server-reported failures.
    fn cloud_queue_retry(
        &self,
        request: &CloudQueueRetryRequest,
    ) -> Result<String, IndexdClientError>;

    /// Acquire an upload lease token.
    ///
    /// # Errors
    ///
    /// Returns [`IndexdClientError`] on transport, framing, decode, or
    /// server-reported failures.
    fn upload_lease_acquire(
        &self,
        archive_item_id: i64,
        ttl_ms: u32,
    ) -> Result<UploadLeaseAcquireResult, IndexdClientError>;

    /// Renew an upload lease token.
    ///
    /// # Errors
    ///
    /// Returns [`IndexdClientError`] on transport, framing, decode, or
    /// server-reported failures.
    fn upload_lease_renew(
        &self,
        token: &str,
        ttl_ms: u32,
    ) -> Result<UploadLeaseRenewResult, IndexdClientError>;

    /// Release an upload lease token.
    ///
    /// # Errors
    ///
    /// Returns [`IndexdClientError`] on transport, framing, decode, or
    /// server-reported failures.
    fn upload_lease_release(&self, token: &str) -> Result<UploadLeaseReleaseResult, IndexdClientError>;

    /// Record one failed upload attempt and return resulting state.
    ///
    /// # Errors
    ///
    /// Returns [`IndexdClientError`] on transport, framing, decode, or
    /// server-reported failures.
    fn cloud_upload_fail(
        &self,
        request: &CloudQueueFailRequest,
    ) -> Result<CloudQueueFailResult, IndexdClientError>;

    /// Request one candidates page.
    ///
    /// # Errors
    ///
    /// Returns [`IndexdClientError`] on transport, framing, decode, or
    /// server-reported failures.
    fn cloud_candidates(
        &self,
        folders: &[String],
        after_cursor: Option<String>,
        limit: u32,
    ) -> Result<Page<CloudCandidateRow>, IndexdClientError>;
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
#[allow(clippy::enum_variant_names)]
enum WireRequest {
    CloudDiscover {
        after_cursor: Option<String>,
        limit: u32,
    },
    CloudQueueUpsert {
        item: CloudQueueUpsertItem,
    },
    CloudQueueLoad {
        after_cursor: Option<String>,
        limit: u32,
        #[serde(default)]
        upload_set_id: Option<String>,
    },
    CloudUploadCommit {
        queue_pk: CloudQueuePk,
        attempt_id: String,
        #[serde(default)]
        upload_set_id: Option<String>,
        hash: String,
        hash_alg: String,
        size: i64,
    },
    CloudQueueRetry {
        archive_item_id: i64,
        child_key: Option<String>,
        #[serde(default)]
        upload_set_id: Option<String>,
        resolution: CloudQueueRetryResolution,
    },
    UploadLeaseAcquire {
        archive_item_id: i64,
        ttl_ms: u32,
    },
    UploadLeaseRenew {
        token: String,
        ttl_ms: u32,
    },
    UploadLeaseRelease {
        token: String,
    },
    CloudUploadFail {
        queue_pk: CloudQueuePk,
        attempt_id: String,
        #[serde(default)]
        upload_set_id: Option<String>,
        error_class: String,
        not_before: Option<i64>,
        terminal: bool,
    },
    CloudCandidates {
        folders: Vec<String>,
        after_cursor: Option<String>,
        limit: u32,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
enum WireResponse {
    CloudDiscoverPage {
        items: Vec<CloudDiscoverRow>,
        next_cursor: Option<String>,
    },
    CloudQueueState {
        state: String,
    },
    CloudQueuePage {
        items: Vec<CloudQueueRow>,
        next_cursor: Option<String>,
    },
    CloudUploadCommitted {
        ok: bool,
        durable_parent: bool,
    },
    UploadLeaseAcquired {
        granted: bool,
        token: Option<String>,
        boot_id: Option<String>,
        expires_mono_ms: Option<i64>,
    },
    UploadLeaseRenewed {
        ok: bool,
        expires_mono_ms: Option<i64>,
    },
    UploadLeaseReleased {
        ok: bool,
    },
    CloudUploadFailed {
        ok: bool,
        state: String,
    },
    CloudCandidates {
        items: Vec<CloudCandidateRow>,
        next_cursor: Option<String>,
    },
    Error {
        message: String,
    },
    Rejected {
        message: String,
    },
}

fn frame_cap_usize(cap: u32) -> Result<usize, IndexdClientError> {
    usize::try_from(cap).map_err(|_| IndexdClientError::Decode("frame cap overflow".to_owned()))
}

/// Read one framed payload (4-byte little-endian length + JSON payload).
///
/// # Errors
///
/// Returns [`IndexdClientError`] when I/O fails, the frame is torn, or its
/// length exceeds `cap`.
pub(crate) fn read_frame(
    stream: &mut impl Read,
    cap: u32,
) -> Result<Vec<u8>, IndexdClientError> {
    let mut len_buf = [0_u8; 4];
    stream.read_exact(&mut len_buf)?;
    let len_u32 = u32::from_le_bytes(len_buf);
    let len = usize::try_from(len_u32)
        .map_err(|_| IndexdClientError::Decode("frame length overflow".to_owned()))?;
    let cap_len = frame_cap_usize(cap)?;
    if len > cap_len {
        return Err(IndexdClientError::FrameTooLarge { len, cap: cap_len });
    }

    let mut payload = vec![0_u8; len];
    stream.read_exact(&mut payload)?;
    Ok(payload)
}

/// Write one framed payload (4-byte little-endian length + JSON payload).
///
/// # Errors
///
/// Returns [`IndexdClientError`] when framing bounds are exceeded or write fails.
pub(crate) fn write_frame(
    stream: &mut impl Write,
    payload: &[u8],
    cap: u32,
) -> Result<(), IndexdClientError> {
    let cap_len = frame_cap_usize(cap)?;
    if payload.len() > cap_len {
        return Err(IndexdClientError::FrameTooLarge {
            len: payload.len(),
            cap: cap_len,
        });
    }

    let len_u32 = u32::try_from(payload.len()).map_err(|_| IndexdClientError::FrameTooLarge {
        len: payload.len(),
        cap: cap_len,
    })?;
    stream.write_all(&len_u32.to_le_bytes())?;
    stream.write_all(payload)?;
    stream.flush()?;
    Ok(())
}

fn decode_response_payload(payload: &[u8]) -> Result<WireResponse, IndexdClientError> {
    let response: WireResponse = serde_json::from_slice(payload)
        .map_err(|err| IndexdClientError::Decode(err.to_string()))?;
    match response {
        WireResponse::Error { message } => Err(IndexdClientError::Server { message }),
        WireResponse::Rejected { message } => Err(IndexdClientError::Rejected { message }),
        other => Ok(other),
    }
}

fn encode_wire_request_frame(request: &WireRequest) -> Result<Vec<u8>, IndexdClientError> {
    let payload =
        serde_json::to_vec(request).map_err(|err| IndexdClientError::Decode(err.to_string()))?;
    let mut framed = Vec::with_capacity(payload.len() + 4);
    write_frame(&mut framed, &payload, MAX_REQUEST_FRAME)?;
    Ok(framed)
}

#[cfg(test)]
fn decode_response_frame(frame: &[u8]) -> Result<WireResponse, IndexdClientError> {
    let mut cursor = std::io::Cursor::new(frame);
    let payload = read_frame(&mut cursor, MAX_REQUEST_FRAME)?;
    let consumed = usize::try_from(cursor.position())
        .map_err(|_| IndexdClientError::Decode("cursor position overflow".to_owned()))?;
    if consumed != frame.len() {
        return Err(IndexdClientError::Decode(
            "trailing bytes after response frame".to_owned(),
        ));
    }
    decode_response_payload(&payload)
}

fn decode_page_discover(response: WireResponse) -> Result<Page<CloudDiscoverRow>, IndexdClientError> {
    match response {
        WireResponse::CloudDiscoverPage { items, next_cursor } => Ok(Page { items, next_cursor }),
        other => Err(IndexdClientError::Decode(format!(
            "unexpected response status: {}",
            wire_response_status(&other)
        ))),
    }
}

fn decode_queue_state(response: WireResponse) -> Result<String, IndexdClientError> {
    match response {
        WireResponse::CloudQueueState { state } => Ok(state),
        other => Err(IndexdClientError::Decode(format!(
            "unexpected response status: {}",
            wire_response_status(&other)
        ))),
    }
}

fn decode_queue_page(response: WireResponse) -> Result<Page<CloudQueueRow>, IndexdClientError> {
    match response {
        WireResponse::CloudQueuePage { items, next_cursor } => Ok(Page { items, next_cursor }),
        other => Err(IndexdClientError::Decode(format!(
            "unexpected response status: {}",
            wire_response_status(&other)
        ))),
    }
}

fn decode_upload_committed(
    response: WireResponse,
) -> Result<CloudQueueCommitResult, IndexdClientError> {
    match response {
        WireResponse::CloudUploadCommitted { ok, durable_parent } => {
            Ok(CloudQueueCommitResult { ok, durable_parent })
        }
        other => Err(IndexdClientError::Decode(format!(
            "unexpected response status: {}",
            wire_response_status(&other)
        ))),
    }
}

fn decode_candidates_page(response: WireResponse) -> Result<Page<CloudCandidateRow>, IndexdClientError> {
    match response {
        WireResponse::CloudCandidates { items, next_cursor } => Ok(Page { items, next_cursor }),
        other => Err(IndexdClientError::Decode(format!(
            "unexpected response status: {}",
            wire_response_status(&other)
        ))),
    }
}

fn decode_upload_lease_acquired(
    response: WireResponse,
) -> Result<UploadLeaseAcquireResult, IndexdClientError> {
    match response {
        WireResponse::UploadLeaseAcquired {
            granted,
            token,
            boot_id,
            expires_mono_ms,
        } => Ok(UploadLeaseAcquireResult {
            granted,
            token,
            boot_id,
            expires_mono_ms,
        }),
        other => Err(IndexdClientError::Decode(format!(
            "unexpected response status: {}",
            wire_response_status(&other)
        ))),
    }
}

fn decode_upload_lease_renewed(
    response: WireResponse,
) -> Result<UploadLeaseRenewResult, IndexdClientError> {
    match response {
        WireResponse::UploadLeaseRenewed { ok, expires_mono_ms } => {
            Ok(UploadLeaseRenewResult { ok, expires_mono_ms })
        }
        other => Err(IndexdClientError::Decode(format!(
            "unexpected response status: {}",
            wire_response_status(&other)
        ))),
    }
}

fn decode_upload_lease_released(
    response: WireResponse,
) -> Result<UploadLeaseReleaseResult, IndexdClientError> {
    match response {
        WireResponse::UploadLeaseReleased { ok } => Ok(UploadLeaseReleaseResult { ok }),
        other => Err(IndexdClientError::Decode(format!(
            "unexpected response status: {}",
            wire_response_status(&other)
        ))),
    }
}

fn decode_upload_failed(response: WireResponse) -> Result<CloudQueueFailResult, IndexdClientError> {
    match response {
        WireResponse::CloudUploadFailed { ok, state } => Ok(CloudQueueFailResult { ok, state }),
        other => Err(IndexdClientError::Decode(format!(
            "unexpected response status: {}",
            wire_response_status(&other)
        ))),
    }
}

fn wire_response_status(response: &WireResponse) -> &'static str {
    match response {
        WireResponse::CloudDiscoverPage { .. } => "cloud_discover_page",
        WireResponse::CloudQueueState { .. } => "cloud_queue_state",
        WireResponse::CloudQueuePage { .. } => "cloud_queue_page",
        WireResponse::CloudUploadCommitted { .. } => "cloud_upload_committed",
        WireResponse::UploadLeaseAcquired { .. } => "upload_lease_acquired",
        WireResponse::UploadLeaseRenewed { .. } => "upload_lease_renewed",
        WireResponse::UploadLeaseReleased { .. } => "upload_lease_released",
        WireResponse::CloudUploadFailed { .. } => "cloud_upload_failed",
        WireResponse::CloudCandidates { .. } => "cloud_candidates",
        WireResponse::Error { .. } => "error",
        WireResponse::Rejected { .. } => "rejected",
    }
}

#[cfg(unix)]
/// Per-request socket I/O timeout for `indexd` cloud RPCs.
pub(crate) const IO_TIMEOUT_SECS: u64 = 5;

/// Live Unix-domain-socket `indexd` cloud client.
#[cfg(unix)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnixIndexdClient {
    socket_path: std::path::PathBuf,
}

#[cfg(unix)]
impl UnixIndexdClient {
    /// Build a client that connects to `socket_path`.
    #[must_use]
    pub fn new(socket_path: impl Into<std::path::PathBuf>) -> Self {
        Self {
            socket_path: socket_path.into(),
        }
    }

    fn send_request(&self, request: &WireRequest) -> Result<WireResponse, IndexdClientError> {
        use std::os::unix::net::UnixStream;
        use std::time::Duration;

        let mut stream = UnixStream::connect(&self.socket_path)?;
        let timeout = Duration::from_secs(IO_TIMEOUT_SECS);
        stream.set_read_timeout(Some(timeout))?;
        stream.set_write_timeout(Some(timeout))?;

        let frame = encode_wire_request_frame(request)?;
        stream.write_all(&frame)?;
        stream.flush()?;

        let payload = read_frame(&mut stream, MAX_REQUEST_FRAME)?;
        decode_response_payload(&payload)
    }
}

#[cfg(unix)]
impl IndexdCloudClient for UnixIndexdClient {
    fn cloud_discover(
        &self,
        after_cursor: Option<String>,
        limit: u32,
    ) -> Result<Page<CloudDiscoverRow>, IndexdClientError> {
        let response = self.send_request(&WireRequest::CloudDiscover {
            after_cursor,
            limit,
        })?;
        decode_page_discover(response)
    }

    fn cloud_queue_upsert(
        &self,
        item: &CloudQueueUpsertItem,
    ) -> Result<String, IndexdClientError> {
        let response = self.send_request(&WireRequest::CloudQueueUpsert { item: item.clone() })?;
        decode_queue_state(response)
    }

    fn cloud_queue_load(
        &self,
        after_cursor: Option<String>,
        limit: u32,
        upload_set_id: Option<String>,
    ) -> Result<Page<CloudQueueRow>, IndexdClientError> {
        let response = self.send_request(&WireRequest::CloudQueueLoad {
            after_cursor,
            limit,
            upload_set_id,
        })?;
        decode_queue_page(response)
    }

    fn cloud_queue_commit(
        &self,
        request: &CloudQueueCommitRequest,
    ) -> Result<CloudQueueCommitResult, IndexdClientError> {
        let response = self.send_request(&WireRequest::CloudUploadCommit {
            queue_pk: request.queue_pk.clone(),
            attempt_id: request.attempt_id.clone(),
            upload_set_id: request.upload_set_id.clone(),
            hash: request.hash.clone(),
            hash_alg: request.hash_alg.clone(),
            size: request.size,
        })?;
        decode_upload_committed(response)
    }

    fn cloud_queue_retry(
        &self,
        request: &CloudQueueRetryRequest,
    ) -> Result<String, IndexdClientError> {
        let response = self.send_request(&WireRequest::CloudQueueRetry {
            archive_item_id: request.archive_item_id,
            child_key: request.child_key.clone(),
            upload_set_id: request.upload_set_id.clone(),
            resolution: request.resolution.clone(),
        })?;
        decode_queue_state(response)
    }

    fn upload_lease_acquire(
        &self,
        archive_item_id: i64,
        ttl_ms: u32,
    ) -> Result<UploadLeaseAcquireResult, IndexdClientError> {
        let response = self.send_request(&WireRequest::UploadLeaseAcquire {
            archive_item_id,
            ttl_ms,
        })?;
        decode_upload_lease_acquired(response)
    }

    fn upload_lease_renew(
        &self,
        token: &str,
        ttl_ms: u32,
    ) -> Result<UploadLeaseRenewResult, IndexdClientError> {
        let response = self.send_request(&WireRequest::UploadLeaseRenew {
            token: token.to_owned(),
            ttl_ms,
        })?;
        decode_upload_lease_renewed(response)
    }

    fn upload_lease_release(&self, token: &str) -> Result<UploadLeaseReleaseResult, IndexdClientError> {
        let response = self.send_request(&WireRequest::UploadLeaseRelease {
            token: token.to_owned(),
        })?;
        decode_upload_lease_released(response)
    }

    fn cloud_upload_fail(
        &self,
        request: &CloudQueueFailRequest,
    ) -> Result<CloudQueueFailResult, IndexdClientError> {
        let response = self.send_request(&WireRequest::CloudUploadFail {
            queue_pk: request.queue_pk.clone(),
            attempt_id: request.attempt_id.clone(),
            upload_set_id: request.upload_set_id.clone(),
            error_class: request.error_class.clone(),
            not_before: request.not_before,
            terminal: request.terminal,
        })?;
        decode_upload_failed(response)
    }

    fn cloud_candidates(
        &self,
        folders: &[String],
        after_cursor: Option<String>,
        limit: u32,
    ) -> Result<Page<CloudCandidateRow>, IndexdClientError> {
        let response = self.send_request(&WireRequest::CloudCandidates {
            folders: folders.to_vec(),
            after_cursor,
            limit,
        })?;
        decode_candidates_page(response)
    }
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        clippy::panic,
        clippy::indexing_slicing,
        clippy::too_many_lines
    )]

    use super::{
        CloudCandidateRow, CloudDiscoverRow, CloudQueueCommitRequest, CloudQueueCommitResult,
        CloudQueueFailRequest, CloudQueueFailResult, CloudQueuePk, CloudQueueRetryRequest,
        CloudQueueRetryResolution, CloudQueueRow, CloudQueueUpsertItem, IndexdClientError,
        MAX_REQUEST_FRAME, UploadLeaseAcquireResult, UploadLeaseReleaseResult, UploadLeaseRenewResult,
        WireRequest, WireResponse, decode_response_frame, decode_response_payload,
        encode_wire_request_frame, read_frame, write_frame,
    };
    use serde_json::json;
    use std::io::Cursor;

    fn sample_upsert() -> CloudQueueUpsertItem {
        CloudQueueUpsertItem {
            archive_item_id: 42,
            child_key: "front".to_owned(),
            destination_id: "dest-a".to_owned(),
            remote_key: "remote/front.mp4".to_owned(),
            category: "trip".to_owned(),
            seq: 7,
            total_bytes: 1024,
            content_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                .to_owned(),
            expected_hash: Some("etag-42".to_owned()),
            verify_alg: "etag".to_owned(),
        }
    }

    fn sample_commit_request() -> CloudQueueCommitRequest {
        CloudQueueCommitRequest {
            queue_pk: CloudQueuePk {
                destination_id: "dest-a".to_owned(),
                remote_key: "remote/front.mp4".to_owned(),
            },
            attempt_id: "attempt-1".to_owned(),
            upload_set_id: Some("11111111111111111111111111111111".to_owned()),
            hash: "etag-42".to_owned(),
            hash_alg: "etag".to_owned(),
            size: 1024,
        }
    }

    fn sample_retry_request() -> CloudQueueRetryRequest {
        CloudQueueRetryRequest {
            archive_item_id: 42,
            child_key: Some("front".to_owned()),
            upload_set_id: Some("11111111111111111111111111111111".to_owned()),
            resolution: CloudQueueRetryResolution::Replace,
        }
    }

    #[test]
    fn request_frames_encode_with_expected_cmd_and_shape() {
        let requests = vec![
            (
                WireRequest::CloudDiscover {
                    after_cursor: Some("opaque-1".to_owned()),
                    limit: 8,
                },
                "cloud_discover",
            ),
            (
                WireRequest::CloudQueueUpsert {
                    item: sample_upsert(),
                },
                "cloud_queue_upsert",
            ),
            (
                WireRequest::CloudQueueLoad {
                    after_cursor: Some("opaque-2".to_owned()),
                    limit: 9,
                    upload_set_id: None,
                },
                "cloud_queue_load",
            ),
            (
                WireRequest::CloudUploadCommit {
                    queue_pk: sample_commit_request().queue_pk,
                    attempt_id: "attempt-1".to_owned(),
                    upload_set_id: Some("11111111111111111111111111111111".to_owned()),
                    hash: "etag-42".to_owned(),
                    hash_alg: "etag".to_owned(),
                    size: 1024,
                },
                "cloud_upload_commit",
            ),
            (
                WireRequest::CloudQueueRetry {
                    archive_item_id: sample_retry_request().archive_item_id,
                    child_key: sample_retry_request().child_key,
                    upload_set_id: sample_retry_request().upload_set_id,
                    resolution: CloudQueueRetryResolution::Replace,
                },
                "cloud_queue_retry",
            ),
            (
                WireRequest::UploadLeaseAcquire {
                    archive_item_id: 42,
                    ttl_ms: 60_000,
                },
                "upload_lease_acquire",
            ),
            (
                WireRequest::UploadLeaseRenew {
                    token: "42:0123456789abcdef0123456789abcdef".to_owned(),
                    ttl_ms: 60_000,
                },
                "upload_lease_renew",
            ),
            (
                WireRequest::UploadLeaseRelease {
                    token: "42:0123456789abcdef0123456789abcdef".to_owned(),
                },
                "upload_lease_release",
            ),
            (
                WireRequest::CloudUploadFail {
                    queue_pk: CloudQueuePk {
                        destination_id: "dest-a".to_owned(),
                        remote_key: "remote/front.mp4".to_owned(),
                    },
                    attempt_id: "attempt-2".to_owned(),
                    upload_set_id: None,
                    error_class: "timeout".to_owned(),
                    not_before: Some(1234),
                    terminal: false,
                },
                "cloud_upload_fail",
            ),
            (
                WireRequest::CloudCandidates {
                    folders: vec!["RecentClips".to_owned(), "SavedClips".to_owned()],
                    after_cursor: Some("opaque-3".to_owned()),
                    limit: 10,
                },
                "cloud_candidates",
            ),
        ];

        for (request, expected_cmd) in requests {
            let frame = encode_wire_request_frame(&request).expect("encode request frame");
            let mut cursor = Cursor::new(frame.as_slice());
            let payload = read_frame(&mut cursor, MAX_REQUEST_FRAME).expect("read payload");
            assert_eq!(
                usize::try_from(cursor.position()).expect("cursor position to usize"),
                frame.len()
            );

            let value: serde_json::Value = serde_json::from_slice(&payload).expect("valid json");
            assert_eq!(value["cmd"], expected_cmd);
        }
    }

    #[test]
    fn response_decode_succeeds_for_each_cloud_verb() {
        let discover_payload = serde_json::to_vec(&WireResponse::CloudDiscoverPage {
            items: vec![CloudDiscoverRow {
                archive_item_id: 1,
                folder_class: "RecentClips".to_owned(),
                path: "archive/a".to_owned(),
                manifest_digest: Some("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".to_owned()),
                category: "trip".to_owned(),
            }],
            next_cursor: Some("opaque".to_owned()),
        })
        .expect("encode discover response");
        assert!(matches!(
            decode_response_payload(&discover_payload).expect("decode discover response"),
            WireResponse::CloudDiscoverPage { .. }
        ));

        let upsert_payload = serde_json::to_vec(&WireResponse::CloudQueueState {
            state: "queued".to_owned(),
        })
        .expect("encode queue state response");
        assert!(matches!(
            decode_response_payload(&upsert_payload).expect("decode upsert response"),
            WireResponse::CloudQueueState { .. }
        ));

        let load_payload = serde_json::to_vec(&WireResponse::CloudQueuePage {
            items: vec![CloudQueueRow {
                archive_item_id: 1,
                child_key: "front".to_owned(),
                destination_id: "dest-a".to_owned(),
                remote_key: "remote/front.mp4".to_owned(),
                category: "trip".to_owned(),
                seq: 1,
                total_bytes: 100,
                bytes_uploaded: 20,
                expected_hash: Some("etag-1".to_owned()),
                verify_alg: "etag".to_owned(),
                content_sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
                    .to_owned(),
                state: "queued".to_owned(),
                attempts: 1,
                not_before: None,
                last_error: None,
                upload_set_id: Some("22222222222222222222222222222222".to_owned()),
            }],
            next_cursor: None,
        })
        .expect("encode queue page response");
        assert!(matches!(
            decode_response_payload(&load_payload).expect("decode queue load response"),
            WireResponse::CloudQueuePage { .. }
        ));

        let commit_payload = serde_json::to_vec(&WireResponse::CloudUploadCommitted {
            ok: true,
            durable_parent: false,
        })
        .expect("encode commit response");
        let commit = decode_response_payload(&commit_payload).expect("decode queue commit response");
        assert_eq!(
            commit,
            WireResponse::CloudUploadCommitted {
                ok: true,
                durable_parent: false
            }
        );

        let retry_payload = serde_json::to_vec(&WireResponse::CloudQueueState {
            state: "failed".to_owned(),
        })
        .expect("encode retry response");
        assert!(matches!(
            decode_response_payload(&retry_payload).expect("decode queue retry response"),
            WireResponse::CloudQueueState { .. }
        ));

        let lease_acquire_payload = serde_json::to_vec(&WireResponse::UploadLeaseAcquired {
            granted: true,
            token: Some("42:0123456789abcdef0123456789abcdef".to_owned()),
            boot_id: Some("boot-1".to_owned()),
            expires_mono_ms: Some(1200),
        })
        .expect("encode lease acquire response");
        let lease_acquire = decode_response_payload(&lease_acquire_payload)
            .expect("decode lease acquire response");
        assert_eq!(
            lease_acquire,
            WireResponse::UploadLeaseAcquired {
                granted: true,
                token: Some("42:0123456789abcdef0123456789abcdef".to_owned()),
                boot_id: Some("boot-1".to_owned()),
                expires_mono_ms: Some(1200)
            }
        );

        let lease_renew_payload = serde_json::to_vec(&WireResponse::UploadLeaseRenewed {
            ok: true,
            expires_mono_ms: Some(1800),
        })
        .expect("encode lease renew response");
        assert!(matches!(
            decode_response_payload(&lease_renew_payload).expect("decode lease renew response"),
            WireResponse::UploadLeaseRenewed { .. }
        ));

        let lease_release_payload = serde_json::to_vec(&WireResponse::UploadLeaseReleased { ok: true })
            .expect("encode lease release response");
        assert!(matches!(
            decode_response_payload(&lease_release_payload).expect("decode lease release response"),
            WireResponse::UploadLeaseReleased { .. }
        ));

        let fail_payload = serde_json::to_vec(&WireResponse::CloudUploadFailed {
            ok: true,
            state: "failed".to_owned(),
        })
        .expect("encode upload fail response");
        assert!(matches!(
            decode_response_payload(&fail_payload).expect("decode upload fail response"),
            WireResponse::CloudUploadFailed { .. }
        ));

        let candidates_payload = serde_json::to_vec(&WireResponse::CloudCandidates {
            items: vec![CloudCandidateRow {
                archive_item_id: 1,
                child_key: "front".to_owned(),
                source_rel: "archive/front.mp4".to_owned(),
                destination_id: "dest-a".to_owned(),
                remote_key: "remote/front.mp4".to_owned(),
                size_bytes: 100,
                content_sha256:
                    "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
                        .to_owned(),
                state: "queued".to_owned(),
                category: "trip".to_owned(),
                seq: 1,
            }],
            next_cursor: Some("opaque-2".to_owned()),
        })
        .expect("encode candidates response");
        assert!(matches!(
            decode_response_payload(&candidates_payload).expect("decode candidates response"),
            WireResponse::CloudCandidates { .. }
        ));
    }

    #[test]
    fn decode_response_payload_maps_server_and_rejected_distinctly() {
        let server_payload = serde_json::to_vec(&WireResponse::Error {
            message: "db busy".to_owned(),
        })
        .expect("encode server response");
        let server_err = decode_response_payload(&server_payload).expect_err("error should map");
        match server_err {
            IndexdClientError::Server { message } => assert_eq!(message, "db busy"),
            other => panic!("unexpected error: {other:?}"),
        }

        let rejected_payload = serde_json::to_vec(&WireResponse::Rejected {
            message: "invalid cursor".to_owned(),
        })
        .expect("encode rejected response");
        let rejected_err =
            decode_response_payload(&rejected_payload).expect_err("rejected should map");
        match rejected_err {
            IndexdClientError::Rejected { message } => assert_eq!(message, "invalid cursor"),
            other => panic!("unexpected error: {other:?}"),
        }
    }

    #[test]
    fn oversize_frames_are_refused_in_both_directions() {
        let mut oversized = Vec::new();
        oversized.extend_from_slice(&(MAX_REQUEST_FRAME + 1).to_le_bytes());
        let read_err =
            decode_response_frame(&oversized).expect_err("oversize read frame should fail");
        assert!(matches!(read_err, IndexdClientError::FrameTooLarge { .. }));

        let payload = vec![0_u8; usize::try_from(MAX_REQUEST_FRAME).expect("cap to usize") + 1];
        let mut sink = Vec::new();
        let write_err =
            write_frame(&mut sink, &payload, MAX_REQUEST_FRAME).expect_err("oversize write frame");
        assert!(matches!(write_err, IndexdClientError::FrameTooLarge { .. }));
    }

    #[test]
    fn cloud_queue_load_upload_set_id_defaults_to_none_when_absent() {
        let payload = serde_json::to_vec(&json!({
            "status": "cloud_queue_page",
            "items": [{
                "archive_item_id": 1,
                "child_key": "child",
                "destination_id": "dest",
                "remote_key": "rk",
                "category": "bulk",
                "seq": 1,
                "total_bytes": 10,
                "bytes_uploaded": 0,
                "expected_hash": "etag",
                "verify_alg": "etag",
                "content_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "state": "queued",
                "attempts": 0,
                "not_before": null,
                "last_error": null
            }],
            "next_cursor": null
        }))
        .expect("encode queue page payload");

        let decoded = decode_response_payload(&payload).expect("decode queue page");
        match decoded {
            WireResponse::CloudQueuePage { items, next_cursor } => {
                assert_eq!(next_cursor, None);
                assert_eq!(items.len(), 1);
                assert_eq!(items[0].upload_set_id, None);
            }
            other => panic!("unexpected response: {other:?}"),
        }
    }

    #[test]
    fn decode_response_frame_cloud_upload_committed() {
        let mut frame = Vec::new();
        let payload = serde_json::to_vec(&WireResponse::CloudUploadCommitted {
            ok: true,
            durable_parent: true,
        })
        .expect("serialize response");
        write_frame(&mut frame, &payload, MAX_REQUEST_FRAME).expect("write frame");

        let decoded = decode_response_frame(&frame).expect("decode response");
        assert_eq!(
            decoded,
            WireResponse::CloudUploadCommitted {
                ok: true,
                durable_parent: true
            }
        );
    }

    #[test]
    fn cloud_queue_commit_result_roundtrip_shape() {
        let result = CloudQueueCommitResult {
            ok: true,
            durable_parent: false,
        };
        let decoded: CloudQueueCommitResult =
            serde_json::from_value(serde_json::to_value(&result).expect("to value"))
                .expect("from value");
        assert_eq!(decoded, result);
    }

    #[test]
    fn cloud_upload_fail_result_roundtrip_shape() {
        let result = CloudQueueFailResult {
            ok: true,
            state: "failed".to_owned(),
        };
        let decoded: CloudQueueFailResult =
            serde_json::from_value(serde_json::to_value(&result).expect("to value"))
                .expect("from value");
        assert_eq!(decoded, result);
    }

    #[test]
    fn upload_lease_result_shapes_roundtrip() {
        let acquired = UploadLeaseAcquireResult {
            granted: true,
            token: Some("42:0123456789abcdef0123456789abcdef".to_owned()),
            boot_id: Some("boot-1".to_owned()),
            expires_mono_ms: Some(1234),
        };
        let renewed = UploadLeaseRenewResult {
            ok: true,
            expires_mono_ms: Some(2345),
        };
        let released = UploadLeaseReleaseResult { ok: true };
        let fail = CloudQueueFailRequest {
            queue_pk: CloudQueuePk {
                destination_id: "dest-a".to_owned(),
                remote_key: "remote/front.mp4".to_owned(),
            },
            attempt_id: "attempt-2".to_owned(),
            upload_set_id: None,
            error_class: "timeout".to_owned(),
            not_before: Some(1234),
            terminal: false,
        };

        let acquired_rt: UploadLeaseAcquireResult =
            serde_json::from_value(serde_json::to_value(&acquired).expect("to value"))
                .expect("from value");
        let renewed_rt: UploadLeaseRenewResult =
            serde_json::from_value(serde_json::to_value(&renewed).expect("to value"))
                .expect("from value");
        let released_rt: UploadLeaseReleaseResult =
            serde_json::from_value(serde_json::to_value(&released).expect("to value"))
                .expect("from value");
        let fail_rt: CloudQueueFailRequest =
            serde_json::from_value(serde_json::to_value(&fail).expect("to value"))
                .expect("from value");
        assert_eq!(acquired_rt, acquired);
        assert_eq!(renewed_rt, renewed);
        assert_eq!(released_rt, released);
        assert_eq!(fail_rt, fail);
    }
}

#[cfg(all(test, unix))]
mod unix_tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        clippy::panic,
        clippy::indexing_slicing
    )]

    use super::{
        CloudCandidateRow, IndexdCloudClient, MAX_REQUEST_FRAME, UnixIndexdClient, WireRequest,
        WireResponse, read_frame, write_frame,
    };
    use std::fs;
    use std::os::unix::net::UnixListener;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::thread;

    static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn new_temp_dir() -> PathBuf {
        let unique = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let name = format!("uploadd-indexd-client-{}-{unique}", std::process::id());
        let dir = std::env::temp_dir().join(name);
        fs::create_dir_all(&dir).expect("create temp dir");
        dir
    }

    #[test]
    fn unix_indexd_client_roundtrip_cloud_candidates() {
        let temp_dir = new_temp_dir();
        let socket_path = temp_dir.join("indexd.sock");
        let listener = UnixListener::bind(&socket_path).expect("bind listener");

        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request frame");
            let request: WireRequest = serde_json::from_slice(&payload).expect("decode request");
            assert_eq!(
                request,
                WireRequest::CloudCandidates {
                    folders: vec!["RecentClips".to_owned()],
                    after_cursor: Some("cursor-a".to_owned()),
                    limit: 5
                }
            );

            let payload = serde_json::to_vec(&WireResponse::CloudCandidates {
                items: vec![CloudCandidateRow {
                    archive_item_id: 9,
                    child_key: "front".to_owned(),
                    source_rel: "archive/front.mp4".to_owned(),
                    destination_id: "dest-a".to_owned(),
                    remote_key: "remote/front.mp4".to_owned(),
                    size_bytes: 111,
                    content_sha256:
                        "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
                            .to_owned(),
                    state: "queued".to_owned(),
                    category: "trip".to_owned(),
                    seq: 4,
                }],
                next_cursor: Some("cursor-b".to_owned()),
            })
            .expect("encode response");
            write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
        });

        let client = UnixIndexdClient::new(socket_path);
        let got = client
            .cloud_candidates(&["RecentClips".to_owned()], Some("cursor-a".to_owned()), 5)
            .expect("cloud_candidates request");

        assert_eq!(got.items.len(), 1);
        assert_eq!(got.items[0].archive_item_id, 9);
        assert_eq!(got.next_cursor, Some("cursor-b".to_owned()));
        server.join().expect("server join");
        let _ = fs::remove_dir_all(temp_dir);
    }
}
