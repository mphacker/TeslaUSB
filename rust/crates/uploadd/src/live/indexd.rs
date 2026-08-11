//! Live `indexd` adapters for the queue and lease seams.

use std::str::FromStr;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU32, Ordering};

use crate::error::IndexError;
use crate::indexd_client::{
    CloudQueueCommitRequest, CloudQueueFailRequest, CloudQueueRow, IndexdClientError,
    IndexdCloudClient, UploadLeaseAcquireResult, UploadLeaseReleaseResult, UploadLeaseRenewResult,
};
use crate::lease::{
    LeaseClient, LeaseGen, LeaseGrant, LeaseId, LeaseKind, ReleaseResult, RenewResult,
};
use crate::priority::UploadCategory;
use crate::queue::{CommitEvidence, QueueItem, QueueKey, QueueStore, UploadState, attempt_id_for};
use crate::source::ArchiveItemId;
use crate::transfer::{VerifyAlg, VerifySpec};

const LOAD_PAGE_SIZE: u32 = 256;
const SOURCE_REJECTED_PREFIX: &str = "source path rejected";
/// Shared opening of both engines' integrity messages (`rclone.rs` and
/// `engine.rs` diverge after this point), so either engine's corruption report
/// classifies as `integrity` rather than degrading to a generic failure.
const INTEGRITY_FAILURE_PREFIX: &str = "integrity check failed";
const LEASE_LOST_PREFIX: &str = "subject no longer LIVE";
const LEASE_RENEW_PREFIX: &str = "lease renew ";
const ERROR_CLASS_SOURCE_REJECTED: &str = "source_rejected";
const ERROR_CLASS_INTEGRITY: &str = "integrity";
const ERROR_CLASS_LEASE_LOST: &str = "lease_lost";
const ERROR_CLASS_UPLOAD_FAILED: &str = "upload_failed";

/// Live queue store backed by `IndexdCloudClient`.
pub struct LiveQueueStore<C: IndexdCloudClient> {
    client: C,
    max_attempts: u32,
    /// Rows dropped this hydrate because `indexd` returned no `source_rel` for
    /// them. Counted rather than raised so one unusable row cannot block the
    /// queue that is the only path footage takes off the device.
    skipped_missing_source_rel: AtomicU32,
}

impl<C: IndexdCloudClient> LiveQueueStore<C> {
    #[must_use]
    /// Build a queue store from an indexd client.
    ///
    /// `max_attempts` mirrors [`crate::config::RetryConfig::max_attempts`] and is
    /// needed at load time: the store elects a single parent to work, and that
    /// election must ignore parents with no workable rows left.
    pub fn new(client: C, max_attempts: u32) -> Self {
        Self {
            client,
            max_attempts,
            skipped_missing_source_rel: AtomicU32::new(0),
        }
    }

    /// Take and clear the count of rows skipped for a missing `source_rel`, so
    /// the serve loop can report the anomaly once per cycle.
    pub fn take_skipped_missing_source_rel(&self) -> u32 {
        self.skipped_missing_source_rel.swap(0, Ordering::Relaxed)
    }

