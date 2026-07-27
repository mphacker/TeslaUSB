//! Discover→enqueue producer for the unsealed upload slice.
//!
//! This module pages `cloud_discover` parent rows and upserts one
//! `cloud_queue_upsert` row per child file.
//!
//! ## Known unsealed-slice limitation
//! The producer first skips any discovered parent that already appears in
//! `cloud_queue_load` (by `archive_item_id`) to avoid re-hashing the same parent
//! every cycle. In this slice, a crash between child upserts can leave a parent
//! partially enqueued; that parent is then skipped on later cycles and remaining
//! children are never enqueued. This cannot make footage evictable in this slice
//! (`durable` stays `0`), but it can stall uploads for those children. Must fix
//! before eviction is live.

use std::collections::BTreeSet;
#[cfg(unix)]
use std::fs::{self, File};
#[cfg(unix)]
use std::io::Read;
#[cfg(unix)]
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::error::IndexError;
use crate::indexd_client::{CloudDiscoverRow, CloudQueueUpsertItem, IndexdCloudClient};
#[cfg(unix)]
use crate::source::ArchiveRoot;

const PAGE_LIMIT: u32 = 256;
const REMOTE_KEY_LIMIT: usize = 1024;
const DESTINATION_ID_LIMIT: usize = 128;
const VERIFY_ALG_NONE: &str = "none";
const REMOTE_KEY_TOO_LONG_CODE: &str = "remote_key_too_long";

/// One child file discovered under a parent archive directory.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChildFile {
    /// Child path relative to the parent archive directory (`/` separated).
    pub child_key: String,
    /// Child byte length from the same streaming pass used for hashing.
    pub total_bytes: i64,
    /// Lowercase hex SHA-256 of the child bytes.
    pub content_sha256: String,
}

/// Child-file discovery and streaming hash seam.
pub trait ChildSource {
    /// Enumerate child files for one discover parent.
    ///
    /// # Errors
    /// Returns an error if the parent path cannot be read or a child cannot be
    /// hashed.
    fn children_for_parent(&self, parent: &CloudDiscoverRow) -> Result<Vec<ChildFile>, IndexError>;
}

/// Producer run summary.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct EnqueueReport {
    /// Number of discover parent rows seen.
    pub discovered_parents: u64,
    /// Number of discovered parents skipped because they already had queue rows.
    pub skipped_existing_parents: u64,
    /// Number of child upserts sent.
    pub enqueued_children: u64,
    /// Number of children skipped because their composed remote key exceeded 1024.
    pub skipped_remote_key_too_long: u64,
    /// Sanitized bounded skip/error codes surfaced by this pass.
    pub event_codes: Vec<&'static str>,
}

/// Discover→enqueue producer.
pub struct DiscoverEnqueuer<'a, C: IndexdCloudClient, S: ChildSource> {
    client: &'a C,
    child_source: &'a S,
    destination_id: String,
    remote_prefix: String,
}

impl<'a, C: IndexdCloudClient, S: ChildSource> DiscoverEnqueuer<'a, C, S> {
    /// Build a producer.
    ///
    /// # Errors
    /// Returns an error when `destination_id` is empty or exceeds 128 bytes.
    pub fn new(
        client: &'a C,
        child_source: &'a S,
        destination_id: impl Into<String>,
        remote_prefix: impl Into<String>,
    ) -> Result<Self, IndexError> {
        let destination_id = destination_id.into();
        if destination_id.is_empty() || destination_id.len() > DESTINATION_ID_LIMIT {
            return Err(IndexError::new(
                "enqueue_config",
                "destination_id must be 1..=128 bytes",
            ));
        }
        Ok(Self {
            client,
            child_source,
            destination_id,
            remote_prefix: remote_prefix.into().trim_matches('/').to_owned(),
        })
    }

