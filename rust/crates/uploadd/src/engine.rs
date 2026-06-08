//! The orchestration **engine**: process one queued item end-to-end —
//! throttle-gate, acquire an upload lease, drive a paced + resumable +
//! integrity-checked transfer (renewing the lease by heartbeat), flag durability
//! on success, and always release the lease.
//!
//! This is where the queue, lease, throttle, transfer, and durability seams come
//! together. It is pure over those traits, so every branch — happy path, resume,
//! mid-transfer failure, integrity mismatch, a lost lease, a throttle pause, a
//! denied lease — is host-unit-tested with deterministic mocks and a synthetic
//! clock (no real sleeping, no real I/O).
//!
//! # Invariant guards live here
//!
//! - **Source only from the archive.** The only path the transfer ever opens is
//!   produced by [`crate::source::ArchiveRoot::resolve`]; a rejected path fails
//!   the item (retryable) and **never** reaches the uploader — the live LUN is
//!   unreachable.
//! - **Never delete.** There is no delete call anywhere in this flow; on success
//!   `uploadd` only flags `UPLOADED_VERIFIED`.
//! - **Never exceed the cap.** Every chunk is gated by the [`Pacer`] before it is
//!   read or sent.
//! - **Never evict mid-read.** The upload lease is held for the whole transfer
//!   and renewed on the configured cadence; a `Stale` renew stops the transfer.

use crate::config::UploaddConfig;
use crate::durability::DurabilityClient;
use crate::error::{EngineError, IndexError, SourceError, TransferError};
use crate::lease::{LeaseClient, LeaseGen, LeaseGrant, LeaseId, LeaseKind, RenewResult};
use crate::queue::{QueueItem, QueueStore};
use crate::source::{ArchiveItemId, ArchiveRoot, ArchiveSource, ContentHash};
use crate::throttle::{GateReason, Pacer, PauseAction, ThrottleSource, UploadGate};
use crate::time::{Clock, MonoMs, Waiter};
use crate::transfer::{Integrity, Uploader, verify_digest};

/// The set of I/O seams plus config the engine drives. Fields are public so the
/// engine can be assembled with a struct literal (avoiding a wide constructor);
/// each is a borrowed trait object resolved by the live binary or a test mock.
pub struct UploadEngine<'a> {
    /// Tunable policy (lease TTL/renew, retry cap, priority, holder id).
    pub cfg: &'a UploaddConfig,
    /// The archive root every source read is confined under.
    pub archive_root: &'a ArchiveRoot,
    /// Archive read seam.
    pub source: &'a dyn ArchiveSource,
    /// Transfer backend seam.
    pub uploader: &'a dyn Uploader,
    /// Lease acquire/renew/release seam (`indexd`).
    pub lease: &'a dyn LeaseClient,
    /// Durability-flag seam (`indexd`).
    pub durability: &'a dyn DurabilityClient,
    /// Durable queue persistence seam (`indexd`).
    pub queue_store: &'a dyn QueueStore,
    /// Combined `wifid` + `retentiond` throttle source.
    pub throttle: &'a dyn ThrottleSource,
    /// Monotonic clock.
    pub clock: &'a dyn Clock,
    /// Self-pacing waiter.
    pub waiter: &'a dyn Waiter,
}

/// The outcome of processing one item.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StepOutcome {
    /// The item uploaded, verified, and was flagged durable. Terminal success.
    Uploaded {
        /// The item.
        item: ArchiveItemId,
        /// Bytes uploaded.
        bytes: u64,
    },
    /// The attempt failed but retries remain; the item is `Failed` and will be
    /// re-selected.
    Retry {
        /// The item.
        item: ArchiveItemId,
        /// Why it failed.
        reason: String,
    },
    /// The attempt failed and retries are exhausted; the item is parked as
    /// terminal `Failed` for operator inspection (never deleted).
    Exhausted {
        /// The item.
        item: ArchiveItemId,
        /// Why it failed.
        reason: String,
    },
    /// The lease was denied (the item is being deleted by `retentiond`); no
    /// transfer was attempted and the item state is unchanged.
    ///
    /// The item's queue state is intentionally left untouched: a denial means
    /// `retentiond` has claimed the item for deletion, so `indexd` will drop the
    /// row and the next [`QueueStore::load`] hydrate reaps it. The live scheduler
    /// must therefore treat this outcome as "skip and back off" rather than
    /// immediately reselecting the same item (which would otherwise head-of-line
    /// block lower-priority work until the next hydrate).
    ///
    /// [`QueueStore::load`]: crate::queue::QueueStore::load
    SkippedLeaseDenied {
        /// The item.
        item: ArchiveItemId,
        /// Why the lease was denied.
        reason: String,
    },
    /// Uploads are not allowed right now; no lease was taken and no source was
    /// read. The caller should yield per `action` and retry later.
    Paused {
        /// Which plane paused and why.
        reason: GateReason,
        /// How to yield.
        action: PauseAction,
    },
}

/// A lease currently held by the engine for one in-flight transfer. Only the
/// identity + generation token are needed to renew/release; the deadline/boot
/// live in `indexd` and are re-read implicitly by each conditional renew.
struct HeldLease {
    lease_id: LeaseId,
    gen_token: LeaseGen,
}

/// Why a transfer stopped before producing a verified digest.
enum TransferStop {
    /// A chunk transmit or source read failed (resume keeps the checkpoint).
    Recoverable(String),
    /// The lease was lost (`Stale` renew) — stop and re-acquire later.
    LeaseLost(String),
    /// The throttle paused uploads mid-transfer. The checkpoint is already
    /// persisted, so the item resumes when uploads resume. This is **not** a
    /// failure — no upload attempt is charged.
    Paused {
        /// How to yield (drain / checkpoint / abort).
        action: PauseAction,
        /// Which plane paused, and why.
        reason: GateReason,
    },
    /// A durable queue-store RPC failed mid-transfer. Surfaced as an
    /// infrastructure error (not a transfer failure) so a flaky `indexd` RPC
    /// never consumes an upload attempt or parks a healthy item.
    Infra(IndexError),
}

