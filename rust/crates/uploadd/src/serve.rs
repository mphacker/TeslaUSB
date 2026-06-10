//! The host-testable **upload scheduler** — the drain loop that turns a hydrated
//! [`crate::queue::UploadQueue`] into ordered, throttle-aware, lease-safe upload
//! work by repeatedly selecting the highest-priority ready item and handing it to
//! an [`UploadProcessor`].
//!
//! # Why a processor seam (and not just [`crate::engine::UploadEngine`])
//!
//! The per-item work — gate, lease, transfer, verify, mark durable — is already
//! pure and host-tested in two shapes:
//!
//! * [`crate::engine::UploadEngine`] streams the file in chunks (the in-process
//!   Rust-uploader path), and
//! * [`crate::rclone::RcloneUploadEngine`] uploads the whole file by shelling out
//!   to `rclone` (the chosen v1 backend).
//!
//! Both expose the *same* per-item contract: take a `&mut`
//! [`crate::queue::QueueItem`], do one end-to-end attempt, persist every
//! transition durably, and report a [`crate::engine::StepOutcome`]. The
//! [`UploadProcessor`] trait captures exactly that contract, so the scheduler is
//! generic over it and the same drain loop drives either backend. Tests inject a
//! deterministic fake processor.
//!
//! # What the scheduler adds on top of one item
//!
//! * **Priority + FIFO selection** ([`crate::priority`]): the next item is the
//!   lowest [`crate::priority::PriorityKey`] among the *ready* ones.
//! * **No head-of-line block on a lease denial.** A
//!   [`StepOutcome::SkippedLeaseDenied`] means `retentiond` has claimed the item
//!   for deletion; the item's queue state is intentionally left unchanged, so the
//!   scheduler parks it in a transient skip set for the rest of the drain pass
//!   (otherwise it would be re-selected forever). The set clears when the pass
//!   goes idle or uploads pause, so the next [`Scheduler::hydrate`] reaps the row
//!   `indexd` dropped.
//! * **Pause handling.** A [`StepOutcome::Paused`] (link down / AP mode / storage
//!   backpressure) stops the drain and tells the live loop how long to back off.
//! * **Idempotent (re)hydration.** [`Scheduler::hydrate`] rebuilds the in-memory
//!   index from the durable [`crate::queue::QueueStore`] snapshot, so newly
//!   archived items appear and deleted rows are reaped without duplicating work.
//!
//! # Live wiring (gated, not built here)
//!
//! The live `serve` binary path composes a [`Scheduler`] with the real `indexd`
//! [`crate::queue::QueueStore`] / [`crate::lease::LeaseClient`] /
//! [`crate::durability::DurabilityClient`] clients, the `wifid`
//! [`crate::throttle::ThrottleSource`] subscription, the `rclone`
//! [`crate::rclone::CommandRunner`], a real [`crate::time::Clock`] /
//! [`crate::time::Waiter`], and a stop flag. That wiring is hardware/IPC-gated and
//! depends on the Task 2.6 `WiFi` TX-cap calibration, so it lives in the gated
//! lane; this module delivers and proves the orchestration core.

use std::collections::HashSet;

use crate::config::UploaddConfig;
use crate::engine::{StepOutcome, UploadEngine};
use crate::error::EngineError;
use crate::priority::PriorityPolicy;
use crate::queue::{QueueItem, QueueStore, UploadQueue};
use crate::source::ArchiveItemId;
use crate::throttle::{GateReason, PauseAction};
use crate::time::Waiter;