    fn map_row(&self, row: CloudQueueRow) -> Result<Option<QueueItem>, IndexError> {
        let state = match row.state.as_str() {
            "queued" => UploadState::Queued,
            "in_progress" => UploadState::InProgress,
            "done" => return Ok(None),
            "failed" | "parked" => UploadState::Failed,
            other => {
                return Err(IndexError::new(
                    "load",
                    format!("unsupported queue state `{other}`"),
                ));
            }
        };
        // A `done` row (returned above) needs no source path. For any row we
        // would actually work, an empty `source_rel` means `indexd` found no
        // parent `archive_items` row — impossible while the FK cascade holds.
        // Drop just this row: raising here would fail the whole hydrate and
        // halt every upload for as long as the row existed.
        if row.source_rel.is_empty() {
            self.skipped_missing_source_rel
                .fetch_add(1, Ordering::Relaxed);
            return Ok(None);
        }
        let category = match row.category.as_str() {
            "event_sentry" => UploadCategory::EventSentry,
            "trip" => UploadCategory::Trip,
            "bulk" => UploadCategory::Bulk,
            other => {
                return Err(IndexError::new(
                    "load",
                    format!("unsupported upload category `{other}`"),
                ));
            }
        };
        let seq = u64::try_from(row.seq)
            .map_err(|_| IndexError::new("load", format!("seq out of range: {}", row.seq)))?;
        let total_bytes = u64::try_from(row.total_bytes).map_err(|_| {
            IndexError::new(
                "load",
                format!("total_bytes out of range: {}", row.total_bytes),
            )
        })?;
        let bytes_uploaded = u64::try_from(row.bytes_uploaded).map_err(|_| {
            IndexError::new(
                "load",
                format!("bytes_uploaded out of range: {}", row.bytes_uploaded),
            )
        })?;
        let attempts = u32::try_from(row.attempts).map_err(|_| {
            IndexError::new("load", format!("attempts out of range: {}", row.attempts))
        })?;

        let verify = if row.verify_alg == "none" {
            VerifySpec::CopyIntegrity
        } else {
            let alg = VerifyAlg::from_str(&row.verify_alg).map_err(|_| {
                IndexError::new(
                    "load",
                    format!("unsupported verify_alg `{}`", row.verify_alg),
                )
            })?;
            let expected = row.expected_hash.ok_or_else(|| {
                IndexError::new(
                    "load",
                    format!("missing expected_hash for verify_alg `{}`", row.verify_alg),
                )
            })?;
            VerifySpec::Native { alg, expected }
        };

        Ok(Some(QueueItem {
            key: QueueKey::new(row.destination_id, row.remote_key),
            archive_item_id: ArchiveItemId(row.archive_item_id),
            child_key: row.child_key,
            source_rel: row.source_rel,
            category,
            seq,
            total_bytes,
            verify,
            state,
            bytes_uploaded,
            attempts,
            not_before: row.not_before,
            last_error: row.last_error,
        }))
    }

    fn classify_error_class(reason: Option<&str>) -> &'static str {
        let Some(reason) = reason else {
            return ERROR_CLASS_UPLOAD_FAILED;
        };
        if reason.starts_with(SOURCE_REJECTED_PREFIX) {
            return ERROR_CLASS_SOURCE_REJECTED;
        }
        if reason.starts_with(INTEGRITY_FAILURE_PREFIX) {
            return ERROR_CLASS_INTEGRITY;
        }
        if reason.starts_with(LEASE_LOST_PREFIX) || reason.starts_with(LEASE_RENEW_PREFIX) {
            return ERROR_CLASS_LEASE_LOST;
        }
        ERROR_CLASS_UPLOAD_FAILED
    }
}

impl<C: IndexdCloudClient> QueueStore for LiveQueueStore<C> {
    fn load(&self) -> Result<Vec<QueueItem>, IndexError> {
        let mut cursor = None;
        let mut rows = Vec::new();
        loop {
            let page = self
                .client
                .cloud_queue_load(cursor.clone(), LOAD_PAGE_SIZE, None)
                .map_err(|err| IndexError::new("load", err.to_string()))?;
            rows.extend(page.items);
            cursor = page.next_cursor;
            if cursor.is_none() {
                break;
            }
        }

        let mut items = Vec::new();
        for row in rows {
            if row.upload_set_id.is_some() {
                continue;
            }
            if let Some(item) = self.map_row(row)? {
                items.push(item);
            }
        }

        // Elect the single parent this drain will work. `map_row` has already
        // dropped `done` rows, so a finished parent cannot win the election;
        // requiring a *retryable* row additionally excludes a parent whose
        // children have all burned their retries. Both cases would otherwise
        // latch here forever, because indexd never deletes a queue row — commit
        // only sets `state = 'done'`.
        let lowest_workable = items
            .iter()
            .filter(|item| item.is_retryable(self.max_attempts))
            .map(|item| item.archive_item_id)
            .min();
        match lowest_workable {
            Some(parent) => items.retain(|item| item.archive_item_id == parent),
            None => items.clear(),
        }
        Ok(items)
    }

    fn persist(&self, item: &QueueItem) -> Result<(), IndexError> {
        match item.state {
            UploadState::Failed => {
                let error_class = Self::classify_error_class(item.last_error.as_deref());
                let request = CloudQueueFailRequest {
                    queue_pk: crate::indexd_client::CloudQueuePk {
                        destination_id: item.key.destination_id.clone(),
                        remote_key: item.key.remote_key.clone(),
                    },
                    // `QueueItem::fail` increments `attempts` before this persist.
                    // The attempt that just failed is therefore `attempts - 1`.
                    attempt_id: attempt_id_for(&item.key, item.attempts.saturating_sub(1)),
                    upload_set_id: None,
                    error_class: error_class.to_owned(),
                    not_before: item.not_before,
                    terminal: false,
                };
                self.client
                    .cloud_upload_fail(&request)
                    .map_err(|err| IndexError::new("persist", err.to_string()))?;
                Ok(())
            }
            UploadState::Queued | UploadState::InProgress => {
                // Whole-file `rclone copyto` has no mid-file checkpoint protocol.
                // A crash simply retries from byte zero against indexd's queued row.
                Ok(())
            }
            UploadState::Done => Err(IndexError::new(
                "persist",
                "done state must be written through commit(item, evidence)",
            )),
        }
    }