    /// Run one discover→enqueue pass.
    ///
    /// `seq` continues from the highest sequence already in the queue so that
    /// footage discovered on a later pass sorts *after* clips still waiting,
    /// preserving the global FIFO drain order.
    ///
    /// # Errors
    /// Returns an error on RPC or child-source failures.
    pub fn run(&self) -> Result<EnqueueReport, IndexError> {
        let mut report = EnqueueReport::default();
        let (mut queued_parents, mut seq) = self.load_existing_queue_state()?;
        let mut discover_cursor = None;
        loop {
            let page = self
                .client
                .cloud_discover(discover_cursor.clone(), PAGE_LIMIT)
                .map_err(|err| IndexError::new("cloud_discover", err.to_string()))?;
            for parent in page.items {
                report.discovered_parents = report.discovered_parents.saturating_add(1);
                if queued_parents.contains(&parent.archive_item_id) {
                    report.skipped_existing_parents = report.skipped_existing_parents.saturating_add(1);
                    continue;
                }
                let children = self.child_source.children_for_parent(&parent)?;
                for child in children {
                    let Some(remote_key) = self.compose_remote_key(
                        &parent.folder_class,
                        parent.archive_item_id,
                        &child.content_sha256,
                        &child.child_key,
                    ) else {
                        report.skipped_remote_key_too_long =
                            report.skipped_remote_key_too_long.saturating_add(1);
                        if !report.event_codes.contains(&REMOTE_KEY_TOO_LONG_CODE) {
                            report.event_codes.push(REMOTE_KEY_TOO_LONG_CODE);
                        }
                        continue;
                    };
                    let item = CloudQueueUpsertItem {
                        archive_item_id: parent.archive_item_id,
                        child_key: child.child_key,
                        destination_id: self.destination_id.clone(),
                        remote_key,
                        category: parent.category.clone(),
                        seq,
                        total_bytes: child.total_bytes,
                        content_sha256: child.content_sha256,
                        expected_hash: None,
                        verify_alg: VERIFY_ALG_NONE.to_owned(),
                    };
                    self.client
                        .cloud_queue_upsert(&item)
                        .map_err(|err| IndexError::new("cloud_queue_upsert", err.to_string()))?;
                    report.enqueued_children = report.enqueued_children.saturating_add(1);
                    seq = seq.saturating_add(1);
                }
                queued_parents.insert(parent.archive_item_id);
            }
            discover_cursor = page.next_cursor;
            if discover_cursor.is_none() {
                break;
            }
        }
        Ok(report)
    }

    /// Load the parents already represented in the queue, and the sequence the
    /// next enqueued child should use.
    ///
    /// `seq` is a global FIFO key — indexd drains `ORDER BY seq ASC` across the
    /// whole queue — so a pass must continue past the existing maximum rather
    /// than restart at zero, or newly discovered clips would sort ahead of older
    /// ones that are closer to being evicted.
    fn load_existing_queue_state(&self) -> Result<(BTreeSet<i64>, i64), IndexError> {
        let mut ids = BTreeSet::new();
        let mut max_seq: Option<i64> = None;
        let mut cursor = None;
        loop {
            let page = self
                .client
                .cloud_queue_load(cursor.clone(), PAGE_LIMIT, None)
                .map_err(|err| IndexError::new("cloud_queue_load", err.to_string()))?;
            for row in page.items {
                ids.insert(row.archive_item_id);
                max_seq = Some(max_seq.map_or(row.seq, |current: i64| current.max(row.seq)));
            }
            cursor = page.next_cursor;
            if cursor.is_none() {
                break;
            }
        }
        let next_seq = max_seq.map_or(0, |value| value.saturating_add(1));
        Ok((ids, next_seq))
    }

    fn compose_remote_key(
        &self,
        folder_class: &str,
        archive_item_id: i64,
        content_sha256: &str,
        child_key: &str,
    ) -> Option<String> {
        let hash_prefix = content_sha256.get(..16)?;
        let key = if self.remote_prefix.is_empty() {
            format!("{folder_class}/{archive_item_id}/{hash_prefix}/{child_key}")
        } else {
            format!(
                "{}/{folder_class}/{archive_item_id}/{hash_prefix}/{child_key}",
                self.remote_prefix
            )
        };
        if key.is_empty() || key.len() > REMOTE_KEY_LIMIT {
            return None;
        }
        Some(key)
    }
}

#[cfg(unix)]
/// Live child-source backed by archive filesystem traversal and streaming SHA-256.
pub struct LiveChildSource {
    archive_root: ArchiveRoot,
}

#[cfg(unix)]
impl LiveChildSource {
    /// Build a live child source.
    #[must_use]
    pub fn new(archive_root: ArchiveRoot) -> Self {
        Self { archive_root }
    }