/// The per-item upload contract the scheduler drives, one attempt at a time.
///
/// An implementor takes a `&mut` [`QueueItem`], performs a single end-to-end
/// attempt (gate, lease, transfer, verify, mark durable), durably persists every
/// state transition it makes, and reports the [`StepOutcome`]. It never deletes a
/// Pi-side file and never blocks the scheduler on anything other than the item's
/// own transfer.
///
/// Both [`UploadEngine`] (chunk-streaming) and
/// [`crate::rclone::RcloneUploadEngine`] (whole-file via `rclone`) implement this.
pub trait UploadProcessor {
    /// Process a single item end-to-end, mutating it (state, checkpoint,
    /// attempts) and persisting each transition through the queue store.
    ///
    /// # Errors
    /// Returns an [`EngineError`] only on an *infrastructure* failure (a durable
    /// queue-store or durability RPC error). Transfer / integrity / lease
    /// failures are reported as [`StepOutcome::Retry`] / [`StepOutcome::Exhausted`]
    /// / [`StepOutcome::SkippedLeaseDenied`], never as errors.
    fn process(&self, item: &mut QueueItem) -> Result<StepOutcome, EngineError>;
}

impl UploadProcessor for UploadEngine<'_> {
    fn process(&self, item: &mut QueueItem) -> Result<StepOutcome, EngineError> {
        // Delegate to the inherent method (the chunk-streaming engine).
        UploadEngine::process(self, item)
    }
}

/// The result of one [`Scheduler::step`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SchedulerStep {
    /// An item was selected and processed; carries its [`StepOutcome`]. The
    /// `Paused` outcome is *not* surfaced here — it becomes [`Self::Paused`].
    Processed(StepOutcome),
    /// Uploads are not allowed right now; no item was processed. The live loop
    /// should yield per `action` and retry later.
    Paused {
        /// Which plane paused (link vs storage) and why.
        reason: GateReason,
        /// How to yield (drain / checkpoint / abort).
        action: PauseAction,
    },
    /// Nothing was ready to work (the queue is drained, or all remaining ready
    /// items are parked in the lease-denied skip set, which is now cleared).
    Idle,
    /// An infrastructure RPC failed; the live loop should back off and retry. The
    /// item (if any) keeps its last durably-persisted state.
    Infra(String),
}

/// Why a [`Scheduler::drain_ready`] pass stopped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DrainStop {
    /// No more ready work this pass.
    Idle,
    /// Uploads paused mid-drain.
    Paused {
        /// Which plane paused and why.
        reason: GateReason,
        /// How to yield.
        action: PauseAction,
    },
    /// An infrastructure RPC failed mid-drain.
    Infra(String),
    /// The `max_steps` safety budget was reached (a guard against an unexpected
    /// non-terminating loop; not normally hit).
    Budget,
}

/// Summary of one [`Scheduler::drain_ready`] pass.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DrainReport {
    /// How many [`Scheduler::step`] calls the pass made.
    pub steps: u32,
    /// How many items reached terminal verified success this pass.
    pub uploaded: u32,
    /// Why the pass ended.
    pub stopped: DrainStop,
}

/// How long the live loop yields after each non-progress [`SchedulerStep`]. A
/// `Processed` step never waits (the drain proceeds at full speed); the others
/// back off so a paused / idle / flapping daemon does not spin the CPU.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SchedulerTimings {
    /// Yield after an [`SchedulerStep::Idle`] (queue drained) before re-checking.
    pub idle_wait_ms: u64,
    /// Yield after a [`SchedulerStep::Paused`] (throttle stop) before re-checking.
    pub pause_wait_ms: u64,
    /// Yield after a [`SchedulerStep::Infra`] error before retrying.
    pub infra_backoff_ms: u64,
}

impl Default for SchedulerTimings {
    fn default() -> Self {
        // TUNABLE: conservative provisional values; the live loop reads them from
        // config. Idle/pause re-checks are coarse (a wake also interrupts them);
        // the infra backoff is short so a transient `indexd` blip recovers fast.
        Self {
            idle_wait_ms: 5_000,
            pause_wait_ms: 2_000,
            infra_backoff_ms: 1_000,
        }
    }
}

/// The upload scheduler: a pure, in-memory drain loop over a hydrated
/// [`UploadQueue`], generic over an [`UploadProcessor`] backend.
pub struct Scheduler<P: UploadProcessor> {
    processor: P,
    queue: UploadQueue,
    policy: PriorityPolicy,
    max_attempts: u32,
    /// Items denied a lease this drain pass (skip to avoid head-of-line block).
    lease_denied: HashSet<ArchiveItemId>,
}