impl UploadEngine<'_> {
    /// Process a single item end-to-end. Mutates `item` (state, checkpoint,
    /// attempts) and persists each transition durably through the queue store.
    ///
    /// # Errors
    /// Returns [`EngineError`] only on an infrastructure failure (a queue-store
    /// or durability RPC error). Transfer/integrity/lease failures are *not*
    /// errors — they are reported as [`StepOutcome::Retry`] /
    /// [`StepOutcome::Exhausted`] so the durable queue retries them.
    pub fn process(&self, item: &mut QueueItem) -> Result<StepOutcome, EngineError> {
        let (max_tx, max_chunk) = match self.throttle.current().gate() {
            UploadGate::Pause { action, reason } => {
                return Ok(StepOutcome::Paused { reason, action });
            }
            UploadGate::Run {
                max_tx_bytes_per_s,
                max_chunk_bytes,
            } => (max_tx_bytes_per_s, max_chunk_bytes),
        };

        let held = match self.acquire(item.id) {
            Ok(held) => held,
            Err(reason) => {
                return Ok(StepOutcome::SkippedLeaseDenied {
                    item: item.id,
                    reason,
                });
            }
        };

        // Mark in-flight and persist *before* sending so a crash leaves a
        // resumable InProgress row, never a lost item. If this persist fails,
        // release the just-acquired lease before surfacing the error so a failed
        // persist never leaks a lease that would block eviction until its TTL.
        item.begin();
        if let Err(e) = self.queue_store.persist(item) {
            let _ = self.lease.release(held.lease_id, held.gen_token);
            return Err(EngineError::Index(e));
        }

        let result = self.run_transfer(item, &held, max_tx, max_chunk);

        // The lease is always released, on every path out (best effort: a failed
        // release just means the lease lapses by its monotonic deadline).
        let _ = self.lease.release(held.lease_id, held.gen_token);

        result
    }

    /// Acquire an upload lease, returning the held lease or a denial reason.
    fn acquire(&self, id: ArchiveItemId) -> Result<HeldLease, String> {
        match self.lease.acquire(
            id,
            LeaseKind::Upload,
            &self.cfg.holder_id,
            self.cfg.lease.ttl_ms,
        ) {
            LeaseGrant::Granted {
                lease_id,
                gen_token,
                expires_mono_ms: _,
            } => Ok(HeldLease {
                lease_id,
                gen_token,
            }),
            LeaseGrant::Denied { reason } => Err(reason),
        }
    }

    /// Drive the transfer, then verify + finalize. Translates the internal stop
    /// reasons into a [`StepOutcome`], applying the queue state machine.
    fn run_transfer(
        &self,
        item: &mut QueueItem,
        held: &HeldLease,
        max_tx: u64,
        max_chunk: u32,
    ) -> Result<StepOutcome, EngineError> {
        match self.transfer(item, held, max_tx, max_chunk) {
            Ok(remote_digest) => self.finish_verified(item, remote_digest),
            // A recoverable stop and a lost lease both park the item for retry
            // keeping its checkpoint (resume continues from the last good byte).
            Err(TransferStop::Recoverable(reason) | TransferStop::LeaseLost(reason)) => {
                self.fail_item(item, &reason, false)
            }
            // A mid-transfer pause is not a failure: the item stays InProgress
            // with its checkpoint and resumes when uploads are allowed again.
            Err(TransferStop::Paused { action, reason }) => {
                Ok(StepOutcome::Paused { reason, action })
            }
            // An infra (queue-store RPC) failure surfaces as an error so the live
            // loop backs off, without charging the item an upload attempt.
            Err(TransferStop::Infra(e)) => Err(EngineError::Index(e)),
        }
    }

    /// The chunked, paced, lease-renewing transfer loop. On success returns the
    /// remote-computed digest from `finalize`.
    ///
    /// Each iteration re-reads the throttle (so a mid-transfer pause or a lowered
    /// cap is honored immediately, never run on a stale cap), then renews the
    /// lease *before* the potentially long pace wait / send when the cadence is
    /// due, so the lease deadline always covers the upcoming chunk.
    fn transfer(
        &self,
        item: &mut QueueItem,
        held: &HeldLease,
        initial_max_tx: u64,
        initial_max_chunk: u32,
    ) -> Result<ContentHash, TransferStop> {
        let path = self
            .archive_root
            .resolve(&item.source_rel)
            .map_err(|e| stop_from_source(&e))?;

        let mut max_tx = initial_max_tx;
        let mut max_chunk = initial_max_chunk;
        // Capacity is one second of the cap (a bounded burst). A single paced
        // write is separately clamped to the cap (see `chunk_len`), so the burst
        // can never exceed the published per-second ceiling even when the
        // per-write ceiling is larger.
        let mut pacer = Pacer::new(max_tx, max_tx.max(1), self.clock.mono_now().0);
        let mut last_renew = self.clock.mono_now();

        let mut offset = item.bytes_uploaded;
        let total = item.total_bytes;
        while offset < total {
            self.apply_throttle(&mut pacer, &mut max_tx, &mut max_chunk)?;
            self.maybe_renew(held, &mut last_renew)?;

            let chunk_len = chunk_len(total - offset, max_chunk, max_tx);
            self.pace(&mut pacer, chunk_len);

            let want = usize::try_from(chunk_len).unwrap_or(usize::MAX);
            let data = self
                .source
                .read_chunk(&path, offset, want)
                .map_err(|e| stop_from_source(&e))?;
            if data.is_empty() {
                // Unexpected EOF before the declared total: a truncated or
                // replaced source. Treat as a recoverable read error (retry),
                // never as silent completion.
                return Err(TransferStop::Recoverable(format!(
                    "source ended early at offset {offset} of {total} bytes"
                )));
            }

            self.uploader
                .put_chunk(&item.remote_key, offset, &data)
                .map_err(|e| stop_from_transfer(&e))?;

            let sent = u64::try_from(data.len()).unwrap_or(0);
            offset = offset.saturating_add(sent);
            item.checkpoint(offset);
            self.queue_store
                .persist(item)
                .map_err(TransferStop::Infra)?;
        }

        self.uploader
            .finalize(&item.remote_key, total)
            .map_err(|e| stop_from_transfer(&e))
    }

    /// Re-read the throttle. On a fresh `Pause`, stop the transfer at the current
    /// (persisted) checkpoint; on `Run`, apply any cap change to the pacer so a
    /// mid-transfer `NearDeadlock` backoff (or recovery) takes effect at once.
    fn apply_throttle(
        &self,
        pacer: &mut Pacer,
        max_tx: &mut u64,
        max_chunk: &mut u32,
    ) -> Result<(), TransferStop> {
        match self.throttle.current().gate() {
            UploadGate::Pause { action, reason } => Err(TransferStop::Paused { action, reason }),
            UploadGate::Run {
                max_tx_bytes_per_s,
                max_chunk_bytes,
            } => {
                if max_tx_bytes_per_s != *max_tx || max_chunk_bytes != *max_chunk {
                    *max_tx = max_tx_bytes_per_s;
                    *max_chunk = max_chunk_bytes;
                    pacer.set_rate(max_tx_bytes_per_s, max_tx_bytes_per_s.max(1));
                }
                Ok(())
            }
        }
    }

    /// Block (via the [`Waiter`]) until `bytes` of TX allowance are available.
    fn pace(&self, pacer: &mut Pacer, bytes: u64) {
        loop {
            let now = self.clock.mono_now().0;
            if pacer.try_consume(bytes, now) {
                return;
            }
            let wait = pacer.wait_ms_for(bytes, now).max(1);
            self.waiter.wait_ms(wait);
        }
    }

    /// Renew the lease if the renew interval has elapsed. A `Stale` result stops
    /// the transfer.
    fn maybe_renew(&self, held: &HeldLease, last_renew: &mut MonoMs) -> Result<(), TransferStop> {
        let now = self.clock.mono_now();
        if now.saturating_elapsed_since(*last_renew) < self.cfg.lease.renew_interval_ms {
            return Ok(());
        }
        match self
            .lease
            .renew(held.lease_id, held.gen_token, self.cfg.lease.ttl_ms)
        {
            RenewResult::Renewed { expires_mono_ms: _ } => {
                *last_renew = now;
                Ok(())
            }
            RenewResult::Stale { reason } => Err(TransferStop::LeaseLost(reason)),
        }
    }

    /// A transfer produced a digest: verify integrity, then on success flag
    /// durability and complete; on a mismatch, fail (resetting the checkpoint).
    fn finish_verified(
        &self,
        item: &mut QueueItem,
        remote_digest: ContentHash,
    ) -> Result<StepOutcome, EngineError> {
        match verify_digest(item.expected_hash, remote_digest) {
            Integrity::Corrupt => {
                self.fail_item(item, "integrity check failed: remote digest mismatch", true)
            }
            Integrity::Verified => {
                // Durability is the ONLY authority uploadd asserts over the file;
                // it never deletes. The mark is idempotent, so a crash before the
                // queue persist below simply re-marks on resume.
                self.durability.mark_uploaded_verified(item.id)?;
                item.complete();
                self.queue_store.persist(item)?;
                Ok(StepOutcome::Uploaded {
                    item: item.id,
                    bytes: item.total_bytes,
                })
            }
        }
    }

    /// Apply a failure to the item, persist it, and report Retry vs Exhausted.
    fn fail_item(
        &self,
        item: &mut QueueItem,
        reason: &str,
        reset_offset: bool,
    ) -> Result<StepOutcome, EngineError> {
        item.fail(reason, reset_offset);
        self.queue_store.persist(item)?;
        if item.is_ready(self.cfg.retry.max_attempts) {
            Ok(StepOutcome::Retry {
                item: item.id,
                reason: reason.to_owned(),
            })
        } else {
            Ok(StepOutcome::Exhausted {
                item: item.id,
                reason: reason.to_owned(),
            })
        }
    }
}