    fn commit(&self, item: &QueueItem, evidence: &CommitEvidence) -> Result<(), IndexError> {
        let size = i64::try_from(evidence.size)
            .map_err(|_| IndexError::new("commit", "evidence.size exceeds i64"))?;
        let request = CloudQueueCommitRequest {
            queue_pk: crate::indexd_client::CloudQueuePk {
                destination_id: item.key.destination_id.clone(),
                remote_key: item.key.remote_key.clone(),
            },
            attempt_id: evidence.attempt_id.clone(),
            upload_set_id: evidence.upload_set_id.clone(),
            hash: evidence.hash.clone(),
            hash_alg: evidence.hash_alg.clone(),
            size,
        };
        match self.client.cloud_queue_commit(&request) {
            Ok(_) => Ok(()),
            Err(IndexdClientError::Rejected { message }) => {
                Err(IndexError::new("commit", format!("rejected: {message}")))
            }
            Err(other) => Err(IndexError::new("commit", other.to_string())),
        }
    }
}

/// Live lease client backed by `IndexdCloudClient`.
pub struct LiveLeaseClient<C: IndexdCloudClient + Sync> {
    client: C,
    tokens: Mutex<Vec<(LeaseId, LeaseGen, String)>>,
}

impl<C: IndexdCloudClient + Sync> LiveLeaseClient<C> {
    #[must_use]
    /// Build a lease client from an indexd client.
    pub fn new(client: C) -> Self {
        Self {
            client,
            tokens: Mutex::new(Vec::new()),
        }
    }

    fn parse_lease_token(token: &str) -> Option<(LeaseId, LeaseGen)> {
        let (lease_id_raw, gen_raw) = token.split_once(':')?;
        let lease_id = lease_id_raw.parse::<i64>().ok()?;
        let gen_token = u128::from_str_radix(gen_raw, 16).ok()?;
        Some((LeaseId(lease_id), LeaseGen(gen_token)))
    }

    fn wire_token(lease_id: LeaseId, gen_token: LeaseGen) -> String {
        format!("{}:{:032x}", lease_id.0, gen_token.0)
    }

    fn token_for(&self, lease_id: LeaseId, gen_token: LeaseGen) -> Option<String> {
        let guard = self.tokens.lock().ok()?;
        guard
            .iter()
            .find(|(id, generation, _)| *id == lease_id && *generation == gen_token)
            .map(|(_, _, token)| token.clone())
    }

    fn record_token(&self, lease_id: LeaseId, gen_token: LeaseGen, token: String) {
        if let Ok(mut guard) = self.tokens.lock() {
            if let Some(entry) = guard
                .iter_mut()
                .find(|(id, generation, _)| *id == lease_id && *generation == gen_token)
            {
                *entry = (lease_id, gen_token, token);
                return;
            }
            guard.push((lease_id, gen_token, token));
        }
    }

    fn drop_token(&self, lease_id: LeaseId, gen_token: LeaseGen) {
        if let Ok(mut guard) = self.tokens.lock() {
            guard.retain(|(id, generation, _)| !(*id == lease_id && *generation == gen_token));
        }
    }

    fn acquire_inner(
        &self,
        archive_item_id: i64,
        ttl_ms: u32,
    ) -> Result<UploadLeaseAcquireResult, IndexdClientError> {
        self.client.upload_lease_acquire(archive_item_id, ttl_ms)
    }

    fn renew_inner(
        &self,
        token: &str,
        ttl_ms: u32,
    ) -> Result<UploadLeaseRenewResult, IndexdClientError> {
        self.client.upload_lease_renew(token, ttl_ms)
    }

    fn release_inner(&self, token: &str) -> Result<UploadLeaseReleaseResult, IndexdClientError> {
        self.client.upload_lease_release(token)
    }
}