    fn walk_parent_files(parent_dir: &Path) -> Result<Vec<PathBuf>, IndexError> {
        let mut files = Vec::new();
        let entries = fs::read_dir(parent_dir)
            .map_err(|err| IndexError::new("child_source_walk", err.to_string()))?;
        for entry_result in entries {
            let entry = entry_result
                .map_err(|err| IndexError::new("child_source_walk", err.to_string()))?;
            let entry_path = entry.path();
            let file_type = entry
                .file_type()
                .map_err(|err| IndexError::new("child_source_walk", err.to_string()))?;
            if file_type.is_dir() {
                let mut nested = Self::walk_parent_files(&entry_path)?;
                files.append(&mut nested);
            } else if file_type.is_file() {
                files.push(entry_path);
            }
        }
        Ok(files)
    }

    fn hash_file_streaming(path: &Path) -> Result<(i64, String), IndexError> {
        let mut file =
            File::open(path).map_err(|err| IndexError::new("child_source_hash", err.to_string()))?;
        let mut hasher = Sha256::new();
        let mut total: i64 = 0;
        let mut buffer = vec![0_u8; 64 * 1024];
        loop {
            let read = file
                .read(&mut buffer)
                .map_err(|err| IndexError::new("child_source_hash", err.to_string()))?;
            if read == 0 {
                break;
            }
            let Some(chunk) = buffer.get(..read) else {
                return Err(IndexError::new(
                    "child_source_hash",
                    "reader returned out-of-bounds chunk length",
                ));
            };
            hasher.update(chunk);
            let read_i64 =
                i64::try_from(read).map_err(|_| IndexError::new("child_source_hash", "read too large"))?;
            total = total.saturating_add(read_i64);
        }
        let digest = hasher.finalize();
        Ok((total, format!("{digest:x}")))
    }
}

#[cfg(unix)]
impl ChildSource for LiveChildSource {
    fn children_for_parent(&self, parent: &CloudDiscoverRow) -> Result<Vec<ChildFile>, IndexError> {
        let parent_guarded = self
            .archive_root
            .resolve(&parent.path)
            .map_err(|err| IndexError::new("child_source_parent", err.to_string()))?;
        let parent_dir = Path::new(parent_guarded.as_str());
        let mut files = Self::walk_parent_files(parent_dir)?;
        files.sort();

        let mut out = Vec::new();
        for file_path in files {
            let relative = file_path.strip_prefix(parent_dir).map_err(|_| {
                IndexError::new("child_source_parent", "child path escaped parent")
            })?;
            let child_key = relative
                .to_string_lossy()
                .replace('\\', "/")
                .trim_start_matches('/')
                .to_owned();
            let (total_bytes, content_sha256) = Self::hash_file_streaming(&file_path)?;
            out.push(ChildFile {
                child_key,
                total_bytes,
                content_sha256,
            });
        }
        Ok(out)
    }
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines,
    clippy::indexing_slicing
)]
mod tests {
    use std::cell::RefCell;
    use std::collections::BTreeMap;

    use crate::indexd_client::{
        CloudCandidateRow, CloudQueueCommitRequest, CloudQueueCommitResult, CloudQueueFailRequest,
        CloudQueueFailResult, CloudQueueRetryRequest, CloudQueueUpsertItem, Page, UploadLeaseAcquireResult,
        UploadLeaseReleaseResult, UploadLeaseRenewResult, CloudQueueRow,
    };

    use super::*;

    struct FakeChildSource {
        by_parent: BTreeMap<i64, Vec<ChildFile>>,
    }

    impl ChildSource for FakeChildSource {
        fn children_for_parent(&self, parent: &CloudDiscoverRow) -> Result<Vec<ChildFile>, IndexError> {
            Ok(self
                .by_parent
                .get(&parent.archive_item_id)
                .cloned()
                .unwrap_or_default())
        }
    }

    #[derive(Default)]
    struct FakeClient {
        discover_pages: RefCell<Vec<Page<CloudDiscoverRow>>>,
        queue_pages: RefCell<Vec<Page<CloudQueueRow>>>,
        upserts: RefCell<Vec<CloudQueueUpsertItem>>,
        discover_cursors: RefCell<Vec<Option<String>>>,
    }