/// The largest chunk to read/send next: the smaller of the remaining bytes, the
/// published per-write ceiling, and one second of the TX cap (so a single paced
/// write can never burst past the per-second cap). Never zero while bytes remain
/// (the cap is `> 0` whenever the gate says `Run`).
fn chunk_len(remaining: u64, max_chunk: u32, max_tx: u64) -> u64 {
    let per_write = u64::from(max_chunk).max(1).min(max_tx.max(1));
    remaining.min(per_write)
}

/// Map a source read error into a transfer stop. An archive-root rejection is
/// recoverable at the queue level (the item is parked after retries) — it never
/// reaches the uploader, upholding the "source only from archive" invariant.
fn stop_from_source(err: &SourceError) -> TransferStop {
    TransferStop::Recoverable(err.to_string())
}

/// Map a transfer-backend error into a transfer stop (mid-transfer or finalize).
fn stop_from_transfer(err: &TransferError) -> TransferStop {
    TransferStop::Recoverable(err.to_string())
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing,
    clippy::cast_possible_truncation,
    clippy::cast_sign_loss,
    clippy::cast_possible_wrap
)]
mod tests {
    use std::cell::{Cell, RefCell};

    use super::{StepOutcome, UploadEngine};
    use crate::config::UploaddConfig;
    use crate::durability::DurabilityClient;
    use crate::error::{IndexError, SourceError, TransferError};
    use crate::lease::{
        LeaseClient, LeaseGen, LeaseGrant, LeaseId, LeaseKind, ReleaseResult, RenewResult,
    };
    use crate::priority::UploadCategory;
    use crate::queue::{QueueItem, QueueStore};
    use crate::source::{ArchiveItemId, ArchivePath, ArchiveRoot, ArchiveSource, ContentHash};
    use crate::throttle::{
        LinkMode, PauseAction, PauseReason, StoragePressure, ThrottleSnapshot, ThrottleSource,
        WifiThrottle,
    };
    use crate::time::{BootId, Clock, MonoMs, Waiter};