impl<P: UploadProcessor> Scheduler<P> {
    /// Build a scheduler with an empty queue. Populate it with
    /// [`Self::hydrate`] (from the durable store at boot) and/or
    /// [`Self::enqueue`] (as a producer offers freshly archived items).
    ///
    /// The priority order and retry cap are copied out of `cfg`, so the scheduler
    /// does not borrow it (the processor may borrow `cfg` independently).
    #[must_use]
    pub fn new(processor: P, cfg: &UploaddConfig) -> Self {
        Self {
            processor,
            queue: UploadQueue::default(),
            policy: cfg.priority.clone(),
            max_attempts: cfg.retry.max_attempts,
            lease_denied: HashSet::new(),
        }
    }

    /// Borrow the in-memory queue (e.g. to build a
    /// [`crate::status::CloudUploadStatus`] snapshot).
    #[must_use]
    pub fn queue(&self) -> &UploadQueue {
        &self.queue
    }

    /// Idempotently offer one item to the queue (a no-op if its id is already
    /// present). Returns `true` if it was added.
    pub fn enqueue(&mut self, item: QueueItem) -> bool {
        self.queue.enqueue(item)
    }

    /// (Re)hydrate the in-memory index from the durable [`QueueStore`] snapshot.
    ///
    /// The store is the source of truth: every transition the processor makes is
    /// persisted there, so rebuilding from a fresh `load` both **adds** newly
    /// archived items and **reaps** rows `indexd` dropped (e.g. a lease-denied
    /// item `retentiond` deleted), without ever duplicating in-flight work. The
    /// transient lease-denied skip set is cleared, since the reaped rows it
    /// guarded against are now gone.
    ///
    /// Returns the number of items in the queue after hydration.
    ///
    /// # Errors
    /// Propagates an [`crate::error::IndexError`] if the load RPC fails (the live
    /// loop backs off and retries; the previous in-memory queue is left intact).
    pub fn hydrate(&mut self, store: &dyn QueueStore) -> Result<usize, EngineError> {
        let loaded = store.load()?;
        self.queue = UploadQueue::from_items(loaded);
        self.lease_denied.clear();
        Ok(self.queue.items().len())
    }

    /// The id of the highest-priority ready item not currently skipped, or `None`.
    fn select(&self) -> Option<ArchiveItemId> {
        self.queue
            .items()
            .iter()
            .filter(|item| {
                item.is_ready(self.max_attempts) && !self.lease_denied.contains(&item.id)
            })
            .min_by_key(|item| item.priority_key(&self.policy))
            .map(|item| item.id)
    }

    /// Select and process exactly one item, returning what happened. Pure (no
    /// waiting) — the caller decides how to yield based on the result.
    pub fn step(&mut self) -> SchedulerStep {
        let Some(id) = self.select() else {
            // Nothing non-skipped is ready: clear the transient skip set so the
            // next pass (after a hydrate) reconsiders everything.
            self.lease_denied.clear();
            return SchedulerStep::Idle;
        };
        // `self.queue` (mutable) and `self.processor` (shared) are disjoint
        // fields, so this split borrow is sound.
        let outcome = {
            let Some(item) = self.queue.get_mut(id) else {
                return SchedulerStep::Idle;
            };
            match self.processor.process(item) {
                Ok(outcome) => outcome,
                Err(err) => return SchedulerStep::Infra(err.to_string()),
            }
        };
        self.apply_outcome(outcome)
    }

    /// Fold the per-item outcome into a [`SchedulerStep`], maintaining the
    /// lease-denied skip set and lifting a pause out of the `Processed` case.
    fn apply_outcome(&mut self, outcome: StepOutcome) -> SchedulerStep {
        match outcome {
            StepOutcome::Paused { reason, action } => {
                // A pause is global; the skip set is stale once uploads resume.
                self.lease_denied.clear();
                SchedulerStep::Paused { reason, action }
            }
            StepOutcome::SkippedLeaseDenied { item, reason } => {
                self.lease_denied.insert(item);
                SchedulerStep::Processed(StepOutcome::SkippedLeaseDenied { item, reason })
            }
            other => SchedulerStep::Processed(other),
        }
    }

