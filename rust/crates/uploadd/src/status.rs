//! The status/queue snapshot `uploadd` exposes to `webd` for the cloud-archive
//! UI ([`uploadd.md`] §2.5, D2 `/api/cloud/*`).
//!
//! This is a **read model**: a serde-serializable projection of the durable
//! queue plus the current throttle state, built from the pure
//! [`crate::queue::UploadQueue`] and [`crate::throttle::ThrottleSnapshot`]. It
//! carries no behavior and no secrets (no provider tokens) — just what the UI
//! renders.
//!
//! [`uploadd.md`]: ../../../../docs/specs/uploadd.md

use serde::Serialize;

use crate::priority::UploadCategory;
use crate::queue::{QueueItem, UploadQueue, UploadState};
use crate::throttle::{ThrottleSnapshot, UploadGate};

/// Per-item status row for the cloud-archive UI.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct UploadItemStatus {
    /// Archive-item id.
    pub id: i64,
    /// Lifecycle state (`queued` / `in_progress` / `done` / `failed`).
    pub state: &'static str,
    /// Priority class (`event_sentry` / `trip` / `bulk`).
    pub category: &'static str,
    /// Bytes uploaded so far (the resume checkpoint).
    pub bytes_uploaded: u64,
    /// Total bytes.
    pub total_bytes: u64,
    /// Attempts made so far.
    pub attempts: u32,
    /// Last failure reason, if any.
    pub last_error: Option<String>,
}

/// The whole cloud-archive status payload for `webd`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CloudUploadStatus {
    /// Whether uploads are currently allowed (the effective gate).
    pub uploads_allowed: bool,
    /// Current sustained TX cap in bytes/sec (`0` when paused).
    pub max_tx_bytes_per_s: u64,
    /// The queue, in insertion order.
    pub items: Vec<UploadItemStatus>,
}

/// The serialized name of an [`UploadState`].
#[must_use]
pub fn state_name(state: UploadState) -> &'static str {
    match state {
        UploadState::Queued => "queued",
        UploadState::InProgress => "in_progress",
        UploadState::Done => "done",
        UploadState::Failed => "failed",
    }
}

/// The serialized name of an [`UploadCategory`].
#[must_use]
pub fn category_name(category: UploadCategory) -> &'static str {
    match category {
        UploadCategory::EventSentry => "event_sentry",
        UploadCategory::Trip => "trip",
        UploadCategory::Bulk => "bulk",
    }
}

impl UploadItemStatus {
    /// Project one [`QueueItem`] into a status row.
    #[must_use]
    pub fn from_item(item: &QueueItem) -> Self {
        Self {
            id: item.id.0,
            state: state_name(item.state),
            category: category_name(item.category),
            bytes_uploaded: item.bytes_uploaded,
            total_bytes: item.total_bytes,
            attempts: item.attempts,
            last_error: item.last_error.clone(),
        }
    }
}

impl CloudUploadStatus {
    /// Build the status payload from the queue and the current throttle gate.
    #[must_use]
    pub fn build(queue: &UploadQueue, throttle: &ThrottleSnapshot) -> Self {
        let (uploads_allowed, max_tx_bytes_per_s) = match throttle.gate() {
            UploadGate::Run {
                max_tx_bytes_per_s, ..
            } => (true, max_tx_bytes_per_s),
            UploadGate::Pause { .. } => (false, 0),
        };
        Self {
            uploads_allowed,
            max_tx_bytes_per_s,
            items: queue
                .items()
                .iter()
                .map(UploadItemStatus::from_item)
                .collect(),
        }
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
    use super::CloudUploadStatus;
    use crate::priority::UploadCategory;
    use crate::queue::{QueueItem, UploadQueue};
    use crate::source::{ArchiveItemId, ContentHash};
    use crate::throttle::{StoragePressure, ThrottleSnapshot, WifiThrottle};

    #[test]
    fn status_projects_queue_and_gate() {
        let mut q = UploadQueue::default();
        q.enqueue(QueueItem::new(
            ArchiveItemId(1),
            "clips/1.mp4",
            "remote/1.mp4",
            UploadCategory::EventSentry,
            0,
            1000,
            ContentHash::new([0u8; 32]),
        ));
        let snap = ThrottleSnapshot {
            wifi: WifiThrottle::closed(),
            storage: StoragePressure::open(),
        };
        let status = CloudUploadStatus::build(&q, &snap);
        assert!(!status.uploads_allowed, "closed link ⇒ not allowed");
        assert_eq!(status.items.len(), 1);
        assert_eq!(status.items[0].category, "event_sentry");
        assert_eq!(status.items[0].state, "queued");
        // It serializes without panicking and includes the id.
        let json = serde_json::to_string(&status).expect("serialize");
        assert!(json.contains("\"id\":1"));
    }
}
