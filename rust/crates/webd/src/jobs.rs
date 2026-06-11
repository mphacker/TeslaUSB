//! The in-process job bus backing the `GET /api/jobs` SSE stream and the
//! `GET /api/jobs/failed` REST snapshot (contract **D2** `webd-api.md` §2.5/§3).
//!
//! [`JobHub`] is a thin fan-out over a [`tokio::sync::broadcast`] channel plus a
//! small retained snapshot (the currently-active jobs replayed to a fresh SSE
//! subscriber, and a bounded ring of the most recent failures for the
//! failed-jobs screen). It is **axum-free** on purpose — the route layer turns
//! [`JobEvent`]s into SSE frames — so the bus logic is unit-testable without an
//! HTTP harness.
//!
//! ## Event model (contract §3)
//!
//! Four named SSE events flow over `GET /api/jobs`:
//!
//! * `job_status` — `webd`'s own job lifecycle (the car-delete handoff today).
//!   These are the only events `webd` both **produces and retains** in the
//!   snapshot/failed ring.
//! * `index_progress` / `handoff_status` / `upload_queue` — status snapshots
//!   relayed from `indexd` / `gadgetd` / `uploadd`. Their producer clients are
//!   not wired yet (later config lanes), so the [`JobEvent`] variants exist as
//!   the forward-compatible seam but are not retained.

use std::collections::VecDeque;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, MutexGuard};

use serde::Serialize;
use serde_json::Value;
use tokio::sync::broadcast;

/// Per-subscriber broadcast buffer. A subscriber that falls this far behind is
/// told it lagged (the route layer drops the gap and keeps streaming).
const CHANNEL_CAPACITY: usize = 256;

/// Cap on retained non-terminal jobs replayed to a new SSE subscriber. Bounds
/// memory if producers ever leak running jobs; the car-delete path holds at
/// most one at a time.
const MAX_ACTIVE_RETAINED: usize = 64;

/// Cap on the retained failed-job ring served by `GET /api/jobs/failed`.
const MAX_FAILED_RETAINED: usize = 100;

/// Terminal/running state of a `webd` job (the `state` field of a `job_status`
/// event, contract §3). Serializes to the snake-case wire strings.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum JobState {
    /// In flight; retained in the active snapshot until a terminal update.
    Running,
    /// Completed successfully.
    Done,
    /// Failed; retained in the failed-jobs ring.
    Failed,
    /// Permanently refused (validation) — terminal, not retried.
    Refused,
    /// Declined for a transient device state — terminal for this job; the
    /// caller may issue a new request.
    Busy,
    /// Accepted into `gadgetd`'s durable mutation queue — terminal for this
    /// `webd` job; the change is saved and applies automatically at the next
    /// safe window (the frictionless write path). The SPA shows "saved, syncing"
    /// rather than blocking on the handoff.
    Queued,
}

/// A single `webd` job's status — the payload of a `job_status` SSE event.
///
/// `job_id` is a process-monotonic counter (no `uuid` dependency); it is unique
/// within a `webd` process lifetime, which is all the SPA needs to correlate a
/// `running` event with its terminal update.
#[derive(Clone, Debug, Serialize)]
pub(crate) struct JobStatus {
    /// Process-monotonic job identifier.
    pub job_id: u64,
    /// Job kind discriminator, e.g. `"clip_delete"`.
    pub kind: String,
    /// Current lifecycle state.
    pub state: JobState,
    /// Fractional progress in `0.0..=1.0` when known; `null` otherwise.
    /// Always serialized: contract §3 lists `progress` as a `job_status` field,
    /// so the SPA can rely on it existing. Start/end-granular jobs report `null`
    /// while running and `1.0` on success.
    pub progress: Option<f64>,
    /// Human-readable detail, set on failure/refusal.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    /// The `gadgetd` handoff id, when the job drove one.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub handoff_id: Option<String>,
}

impl JobStatus {
    /// A fresh `running` job with no detail yet.
    pub(crate) fn running(job_id: u64, kind: &str) -> Self {
        JobStatus {
            job_id,
            kind: kind.to_owned(),
            state: JobState::Running,
            progress: None,
            detail: None,
            handoff_id: None,
        }
    }
}