    /// Deterministic digest used by both the source-expected hash and the mock
    /// uploader, so a correct end-to-end transfer verifies.
    fn digest(bytes: &[u8]) -> ContentHash {
        let mut h = [0u8; 32];
        for (i, b) in bytes.iter().enumerate() {
            h[i % 32] = h[i % 32].wrapping_add(*b);
        }
        ContentHash::new(h)
    }

    /// A shared synthetic timeline: the `Clock` reads it, the `Waiter` advances
    /// it. No real time passes.
    struct Timeline {
        ms: Cell<i64>,
    }
    impl Timeline {
        fn new() -> Self {
            Self { ms: Cell::new(0) }
        }
        fn now(&self) -> i64 {
            self.ms.get()
        }
    }
    impl Clock for Timeline {
        fn mono_now(&self) -> MonoMs {
            MonoMs(self.ms.get())
        }
        fn boot_id(&self) -> BootId {
            BootId("boot-test".to_owned())
        }
    }
    impl Waiter for Timeline {
        fn wait_ms(&self, ms: u64) {
            let add = i64::try_from(ms).unwrap_or(i64::MAX);
            self.ms.set(self.ms.get().saturating_add(add));
        }
    }

    /// In-memory archive source. Records every path it is asked to read so tests
    /// can assert the engine only ever reads under the archive root.
    struct MockSource {
        data: Vec<u8>,
        read_paths: RefCell<Vec<String>>,
        fail_at: Option<u64>,
        eof_at: Option<u64>,
    }
    impl MockSource {
        fn new(data: Vec<u8>) -> Self {
            Self {
                data,
                read_paths: RefCell::new(Vec::new()),
                fail_at: None,
                eof_at: None,
            }
        }
    }
    impl ArchiveSource for MockSource {
        fn size(&self, _path: &ArchivePath) -> Result<u64, SourceError> {
            Ok(self.data.len() as u64)
        }
        fn read_chunk(
            &self,
            path: &ArchivePath,
            offset: u64,
            len: usize,
        ) -> Result<Vec<u8>, SourceError> {
            self.read_paths.borrow_mut().push(path.as_str().to_owned());
            if let Some(f) = self.fail_at {
                if offset >= f {
                    return Err(SourceError::Io("injected read failure".to_owned()));
                }
            }
            if let Some(e) = self.eof_at {
                if offset >= e {
                    return Ok(Vec::new()); // simulate a truncated/replaced source
                }
            }
            let start = usize::try_from(offset)
                .unwrap_or(usize::MAX)
                .min(self.data.len());
            let end = start.saturating_add(len).min(self.data.len());
            Ok(self.data[start..end].to_vec())
        }
    }

    /// Mock transfer backend. Concatenates received chunks (by offset) to compute
    /// a finalize digest; can fail at an offset or force a wrong digest.
    struct MockUploader {
        chunks: RefCell<Vec<(u64, Vec<u8>)>>,
        fail_at: Option<u64>,
        force_digest: Option<ContentHash>,
    }
    impl MockUploader {
        fn new() -> Self {
            Self {
                chunks: RefCell::new(Vec::new()),
                fail_at: None,
                force_digest: None,
            }
        }
        fn received_bytes(&self) -> u64 {
            self.chunks
                .borrow()
                .iter()
                .map(|(_, d)| d.len() as u64)
                .sum()
        }
    }
    impl crate::transfer::Uploader for MockUploader {
        fn put_chunk(
            &self,
            _remote_key: &str,
            offset: u64,
            data: &[u8],
        ) -> Result<(), TransferError> {
            if let Some(f) = self.fail_at {
                if offset >= f {
                    return Err(TransferError::Chunk {
                        offset,
                        reason: "injected transmit failure".to_owned(),
                    });
                }
            }
            self.chunks.borrow_mut().push((offset, data.to_vec()));
            Ok(())
        }
        fn finalize(&self, _remote_key: &str, _total: u64) -> Result<ContentHash, TransferError> {
            if let Some(d) = self.force_digest {
                return Ok(d);
            }
            let mut chunks = self.chunks.borrow().clone();
            chunks.sort_by_key(|(o, _)| *o);
            let mut all = Vec::new();
            for (_, d) in chunks {
                all.extend_from_slice(&d);
            }
            Ok(digest(&all))
        }
    }