    impl IndexdCloudClient for FakeClient {
        fn cloud_discover(
            &self,
            after_cursor: Option<String>,
            _limit: u32,
        ) -> Result<Page<CloudDiscoverRow>, crate::indexd_client::IndexdClientError> {
            self.discover_cursors.borrow_mut().push(after_cursor);
            let mut pages = self.discover_pages.borrow_mut();
            if pages.is_empty() {
                return Ok(Page {
                    items: Vec::new(),
                    next_cursor: None,
                });
            }
            Ok(pages.remove(0))
        }

        fn cloud_queue_upsert(
            &self,
            item: &CloudQueueUpsertItem,
        ) -> Result<String, crate::indexd_client::IndexdClientError> {
            self.upserts.borrow_mut().push(item.clone());
            Ok("queued".to_owned())
        }

        fn cloud_queue_load(
            &self,
            _after_cursor: Option<String>,
            _limit: u32,
            _upload_set_id: Option<String>,
        ) -> Result<Page<CloudQueueRow>, crate::indexd_client::IndexdClientError> {
            let mut pages = self.queue_pages.borrow_mut();
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
            _request: &CloudQueueCommitRequest,
        ) -> Result<CloudQueueCommitResult, crate::indexd_client::IndexdClientError> {
            panic!("unused")
        }

        fn cloud_queue_retry(
            &self,
            _request: &CloudQueueRetryRequest,
        ) -> Result<String, crate::indexd_client::IndexdClientError> {
            panic!("unused")
        }

        fn upload_lease_acquire(
            &self,
            _archive_item_id: i64,
            _ttl_ms: u32,
        ) -> Result<UploadLeaseAcquireResult, crate::indexd_client::IndexdClientError> {
            panic!("unused")
        }

        fn upload_lease_renew(
            &self,
            _token: &str,
            _ttl_ms: u32,
        ) -> Result<UploadLeaseRenewResult, crate::indexd_client::IndexdClientError> {
            panic!("unused")
        }

        fn upload_lease_release(
            &self,
            _token: &str,
        ) -> Result<UploadLeaseReleaseResult, crate::indexd_client::IndexdClientError> {
            panic!("unused")
        }

        fn cloud_upload_fail(
            &self,
            _request: &CloudQueueFailRequest,
        ) -> Result<CloudQueueFailResult, crate::indexd_client::IndexdClientError> {
            panic!("unused")
        }

        fn cloud_candidates(
            &self,
            _folders: &[String],
            _after_cursor: Option<String>,
            _limit: u32,
        ) -> Result<Page<CloudCandidateRow>, crate::indexd_client::IndexdClientError> {
            panic!("unused")
        }
    }

    fn discover_parent(id: i64, folder_class: &str, category: &str) -> CloudDiscoverRow {
        CloudDiscoverRow {
            archive_item_id: id,
            folder_class: folder_class.to_owned(),
            path: format!("archive/{id}"),
            manifest_digest: None,
            category: category.to_owned(),
        }
    }

    fn queue_row(id: i64) -> CloudQueueRow {
        CloudQueueRow {
            archive_item_id: id,
            child_key: "child".to_owned(),
            destination_id: "dest".to_owned(),
            remote_key: "rk".to_owned(),
            category: "bulk".to_owned(),
            seq: 0,
            total_bytes: 1,
            bytes_uploaded: 0,
            expected_hash: None,
            verify_alg: "none".to_owned(),
            content_sha256: "a".repeat(64),
            state: "queued".to_owned(),
            attempts: 0,
            not_before: None,
            last_error: None,
            upload_set_id: None,
        }
    }

    fn child(child_key: &str, bytes: i64, hash: &str) -> ChildFile {
        ChildFile {
            child_key: child_key.to_owned(),
            total_bytes: bytes,
            content_sha256: hash.to_owned(),
        }
    }