/// One event on the `GET /api/jobs` stream. Each variant maps to a contract §3
/// SSE event name via [`JobEvent::name`]; the payload is exposed as JSON via
/// [`JobEvent::data`] so the route layer can build the SSE frame.
#[derive(Clone, Debug)]
pub(crate) enum JobEvent {
    /// A `webd` job lifecycle update.
    JobStatus(JobStatus),
    /// An `indexd` scan-progress snapshot (relayed; producer wired in a later
    /// config lane, so the variant is not yet constructed).
    #[allow(dead_code)]
    IndexProgress(Value),
    /// A `gadgetd` handoff-state snapshot (relayed; producer not yet wired).
    #[allow(dead_code)]
    HandoffStatus(Value),
    /// An `uploadd` queue snapshot (relayed; producer not yet wired).
    #[allow(dead_code)]
    UploadQueue(Value),
}

impl JobEvent {
    /// The SSE `event:` name (contract §3).
    pub(crate) fn name(&self) -> &'static str {
        match self {
            JobEvent::JobStatus(_) => "job_status",
            JobEvent::IndexProgress(_) => "index_progress",
            JobEvent::HandoffStatus(_) => "handoff_status",
            JobEvent::UploadQueue(_) => "upload_queue",
        }
    }

    /// The SSE `data:` JSON payload.
    pub(crate) fn data(&self) -> Value {
        match self {
            JobEvent::JobStatus(job) => serde_json::to_value(job).unwrap_or(Value::Null),
            JobEvent::IndexProgress(v) | JobEvent::HandoffStatus(v) | JobEvent::UploadQueue(v) => {
                v.clone()
            }
        }
    }
}

/// Retained job snapshot guarded by the hub mutex.
#[derive(Default)]
struct Snapshot {
    /// Currently-running jobs, replayed to a new subscriber (bounded FIFO).
    active: VecDeque<JobStatus>,
    /// Most-recent failures for the failed-jobs screen (bounded ring).
    failed: VecDeque<JobStatus>,
}

/// A cloneable handle to the process-wide job bus. Cloning shares the same
/// broadcast channel, snapshot, and id counter (all `Arc`-backed).
#[derive(Clone)]
pub(crate) struct JobHub {
    tx: broadcast::Sender<JobEvent>,
    snapshot: Arc<Mutex<Snapshot>>,
    next_id: Arc<AtomicU64>,
}

impl JobHub {
    /// Create an empty hub.
    pub(crate) fn new() -> Self {
        let (tx, _rx) = broadcast::channel(CHANNEL_CAPACITY);
        JobHub {
            tx,
            snapshot: Arc::new(Mutex::new(Snapshot::default())),
            next_id: Arc::new(AtomicU64::new(1)),
        }
    }

    /// Allocate the next process-monotonic job id.
    pub(crate) fn next_job_id(&self) -> u64 {
        self.next_id.fetch_add(1, Ordering::Relaxed)
    }

    /// Subscribe to the live event stream. Events published before the call are
    /// not replayed (use [`JobHub::active_snapshot`] for the initial burst).
    pub(crate) fn subscribe(&self) -> broadcast::Receiver<JobEvent> {
        self.tx.subscribe()
    }

    /// Publish a `job_status` update: retain it (active set / failed ring) and
    /// broadcast it to live subscribers.
    pub(crate) fn publish_job(&self, job: JobStatus) {
        {
            let mut snap = self.lock();
            // A new state for this job supersedes any retained running entry.
            snap.active.retain(|j| j.job_id != job.job_id);
            match job.state {
                JobState::Running => {
                    snap.active.push_back(job.clone());
                    while snap.active.len() > MAX_ACTIVE_RETAINED {
                        snap.active.pop_front();
                    }
                }
                JobState::Failed => {
                    snap.failed.push_back(job.clone());
                    while snap.failed.len() > MAX_FAILED_RETAINED {
                        snap.failed.pop_front();
                    }
                }
                JobState::Done | JobState::Refused | JobState::Busy | JobState::Queued => {}
            }
        }
        // No subscribers is not an error: the snapshot still carries state.
        let _ = self.tx.send(JobEvent::JobStatus(job));
    }