impl<C: IndexdCloudClient + Sync> LeaseClient for LiveLeaseClient<C> {
    fn acquire(
        &self,
        item: ArchiveItemId,
        kind: LeaseKind,
        _holder: &str,
        ttl_ms: i64,
    ) -> LeaseGrant {
        if kind != LeaseKind::Upload {
            return LeaseGrant::Denied {
                reason: "unsupported lease kind".to_owned(),
            };
        }
        let Ok(ttl_ms) = u32::try_from(ttl_ms) else {
            return LeaseGrant::Denied {
                reason: "invalid lease ttl".to_owned(),
            };
        };

        match self.acquire_inner(item.0, ttl_ms) {
            Ok(UploadLeaseAcquireResult {
                granted: true,
                token: Some(token),
                expires_mono_ms: Some(expires_mono_ms),
                ..
            }) => match Self::parse_lease_token(&token) {
                Some((lease_id, gen_token)) => {
                    self.record_token(lease_id, gen_token, token);
                    LeaseGrant::Granted {
                        lease_id,
                        gen_token,
                        expires_mono_ms: crate::time::MonoMs(expires_mono_ms),
                    }
                }
                None => LeaseGrant::Denied {
                    reason: "invalid lease token format".to_owned(),
                },
            },
            Ok(UploadLeaseAcquireResult { granted: false, .. }) => LeaseGrant::Denied {
                reason: "lease denied".to_owned(),
            },
            Ok(_) => LeaseGrant::Denied {
                reason: "lease acquire response missing token or expiry".to_owned(),
            },
            Err(err) => LeaseGrant::Denied {
                reason: err.to_string(),
            },
        }
    }

    fn renew(&self, lease_id: LeaseId, gen_token: LeaseGen, ttl_ms: i64) -> RenewResult {
        let Ok(ttl_ms) = u32::try_from(ttl_ms) else {
            return RenewResult::Stale {
                reason: "invalid lease ttl".to_owned(),
            };
        };
        let token = self
            .token_for(lease_id, gen_token)
            .unwrap_or_else(|| Self::wire_token(lease_id, gen_token));
        match self.renew_inner(&token, ttl_ms) {
            Ok(UploadLeaseRenewResult {
                ok: true,
                expires_mono_ms: Some(expires_mono_ms),
            }) => RenewResult::Renewed {
                expires_mono_ms: crate::time::MonoMs(expires_mono_ms),
            },
            Ok(UploadLeaseRenewResult { ok: false, .. }) => RenewResult::Stale {
                reason: "lease renew denied".to_owned(),
            },
            Ok(_) => RenewResult::Stale {
                reason: "lease renew response missing expiry".to_owned(),
            },
            Err(err) => RenewResult::Unavailable {
                reason: err.to_string(),
            },
        }
    }