    /// `seq` is a *global* FIFO key: indexd drains `ORDER BY seq ASC` across the
    /// whole queue and uploadd's priority policy treats a smaller `seq` as older.
    /// A pass that restarts numbering at 0 therefore stamps freshly discovered
    /// footage as older than clips already waiting, which inverts upload order in
    /// the worst possible direction — the oldest clips are the ones retention
    /// deletes first, so they are exactly the ones that must upload first.
    #[test]
    fn seq_continues_past_existing_queue_rows_across_passes() {
        let mut existing = queue_row(10);
        existing.seq = 41;
        let client = FakeClient {
            discover_pages: RefCell::new(vec![Page {
                items: vec![discover_parent(20, "RecentClips", "bulk")],
                next_cursor: None,
            }]),
            queue_pages: RefCell::new(vec![Page {
                items: vec![existing],
                next_cursor: None,
            }]),
            ..FakeClient::default()
        };
        let source = FakeChildSource {
            by_parent: BTreeMap::from([(
                20,
                vec![
                    child("a.mp4", 1, &"1".repeat(64)),
                    child("b.mp4", 1, &"2".repeat(64)),
                ],
            )]),
        };
        let producer = DiscoverEnqueuer::new(&client, &source, "dest-a", "prefix").expect("producer");
        producer.run().expect("run");
        let upserts = client.upserts.borrow();
        let seqs: Vec<i64> = upserts.iter().map(|item| item.seq).collect();
        assert_eq!(
            seqs,
            vec![42, 43],
            "new children must sort after the seq 41 row already queued"
        );
    }

    #[test]
    fn discovered_parent_already_queued_is_skipped() {
        let client = FakeClient {
            discover_pages: RefCell::new(vec![Page {
                items: vec![
                    discover_parent(10, "RecentClips", "bulk"),
                    discover_parent(20, "RecentClips", "bulk"),
                ],
                next_cursor: None,
            }]),
            queue_pages: RefCell::new(vec![Page {
                items: vec![queue_row(10)],
                next_cursor: None,
            }]),
            ..FakeClient::default()
        };
        let source = FakeChildSource {
            by_parent: BTreeMap::from([
                (10, vec![child("a.mp4", 1, &"1".repeat(64))]),
                (20, vec![child("b.mp4", 1, &"2".repeat(64))]),
            ]),
        };
        let producer = DiscoverEnqueuer::new(&client, &source, "dest-a", "prefix").expect("producer");
        let report = producer.run().expect("run");
        let upserts = client.upserts.borrow();
        assert_eq!(report.skipped_existing_parents, 1);
        assert_eq!(upserts.len(), 1);
        assert_eq!(upserts[0].archive_item_id, 20);
    }

    #[test]
    fn fresh_parent_enqueues_children_with_hash_and_size() {
        let client = FakeClient {
            discover_pages: RefCell::new(vec![Page {
                items: vec![discover_parent(30, "SavedClips", "event_sentry")],
                next_cursor: None,
            }]),
            queue_pages: RefCell::new(vec![Page {
                items: vec![],
                next_cursor: None,
            }]),
            ..FakeClient::default()
        };
        let source = FakeChildSource {
            by_parent: BTreeMap::from([(
                30,
                vec![
                    child("cam/front.mp4", 111, &"a".repeat(64)),
                    child("cam/back.mp4", 222, &"b".repeat(64)),
                ],
            )]),
        };
        let producer = DiscoverEnqueuer::new(&client, &source, "dest-a", "root").expect("producer");
        let report = producer.run().expect("run");
        let upserts = client.upserts.borrow();
        assert_eq!(report.enqueued_children, 2);
        assert_eq!(upserts[0].child_key, "cam/front.mp4");
        assert_eq!(upserts[0].content_sha256, "a".repeat(64));
        assert_eq!(upserts[0].total_bytes, 111);
        assert_eq!(upserts[1].child_key, "cam/back.mp4");
        assert_eq!(upserts[1].content_sha256, "b".repeat(64));
        assert_eq!(upserts[1].total_bytes, 222);
    }

    #[test]
    fn content_addressing_same_content_same_key_changed_content_different_key() {
        let client = FakeClient::default();
        let source = FakeChildSource {
            by_parent: BTreeMap::new(),
        };
        let producer = DiscoverEnqueuer::new(&client, &source, "dest-a", "root").expect("producer");
        let same_a = producer
            .compose_remote_key("RecentClips", 1, &"c".repeat(64), "child.mp4")
            .expect("key");
        let same_b = producer
            .compose_remote_key("RecentClips", 1, &"c".repeat(64), "child.mp4")
            .expect("key");
        let changed = producer
            .compose_remote_key("RecentClips", 1, &"d".repeat(64), "child.mp4")
            .expect("key");
        assert_eq!(same_a, same_b);
        assert_ne!(same_a, changed);
    }