    /// Mock lease client recording acquire/renew/release and able to deny or go
    /// stale.
    struct MockLease {
        deny: bool,
        stale_on_call: Option<u32>,
        acquire_calls: Cell<u32>,
        renew_calls: Cell<u32>,
        release_calls: Cell<u32>,
        acquire_kind: RefCell<Option<LeaseKind>>,
        acquire_ttl: Cell<i64>,
    }
    impl MockLease {
        fn granting() -> Self {
            Self {
                deny: false,
                stale_on_call: None,
                acquire_calls: Cell::new(0),
                renew_calls: Cell::new(0),
                release_calls: Cell::new(0),
                acquire_kind: RefCell::new(None),
                acquire_ttl: Cell::new(0),
            }
        }
    }
    impl LeaseClient for MockLease {
        fn acquire(
            &self,
            _item: ArchiveItemId,
            kind: LeaseKind,
            _holder: &str,
            ttl_ms: i64,
        ) -> LeaseGrant {
            self.acquire_calls.set(self.acquire_calls.get() + 1);
            *self.acquire_kind.borrow_mut() = Some(kind);
            self.acquire_ttl.set(ttl_ms);
            if self.deny {
                return LeaseGrant::Denied {
                    reason: "item DELETE_CLAIMED".to_owned(),
                };
            }
            LeaseGrant::Granted {
                lease_id: LeaseId(1),
                gen_token: LeaseGen(0xfeed),
                expires_mono_ms: MonoMs(ttl_ms),
            }
        }
        fn renew(&self, _id: LeaseId, _g: LeaseGen, ttl_ms: i64) -> RenewResult {
            let n = self.renew_calls.get() + 1;
            self.renew_calls.set(n);
            if self.stale_on_call == Some(n) {
                return RenewResult::Stale {
                    reason: "subject no longer LIVE".to_owned(),
                };
            }
            RenewResult::Renewed {
                expires_mono_ms: MonoMs(ttl_ms),
            }
        }
        fn release(&self, _id: LeaseId, _g: LeaseGen) -> ReleaseResult {
            self.release_calls.set(self.release_calls.get() + 1);
            ReleaseResult::Released
        }
    }

    /// Mock durability sink.
    struct MockDurability {
        marked: RefCell<Vec<ArchiveItemId>>,
    }
    impl MockDurability {
        fn new() -> Self {
            Self {
                marked: RefCell::new(Vec::new()),
            }
        }
    }
    impl DurabilityClient for MockDurability {
        fn mark_uploaded_verified(&self, item: ArchiveItemId) -> Result<(), IndexError> {
            self.marked.borrow_mut().push(item);
            Ok(())
        }
    }

    /// Mock durable queue store (records every persisted snapshot). Can be told
    /// to fail a specific persist call (1-indexed) to exercise infra-error paths.
    struct MockQueueStore {
        persisted: RefCell<Vec<QueueItem>>,
        calls: Cell<u32>,
        fail_on_call: Option<u32>,
    }
    impl MockQueueStore {
        fn new() -> Self {
            Self {
                persisted: RefCell::new(Vec::new()),
                calls: Cell::new(0),
                fail_on_call: None,
            }
        }
        fn failing_on(call: u32) -> Self {
            let mut s = Self::new();
            s.fail_on_call = Some(call);
            s
        }
    }
    impl QueueStore for MockQueueStore {
        fn load(&self) -> Result<Vec<QueueItem>, IndexError> {
            Ok(self.persisted.borrow().clone())
        }
        fn persist(&self, item: &QueueItem) -> Result<(), IndexError> {
            let n = self.calls.get() + 1;
            self.calls.set(n);
            if self.fail_on_call == Some(n) {
                return Err(IndexError::new("persist", "injected persist failure"));
            }
            let mut p = self.persisted.borrow_mut();
            if let Some(slot) = p.iter_mut().find(|i| i.id == item.id) {
                *slot = item.clone();
            } else {
                p.push(item.clone());
            }
            Ok(())
        }
    }

    /// Fixed throttle source.
    struct FixedThrottle {
        snap: ThrottleSnapshot,
    }
    impl ThrottleSource for FixedThrottle {
        fn current(&self) -> ThrottleSnapshot {
            self.snap
        }
    }

    /// Throttle that returns `first` for the first `switch_after` reads, then
    /// `second` — to drive a mid-transfer pause or cap change.
    struct SwitchingThrottle {
        calls: Cell<u32>,
        switch_after: u32,
        first: ThrottleSnapshot,
        second: ThrottleSnapshot,
    }
    impl ThrottleSource for SwitchingThrottle {
        fn current(&self) -> ThrottleSnapshot {
            let n = self.calls.get() + 1;
            self.calls.set(n);
            if n <= self.switch_after {
                self.first
            } else {
                self.second
            }
        }
    }

    fn paused_snapshot() -> ThrottleSnapshot {
        ThrottleSnapshot {
            wifi: WifiThrottle::closed(),
            storage: StoragePressure::open(),
        }
    }

    fn running(max_tx: u64, max_chunk: u32) -> ThrottleSnapshot {
        ThrottleSnapshot {
            wifi: WifiThrottle {
                seq: 1,
                link_mode: LinkMode::Sta,
                uploads_allowed: true,
                max_tx_bytes_per_s: max_tx,
                max_chunk_bytes: max_chunk,
                action: PauseAction::Run,
                reason: PauseReason::None,
            },
            storage: StoragePressure::open(),
        }
    }

    fn test_item(total: u64, expected: ContentHash) -> QueueItem {
        QueueItem::new(
            ArchiveItemId(42),
            "SentryClips/2026/event.mp4",
            "remote/event.mp4",
            UploadCategory::EventSentry,
            0,
            total,
            expected,
        )
    }

    fn root() -> ArchiveRoot {
        ArchiveRoot::new("/mnt/archive")
    }

    #[test]
    fn happy_path_uploads_verifies_marks_durable_and_releases_lease() {
        let data: Vec<u8> = (0..1000u32).map(|i| (i % 251) as u8).collect();
        let cfg = UploaddConfig::default();
        let root = root();
        let source = MockSource::new(data.clone());
        let uploader = MockUploader::new();
        let lease = MockLease::granting();
        let durability = MockDurability::new();
        let store = MockQueueStore::new();
        let throttle = FixedThrottle {
            snap: running(1_000_000, 4096),
        };
        let timeline = Timeline::new();
        let engine = UploadEngine {
            cfg: &cfg,
            archive_root: &root,
            source: &source,
            uploader: &uploader,
            lease: &lease,
            durability: &durability,
            queue_store: &store,
            throttle: &throttle,
            clock: &timeline,
            waiter: &timeline,
        };
        let mut item = test_item(data.len() as u64, digest(&data));

        let outcome = engine.process(&mut item).expect("no infra error");
        assert_eq!(
            outcome,
            StepOutcome::Uploaded {
                item: ArchiveItemId(42),
                bytes: 1000
            }
        );
        assert_eq!(item.state, crate::queue::UploadState::Done);
        assert_eq!(uploader.received_bytes(), 1000, "all bytes sent");
        assert_eq!(durability.marked.borrow().as_slice(), &[ArchiveItemId(42)]);
        assert_eq!(lease.acquire_calls.get(), 1);
        assert_eq!(*lease.acquire_kind.borrow(), Some(LeaseKind::Upload));
        assert_eq!(lease.acquire_ttl.get(), cfg.lease.ttl_ms);
        assert_eq!(lease.release_calls.get(), 1, "lease released on success");
        // Invariant: every read was under the archive root (never the LUN).
        assert!(
            source
                .read_paths
                .borrow()
                .iter()
                .all(|p| p.starts_with("/mnt/archive/")),
            "a read escaped the archive root"
        );
    }