    fn release(&self, lease_id: LeaseId, gen_token: LeaseGen) -> ReleaseResult {
        let token = self
            .token_for(lease_id, gen_token)
            .unwrap_or_else(|| Self::wire_token(lease_id, gen_token));
        let result = self.release_inner(&token);
        self.drop_token(lease_id, gen_token);
        match result {
            Ok(UploadLeaseReleaseResult { ok: true }) => ReleaseResult::Released,
            Ok(UploadLeaseReleaseResult { ok: false }) | Err(_) => ReleaseResult::NoOp,
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use std::sync::{Arc, Mutex};

    use crate::indexd_client::{
        CloudCandidateRow, CloudDiscoverRow, CloudQueueCommitResult, CloudQueueFailResult,
        CloudQueueRetryRequest, CloudQueueRow, CloudQueueUpsertItem, Page,
    };

    use super::*;

    const MAX_ATTEMPTS: u32 = 5;

    struct FakeClient {
        pages: Mutex<Vec<Page<CloudQueueRow>>>,
        fail_persist: Arc<Mutex<Option<CloudQueueFailRequest>>>,
        commit_error: Mutex<Option<IndexdClientError>>,
        commit_request: Arc<Mutex<Option<CloudQueueCommitRequest>>>,
        lease_acquire_error: bool,
        lease_renew_error_kind: Option<std::io::ErrorKind>,
        lease_release_error: bool,
    }

    impl FakeClient {
        fn with_pages(pages: Vec<Page<CloudQueueRow>>) -> Self {
            Self {
                pages: Mutex::new(pages),
                ..Self::default()
            }
        }
    }

    impl Default for FakeClient {
        fn default() -> Self {
            Self {
                pages: Mutex::new(Vec::new()),
                fail_persist: Arc::new(Mutex::new(None)),
                commit_error: Mutex::new(None),
                commit_request: Arc::new(Mutex::new(None)),
                lease_acquire_error: false,
                lease_renew_error_kind: None,
                lease_release_error: false,
            }
        }
    }

    impl IndexdCloudClient for FakeClient {
        fn cloud_discover(
            &self,
            _after_cursor: Option<String>,
            _limit: u32,
        ) -> Result<Page<CloudDiscoverRow>, IndexdClientError> {
            panic!("unused")
        }

        fn cloud_queue_upsert(
            &self,
            _item: &CloudQueueUpsertItem,
        ) -> Result<String, IndexdClientError> {
            panic!("unused")
        }

        fn cloud_queue_load(
            &self,
            _after_cursor: Option<String>,
            _limit: u32,
            _upload_set_id: Option<String>,
        ) -> Result<Page<CloudQueueRow>, IndexdClientError> {
            let mut pages = self
                .pages
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if pages.is_empty() {
                return Ok(Page {
                    items: Vec::new(),
                    next_cursor: None,
                });
            }
            Ok(pages.remove(0))
        }

        fn cloud_queue_commit(
            &self,
            request: &CloudQueueCommitRequest,
        ) -> Result<CloudQueueCommitResult, IndexdClientError> {
            *self
                .commit_request
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(request.clone());
            if let Some(err) = self
                .commit_error
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .take()
            {
                return Err(err);
            }
            Ok(CloudQueueCommitResult {
                ok: true,
                durable_parent: false,
            })
        }

        fn cloud_queue_retry(
            &self,
            _request: &CloudQueueRetryRequest,
        ) -> Result<String, IndexdClientError> {
            panic!("unused")
        }

        fn upload_lease_acquire(
            &self,
            _archive_item_id: i64,
            _ttl_ms: u32,
        ) -> Result<UploadLeaseAcquireResult, IndexdClientError> {
            if self.lease_acquire_error {
                return Err(IndexdClientError::Io(std::io::Error::other("offline")));
            }
            Ok(UploadLeaseAcquireResult {
                granted: true,
                token: Some("1:00000000000000000000000000000001".to_owned()),
                boot_id: Some("boot-1".to_owned()),
                expires_mono_ms: Some(1_000),
            })
        }

        fn upload_lease_renew(
            &self,
            _token: &str,
            _ttl_ms: u32,
        ) -> Result<UploadLeaseRenewResult, IndexdClientError> {
            if let Some(kind) = self.lease_renew_error_kind {
                return Err(IndexdClientError::Io(std::io::Error::from(kind)));
            }
            Ok(UploadLeaseRenewResult {
                ok: true,
                expires_mono_ms: Some(2_000),
            })
        }

        fn upload_lease_release(
            &self,
            _token: &str,
        ) -> Result<UploadLeaseReleaseResult, IndexdClientError> {
            if self.lease_release_error {
                return Err(IndexdClientError::Io(std::io::Error::other("offline")));
            }
            Ok(UploadLeaseReleaseResult { ok: true })
        }

        fn cloud_upload_fail(
            &self,
            request: &CloudQueueFailRequest,
        ) -> Result<CloudQueueFailResult, IndexdClientError> {
            *self
                .fail_persist
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(request.clone());
            Ok(CloudQueueFailResult {
                ok: true,
                state: "failed".to_owned(),
            })
        }

        fn cloud_candidates(
            &self,
            _folders: &[String],
            _after_cursor: Option<String>,
            _limit: u32,
        ) -> Result<Page<CloudCandidateRow>, IndexdClientError> {
            panic!("unused")
        }
    }

    /// indexd never deletes a queue row — commit only does `SET state = 'done'`
    /// — so a fully-uploaded parent keeps its rows forever. Picking the lowest
    /// parent *before* dropping done rows therefore latches onto that finished
    /// parent permanently: every later load maps its rows to `None`, the
    /// scheduler idles, and no younger parent is ever loaded again. That would
    /// upload exactly one parent's clips and then silently stop.
    #[test]
    fn load_skips_a_parent_whose_rows_are_all_done() {
        let client = FakeClient::with_pages(vec![Page {
            items: vec![row(10, "done", None), row(20, "queued", None)],
            next_cursor: None,
        }]);
        let store = LiveQueueStore::new(client, MAX_ATTEMPTS);
        let items = store.load().expect("load");
        let parents: Vec<i64> = items.iter().map(|item| item.archive_item_id.0).collect();
        assert_eq!(
            parents,
            vec![20],
            "a finished parent must not block younger parents"
        );
    }

    /// Same head-of-line hazard, reached the other way: a child that has burned
    /// every retry stays in the queue as `failed` forever. If it still wins the
    /// lowest-parent election, one permanently-broken clip stalls the entire
    /// upload pipeline.
    #[test]
    fn load_skips_a_parent_whose_rows_are_all_exhausted() {
        let mut exhausted = row(10, "failed", None);
        exhausted.attempts = i64::from(MAX_ATTEMPTS);
        let client = FakeClient::with_pages(vec![Page {
            items: vec![exhausted, row(20, "queued", None)],
            next_cursor: None,
        }]);
        let store = LiveQueueStore::new(client, MAX_ATTEMPTS);
        let items = store.load().expect("load");
        let parents: Vec<i64> = items.iter().map(|item| item.archive_item_id.0).collect();
        assert_eq!(
            parents,
            vec![20],
            "an exhausted parent must not block younger parents"
        );
    }

    #[test]
    fn row_with_empty_source_rel_is_skipped_without_blocking_other_rows() {
        // Regression guard: raising on the bad row would fail the whole hydrate
        // and stop every upload. The good row must still load, and the anomaly
        // must be counted so the serve loop can report it.
        let mut bad = row(10, "queued", None);
        bad.source_rel.clear();
        let good = row(11, "queued", None);
        let store = LiveQueueStore::new(
            FakeClient::with_pages(vec![Page {
                items: vec![bad, good],
                next_cursor: None,
            }]),
            MAX_ATTEMPTS,
        );
        let items = store.load().expect("a bad row must not fail the hydrate");
        assert_eq!(
            items
                .iter()
                .map(|i| i.archive_item_id.0)
                .collect::<Vec<_>>(),
            vec![11],
            "the good row must still be worked"
        );
        assert_eq!(store.take_skipped_missing_source_rel(), 1);
        assert_eq!(
            store.take_skipped_missing_source_rel(),
            0,
            "taking the count must clear it so it is reported once"
        );
    }

    #[test]
    fn done_row_with_empty_source_rel_is_not_counted_as_an_anomaly() {
        // A committed row keeps no source path; it must not look like corruption.
        let mut done = row(12, "done", None);
        done.source_rel.clear();
        let store = LiveQueueStore::new(
            FakeClient::with_pages(vec![Page {
                items: vec![done],
                next_cursor: None,
            }]),
            MAX_ATTEMPTS,
        );
        assert!(store.load().expect("done rows load cleanly").is_empty());
        assert_eq!(store.take_skipped_missing_source_rel(), 0);
    }

    fn row(parent: i64, state: &str, upload_set_id: Option<&str>) -> CloudQueueRow {
        CloudQueueRow {
            archive_item_id: parent,
            child_key: format!("child-{parent}"),
            source_rel: format!("archive/parent-{parent}/child-{parent}"),
            destination_id: "dest-a".to_owned(),
            remote_key: format!("remote-{parent}.mp4"),
            category: "bulk".to_owned(),
            seq: parent,
            total_bytes: 100,
            bytes_uploaded: 0,
            expected_hash: Some("hash".to_owned()),
            verify_alg: "sha256".to_owned(),
            content_sha256: "a".repeat(64),
            state: state.to_owned(),
            attempts: 1,
            not_before: None,
            last_error: None,
            upload_set_id: upload_set_id.map(ToOwned::to_owned),
        }
    }

    #[test]
    fn load_pages_filters_sealed_and_keeps_lowest_parent_only() {
        let page1 = Page {
            items: vec![
                row(20, "queued", None),
                row(10, "queued", None),
                row(10, "queued", Some("sealed")),
            ],
            next_cursor: Some("next".to_owned()),
        };
        let page2 = Page {
            items: vec![row(10, "done", None)],
            next_cursor: None,
        };
        let store = LiveQueueStore::new(FakeClient::with_pages(vec![page1, page2]), MAX_ATTEMPTS);
        let loaded = store.load().expect("load queue");
        assert_eq!(loaded.len(), 1);
        let first = loaded.first().expect("first loaded item");
        assert_eq!(first.archive_item_id, ArchiveItemId(10));
        assert_eq!(first.state, UploadState::Queued);
    }

    #[test]
    fn persist_done_state_is_rejected() {
        let store = LiveQueueStore::new(FakeClient::default(), MAX_ATTEMPTS);
        let item = QueueItem {
            key: QueueKey::new("dest-a", "remote-a"),
            archive_item_id: ArchiveItemId(1),
            child_key: "c".to_owned(),
            source_rel: String::new(),
            category: UploadCategory::Bulk,
            seq: 1,
            total_bytes: 1,
            verify: VerifySpec::CopyIntegrity,
            state: UploadState::Done,
            bytes_uploaded: 1,
            attempts: 1,
            not_before: None,
            last_error: None,
        };
        let err = store.persist(&item).expect_err("done state should fail");
        assert!(err.reason.contains("commit"));
    }

    #[test]
    fn commit_rejected_maps_distinct_reason() {
        let client = FakeClient {
            commit_error: Mutex::new(Some(IndexdClientError::Rejected {
                message: "fence mismatch".to_owned(),
            })),
            ..FakeClient::default()
        };
        let store = LiveQueueStore::new(client, MAX_ATTEMPTS);
        let item = QueueItem {
            key: QueueKey::new("dest-a", "remote-a"),
            archive_item_id: ArchiveItemId(1),
            child_key: "c".to_owned(),
            source_rel: String::new(),
            category: UploadCategory::Bulk,
            seq: 1,
            total_bytes: 1,
            verify: VerifySpec::CopyIntegrity,
            state: UploadState::InProgress,
            bytes_uploaded: 1,
            attempts: 1,
            not_before: None,
            last_error: None,
        };
        let evidence = CommitEvidence {
            attempt_id: "attempt-1".to_owned(),
            hash: String::new(),
            hash_alg: "none".to_owned(),
            size: 1,
            upload_set_id: None,
        };
        let err = store
            .commit(&item, &evidence)
            .expect_err("commit should fail");
        assert!(err.reason.contains("rejected: fence mismatch"));
    }

    #[test]
    fn failed_attempt_and_next_commit_use_different_attempt_ids() {
        let fail_capture = Arc::new(Mutex::new(None));
        let commit_capture = Arc::new(Mutex::new(None));
        let client = FakeClient {
            fail_persist: Arc::clone(&fail_capture),
            commit_request: Arc::clone(&commit_capture),
            ..FakeClient::default()
        };
        let store = LiveQueueStore::new(client, MAX_ATTEMPTS);
        let mut item = QueueItem {
            key: QueueKey::new("dest-a", "remote-a"),
            archive_item_id: ArchiveItemId(1),
            child_key: "c".to_owned(),
            source_rel: "archive/clip.mp4".to_owned(),
            category: UploadCategory::Bulk,
            seq: 1,
            total_bytes: 1,
            verify: VerifySpec::CopyIntegrity,
            state: UploadState::InProgress,
            bytes_uploaded: 0,
            attempts: 0,
            not_before: None,
            last_error: None,
        };
        item.fail("copy failed", false);
        store.persist(&item).expect("persist failed attempt");
        let fail_attempt_id = fail_capture
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .map(|request| request.attempt_id.clone())
            .expect("captured fail attempt id");
        let evidence = CommitEvidence {
            attempt_id: attempt_id_for(&item.key, item.attempts),
            hash: String::new(),
            hash_alg: "none".to_owned(),
            size: item.total_bytes,
            upload_set_id: None,
        };
        store.commit(&item, &evidence).expect("commit");
        let commit_attempt_id = commit_capture
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .map(|request| request.attempt_id.clone())
            .expect("captured commit attempt id");
        assert_eq!(fail_attempt_id, attempt_id_for(&item.key, 0));
        assert_eq!(commit_attempt_id, attempt_id_for(&item.key, 1));
        assert_ne!(fail_attempt_id, commit_attempt_id);
    }

    #[test]
    fn lease_rpc_errors_fail_closed() {
        let client = FakeClient {
            lease_acquire_error: true,
            lease_renew_error_kind: Some(std::io::ErrorKind::Other),
            lease_release_error: true,
            ..FakeClient::default()
        };
        let lease = LiveLeaseClient::new(client);
        assert!(matches!(
            lease.acquire(ArchiveItemId(1), LeaseKind::Upload, "uploadd", 1000),
            LeaseGrant::Denied { .. }
        ));
        assert!(matches!(
            lease.renew(LeaseId(1), LeaseGen(1), 1000),
            RenewResult::Unavailable { .. }
        ));
        assert_eq!(lease.release(LeaseId(1), LeaseGen(1)), ReleaseResult::NoOp);
    }

    #[test]
    fn renew_would_block_maps_to_unavailable_not_stale() {
        let lease = LiveLeaseClient::new(FakeClient {
            lease_renew_error_kind: Some(std::io::ErrorKind::WouldBlock),
            ..FakeClient::default()
        });
        match lease.renew(LeaseId(1), LeaseGen(1), 1000) {
            RenewResult::Unavailable { reason } => {
                let lowered = reason.to_lowercase();
                assert!(
                    lowered.contains("would block")
                        || lowered.contains("temporarily unavailable")
                        || lowered.contains("os error 11"),
                    "reason should carry the EAGAIN transport failure: {reason}"
                );
            }
            other => panic!("expected unavailable renew result, got {other:?}"),
        }
    }

    #[test]
    fn persist_failed_uses_sanitized_bounded_error_class() {
        let captured = Arc::new(Mutex::new(None));
        let client = FakeClient {
            fail_persist: Arc::clone(&captured),
            ..FakeClient::default()
        };
        let store = LiveQueueStore::new(client, MAX_ATTEMPTS);
        let reason_path = "/mnt/archive/SentryClips/2026/very/long/path/file.mp4";
        let reason_url = "https://example.invalid/bucket/private/object";
        let mut long_reason = String::from("source path rejected: ");
        long_reason.push_str(reason_path);
        long_reason.push(' ');
        long_reason.push_str(reason_url);
        long_reason.push(' ');
        long_reason.push_str(&"x".repeat(5_000));
        let item = QueueItem {
            key: QueueKey::new("dest-a", "remote-a"),
            archive_item_id: ArchiveItemId(1),
            child_key: "c".to_owned(),
            source_rel: String::new(),
            category: UploadCategory::Bulk,
            seq: 1,
            total_bytes: 1,
            verify: VerifySpec::CopyIntegrity,
            state: UploadState::Failed,
            bytes_uploaded: 0,
            attempts: 1,
            not_before: None,
            last_error: Some(long_reason),
        };
        store.persist(&item).expect("persist failed state");
        let request = captured
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .cloned()
            .expect("captured fail request");
        assert!(matches!(
            request.error_class.as_str(),
            ERROR_CLASS_SOURCE_REJECTED
                | ERROR_CLASS_INTEGRITY
                | ERROR_CLASS_LEASE_LOST
                | ERROR_CLASS_UPLOAD_FAILED
        ));
        assert!(request.error_class.len() <= 128);
        assert!(!request.error_class.contains(reason_path));
        assert!(!request.error_class.contains(reason_url));
    }

    /// Both upload engines report integrity failures, and their messages diverge
    /// after a shared opening. Keying the classifier on one engine's full
    /// sentence silently demotes the other engine's corruption report to a
    /// generic failure — losing the single signal most worth distinguishing in
    /// the durable attempts ledger.
    #[test]
    fn integrity_failures_from_either_engine_classify_as_integrity() {
        let rclone_reason =
            "integrity check failed: remote verification did not match expected spec";
        let engine_reason = "integrity check failed: remote verification mismatch";
        assert_eq!(
            LiveQueueStore::<FakeClient>::classify_error_class(Some(rclone_reason)),
            ERROR_CLASS_INTEGRITY,
            "rclone engine integrity failure must classify as integrity"
        );
        assert_eq!(
            LiveQueueStore::<FakeClient>::classify_error_class(Some(engine_reason)),
            ERROR_CLASS_INTEGRITY,
            "chunked engine integrity failure must classify as integrity"
        );
    }

    #[test]
    fn release_drops_cached_token_on_failed_release() {
        let lease = LiveLeaseClient::new(FakeClient {
            lease_release_error: true,
            ..FakeClient::default()
        });
        let grant = lease.acquire(ArchiveItemId(1), LeaseKind::Upload, "uploadd", 1000);
        let (lease_id, gen_token) = match grant {
            LeaseGrant::Granted {
                lease_id,
                gen_token,
                ..
            } => (lease_id, gen_token),
            other @ LeaseGrant::Denied { .. } => {
                panic!("expected granted lease, got {other:?}")
            }
        };
        assert_eq!(lease.release(lease_id, gen_token), ReleaseResult::NoOp);
        let token_count = lease.tokens.lock().expect("token cache lock").len();
        assert_eq!(token_count, 0);
    }
}