    #[test]
    fn too_long_remote_key_is_skipped_not_truncated() {
        let client = FakeClient {
            discover_pages: RefCell::new(vec![Page {
                items: vec![discover_parent(40, "RecentClips", "bulk")],
                next_cursor: None,
            }]),
            queue_pages: RefCell::new(vec![Page {
                items: vec![],
                next_cursor: None,
            }]),
            ..FakeClient::default()
        };
        let source = FakeChildSource {
            by_parent: BTreeMap::from([(
                40,
                vec![child(&"k".repeat(1_100), 1, &"e".repeat(64))],
            )]),
        };
        let producer = DiscoverEnqueuer::new(&client, &source, "dest-a", "root").expect("producer");
        let report = producer.run().expect("run");
        assert_eq!(report.skipped_remote_key_too_long, 1);
        assert_eq!(report.event_codes, vec![REMOTE_KEY_TOO_LONG_CODE]);
        assert_eq!(client.upserts.borrow().len(), 0);
    }

    #[test]
    fn verify_alg_none_sets_expected_hash_none() {
        let client = FakeClient {
            discover_pages: RefCell::new(vec![Page {
                items: vec![discover_parent(50, "RecentClips", "bulk")],
                next_cursor: None,
            }]),
            queue_pages: RefCell::new(vec![Page {
                items: vec![],
                next_cursor: None,
            }]),
            ..FakeClient::default()
        };
        let source = FakeChildSource {
            by_parent: BTreeMap::from([(50, vec![child("x.mp4", 1, &"f".repeat(64))])]),
        };
        let producer = DiscoverEnqueuer::new(&client, &source, "dest-a", "root").expect("producer");
        let _ = producer.run().expect("run");
        let first = client.upserts.borrow()[0].clone();
        assert_eq!(first.verify_alg, VERIFY_ALG_NONE);
        assert!(first.expected_hash.is_none());
    }

    #[test]
    fn seq_is_monotonic_across_children() {
        let client = FakeClient {
            discover_pages: RefCell::new(vec![Page {
                items: vec![
                    discover_parent(60, "RecentClips", "bulk"),
                    discover_parent(61, "RecentClips", "bulk"),
                ],
                next_cursor: None,
            }]),
            queue_pages: RefCell::new(vec![Page {
                items: vec![],
                next_cursor: None,
            }]),
            ..FakeClient::default()
        };
        let source = FakeChildSource {
            by_parent: BTreeMap::from([
                (
                    60,
                    vec![
                        child("a.mp4", 1, &"1".repeat(64)),
                        child("b.mp4", 1, &"2".repeat(64)),
                    ],
                ),
                (61, vec![child("c.mp4", 1, &"3".repeat(64))]),
            ]),
        };
        let producer = DiscoverEnqueuer::new(&client, &source, "dest-a", "root").expect("producer");
        let _ = producer.run().expect("run");
        let seqs: Vec<i64> = client.upserts.borrow().iter().map(|row| row.seq).collect();
        assert_eq!(seqs, vec![0, 1, 2]);
    }

    #[test]
    fn discover_pagination_is_followed() {
        let client = FakeClient {
            discover_pages: RefCell::new(vec![
                Page {
                    items: vec![discover_parent(70, "RecentClips", "bulk")],
                    next_cursor: Some("cursor-1".to_owned()),
                },
                Page {
                    items: vec![discover_parent(71, "RecentClips", "bulk")],
                    next_cursor: None,
                },
            ]),
            queue_pages: RefCell::new(vec![Page {
                items: vec![],
                next_cursor: None,
            }]),
            ..FakeClient::default()
        };
        let source = FakeChildSource {
            by_parent: BTreeMap::from([
                (70, vec![child("a.mp4", 1, &"a".repeat(64))]),
                (71, vec![child("b.mp4", 1, &"b".repeat(64))]),
            ]),
        };
        let producer = DiscoverEnqueuer::new(&client, &source, "dest-a", "root").expect("producer");
        let _ = producer.run().expect("run");
        assert_eq!(
            *client.discover_cursors.borrow(),
            vec![None, Some("cursor-1".to_owned())]
        );
        assert_eq!(client.upserts.borrow().len(), 2);
    }
}