    #[test]
    fn mid_transfer_failure_retries_and_keeps_checkpoint() {
        let data: Vec<u8> = (0..1000u32).map(|i| (i % 251) as u8).collect();
        let cfg = UploaddConfig::default();
        let root = root();
        let source = MockSource::new(data.clone());
        let mut uploader = MockUploader::new();
        uploader.fail_at = Some(500); // fail once we pass halfway
        let lease = MockLease::granting();
        let durability = MockDurability::new();
        let store = MockQueueStore::new();
        let throttle = FixedThrottle {
            snap: running(1_000_000, 200),
        };
        let timeline = Timeline::new();
        let engine = UploadEngine {
            cfg: &cfg,
            archive_root: &root,
            source: &source,
            uploader: &uploader,
            lease: &lease,
            durability: &durability,
            queue_store: &store,
            throttle: &throttle,
            clock: &timeline,
            waiter: &timeline,
        };
        let mut item = test_item(data.len() as u64, digest(&data));

        let outcome = engine.process(&mut item).expect("no infra error");
        match outcome {
            StepOutcome::Retry { item: id, .. } => assert_eq!(id, ArchiveItemId(42)),
            other => panic!("expected Retry, got {other:?}"),
        }
        assert_eq!(item.state, crate::queue::UploadState::Failed);
        assert_eq!(item.attempts, 1);
        assert!(
            item.bytes_uploaded > 0 && item.bytes_uploaded < 1000,
            "checkpoint retained mid-file: {}",
            item.bytes_uploaded
        );
        assert!(
            durability.marked.borrow().is_empty(),
            "never durable on failure"
        );
        assert_eq!(lease.release_calls.get(), 1, "lease released on failure");
    }

    #[test]
    fn integrity_mismatch_resets_checkpoint_and_is_not_durable() {
        let data: Vec<u8> = (0..400u32).map(|i| (i % 251) as u8).collect();
        let cfg = UploaddConfig::default();
        let root = root();
        let source = MockSource::new(data.clone());
        let mut uploader = MockUploader::new();
        uploader.force_digest = Some(ContentHash::new([0xff; 32])); // wrong
        let lease = MockLease::granting();
        let durability = MockDurability::new();
        let store = MockQueueStore::new();
        let throttle = FixedThrottle {
            snap: running(1_000_000, 4096),
        };
        let timeline = Timeline::new();
        let engine = UploadEngine {
            cfg: &cfg,
            archive_root: &root,
            source: &source,
            uploader: &uploader,
            lease: &lease,
            durability: &durability,
            queue_store: &store,
            throttle: &throttle,
            clock: &timeline,
            waiter: &timeline,
        };
        let mut item = test_item(data.len() as u64, digest(&data));

        let outcome = engine.process(&mut item).expect("no infra error");
        assert!(matches!(outcome, StepOutcome::Retry { .. }));
        assert_eq!(item.state, crate::queue::UploadState::Failed);
        assert_eq!(
            item.bytes_uploaded, 0,
            "integrity failure resets checkpoint"
        );
        assert!(
            durability.marked.borrow().is_empty(),
            "corrupt ⇒ not durable"
        );
    }

    #[test]
    fn throttle_paused_takes_no_lease_and_reads_nothing() {
        let cfg = UploaddConfig::default();
        let root = root();
        let source = MockSource::new(vec![1, 2, 3]);
        let uploader = MockUploader::new();
        let lease = MockLease::granting();
        let durability = MockDurability::new();
        let store = MockQueueStore::new();
        let throttle = FixedThrottle {
            snap: ThrottleSnapshot {
                wifi: WifiThrottle::closed(),
                storage: StoragePressure::open(),
            },
        };
        let timeline = Timeline::new();
        let engine = UploadEngine {
            cfg: &cfg,
            archive_root: &root,
            source: &source,
            uploader: &uploader,
            lease: &lease,
            durability: &durability,
            queue_store: &store,
            throttle: &throttle,
            clock: &timeline,
            waiter: &timeline,
        };
        let mut item = test_item(3, digest(&[1, 2, 3]));

        let outcome = engine.process(&mut item).expect("no infra error");
        assert!(matches!(outcome, StepOutcome::Paused { .. }));
        assert_eq!(lease.acquire_calls.get(), 0, "no lease while paused");
        assert!(
            source.read_paths.borrow().is_empty(),
            "no read while paused"
        );
        assert_eq!(
            item.state,
            crate::queue::UploadState::Queued,
            "state untouched"
        );
    }