    /// Snapshot of the currently-running jobs, oldest first.
    pub(crate) fn active_snapshot(&self) -> Vec<JobStatus> {
        self.lock().active.iter().cloned().collect()
    }

    /// Snapshot of the retained failed jobs, oldest first.
    pub(crate) fn failed_snapshot(&self) -> Vec<JobStatus> {
        self.lock().failed.iter().cloned().collect()
    }

    /// Lock the snapshot, recovering the inner data if a previous holder
    /// panicked (a poisoned status mutex must never take down the web backend).
    fn lock(&self) -> MutexGuard<'_, Snapshot> {
        self.snapshot
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic, clippy::indexing_slicing)]

    use super::*;

    fn done(job_id: u64) -> JobStatus {
        JobStatus {
            job_id,
            kind: "clip_delete".to_owned(),
            state: JobState::Done,
            progress: Some(1.0),
            detail: None,
            handoff_id: Some("h-1".to_owned()),
        }
    }

    fn failed(job_id: u64) -> JobStatus {
        JobStatus {
            job_id,
            kind: "clip_delete".to_owned(),
            state: JobState::Failed,
            progress: None,
            detail: Some("boom".to_owned()),
            handoff_id: None,
        }
    }

    #[test]
    fn job_ids_are_monotonic() {
        let hub = JobHub::new();
        assert_eq!(hub.next_job_id(), 1);
        assert_eq!(hub.next_job_id(), 2);
        assert_eq!(hub.next_job_id(), 3);
    }

    #[test]
    fn running_job_is_retained_then_cleared_on_terminal() {
        let hub = JobHub::new();
        hub.publish_job(JobStatus::running(7, "clip_delete"));
        let active = hub.active_snapshot();
        assert_eq!(active.len(), 1);
        assert_eq!(active[0].job_id, 7);
        assert_eq!(active[0].state, JobState::Running);

        hub.publish_job(done(7));
        assert!(hub.active_snapshot().is_empty());
        // A successful job is not retained in the failed ring.
        assert!(hub.failed_snapshot().is_empty());
    }

    #[test]
    fn failed_job_lands_in_failed_ring_not_active() {
        let hub = JobHub::new();
        hub.publish_job(JobStatus::running(9, "clip_delete"));
        hub.publish_job(failed(9));
        assert!(hub.active_snapshot().is_empty());
        let bad = hub.failed_snapshot();
        assert_eq!(bad.len(), 1);
        assert_eq!(bad[0].job_id, 9);
        assert_eq!(bad[0].detail.as_deref(), Some("boom"));
    }

    #[test]
    fn failed_ring_is_bounded() {
        let hub = JobHub::new();
        for id in 0..(MAX_FAILED_RETAINED as u64 + 10) {
            hub.publish_job(failed(id));
        }
        let bad = hub.failed_snapshot();
        assert_eq!(bad.len(), MAX_FAILED_RETAINED);
        // Oldest entries were evicted; the newest id is retained.
        assert_eq!(bad[bad.len() - 1].job_id, MAX_FAILED_RETAINED as u64 + 9);
    }

    #[tokio::test]
    async fn subscriber_receives_published_events() {
        let hub = JobHub::new();
        let mut rx = hub.subscribe();
        hub.publish_job(JobStatus::running(3, "clip_delete"));
        let ev = rx.recv().await.unwrap();
        assert_eq!(ev.name(), "job_status");
        assert_eq!(ev.data()["job_id"], 3);
        assert_eq!(ev.data()["state"], "running");
    }

    #[test]
    fn relayed_event_names_match_contract() {
        assert_eq!(
            JobEvent::IndexProgress(Value::Null).name(),
            "index_progress"
        );
        assert_eq!(
            JobEvent::HandoffStatus(Value::Null).name(),
            "handoff_status"
        );
        assert_eq!(JobEvent::UploadQueue(Value::Null).name(), "upload_queue");
    }
}