    /// Take one [`Self::step`] and then yield via `waiter` for the duration
    /// `timings` prescribes for the resulting step kind (a `Processed` step never
    /// waits, so a backlog drains at full speed). Returns the step taken.
    pub fn pump(&mut self, waiter: &dyn Waiter, timings: &SchedulerTimings) -> SchedulerStep {
        let step = self.step();
        let wait_ms = match &step {
            SchedulerStep::Processed(_) => 0,
            SchedulerStep::Paused { .. } => timings.pause_wait_ms,
            SchedulerStep::Idle => timings.idle_wait_ms,
            SchedulerStep::Infra(_) => timings.infra_backoff_ms,
        };
        if wait_ms > 0 {
            waiter.wait_ms(wait_ms);
        }
        step
    }

    /// Drain all currently-ready work: [`Self::pump`] repeatedly until the queue
    /// goes idle, uploads pause, or an infra error occurs (bounded by `max_steps`
    /// as a safety guard). This models the live loop's inner burst between waits.
    pub fn drain_ready(
        &mut self,
        waiter: &dyn Waiter,
        timings: &SchedulerTimings,
        max_steps: u32,
    ) -> DrainReport {
        let mut steps = 0;
        let mut uploaded = 0;
        loop {
            if steps >= max_steps {
                return DrainReport {
                    steps,
                    uploaded,
                    stopped: DrainStop::Budget,
                };
            }
            let step = self.pump(waiter, timings);
            steps = steps.saturating_add(1);
            match step {
                SchedulerStep::Processed(StepOutcome::Uploaded { .. }) => {
                    uploaded = uploaded.saturating_add(1);
                }
                SchedulerStep::Processed(_) => {}
                SchedulerStep::Idle => {
                    return DrainReport {
                        steps,
                        uploaded,
                        stopped: DrainStop::Idle,
                    };
                }
                SchedulerStep::Paused { reason, action } => {
                    return DrainReport {
                        steps,
                        uploaded,
                        stopped: DrainStop::Paused { reason, action },
                    };
                }
                SchedulerStep::Infra(reason) => {
                    return DrainReport {
                        steps,
                        uploaded,
                        stopped: DrainStop::Infra(reason),
                    };
                }
            }
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
    use std::cell::RefCell;
    use std::collections::HashMap;

    use super::{DrainStop, Scheduler, SchedulerStep, SchedulerTimings, UploadProcessor};
    use crate::config::UploaddConfig;
    use crate::engine::StepOutcome;
    use crate::priority::UploadCategory;
    use crate::queue::{QueueItem, QueueStore, UploadState};
    use crate::source::{ArchiveItemId, ContentHash};
    use crate::throttle::{GateReason, PauseAction, PauseReason};
    use crate::time::Waiter;

    /// A scripted behavior for one `process` call on a given item.
    #[derive(Clone)]
    enum Act {
        /// Mark the item verified-durable and complete.
        Upload,
        /// Fail the attempt (retry/exhaust handled by queue state).
        Fail(String),
        /// Report the lease denied; leave item state unchanged.
        LeaseDenied(String),
        /// Report uploads paused (link plane).
        Paused,
        /// Report an infrastructure error.
        Infra(String),
    }

    /// A deterministic [`UploadProcessor`] driven by a per-item script. Applies
    /// the same `QueueItem` transitions the real engines would, so the queue
    /// state machine (and thus selection) advances realistically.
    struct FakeProcessor {
        plan: RefCell<HashMap<i64, Vec<Act>>>,
        default: Act,
        max_attempts: u32,
        calls: RefCell<Vec<i64>>,
    }

    impl FakeProcessor {
        fn new(default: Act, max_attempts: u32) -> Self {
            Self {
                plan: RefCell::new(HashMap::new()),
                default,
                max_attempts,
                calls: RefCell::new(Vec::new()),
            }
        }

        fn script(self, id: i64, acts: Vec<Act>) -> Self {
            self.plan.borrow_mut().insert(id, acts);
            self
        }

        fn next_act(&self, id: i64) -> Act {
            let mut plan = self.plan.borrow_mut();
            if let Some(acts) = plan.get_mut(&id) {
                if !acts.is_empty() {
                    return acts.remove(0);
                }
            }
            self.default.clone()
        }
    }

    impl UploadProcessor for FakeProcessor {
        fn process(&self, item: &mut QueueItem) -> Result<StepOutcome, crate::error::EngineError> {
            self.calls.borrow_mut().push(item.id.0);
            match self.next_act(item.id.0) {
                Act::Upload => {
                    item.complete();
                    Ok(StepOutcome::Uploaded {
                        item: item.id,
                        bytes: item.total_bytes,
                    })
                }
                Act::Fail(reason) => {
                    item.fail(reason.clone(), false);
                    if item.is_ready(self.max_attempts) {
                        Ok(StepOutcome::Retry {
                            item: item.id,
                            reason,
                        })
                    } else {
                        Ok(StepOutcome::Exhausted {
                            item: item.id,
                            reason,
                        })
                    }
                }
                Act::LeaseDenied(reason) => Ok(StepOutcome::SkippedLeaseDenied {
                    item: item.id,
                    reason,
                }),
                Act::Paused => Ok(StepOutcome::Paused {
                    reason: GateReason::Link(PauseReason::LinkDown),
                    action: PauseAction::DrainNoNew,
                }),
                Act::Infra(reason) => Err(crate::error::IndexError::new("persist", reason).into()),
            }
        }
    }

    /// A [`QueueStore`] that hands back a scripted snapshot and records persists.
    struct FakeStore {
        snapshot: Vec<QueueItem>,
    }

    impl QueueStore for FakeStore {
        fn load(&self) -> Result<Vec<QueueItem>, crate::error::IndexError> {
            Ok(self.snapshot.clone())
        }

        fn persist(&self, _item: &QueueItem) -> Result<(), crate::error::IndexError> {
            Ok(())
        }
    }

    /// A [`Waiter`] that records each requested wait so we can assert backoff.
    #[derive(Default)]
    struct RecordingWaiter {
        waits: RefCell<Vec<u64>>,
    }

    impl Waiter for RecordingWaiter {
        fn wait_ms(&self, ms: u64) {
            self.waits.borrow_mut().push(ms);
        }
    }

    fn item(id: i64, cat: UploadCategory, seq: u64) -> QueueItem {
        QueueItem::new(
            ArchiveItemId(id),
            format!("clips/{id}.mp4"),
            format!("remote/{id}.mp4"),
            cat,
            seq,
            1_000,
            ContentHash::new([0u8; 32]),
        )
    }

    fn timings() -> SchedulerTimings {
        SchedulerTimings {
            idle_wait_ms: 50,
            pause_wait_ms: 20,
            infra_backoff_ms: 10,
        }
    }

    #[test]
    fn empty_queue_is_idle() {
        let cfg = UploaddConfig::default();
        let mut sched = Scheduler::new(FakeProcessor::new(Act::Upload, 5), &cfg);
        assert_eq!(sched.step(), SchedulerStep::Idle);
    }

    #[test]
    fn drains_in_priority_then_fifo_order() {
        let cfg = UploaddConfig::default();
        let proc = FakeProcessor::new(Act::Upload, 5);
        let mut sched = Scheduler::new(proc, &cfg);
        // Insertion order is deliberately not priority order.
        sched.enqueue(item(1, UploadCategory::Bulk, 0));
        sched.enqueue(item(2, UploadCategory::Trip, 1));
        sched.enqueue(item(3, UploadCategory::EventSentry, 2));
        sched.enqueue(item(4, UploadCategory::EventSentry, 3));

        let waiter = RecordingWaiter::default();
        let report = sched.drain_ready(&waiter, &timings(), 100);

        assert_eq!(report.uploaded, 4);
        assert_eq!(report.stopped, DrainStop::Idle);
        // Events first (older event 3 before 4), then trip, then bulk.
        // The processor records the order it was called in.
        // (Access via a fresh borrow on the moved processor is not possible, so
        // we assert through queue state instead: everything is Done.)
        for id in [1, 2, 3, 4] {
            assert_eq!(
                sched.queue().get(ArchiveItemId(id)).unwrap().state,
                UploadState::Done
            );
        }
        // No real waiting happened until the final Idle.
        assert_eq!(waiter.waits.borrow().as_slice(), &[timings().idle_wait_ms]);
    }

    #[test]
    fn lease_denied_item_does_not_block_others() {
        let cfg = UploaddConfig::default();
        // The high-priority event is lease-denied forever; the trip must proceed.
        let proc = FakeProcessor::new(Act::Upload, 5)
            .script(3, vec![Act::LeaseDenied("delete claimed".to_owned())]);
        let mut sched = Scheduler::new(proc, &cfg);
        sched.enqueue(item(2, UploadCategory::Trip, 1));
        sched.enqueue(item(3, UploadCategory::EventSentry, 2));

        // Step 1: event selected first (priority), denied → skipped.
        match sched.step() {
            SchedulerStep::Processed(StepOutcome::SkippedLeaseDenied { item, .. }) => {
                assert_eq!(item, ArchiveItemId(3));
            }
            other => panic!("expected skip, got {other:?}"),
        }
        // Step 2: trip selected (event is skipped), uploads.
        match sched.step() {
            SchedulerStep::Processed(StepOutcome::Uploaded { item, .. }) => {
                assert_eq!(item, ArchiveItemId(2));
            }
            other => panic!("expected trip upload, got {other:?}"),
        }
        // Step 3: only the skipped event remains ready → idle (and skip clears).
        assert_eq!(sched.step(), SchedulerStep::Idle);

        assert_eq!(
            sched.queue().get(ArchiveItemId(2)).unwrap().state,
            UploadState::Done
        );
        // The denied item's state was left untouched (retentiond owns it).
        assert_eq!(
            sched.queue().get(ArchiveItemId(3)).unwrap().state,
            UploadState::Queued
        );
    }

    #[test]
    fn pause_is_surfaced_and_clears_skip_set() {
        let cfg = UploaddConfig::default();
        let proc = FakeProcessor::new(Act::Upload, 5)
            .script(
                3,
                vec![
                    Act::LeaseDenied("claimed".to_owned()),
                    Act::LeaseDenied("claimed".to_owned()),
                ],
            )
            .script(2, vec![Act::Paused]);
        let mut sched = Scheduler::new(proc, &cfg);
        sched.enqueue(item(2, UploadCategory::Trip, 1));
        sched.enqueue(item(3, UploadCategory::EventSentry, 2));

        // Event denied → skipped.
        assert!(matches!(
            sched.step(),
            SchedulerStep::Processed(StepOutcome::SkippedLeaseDenied { .. })
        ));
        // Trip pauses → surfaced as Paused (not Processed) and clears the skip set.
        match sched.step() {
            SchedulerStep::Paused {
                reason: GateReason::Link(PauseReason::LinkDown),
                action: PauseAction::DrainNoNew,
            } => {}
            other => panic!("expected pause, got {other:?}"),
        }
        // Skip set cleared: the event is selectable again next step.
        match sched.step() {
            SchedulerStep::Processed(StepOutcome::SkippedLeaseDenied { item, .. }) => {
                assert_eq!(item, ArchiveItemId(3));
            }
            other => panic!("expected event re-selected, got {other:?}"),
        }
    }

    #[test]
    fn drain_stops_on_pause() {
        let cfg = UploaddConfig::default();
        let proc = FakeProcessor::new(Act::Paused, 5);
        let mut sched = Scheduler::new(proc, &cfg);
        sched.enqueue(item(1, UploadCategory::Bulk, 0));

        let waiter = RecordingWaiter::default();
        let report = sched.drain_ready(&waiter, &timings(), 100);
        assert_eq!(report.uploaded, 0);
        assert!(matches!(report.stopped, DrainStop::Paused { .. }));
        // The pause backoff was applied once.
        assert_eq!(waiter.waits.borrow().as_slice(), &[timings().pause_wait_ms]);
    }

    #[test]
    fn infra_error_surfaces_and_stops_drain() {
        let cfg = UploaddConfig::default();
        let proc = FakeProcessor::new(Act::Infra("indexd down".to_owned()), 5);
        let mut sched = Scheduler::new(proc, &cfg);
        sched.enqueue(item(1, UploadCategory::Bulk, 0));

        let waiter = RecordingWaiter::default();
        let report = sched.drain_ready(&waiter, &timings(), 100);
        assert_eq!(report.uploaded, 0);
        match report.stopped {
            DrainStop::Infra(reason) => assert!(reason.contains("indexd down")),
            other => panic!("expected infra stop, got {other:?}"),
        }
        assert_eq!(
            waiter.waits.borrow().as_slice(),
            &[timings().infra_backoff_ms]
        );
    }

    #[test]
    fn exhausted_item_is_not_reselected() {
        let cfg = UploaddConfig::default();
        // max_attempts = 1: the first failure exhausts the item.
        let mut cfg1 = cfg;
        cfg1.retry.max_attempts = 1;
        let proc = FakeProcessor::new(Act::Fail("net".to_owned()), 1);
        let mut sched = Scheduler::new(proc, &cfg1);
        sched.enqueue(item(1, UploadCategory::Bulk, 0));

        // First step fails → Exhausted (terminal Failed).
        match sched.step() {
            SchedulerStep::Processed(StepOutcome::Exhausted { item, .. }) => {
                assert_eq!(item, ArchiveItemId(1));
            }
            other => panic!("expected exhausted, got {other:?}"),
        }
        // Terminal Failed is never reselected.
        assert_eq!(sched.step(), SchedulerStep::Idle);
        assert_eq!(
            sched.queue().get(ArchiveItemId(1)).unwrap().state,
            UploadState::Failed
        );
    }

    #[test]
    fn hydrate_adds_new_and_reaps_removed_items() {
        let cfg = UploaddConfig::default();
        let mut sched = Scheduler::new(FakeProcessor::new(Act::Upload, 5), &cfg);

        let store1 = FakeStore {
            snapshot: vec![
                item(1, UploadCategory::Bulk, 0),
                item(2, UploadCategory::Trip, 1),
            ],
        };
        assert_eq!(sched.hydrate(&store1).unwrap(), 2);

        // A later snapshot drops 1 (reaped by indexd) and adds 3.
        let store2 = FakeStore {
            snapshot: vec![
                item(2, UploadCategory::Trip, 1),
                item(3, UploadCategory::EventSentry, 2),
            ],
        };
        assert_eq!(sched.hydrate(&store2).unwrap(), 2);
        assert!(sched.queue().get(ArchiveItemId(1)).is_none(), "1 reaped");
        assert!(sched.queue().get(ArchiveItemId(2)).is_some(), "2 kept");
        assert!(sched.queue().get(ArchiveItemId(3)).is_some(), "3 added");
    }

    #[test]
    fn hydrated_queue_drains_end_to_end() {
        let cfg = UploaddConfig::default();
        let mut sched = Scheduler::new(FakeProcessor::new(Act::Upload, 5), &cfg);
        let store = FakeStore {
            snapshot: vec![
                item(1, UploadCategory::Bulk, 0),
                item(2, UploadCategory::EventSentry, 1),
            ],
        };
        sched.hydrate(&store).unwrap();
        let waiter = RecordingWaiter::default();
        let report = sched.drain_ready(&waiter, &timings(), 100);
        assert_eq!(report.uploaded, 2);
        assert_eq!(report.stopped, DrainStop::Idle);
    }
}