    #[test]
    fn lease_denied_skips_without_transfer() {
        let cfg = UploaddConfig::default();
        let root = root();
        let source = MockSource::new(vec![1, 2, 3]);
        let uploader = MockUploader::new();
        let mut lease = MockLease::granting();
        lease.deny = true;
        let durability = MockDurability::new();
        let store = MockQueueStore::new();
        let throttle = FixedThrottle {
            snap: running(1_000_000, 4096),
        };
        let timeline = Timeline::new();
        let engine = UploadEngine {
            cfg: &cfg,
            archive_root: &root,
            source: &source,
            uploader: &uploader,
            lease: &lease,
            durability: &durability,
            queue_store: &store,
            throttle: &throttle,
            clock: &timeline,
            waiter: &timeline,
        };
        let mut item = test_item(3, digest(&[1, 2, 3]));

        let outcome = engine.process(&mut item).expect("no infra error");
        assert!(matches!(outcome, StepOutcome::SkippedLeaseDenied { .. }));
        assert!(source.read_paths.borrow().is_empty(), "no read when denied");
        assert_eq!(uploader.received_bytes(), 0);
        assert_eq!(item.state, crate::queue::UploadState::Queued);
    }

    #[test]
    fn lost_lease_mid_transfer_stops_and_retries() {
        // Many small chunks + a tiny cap force pacing waits that advance the
        // clock past the renew interval, triggering a renew that goes Stale.
        let data: Vec<u8> = (0..1000u32).map(|i| (i % 251) as u8).collect();
        let mut cfg = UploaddConfig::default();
        cfg.lease.renew_interval_ms = 1; // renew almost immediately
        let root = root();
        let source = MockSource::new(data.clone());
        let uploader = MockUploader::new();
        let mut lease = MockLease::granting();
        lease.stale_on_call = Some(1); // first renew → Stale
        let durability = MockDurability::new();
        let store = MockQueueStore::new();
        let throttle = FixedThrottle {
            snap: running(100, 100), // 100 B/s, 100-byte chunks ⇒ waits between chunks
        };
        let timeline = Timeline::new();
        let engine = UploadEngine {
            cfg: &cfg,
            archive_root: &root,
            source: &source,
            uploader: &uploader,
            lease: &lease,
            durability: &durability,
            queue_store: &store,
            throttle: &throttle,
            clock: &timeline,
            waiter: &timeline,
        };
        let mut item = test_item(data.len() as u64, digest(&data));

        let outcome = engine.process(&mut item).expect("no infra error");
        assert!(matches!(outcome, StepOutcome::Retry { .. }));
        assert!(lease.renew_calls.get() >= 1, "renew was attempted");
        assert_eq!(item.state, crate::queue::UploadState::Failed);
        assert!(durability.marked.borrow().is_empty());
        assert_eq!(lease.release_calls.get(), 1, "lease still released");
    }

    #[test]
    fn sustained_transfer_stays_under_the_mocked_cap() {
        // 10 KiB at a 1000 B/s cap with 100-byte chunks. The paced loop advances
        // the synthetic clock; the realized rate must not exceed the cap (plus a
        // one-burst slack).
        let cap: u64 = 1000;
        let data: Vec<u8> = (0..10_000u32).map(|i| (i % 251) as u8).collect();
        let cfg = UploaddConfig::default();
        let root = root();
        let source = MockSource::new(data.clone());
        let uploader = MockUploader::new();
        let lease = MockLease::granting();
        let durability = MockDurability::new();
        let store = MockQueueStore::new();
        let throttle = FixedThrottle {
            snap: running(cap, 100),
        };
        let timeline = Timeline::new();
        let start = timeline.now();
        let engine = UploadEngine {
            cfg: &cfg,
            archive_root: &root,
            source: &source,
            uploader: &uploader,
            lease: &lease,
            durability: &durability,
            queue_store: &store,
            throttle: &throttle,
            clock: &timeline,
            waiter: &timeline,
        };
        let mut item = test_item(data.len() as u64, digest(&data));

        let outcome = engine.process(&mut item).expect("no infra error");
        assert!(matches!(outcome, StepOutcome::Uploaded { .. }));
        let elapsed_ms = u64::try_from(timeline.now() - start).unwrap_or(0);
        assert!(elapsed_ms > 0, "transfer must have taken synthetic time");
        // realized bytes/sec = total*1000/elapsed_ms ≤ cap + one burst of slack.
        let total = data.len() as u64;
        let realized_num = u128::from(total) * 1000;
        let allowed = u128::from(cap + cap) * u128::from(elapsed_ms);
        assert!(
            realized_num <= allowed,
            "realized rate exceeded cap: {total}B in {elapsed_ms}ms vs cap {cap}B/s"
        );
    }

    #[test]
    fn rejected_source_path_never_reaches_the_uploader() {
        // A traversal path must be rejected by the archive-root guard, fail the
        // item, and never hand a single byte to the transfer backend.
        let cfg = UploaddConfig::default();
        let root = root();
        let source = MockSource::new(vec![9; 100]);
        let uploader = MockUploader::new();
        let lease = MockLease::granting();
        let durability = MockDurability::new();
        let store = MockQueueStore::new();
        let throttle = FixedThrottle {
            snap: running(1_000_000, 4096),
        };
        let timeline = Timeline::new();
        let engine = UploadEngine {
            cfg: &cfg,
            archive_root: &root,
            source: &source,
            uploader: &uploader,
            lease: &lease,
            durability: &durability,
            queue_store: &store,
            throttle: &throttle,
            clock: &timeline,
            waiter: &timeline,
        };
        let mut item = QueueItem::new(
            ArchiveItemId(7),
            "../../mnt/cam/live.mp4", // escapes the archive root
            "remote/x.mp4",
            UploadCategory::Bulk,
            0,
            100,
            digest(&[9; 100]),
        );

        let outcome = engine.process(&mut item).expect("no infra error");
        assert!(matches!(outcome, StepOutcome::Retry { .. }));
        assert_eq!(uploader.received_bytes(), 0, "no bytes left the box");
        assert!(source.read_paths.borrow().is_empty(), "no read was issued");
        assert!(durability.marked.borrow().is_empty());
    }

    #[test]
    fn throttle_pause_mid_transfer_checkpoints_and_releases() {
        // First two throttle reads (process gate + first loop iteration) allow
        // uploads; the third pauses, after one chunk has been sent + checkpointed.
        let data: Vec<u8> = (0..1000u32).map(|i| (i % 251) as u8).collect();
        let cfg = UploaddConfig::default();
        let root = root();
        let source = MockSource::new(data.clone());
        let uploader = MockUploader::new();
        let lease = MockLease::granting();
        let durability = MockDurability::new();
        let store = MockQueueStore::new();
        let throttle = SwitchingThrottle {
            calls: Cell::new(0),
            switch_after: 2,
            first: running(1_000_000, 200),
            second: paused_snapshot(),
        };
        let timeline = Timeline::new();
        let engine = UploadEngine {
            cfg: &cfg,
            archive_root: &root,
            source: &source,
            uploader: &uploader,
            lease: &lease,
            durability: &durability,
            queue_store: &store,
            throttle: &throttle,
            clock: &timeline,
            waiter: &timeline,
        };
        let mut item = test_item(data.len() as u64, digest(&data));

        let outcome = engine.process(&mut item).expect("no infra error");
        assert!(matches!(outcome, StepOutcome::Paused { .. }));
        assert_eq!(item.state, crate::queue::UploadState::InProgress);
        assert_eq!(
            item.bytes_uploaded, 200,
            "checkpoint persisted before pause"
        );
        assert_eq!(item.attempts, 0, "a pause is not a failed attempt");
        assert_eq!(uploader.received_bytes(), 200, "exactly one chunk sent");
        assert!(durability.marked.borrow().is_empty());
        assert_eq!(lease.release_calls.get(), 1, "lease released on pause");
    }

    #[test]
    fn lease_released_when_initial_persist_fails() {
        let cfg = UploaddConfig::default();
        let root = root();
        let source = MockSource::new(vec![1, 2, 3]);
        let uploader = MockUploader::new();
        let lease = MockLease::granting();
        let durability = MockDurability::new();
        let store = MockQueueStore::failing_on(1); // the begin() persist fails
        let throttle = FixedThrottle {
            snap: running(1_000_000, 4096),
        };
        let timeline = Timeline::new();
        let engine = UploadEngine {
            cfg: &cfg,
            archive_root: &root,
            source: &source,
            uploader: &uploader,
            lease: &lease,
            durability: &durability,
            queue_store: &store,
            throttle: &throttle,
            clock: &timeline,
            waiter: &timeline,
        };
        let mut item = test_item(3, digest(&[1, 2, 3]));

        let result = engine.process(&mut item);
        assert!(result.is_err(), "initial persist failure is an infra error");
        assert_eq!(lease.acquire_calls.get(), 1);
        assert_eq!(
            lease.release_calls.get(),
            1,
            "the acquired lease must be released even when the first persist fails"
        );
        assert_eq!(uploader.received_bytes(), 0);
    }

    #[test]
    fn midloop_persist_failure_is_infra_not_a_charged_attempt() {
        // The begin() persist (call 1) succeeds; the post-chunk checkpoint persist
        // (call 2) fails. That is an infrastructure error — it must not increment
        // the item's upload attempts or park it as Failed.
        let data: Vec<u8> = (0..1000u32).map(|i| (i % 251) as u8).collect();
        let cfg = UploaddConfig::default();
        let root = root();
        let source = MockSource::new(data.clone());
        let uploader = MockUploader::new();
        let lease = MockLease::granting();
        let durability = MockDurability::new();
        let store = MockQueueStore::failing_on(2);
        let throttle = FixedThrottle {
            snap: running(1_000_000, 200),
        };
        let timeline = Timeline::new();
        let engine = UploadEngine {
            cfg: &cfg,
            archive_root: &root,
            source: &source,
            uploader: &uploader,
            lease: &lease,
            durability: &durability,
            queue_store: &store,
            throttle: &throttle,
            clock: &timeline,
            waiter: &timeline,
        };
        let mut item = test_item(data.len() as u64, digest(&data));

        let result = engine.process(&mut item);
        assert!(
            result.is_err(),
            "checkpoint persist failure surfaces as error"
        );
        assert_eq!(item.attempts, 0, "infra error must not charge an attempt");
        assert_eq!(item.state, crate::queue::UploadState::InProgress);
        assert!(durability.marked.borrow().is_empty());
        assert_eq!(
            lease.release_calls.get(),
            1,
            "lease released on infra error"
        );
    }

    #[test]
    fn early_eof_before_total_is_a_recoverable_retry() {
        // The source reports 1000 bytes but returns EOF at 400 (truncated/replaced
        // file). The engine must not finalize a short upload silently.
        let data: Vec<u8> = (0..1000u32).map(|i| (i % 251) as u8).collect();
        let cfg = UploaddConfig::default();
        let root = root();
        let mut source = MockSource::new(data.clone());
        source.eof_at = Some(400);
        let uploader = MockUploader::new();
        let lease = MockLease::granting();
        let durability = MockDurability::new();
        let store = MockQueueStore::new();
        let throttle = FixedThrottle {
            snap: running(1_000_000, 200),
        };
        let timeline = Timeline::new();
        let engine = UploadEngine {
            cfg: &cfg,
            archive_root: &root,
            source: &source,
            uploader: &uploader,
            lease: &lease,
            durability: &durability,
            queue_store: &store,
            throttle: &throttle,
            clock: &timeline,
            waiter: &timeline,
        };
        let mut item = test_item(data.len() as u64, digest(&data));

        let outcome = engine.process(&mut item).expect("no infra error");
        assert!(matches!(outcome, StepOutcome::Retry { .. }));
        assert_eq!(item.state, crate::queue::UploadState::Failed);
        assert_eq!(
            uploader.received_bytes(),
            400,
            "only pre-EOF bytes were sent"
        );
        assert!(
            durability.marked.borrow().is_empty(),
            "short upload not durable"
        );
    }
}
