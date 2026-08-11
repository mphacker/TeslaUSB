//! Slice 6.1g — the **host-testable retention orchestrator**.
//!
//! Every pure decision (`folder` policy, `manifest` stability, the `RecentClips`
//! rotation estimate, value scoring, the governor tier machine, the crash-safe
//! single-deleter protocol) already lives in its own module behind a trait seam.
//! This module is the **loop** that ties them together: it owns the small amount
//! of cross-pass *state* the daemon must carry (the [`ManifestTracker`], the
//! [`RotationEstimator`], and last cycle's pure space tier) and drives the seams
//! in the correct order each cycle.
//!
//! It is deliberately **I/O-free**: facts about the car volume and the index
//! (what folders are present, what their manifests are, which rows need
//! recovery, the eviction value inputs) are passed in as plain value structs, and
//! all side effects go through the existing seams plus one new in-crate
//! [`Catalog`] seam. The live binary builds the live seam implementations
//! (`statfs`, the `gadgetd` eject-handoff client, the `indexd` RPC client, the
//! `scannerd` fact feed) and calls [`RetentionLoop::recover`] once at startup
//! then [`RetentionLoop::run_cycle`] on the governor cadence; this lane delivers
//! the orchestrator + its full host test suite, not the hardware glue.
//!
//! # The five phases
//!
//! 1. [`RetentionLoop::recover`] — reconcile any half-finished delete from a
//!    prior boot (the crash-safe recovery matrix) **before** anything else runs.
//! 2. [`RetentionLoop::archive_event_folder`] — per event folder: gate on
//!    `manifest` stability, run a verified archive pass, and (only when policy
//!    permits) request a car-side delete via the `gadgetd` handoff.
//! 3. [`RetentionLoop::mirror_recent`] — best-effort mirror of `RecentClips`
//!    into the archive (the card-fill-independent "don't lose footage before the
//!    car rotates it" guarantee) plus the rolling-quota mirror eviction plan.
//! 4. [`RetentionLoop::govern`] — evaluate the space tier and, under pressure,
//!    delete the least-valuable **safe** Pi-side item (honoring leases via the
//!    claim gate; failing closed when nothing is safe).
//! 5. [`RetentionLoop::health`] — project the assessment into the `StorageHealth`
//!    payload `webd` serves, and derive the [`UploadBackpressure`] signal
//!    `uploadd` consumes.

use std::collections::HashMap;
use std::io;

use crate::archive::{
    ArchiveStore, CarDeleteHandoff, CarDeleteRequest, EventArchiveAction, EventArchiveContext,
    HandoffOutcome, VerifiedArchivePass, decide_event_archive, run_verified_pass,
};
use crate::config::RetentionConfig;
use crate::delete::{
    ArchiveDeleteOps, DeleteOutcome, DeleteRequest, FsPresence, IndexClient, RandGen,
    RecoveryAction, recovery_action, run_delete, run_recovery,
};
use crate::durability::ArchiveVerification;
use crate::folder::FolderClass;
use crate::governor::{
    DiskImgAccounting, FsRole, FsSample, GovernorAssessment, Statfs, Tier, evaluate,
};
use crate::io::ArchiveItemId;
use crate::lease::DeleteState;
use crate::manifest::{DirManifest, ManifestStability, ManifestTracker};
use crate::recent::{
    MirrorSegment, RecentCandidate, RecentHealth, RecentSegment, RotationEstimator,
    RotationObservation, archive_order, plan_mirror_eviction,
};
use crate::status::{HealthInputs, StorageHealth, assemble};
use crate::time::Clock;
use crate::value::{
    EvictionItem, EvictionKind, EvictionPolicy, LossClass, hard_exclusion,
    list_eviction_candidates, loss_class,
};

/// Consecutive identical `scannerd` passes a folder's `manifest` must hold before
/// it is eligible for a verified archive pass. Floored at 2 by [`ManifestTracker`]
/// (a single look cannot rule out an in-flight write).
pub const DEFAULT_REQUIRED_STABLE_PASSES: u32 = 2;

// ---------------------------------------------------------------------------
// New in-crate seam: the index catalog the orchestrator reads/writes through.
// ---------------------------------------------------------------------------

/// One archive-item row the startup recovery sweep must reconcile.
///
/// Mirrors the `indexd` `delete_state` columns the recovery matrix needs; the
/// orchestrator probes the filesystem for the on-disk side itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoveryRow {
    /// Stable archive-item identity.
    pub id: ArchiveItemId,
    /// The persisted delete state of the row.
    pub delete_state: DeleteState,
    /// Absolute archive path of the item's source.
    pub source_path: String,
    /// Absolute path the item would have been renamed to in the trash.
    pub trash_path: String,
    /// Size in bytes (reported as `bytes_freed` when a delete is finished).
    pub size_bytes: u64,
}

/// The `indexd`-backed catalog seam: the queries the orchestrator needs that are
/// not already covered by [`IndexClient`] (which owns the per-item delete-state
/// transitions). The live implementation is an `indexd` RPC client; tests inject
/// a deterministic fake.
pub trait Catalog {
    /// Record a successful verified archive pass for an event folder so its
    /// [`ArchiveVerification`] becomes `Verified` for subsequent cycles.
    ///
    /// # Errors
    /// Propagates the underlying index write failure.
    fn record_verified_pass(&self, folder_key: &str, pass: &VerifiedArchivePass) -> io::Result<()>;

    /// The current value-scoring inputs for every Pi-side eviction unit.
    ///
    /// # Errors
    /// Propagates the underlying index read failure.
    fn eviction_items(&self) -> io::Result<Vec<EvictionItem>>;

    /// Resolve a chosen eviction id into a concrete [`DeleteRequest`] (path +
    /// size). Returns `None` if the row vanished between listing and resolution.
    ///
    /// # Errors
    /// Propagates the underlying index read failure.
    fn delete_request(&self, id: ArchiveItemId) -> io::Result<Option<DeleteRequest>>;

    /// Rows the startup recovery sweep must reconcile (anything not cleanly
    /// `LIVE`/`DELETED`).
    ///
    /// # Errors
    /// Propagates the underlying index read failure.
    fn recovery_rows(&self) -> io::Result<Vec<RecoveryRow>>;

    /// Mark a `RecentClips` segment durably mirrored into the archive (the
    /// best-effort mirror path; no verified-pass obligation).
    ///
    /// # Errors
    /// Propagates the underlying index write failure.
    fn mark_recent_archived(&self, key: &str) -> io::Result<()>;
}

// ---------------------------------------------------------------------------
// Fact inputs (passed by value so the orchestrator stays I/O-free + trivially
// testable; the live loop fills these from scannerd/indexd/gadgetd).
// ---------------------------------------------------------------------------

/// One event folder's observed facts for this cycle.
#[derive(Debug, Clone)]
pub struct FolderFact {
    /// Stable folder key (the event-folder relative path).
    pub key: String,
    /// Folder classification (`SavedClips` / `SentryClips` / `TeslaTrackMode`;
    /// a `RecentClips` fact here is treated as never-car-deletable).
    pub class: FolderClass,
    /// Source directory on the car volume to copy from.
    pub src_dir: String,
    /// Destination directory in the Pi-side archive.
    pub dest_dir: String,
    /// The directory `manifest` observed this pass.
    pub manifest: DirManifest,
    /// Existing verification state for this event (from the catalog).
    pub verification: ArchiveVerification,
    /// Whether the car-visible volume is below its cleanup threshold.
    pub car_volume_pressured: bool,
    /// Whether the cloud durability policy is satisfied (gates `SavedClips`
    /// car-side deletion only).
    pub cloud_policy_satisfied: bool,
}

/// A `RecentClips` segment eligible to be mirrored this cycle, with the paths
/// needed to copy it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecentClipCandidate {
    /// Segment key.
    pub key: String,
    /// Capture-time ordering key.
    pub capture_ms: i64,
    /// Whether the segment is adjacent to a Saved/Sentry event (archived first).
    pub event_adjacent: bool,
    /// Source path on the car volume.
    pub src_rel: String,
    /// Destination path in the archive mirror.
    pub dest_rel: String,
}

impl RecentClipCandidate {
    fn ordering(&self) -> RecentCandidate {
        RecentCandidate {
            key: self.key.clone(),
            capture_ms: self.capture_ms,
            event_adjacent: self.event_adjacent,
        }
    }
}

/// An already-mirrored `RecentClips` segment plus the identity needed to evict it
/// under the rolling-quota cap.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MirrorRow {
    /// The segment's eviction-planning shape.
    pub seg: MirrorSegment,
    /// Stable archive-item identity (for the single-deleter protocol).
    pub id: ArchiveItemId,
    /// Absolute archive path of the mirror copy.
    pub source_path: String,
}

/// Facts about the `RecentClips` rolling buffer for one cycle.
#[derive(Debug, Clone)]
pub struct RecentFacts {
    /// Segments currently visible on the car volume this pass.
    pub visible: Vec<RecentSegment>,
    /// Keys already durably mirrored (so their loss from the car is not a
    /// falling-behind event).
    pub archived_keys: std::collections::HashSet<String>,
    /// Milliseconds since the previous successful pass (`0` on the first pass).
    pub scan_gap_ms: i64,
    /// Segments eligible to be mirrored this cycle.
    pub candidates: Vec<RecentClipCandidate>,
    /// Already-mirrored segments (for the rolling-quota eviction plan).
    pub mirror: Vec<MirrorRow>,
    /// Bytes of new segments arriving this cycle (for quota planning).
    pub incoming_bytes: u64,
}

/// Filesystem + `disk.img` readings the governor evaluates this cycle.
#[derive(Debug, Clone)]
pub struct GovernInput {
    /// `statfs` samples (must include at least one [`FsRole::Data`] path).
    pub samples: Vec<FsSample>,
    /// `disk.img` allocation accounting (the sparse-image guard).
    pub disk_img: DiskImgAccounting,
}

/// Inputs to one bounded target-drain pass.
pub struct DrainInput<'a> {
    /// Path to `statfs(2)` for free/total space (the data filesystem holding the archive).
    pub data_fs_path: &'a str,
    /// Dry-run: SELECT + project + record would-evictions with ZERO filesystem/index
    /// mutation. Free space is *projected* (accumulated would-free bytes) so the loop
    /// terminates (real free never moves in dry-run).
    pub dry_run: bool,
    /// Returns `true` when a cooperative shutdown was requested. Checked between each
    /// eviction; a `true` value stops the drain cleanly.
    pub should_stop: &'a dyn Fn() -> bool,
    /// Pet the process watchdog. Called once per loop iteration so a long drain never
    /// trips the hardware watchdog.
    pub pet_watchdog: &'a dyn Fn(),
}

/// One item evicted (or, in dry-run, that WOULD be evicted) by a drain pass.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DrainRecord {
    /// Archive-item identity.
    pub id: ArchiveItemId,
    /// Absolute archive path of the candidate row.
    pub source_path: String,
    /// Candidate row size.
    pub size_bytes: u64,
    /// `true` when this is a permanent local-only loss (`loss_class == ClassB`).
    pub permanent_loss: bool,
    /// `true` if this record is a dry-run projection (no mutation occurred).
    pub dry_run: bool,
}

/// Why a drain pass stopped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DrainStop {
    /// Free space reached `target_exit_frac`.
    TargetReached,
    /// Per-cycle byte cap hit.
    ByteCapReached,
    /// Per-cycle count cap hit.
    CountCapReached,
    /// Per-cycle wall-clock cap hit.
    WallClockCapReached,
    /// A cooperative shutdown was requested.
    ShutdownRequested,
    /// The candidate list was exhausted before reaching target.
    NoSafeCandidate,
    /// Free space was already at/above `target_exit_frac` on entry (no-op).
    AlreadyHealthy,
    /// `bytes_to_free` exceeded `anomaly_free_frac * total` — the whole pass was
    /// refused to guard against a bad `statfs` reading. No eviction occurred.
    AnomalyRefused {
        /// Bytes needed to reach `target_free_frac` from `free_before`.
        bytes_to_free: u64,
        /// Total bytes reported by `statfs`.
        total_bytes: u64,
    },
    /// A `run_delete` returned `Failed` (I/O/IPC error mid-protocol). The drain
    /// halts; startup recovery reconciles the row next boot.
    DeleteFailed {
        /// Failure reason returned by the single-delete protocol.
        reason: String,
    },
    /// A post-delete `statfs` re-stat failed after bytes were already freed this
    /// pass. The drain halts and returns the partial `bytes_freed` so the caller's
    /// blast-radius budget still observes the deletion (fail-safe accounting).
    StatCheckFailed {
        /// The statfs failure reason.
        reason: String,
    },
}

/// Result of one bounded target-drain pass.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DrainOutcome {
    /// One record per evicted (or would-evict) item, in eviction order.
    pub records: Vec<DrainRecord>,
    /// Bytes reclaimed (real) or projected (dry-run).
    pub bytes_freed: u64,
    /// Free bytes on the data fs at entry.
    pub free_before: u64,
    /// Free bytes after the pass (re-stat in real mode; projected in dry-run).
    pub free_after: u64,
    /// Total bytes on the data fs.
    pub total_bytes: u64,
    /// Why the pass stopped.
    pub stop: DrainStop,
}

// ---------------------------------------------------------------------------
// Outcomes.
// ---------------------------------------------------------------------------

/// What happened to one event folder this cycle.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EventOutcome {
    /// Not an event folder (`RecentClips` is handled by the mirror path).
    NotEventFolder,
    /// `manifest` not yet stable; still observing (carries consecutive passes).
    Observing {
        /// Consecutive identical observations so far.
        stable_passes: u32,
    },
    /// A verified archive pass succeeded and was recorded.
    Archived {
        /// Bytes archived.
        bytes: u64,
    },
    /// A verified pass was attempted but failed (drift / mismatch / I/O); it will
    /// be retried next cycle and **never** marked verified.
    VerifyFailed {
        /// Why the pass did not complete.
        reason: String,
    },
    /// Verified; policy keeps the car copy this cycle.
    KeptOnCar,
    /// A car-side delete handoff was requested; carries the `gadgetd` outcome.
    CarDeleteRequested {
        /// The handoff result.
        outcome: HandoffOutcome,
    },
    /// A catalog write failed; the event is left untouched for next cycle.
    Failed {
        /// Why it failed.
        reason: String,
    },
}

/// One evicted/deleted Pi-side item.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EvictedItem {
    /// The item deleted.
    pub id: ArchiveItemId,
    /// Bytes reclaimed.
    pub bytes_freed: u64,
}

/// Result of the `RecentClips` mirror phase.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecentOutcome {
    /// The rotation honesty signals for this pass.
    pub health: RecentHealth,
    /// Keys successfully mirrored this cycle, in archive order.
    pub archived: Vec<String>,
    /// Keys whose mirror copy failed (key + reason); retried next cycle.
    pub archive_failed: Vec<(String, String)>,
    /// Mirror segments evicted by the rolling-quota cap.
    pub evicted: Vec<EvictedItem>,
    /// Whether the mirror is still over quota after all safe evictions.
    pub still_over_quota: bool,
}

/// Result of the space-governor phase.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GovernOutcome {
    /// The governor's assessment this cycle.
    pub assessment: GovernorAssessment,
    /// Items evicted to reclaim space (at most one per cycle by default).
    pub evicted: Vec<EvictedItem>,
    /// Why an attempted eviction was skipped/failed, if any.
    pub skipped: Option<String>,
}

/// Result of the startup recovery sweep.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoverReport {
    /// The action reconciled for each row.
    pub actions: Vec<(ArchiveItemId, RecoveryAction)>,
}

/// The backpressure signal `uploadd` consumes (derived from the tier; retentiond
/// must not depend on the `uploadd` crate, so this is a plain projection).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UploadBackpressure {
    /// Whether optional cloud uploads may run (false at Emergency-or-worse, when
    /// every optional writer must yield).
    pub uploads_allowed: bool,
}

/// All facts for one full cycle (the convenience input to [`RetentionLoop::run_cycle`]).
#[derive(Debug, Clone)]
pub struct CycleInputs {
    /// Event folders observed this pass.
    pub folders: Vec<FolderFact>,
    /// `RecentClips` facts, if a `RecentClips` folder is present this pass.
    pub recent: Option<RecentFacts>,
    /// Governor filesystem readings.
    pub govern: GovernInput,
    /// Health-projection breakdown inputs.
    pub health: HealthInputs,
}

/// The structured report from one full cycle.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CycleReport {
    /// Per-event outcomes (folder key + outcome).
    pub events: Vec<(String, EventOutcome)>,
    /// The `RecentClips` mirror outcome, if a `RecentClips` pass ran.
    pub recent: Option<RecentOutcome>,
    /// The governor outcome.
    pub govern: GovernOutcome,
    /// The published storage-health payload.
    pub health: StorageHealth,
    /// The derived upload backpressure signal.
    pub backpressure: UploadBackpressure,
}

// ---------------------------------------------------------------------------
// The orchestrator.
// ---------------------------------------------------------------------------

/// The borrowed side-effect seams the orchestrator drives. Grouped into one
/// struct so [`RetentionLoop::new`] stays within the argument-count budget.
pub struct Seams<'a> {
    /// Monotonic clock.
    pub clock: &'a dyn Clock,
    /// Verified-archive copy/hash store.
    pub store: &'a dyn ArchiveStore,
    /// The `gadgetd` eject-handoff client.
    pub handoff: &'a dyn CarDeleteHandoff,
    /// `statfs` seam.
    pub statfs: &'a dyn Statfs,
    /// Crash-safe archive delete operations.
    pub fs: &'a dyn ArchiveDeleteOps,
    /// The `indexd` delete-state transition client.
    pub index: &'a dyn IndexClient,
    /// The `indexd` catalog query seam.
    pub catalog: &'a dyn Catalog,
    /// Random token source for trash generation tokens / pass ids.
    pub rand: &'a dyn RandGen,
}

/// The retention orchestrator. Owns the cross-pass trackers; borrows every seam.
pub struct RetentionLoop<'a> {
    cfg: &'a RetentionConfig,
    seams: Seams<'a>,
    trash_dir: String,
    manifests: ManifestTracker,
    rotation: RotationEstimator,
    prev_space_tier: Tier,
}

impl<'a> RetentionLoop<'a> {
    /// Build a retention loop. `trash_dir` is the `.retention-trash` directory on
    /// the archive filesystem the single-deleter renames into.
    #[must_use]
    pub fn new(cfg: &'a RetentionConfig, seams: Seams<'a>, trash_dir: impl Into<String>) -> Self {
        Self {
            cfg,
            seams,
            trash_dir: trash_dir.into(),
            manifests: ManifestTracker::new(DEFAULT_REQUIRED_STABLE_PASSES),
            rotation: RotationEstimator::new(),
            prev_space_tier: Tier::Healthy,
        }
    }

    /// Last cycle's pure space tier (diagnostics; fed back into the governor).
    #[must_use]
    pub const fn space_tier(&self) -> Tier {
        self.prev_space_tier
    }

    /// Number of event folders currently tracked for `manifest` stability.
    #[must_use]
    pub fn tracked_folders(&self) -> usize {
        self.manifests.tracked_len()
    }

    /// Read `statfs` for each `(role, path)` pair (the live loop's sampler).
    ///
    /// # Errors
    /// Propagates the first `statfs` failure (the governor then fails toward
    /// safety on a missing [`FsRole::Data`] sample).
    pub fn sample_filesystems(&self, paths: &[(FsRole, String)]) -> io::Result<Vec<FsSample>> {
        let mut out = Vec::with_capacity(paths.len());
        for (role, path) in paths {
            let stat = self.seams.statfs.statfs(path)?;
            out.push(FsSample { role: *role, stat });
        }
        Ok(out)
    }

    // -- Phase 1: startup recovery -----------------------------------------

    /// Reconcile any half-finished delete from a prior boot through the
    /// crash-safe recovery matrix. Idempotent; run once before the cycle loop.
    ///
    /// # Errors
    /// Propagates the first catalog or recovery IPC failure so the sweep retries
    /// next boot.
    pub fn recover(&self) -> io::Result<RecoverReport> {
        let rows = self.seams.catalog.recovery_rows()?;
        let mut actions = Vec::with_capacity(rows.len());
        for row in rows {
            let presence = self.fs_presence(&row.source_path, &row.trash_path);
            let action = recovery_action(row.delete_state, presence);
            run_recovery(
                row.id,
                &row.source_path,
                &row.trash_path,
                action,
                row.size_bytes,
                self.seams.fs,
                self.seams.index,
            )?;
            actions.push((row.id, action));
        }
        Ok(RecoverReport { actions })
    }

    fn fs_presence(&self, source: &str, trash: &str) -> FsPresence {
        if self.seams.fs.exists(source) {
            FsPresence::OriginalPresent
        } else if self.seams.fs.exists(trash) {
            FsPresence::TrashPresent
        } else {
            FsPresence::Neither
        }
    }

    // -- Phase 2: per-event archiving --------------------------------------

    /// Drive one event folder through the stability gate → verified pass →
    /// car-delete-handoff decision. `RecentClips` returns
    /// [`EventOutcome::NotEventFolder`] (it is handled by [`Self::mirror_recent`]).
    pub fn archive_event_folder(&mut self, fact: &FolderFact) -> EventOutcome {
        if !fact.class.is_event_folder() {
            return EventOutcome::NotEventFolder;
        }
        let stability = self.manifests.observe(&fact.key, &fact.manifest);
        let manifest_stable = matches!(stability, ManifestStability::Stable { .. });
        let action = decide_event_archive(EventArchiveContext {
            folder: fact.class,
            manifest_stable,
            verification: fact.verification,
            car_volume_pressured: fact.car_volume_pressured,
            cloud_policy_satisfied: fact.cloud_policy_satisfied,
        });
        match action {
            EventArchiveAction::WaitForStableManifest => EventOutcome::Observing {
                stable_passes: stable_pass_count(stability),
            },
            EventArchiveAction::RunVerifiedPass => self.run_and_record(fact),
            EventArchiveAction::KeepOnCar => EventOutcome::KeptOnCar,
            EventArchiveAction::RequestCarDelete => self.request_car_delete(fact),
        }
    }

    fn run_and_record(&self, fact: &FolderFact) -> EventOutcome {
        let pass_id = self.seams.rand.next_u128();
        match run_verified_pass(
            self.seams.store,
            &fact.src_dir,
            &fact.dest_dir,
            &fact.manifest,
            pass_id,
        ) {
            Ok(pass) => match self.seams.catalog.record_verified_pass(&fact.key, &pass) {
                Ok(()) => EventOutcome::Archived { bytes: pass.bytes },
                Err(e) => EventOutcome::Failed {
                    reason: format!("record_verified_pass: {e}"),
                },
            },
            Err(e) => EventOutcome::VerifyFailed {
                reason: e.to_string(),
            },
        }
    }

    fn request_car_delete(&mut self, fact: &FolderFact) -> EventOutcome {
        let req = CarDeleteRequest {
            rel_path: fact.src_dir.clone(),
            expected_digest: fact.manifest.digest(),
        };
        let outcome = self.seams.handoff.request_car_delete(&req);
        if outcome == HandoffOutcome::Done {
            // The car copy is gone; stop tracking its manifest to bound memory.
            self.manifests.forget(&fact.key);
        }
        EventOutcome::CarDeleteRequested { outcome }
    }

    // -- Phase 3: RecentClips mirror ---------------------------------------

    /// Observe rotation health, mirror eligible `RecentClips` segments into the
    /// archive (event-adjacent then oldest first), and apply the rolling-quota
    /// mirror eviction. Best-effort: per-segment failures are recorded, never
    /// fatal, and a pinned/in-grace segment is never evicted to fit quota.
    pub fn mirror_recent(&mut self, facts: &RecentFacts) -> RecentOutcome {
        let health = self.rotation.observe(RotationObservation {
            visible: &facts.visible,
            archived_keys: &facts.archived_keys,
            scan_gap_ms: facts.scan_gap_ms,
        });
        let (archived, archive_failed) = self.mirror_archive(facts);
        let (evicted, still_over_quota) = self.mirror_evict(facts);
        RecentOutcome {
            health,
            archived,
            archive_failed,
            evicted,
            still_over_quota,
        }
    }

    fn mirror_archive(&self, facts: &RecentFacts) -> (Vec<String>, Vec<(String, String)>) {
        let ordering: Vec<RecentCandidate> = facts
            .candidates
            .iter()
            .map(RecentClipCandidate::ordering)
            .collect();
        let by_key: HashMap<&str, &RecentClipCandidate> = facts
            .candidates
            .iter()
            .map(|c| (c.key.as_str(), c))
            .collect();

        let mut archived = Vec::new();
        let mut failed = Vec::new();
        for key in archive_order(&ordering) {
            let Some(cand) = by_key.get(key.as_str()) else {
                continue;
            };
            match self
                .seams
                .store
                .copy_and_hash_dest(&cand.src_rel, &cand.dest_rel)
            {
                Ok(_) => match self.seams.catalog.mark_recent_archived(&key) {
                    Ok(()) => archived.push(key),
                    Err(e) => failed.push((key, format!("mark: {e}"))),
                },
                Err(e) => failed.push((key, format!("copy: {e}"))),
            }
        }
        (archived, failed)
    }

    fn mirror_evict(&self, facts: &RecentFacts) -> (Vec<EvictedItem>, bool) {
        let segs: Vec<MirrorSegment> = facts.mirror.iter().map(|r| r.seg.clone()).collect();
        let plan = plan_mirror_eviction(
            &segs,
            facts.incoming_bytes,
            self.cfg.recent.quota_bytes,
            self.cfg.recent.grace_ms,
            self.seams.clock.mono_now(),
        );
        let by_key: HashMap<&str, &MirrorRow> = facts
            .mirror
            .iter()
            .map(|r| (r.seg.key.as_str(), r))
            .collect();

        let mut evicted = Vec::new();
        for key in plan.evict {
            if let Some(row) = by_key.get(key.as_str()) {
                let req = DeleteRequest {
                    id: row.id,
                    source_path: row.source_path.clone(),
                    size_bytes: row.seg.size,
                };
                if let DeleteOutcome::Deleted { bytes_freed } = run_delete(
                    &req,
                    &self.trash_dir,
                    self.seams.fs,
                    self.seams.index,
                    self.seams.rand,
                ) {
                    evicted.push(EvictedItem {
                        id: row.id,
                        bytes_freed,
                    });
                }
            }
        }
        (evicted, plan.still_over_quota)
    }

    // -- Phase 4: the space governor ---------------------------------------

    /// Evaluate the space tier and, under pressure (Low or worse) with a safe
    /// candidate, delete the single least-valuable item. Updates the fed-back
    /// pure space tier.
    ///
    /// # Errors
    /// Propagates a catalog read failure (the governor cannot act blind to the
    /// candidate set).
    pub fn govern(&mut self, input: &GovernInput) -> io::Result<GovernOutcome> {
        let items = self.seams.catalog.eviction_items()?;
        let has_safe = self.probe_has_safe_candidate(&items);
        let assessment = evaluate(
            self.prev_space_tier,
            &input.samples,
            input.disk_img,
            has_safe,
            &self.cfg.governor,
        );
        self.prev_space_tier = assessment.space_tier;

        let (evicted, skipped) = if assessment.tier >= Tier::Low {
            self.evict_least_valuable(&items, assessment.tier)?
        } else {
            (Vec::new(), None)
        };
        Ok(GovernOutcome {
            assessment,
            evicted,
            skipped,
        })
    }

    /// Bounded, oldest-first target drain (Hybrid C). Evicts the oldest SAFE items
    /// (reusing `hard_exclusion` + `delete_request` + crash-safe `run_delete`) until
    /// free space reaches `target_exit_frac` or a per-cycle cap trips. Re-stats between
    /// real deletes, pets the watchdog, and honors shutdown between each item. In
    /// dry-run it selects + records + projects free space with zero mutation.
    ///
    /// # Errors
    /// Propagates a `statfs` or catalog read failure.
    #[allow(clippy::too_many_lines)]
    pub fn drain_to_target(&self, input: &DrainInput<'_>) -> io::Result<DrainOutcome> {
        let stat = self.seams.statfs.statfs(input.data_fs_path)?;
        let free_before = stat.free_bytes;
        let total = stat.total_bytes;
        let cfg = &self.cfg.target_drain;
        let target_free = frac_bytes(total, cfg.target_free_frac);
        let exit_free = frac_bytes(total, cfg.target_exit_frac);
        let anomaly_free = frac_bytes(total, cfg.anomaly_free_frac);

        if free_before >= exit_free {
            return Ok(DrainOutcome {
                records: Vec::new(),
                bytes_freed: 0,
                free_before,
                free_after: free_before,
                total_bytes: total,
                stop: DrainStop::AlreadyHealthy,
            });
        }

        let bytes_to_free = target_free.saturating_sub(free_before);
        if bytes_to_free > anomaly_free {
            return Ok(DrainOutcome {
                records: Vec::new(),
                bytes_freed: 0,
                free_before,
                free_after: free_before,
                total_bytes: total,
                stop: DrainStop::AnomalyRefused {
                    bytes_to_free,
                    total_bytes: total,
                },
            });
        }

        let items = self.seams.catalog.eviction_items()?;
        let policy = EvictionPolicy {
            tier: Tier::Low,
            allow_emergency_undurable_sentry: self.cfg.allow_emergency_undurable_sentry,
            local_only_recent_delete_approved: self.cfg.local_only_recent_delete_approved,
        };

        let mut records = Vec::new();
        let mut freed = 0u64;
        let mut count = 0u32;
        let mut current_free = free_before;
        let loop_start_ms = self.seams.clock.mono_now().0;
        let mut stop = None;
        let count_cap = cfg.per_cycle_evict_count;
        let byte_cap = cfg.per_cycle_evict_bytes;
        let wall_cap = wall_ms_to_i64(cfg.per_cycle_wall_ms);

        for item in &items {
            (input.pet_watchdog)();
            if current_free >= exit_free {
                stop = Some(DrainStop::TargetReached);
                break;
            }
            if (input.should_stop)() {
                stop = Some(DrainStop::ShutdownRequested);
                break;
            }
            if count_cap != 0 && count >= count_cap {
                stop = Some(DrainStop::CountCapReached);
                break;
            }
            if byte_cap != 0 && freed >= byte_cap {
                stop = Some(DrainStop::ByteCapReached);
                break;
            }
            if wall_cap != 0
                && self.seams.clock.mono_now().0.saturating_sub(loop_start_ms) >= wall_cap
            {
                stop = Some(DrainStop::WallClockCapReached);
                break;
            }
            if hard_exclusion(item, policy).is_some() {
                continue;
            }
            // Defense-in-depth: the space governor deletes ONLY RecentClips mirror
            // segments. Never act on an event/other kind even if the catalog yields one.
            if !matches!(item.kind, EvictionKind::RecentMirror) {
                continue;
            }

            let Some(req) = self.seams.catalog.delete_request(item.id)? else {
                continue;
            };
            let permanent_loss = loss_class(item) == LossClass::ClassB;

            if input.dry_run {
                records.push(DrainRecord {
                    id: req.id,
                    source_path: req.source_path.clone(),
                    size_bytes: req.size_bytes,
                    permanent_loss,
                    dry_run: true,
                });
                freed = freed.saturating_add(req.size_bytes);
                count = count.saturating_add(1);
                current_free = current_free.saturating_add(req.size_bytes);
                continue;
            }

            match run_delete(
                &req,
                &self.trash_dir,
                self.seams.fs,
                self.seams.index,
                self.seams.rand,
            ) {
                DeleteOutcome::Deleted { bytes_freed } => {
                    records.push(DrainRecord {
                        id: req.id,
                        source_path: req.source_path.clone(),
                        size_bytes: req.size_bytes,
                        permanent_loss,
                        dry_run: false,
                    });
                    freed = freed.saturating_add(bytes_freed);
                    count = count.saturating_add(1);
                    current_free = match self.seams.statfs.statfs(input.data_fs_path) {
                        Ok(st) => st.free_bytes,
                        Err(err) => {
                            stop = Some(DrainStop::StatCheckFailed {
                                reason: err.to_string(),
                            });
                            break;
                        }
                    };
                }
                DeleteOutcome::Skipped { .. } => continue,
                DeleteOutcome::Failed { reason } => {
                    stop = Some(DrainStop::DeleteFailed { reason });
                    break;
                }
            }
        }

        let stop = stop.unwrap_or({
            if current_free >= exit_free {
                DrainStop::TargetReached
            } else {
                DrainStop::NoSafeCandidate
            }
        });

        Ok(DrainOutcome {
            records,
            bytes_freed: freed,
            free_before,
            free_after: current_free,
            total_bytes: total,
            stop,
        })
    }

    /// Whether any safe candidate exists at the most permissive policy (the
    /// `has_safe_candidate` signal that gates entry/exit of `Exhausted`).
    fn probe_has_safe_candidate(&self, items: &[EvictionItem]) -> bool {
        let policy = EvictionPolicy {
            tier: Tier::Emergency,
            allow_emergency_undurable_sentry: self.cfg.allow_emergency_undurable_sentry,
            local_only_recent_delete_approved: self.cfg.local_only_recent_delete_approved,
        };
        !list_eviction_candidates(items, policy).is_empty()
    }

    fn evict_least_valuable(
        &self,
        items: &[EvictionItem],
        tier: Tier,
    ) -> io::Result<(Vec<EvictedItem>, Option<String>)> {
        let policy = EvictionPolicy {
            tier,
            allow_emergency_undurable_sentry: self.cfg.allow_emergency_undurable_sentry,
            local_only_recent_delete_approved: self.cfg.local_only_recent_delete_approved,
        };
        let Some(top) = list_eviction_candidates(items, policy).into_iter().next() else {
            return Ok((Vec::new(), Some("no safe candidate".to_string())));
        };
        let Some(req) = self.seams.catalog.delete_request(top.id)? else {
            return Ok((Vec::new(), Some("delete_request: row vanished".to_string())));
        };
        match run_delete(
            &req,
            &self.trash_dir,
            self.seams.fs,
            self.seams.index,
            self.seams.rand,
        ) {
            DeleteOutcome::Deleted { bytes_freed } => Ok((
                vec![EvictedItem {
                    id: top.id,
                    bytes_freed,
                }],
                None,
            )),
            DeleteOutcome::Skipped { reason } | DeleteOutcome::Failed { reason } => {
                Ok((Vec::new(), Some(reason)))
            }
        }
    }

    // -- Phase 5: health projection + upload backpressure ------------------

    /// Project the governor assessment into the `StorageHealth` payload `webd`
    /// serves. Consumes `inputs` (it is moved into the payload unchanged).
    #[must_use]
    pub fn health(&self, assessment: &GovernorAssessment, inputs: HealthInputs) -> StorageHealth {
        assemble(assessment, inputs)
    }

    /// Derive the upload backpressure signal from the tier: optional cloud
    /// uploads must yield at Emergency-or-worse.
    #[must_use]
    pub fn upload_backpressure(&self, assessment: &GovernorAssessment) -> UploadBackpressure {
        UploadBackpressure {
            uploads_allowed: assessment.tier < Tier::Emergency,
        }
    }

    // -- The full cycle ----------------------------------------------------

    /// Run one full cycle: archive every event folder, mirror `RecentClips`,
    /// govern space, then project health + backpressure.
    ///
    /// # Errors
    /// Propagates a governor catalog read failure (the only fatal step; archive
    /// and mirror failures are captured per-item in the report).
    pub fn run_cycle(&mut self, inputs: CycleInputs) -> io::Result<CycleReport> {
        let CycleInputs {
            folders,
            recent,
            govern,
            health,
        } = inputs;

        let events = folders
            .iter()
            .map(|f| (f.key.clone(), self.archive_event_folder(f)))
            .collect();
        let recent = recent.map(|r| self.mirror_recent(&r));
        let govern = self.govern(&govern)?;
        let health = self.health(&govern.assessment, health);
        let backpressure = self.upload_backpressure(&govern.assessment);

        Ok(CycleReport {
            events,
            recent,
            govern,
            health,
            backpressure,
        })
    }
}

/// Consecutive identical observations carried by a [`ManifestStability`] value
/// (a `Stable` result is reported as the full required-pass count).
const fn stable_pass_count(stability: ManifestStability) -> u32 {
    match stability {
        ManifestStability::Unstable { stable_passes } => stable_passes,
        ManifestStability::Stable { .. } => DEFAULT_REQUIRED_STABLE_PASSES,
    }
}

#[allow(
    clippy::cast_precision_loss,
    clippy::cast_possible_truncation,
    clippy::cast_sign_loss
)]
fn frac_bytes(total: u64, frac: f64) -> u64 {
    (total as f64 * frac.clamp(0.0, 1.0)) as u64
}

#[allow(clippy::cast_possible_wrap)]
const fn wall_ms_to_i64(per_cycle_wall_ms: u64) -> i64 {
    per_cycle_wall_ms as i64
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]
mod tests {
    use std::cell::{Cell, RefCell};
    use std::collections::{HashMap, HashSet};
    use std::io;
    use std::rc::Rc;

    use super::{
        Catalog, CycleInputs, DrainInput, DrainStop, EventOutcome, FolderFact, GovernInput,
        MirrorRow, RecentClipCandidate, RecentFacts, RecoveryRow, RetentionLoop, Seams,
    };
    use crate::archive::{
        ArchiveStore, CarDeleteHandoff, CarDeleteRequest, HandoffOutcome, VerifiedArchivePass,
    };
    use crate::config::RetentionConfig;
    use crate::delete::{ArchiveDeleteOps, ClaimResult, DeleteRequest, IndexClient, RandGen};
    use crate::durability::{ArchiveVerification, Durability, VerifiedPassId};
    use crate::folder::FolderClass;
    use crate::governor::{DiskImgAccounting, FsRole, FsSample, Tier};
    use crate::io::{ArchiveItemId, ContentHash, FileIdentity, FsStat};
    use crate::lease::DeleteState;
    use crate::manifest::{DirManifest, ManifestEntry};
    use crate::recent::{MirrorSegment, RecentSegment};
    use crate::status::HealthInputs;
    use crate::time::{BootId, Clock, MonoMs};
    use crate::value::{EvictionItem, EvictionKind, Recency};

    const GB: u64 = 1 << 30;

    // -- fakes -------------------------------------------------------------

    struct FakeClock(Cell<i64>);
    impl Clock for FakeClock {
        fn mono_now(&self) -> MonoMs {
            MonoMs(self.0.get())
        }
        fn boot_id(&self) -> BootId {
            BootId("test-boot".to_string())
        }
    }

    struct SeqRand(Cell<u128>);
    impl RandGen for SeqRand {
        fn next_u128(&self) -> u128 {
            let v = self.0.get();
            self.0.set(v.wrapping_add(1));
            v
        }
    }

    struct FakeStore {
        files: HashMap<String, (u64, u8)>,
        copies: RefCell<Vec<(String, String)>>,
        landed: RefCell<HashSet<String>>,
        fail_copy: bool,
    }
    impl FakeStore {
        fn new(files: &[(&str, u64, u8)]) -> Self {
            Self {
                files: files
                    .iter()
                    .map(|(n, s, h)| ((*n).to_string(), (*s, *h)))
                    .collect(),
                copies: RefCell::new(Vec::new()),
                landed: RefCell::new(HashSet::new()),
                fail_copy: false,
            }
        }
        fn name_of(path: &str) -> &str {
            path.rsplit('/').next().unwrap_or(path)
        }
    }
    impl ArchiveStore for FakeStore {
        fn copy_and_hash_dest(&self, src_rel: &str, dest_rel: &str) -> io::Result<ContentHash> {
            self.copies
                .borrow_mut()
                .push((src_rel.to_string(), dest_rel.to_string()));
            if self.fail_copy {
                return Err(io::Error::other("copy failed"));
            }
            match self.files.get(Self::name_of(src_rel)) {
                Some((_, h)) => {
                    self.landed.borrow_mut().insert(dest_rel.to_string());
                    Ok(ContentHash::new([*h; 32]))
                }
                None => Err(io::Error::other("missing source")),
            }
        }
        fn source_identity(&self, src_rel: &str) -> io::Result<FileIdentity> {
            match self.files.get(Self::name_of(src_rel)) {
                Some((sz, h)) => Ok(FileIdentity {
                    size: *sz,
                    hash: ContentHash::new([*h; 32]),
                }),
                None => Err(io::Error::other("missing source")),
            }
        }
        fn list_source_rel_names(&self, _src_dir: &str) -> io::Result<Vec<String>> {
            Ok(self.files.keys().cloned().collect())
        }
        fn remove_dest(&self, dest_rel: &str) -> io::Result<()> {
            self.landed.borrow_mut().remove(dest_rel);
            Ok(())
        }
        fn promote_dest(&self, staging_rel: &str, final_rel: &str) -> io::Result<()> {
            let mut landed = self.landed.borrow_mut();
            if !landed.remove(staging_rel) {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    format!("staging file not found: {staging_rel}"),
                ));
            }
            landed.insert(final_rel.to_string());
            Ok(())
        }
        fn probe_dest_playability(
            &self,
            _dest_rel: &str,
        ) -> io::Result<crate::probe::ArchivePlayability> {
            Ok(crate::probe::ArchivePlayability::Playable)
        }
    }

    struct FakeHandoff {
        outcome: HandoffOutcome,
        seen: RefCell<Vec<CarDeleteRequest>>,
    }
    impl CarDeleteHandoff for FakeHandoff {
        fn request_car_delete(&self, req: &CarDeleteRequest) -> HandoffOutcome {
            self.seen.borrow_mut().push(req.clone());
            self.outcome.clone()
        }
    }

    struct FakeStatfs {
        stats: HashMap<String, FsStat>,
        calls: RefCell<Vec<String>>,
    }
    impl super::Statfs for FakeStatfs {
        fn statfs(&self, path: &str) -> io::Result<FsStat> {
            self.calls.borrow_mut().push(path.to_string());
            self.stats
                .get(path)
                .copied()
                .ok_or_else(|| io::Error::other("no stat"))
        }
    }

    struct FakeFs {
        existing: RefCell<HashSet<String>>,
        log: RefCell<Vec<String>>,
        fail_rename: bool,
        free_bump: Option<(Rc<Cell<u64>>, u64)>,
    }
    impl FakeFs {
        fn new(existing: &[&str]) -> Self {
            Self {
                existing: RefCell::new(existing.iter().map(|s| (*s).to_string()).collect()),
                log: RefCell::new(Vec::new()),
                fail_rename: false,
                free_bump: None,
            }
        }

        fn with_free_bump(mut self, free: Rc<Cell<u64>>, bytes: u64) -> Self {
            self.free_bump = Some((free, bytes));
            self
        }
    }
    impl ArchiveDeleteOps for FakeFs {
        fn exists(&self, path: &str) -> bool {
            self.existing.borrow().contains(path)
        }
        fn rename_into_trash(&self, src: &str, dst: &str) -> io::Result<()> {
            if self.fail_rename {
                return Err(io::Error::other("rename failed"));
            }
            self.log.borrow_mut().push(format!("rename {src} -> {dst}"));
            let mut e = self.existing.borrow_mut();
            e.remove(src);
            e.insert(dst.to_string());
            Ok(())
        }
        fn fsync_parent(&self, _path: &str) -> io::Result<()> {
            Ok(())
        }
        fn recursive_delete(&self, path: &str) -> io::Result<()> {
            self.log.borrow_mut().push(format!("rm {path}"));
            self.existing.borrow_mut().remove(path);
            if let Some((free, bytes)) = &self.free_bump {
                free.set(free.get().saturating_add(*bytes));
            }
            Ok(())
        }
    }

    struct RisingStatfs {
        free: Rc<Cell<u64>>,
        total: u64,
        calls: RefCell<Vec<String>>,
    }
    impl RisingStatfs {
        fn new(free: Rc<Cell<u64>>, total: u64) -> Self {
            Self {
                free,
                total,
                calls: RefCell::new(Vec::new()),
            }
        }
    }
    impl super::Statfs for RisingStatfs {
        fn statfs(&self, path: &str) -> io::Result<FsStat> {
            self.calls.borrow_mut().push(path.to_string());
            Ok(FsStat {
                dev_id: 1,
                free_bytes: self.free.get(),
                total_bytes: self.total,
                free_inodes: 10_000,
                total_inodes: 20_000,
            })
        }
    }

    struct SequencedStatfs {
        responses: RefCell<Vec<io::Result<FsStat>>>,
        calls: RefCell<Vec<String>>,
    }
    impl SequencedStatfs {
        fn new(responses: Vec<io::Result<FsStat>>) -> Self {
            Self {
                responses: RefCell::new(responses),
                calls: RefCell::new(Vec::new()),
            }
        }
    }
    impl super::Statfs for SequencedStatfs {
        fn statfs(&self, path: &str) -> io::Result<FsStat> {
            self.calls.borrow_mut().push(path.to_string());
            if self.responses.borrow().is_empty() {
                return Err(io::Error::other("no stat sequence response"));
            }
            self.responses.borrow_mut().remove(0)
        }
    }

    struct FakeIndex {
        claim: ClaimResult,
        log: RefCell<Vec<String>>,
    }
    impl FakeIndex {
        fn new(claim: ClaimResult) -> Self {
            Self {
                claim,
                log: RefCell::new(Vec::new()),
            }
        }
    }
    impl IndexClient for FakeIndex {
        fn claim_archive_delete(&self, _id: ArchiveItemId) -> ClaimResult {
            self.log.borrow_mut().push("claim".to_string());
            self.claim.clone()
        }
        fn mark_deleting(&self, _id: ArchiveItemId) -> io::Result<()> {
            self.log.borrow_mut().push("mark_deleting".to_string());
            Ok(())
        }
        fn mark_deleted(&self, _id: ArchiveItemId, bytes: u64) -> io::Result<()> {
            self.log.borrow_mut().push(format!("mark_deleted {bytes}"));
            Ok(())
        }
        fn release_delete_claim(&self, _id: ArchiveItemId) -> io::Result<()> {
            self.log.borrow_mut().push("release".to_string());
            Ok(())
        }
        fn quarantine(&self, _id: ArchiveItemId, reason: &str) -> io::Result<()> {
            self.log.borrow_mut().push(format!("quarantine {reason}"));
            Ok(())
        }
    }

    #[derive(Default)]
    struct FakeCatalog {
        recovery: Vec<RecoveryRow>,
        eviction: Vec<EvictionItem>,
        delete_reqs: HashMap<i64, DeleteRequest>,
        recorded: RefCell<Vec<String>>,
        recent_marked: RefCell<Vec<String>>,
    }
    impl Catalog for FakeCatalog {
        fn record_verified_pass(
            &self,
            folder_key: &str,
            _pass: &VerifiedArchivePass,
        ) -> io::Result<()> {
            self.recorded.borrow_mut().push(folder_key.to_string());
            Ok(())
        }
        fn eviction_items(&self) -> io::Result<Vec<EvictionItem>> {
            Ok(self.eviction.clone())
        }
        fn delete_request(&self, id: ArchiveItemId) -> io::Result<Option<DeleteRequest>> {
            Ok(self.delete_reqs.get(&id.0).cloned())
        }
        fn recovery_rows(&self) -> io::Result<Vec<RecoveryRow>> {
            Ok(self.recovery.clone())
        }
        fn mark_recent_archived(&self, key: &str) -> io::Result<()> {
            self.recent_marked.borrow_mut().push(key.to_string());
            Ok(())
        }
    }

    // -- builders ----------------------------------------------------------

    fn manifest(files: &[(&str, u64, u8)]) -> DirManifest {
        DirManifest::from_entries(
            files
                .iter()
                .map(|(n, s, h)| ManifestEntry {
                    rel_name: (*n).to_string(),
                    size: *s,
                    mtime_ms: 1,
                    hash: ContentHash::new([*h; 32]),
                })
                .collect(),
        )
    }

    fn evic(id: i64, kind: EvictionKind, dur: Durability) -> EvictionItem {
        EvictionItem {
            id: ArchiveItemId(id),
            kind,
            durability: dur,
            sentry_flood: false,
            size: 100,
            recency: Recency::Mid,
            user_save: false,
            impact_event: false,
            has_telemetry: false,
            event_adjacent: false,
            duplicate_cluster: false,
            user_marked_disposable: false,
            pinned: false,
            leased: false,
            in_grace: false,
            quarantined: false,
            inside_disk_img: false,
        }
    }

    fn drain_cfg() -> RetentionConfig {
        let mut cfg = RetentionConfig {
            local_only_recent_delete_approved: true,
            ..RetentionConfig::default()
        };
        cfg.target_drain.target_free_frac = 0.60;
        cfg.target_drain.target_exit_frac = 0.70;
        cfg.target_drain.per_cycle_evict_bytes = 0;
        cfg.target_drain.per_cycle_evict_count = 0;
        cfg.target_drain.per_cycle_wall_ms = 0;
        cfg.target_drain.anomaly_free_frac = 0.95;
        cfg
    }

    fn recent_mirror_items_with_reqs(
        ids: &[i64],
        size_bytes: u64,
    ) -> (Vec<EvictionItem>, HashMap<i64, DeleteRequest>) {
        let mut items = Vec::with_capacity(ids.len());
        let mut reqs = HashMap::new();
        for id in ids {
            items.push(evic(*id, EvictionKind::RecentMirror, Durability::Undurable));
            reqs.insert(
                *id,
                DeleteRequest {
                    id: ArchiveItemId(*id),
                    source_path: format!("archive/{id}"),
                    size_bytes,
                },
            );
        }
        (items, reqs)
    }

    fn data_sample(free: u64) -> FsSample {
        FsSample {
            role: FsRole::Data,
            stat: stat(free, 256 * GB),
        }
    }

    fn stat(free_bytes: u64, total_bytes: u64) -> FsStat {
        FsStat {
            dev_id: 1,
            free_bytes,
            total_bytes,
            free_inodes: 1_000_000,
            total_inodes: 1_000_000,
        }
    }

    fn fixed_statfs(path: &str, free_bytes: u64, total_bytes: u64) -> FakeStatfs {
        let mut stats = HashMap::new();
        stats.insert(path.to_string(), stat(free_bytes, total_bytes));
        FakeStatfs {
            stats,
            calls: RefCell::new(Vec::new()),
        }
    }

    fn full_disk_img() -> DiskImgAccounting {
        DiskImgAccounting {
            nominal_bytes: 4 * GB,
            allocated_bytes: 4 * GB,
        }
    }

    struct Harness {
        clock: FakeClock,
        store: FakeStore,
        handoff: FakeHandoff,
        statfs: FakeStatfs,
        fs: FakeFs,
        index: FakeIndex,
        catalog: FakeCatalog,
        rand: SeqRand,
    }

    impl Harness {
        fn seams(&self) -> Seams<'_> {
            Seams {
                clock: &self.clock,
                store: &self.store,
                handoff: &self.handoff,
                statfs: &self.statfs,
                fs: &self.fs,
                index: &self.index,
                catalog: &self.catalog,
                rand: &self.rand,
            }
        }
    }

    fn harness() -> Harness {
        Harness {
            clock: FakeClock(Cell::new(1_000_000)),
            store: FakeStore::new(&[("front.mp4", 10, 1), ("event.json", 2, 7)]),
            handoff: FakeHandoff {
                outcome: HandoffOutcome::Done,
                seen: RefCell::new(Vec::new()),
            },
            statfs: FakeStatfs {
                stats: HashMap::new(),
                calls: RefCell::new(Vec::new()),
            },
            fs: FakeFs::new(&[]),
            index: FakeIndex::new(ClaimResult::Claimed),
            catalog: FakeCatalog::default(),
            rand: SeqRand(Cell::new(0)),
        }
    }

    fn event_fact(verification: ArchiveVerification, pressured: bool) -> FolderFact {
        FolderFact {
            key: "SentryClips/ev1".to_string(),
            class: FolderClass::SentryClips,
            src_dir: "src/SentryClips/ev1".to_string(),
            dest_dir: "dest/SentryClips/ev1".to_string(),
            manifest: manifest(&[("front.mp4", 10, 1), ("event.json", 2, 7)]),
            verification,
            car_volume_pressured: pressured,
            cloud_policy_satisfied: true,
        }
    }

    // -- tests -------------------------------------------------------------

    #[test]
    fn recent_folder_is_not_archived_as_event() {
        let h = harness();
        let cfg = RetentionConfig::default();
        let mut rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let mut fact = event_fact(ArchiveVerification::Unverified, false);
        fact.class = FolderClass::RecentClips;
        assert_eq!(rl.archive_event_folder(&fact), EventOutcome::NotEventFolder);
    }

    #[test]
    fn unstable_manifest_keeps_observing_then_archives_when_stable() {
        let h = harness();
        let cfg = RetentionConfig::default();
        let mut rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let fact = event_fact(ArchiveVerification::Unverified, false);

        // Pass 1: first observation is never stable.
        assert_eq!(
            rl.archive_event_folder(&fact),
            EventOutcome::Observing { stable_passes: 1 }
        );
        // Pass 2: held steady → stable → verified pass runs and is recorded.
        assert_eq!(
            rl.archive_event_folder(&fact),
            EventOutcome::Archived { bytes: 12 }
        );
        assert_eq!(h.catalog.recorded.borrow().as_slice(), &["SentryClips/ev1"]);
    }

    #[test]
    fn verified_unpressured_keeps_on_car() {
        let h = harness();
        let cfg = RetentionConfig::default();
        let mut rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let fact = event_fact(
            ArchiveVerification::Verified {
                pass: VerifiedPassId(9),
            },
            false,
        );
        // Need a stable manifest first; observe twice.
        let _ = rl.archive_event_folder(&fact);
        assert_eq!(rl.archive_event_folder(&fact), EventOutcome::KeptOnCar);
    }

    #[test]
    fn verified_pressured_sentry_requests_car_delete_and_forgets_manifest() {
        let h = harness();
        let cfg = RetentionConfig::default();
        let mut rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let fact = event_fact(
            ArchiveVerification::Verified {
                pass: VerifiedPassId(9),
            },
            true,
        );
        let _ = rl.archive_event_folder(&fact); // pass 1 (observing)
        assert_eq!(rl.tracked_folders(), 1);
        let out = rl.archive_event_folder(&fact); // pass 2 (stable → delete)
        assert_eq!(
            out,
            EventOutcome::CarDeleteRequested {
                outcome: HandoffOutcome::Done
            }
        );
        // The handoff carried the verified digest, and the tracker forgot it.
        assert_eq!(h.handoff.seen.borrow().len(), 1);
        assert_eq!(
            h.handoff.seen.borrow()[0].expected_digest,
            fact.manifest.digest()
        );
        assert_eq!(rl.tracked_folders(), 0);
    }

    #[test]
    fn verify_failure_does_not_record() {
        let mut h = harness();
        // Source listing won't match the manifest (extra file) → SourceChanged.
        h.store = FakeStore::new(&[
            ("front.mp4", 10, 1),
            ("event.json", 2, 7),
            ("extra.mp4", 5, 9),
        ]);
        let cfg = RetentionConfig::default();
        let mut rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let fact = event_fact(ArchiveVerification::Unverified, false);
        let _ = rl.archive_event_folder(&fact);
        let out = rl.archive_event_folder(&fact);
        assert!(matches!(out, EventOutcome::VerifyFailed { .. }));
        assert!(h.catalog.recorded.borrow().is_empty());
    }

    #[test]
    fn mirror_archives_event_adjacent_then_oldest_first() {
        let mut h = harness();
        h.store = FakeStore::new(&[("a", 1, 1), ("b", 1, 2), ("c", 1, 3)]);
        let cfg = RetentionConfig::default();
        let mut rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let facts = RecentFacts {
            visible: Vec::new(),
            archived_keys: HashSet::new(),
            scan_gap_ms: 0,
            candidates: vec![
                RecentClipCandidate {
                    key: "old".to_string(),
                    capture_ms: 10,
                    event_adjacent: false,
                    src_rel: "src/a".to_string(),
                    dest_rel: "dst/a".to_string(),
                },
                RecentClipCandidate {
                    key: "newer".to_string(),
                    capture_ms: 30,
                    event_adjacent: false,
                    src_rel: "src/b".to_string(),
                    dest_rel: "dst/b".to_string(),
                },
                RecentClipCandidate {
                    key: "adjacent".to_string(),
                    capture_ms: 50,
                    event_adjacent: true,
                    src_rel: "src/c".to_string(),
                    dest_rel: "dst/c".to_string(),
                },
            ],
            mirror: Vec::new(),
            incoming_bytes: 0,
        };
        let out = rl.mirror_recent(&facts);
        // Event-adjacent first, then oldest-first among the rest.
        assert_eq!(out.archived, vec!["adjacent", "old", "newer"]);
        assert_eq!(
            h.catalog.recent_marked.borrow().as_slice(),
            &["adjacent", "old", "newer"]
        );
    }

    #[test]
    fn mirror_copy_failure_is_recorded_not_fatal() {
        let mut h = harness();
        let mut store = FakeStore::new(&[("a", 1, 1)]);
        store.fail_copy = true;
        h.store = store;
        let cfg = RetentionConfig::default();
        let mut rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let facts = RecentFacts {
            visible: Vec::new(),
            archived_keys: HashSet::new(),
            scan_gap_ms: 0,
            candidates: vec![RecentClipCandidate {
                key: "x".to_string(),
                capture_ms: 1,
                event_adjacent: false,
                src_rel: "src/a".to_string(),
                dest_rel: "dst/a".to_string(),
            }],
            mirror: Vec::new(),
            incoming_bytes: 0,
        };
        let out = rl.mirror_recent(&facts);
        assert!(out.archived.is_empty());
        assert_eq!(out.archive_failed.len(), 1);
        assert!(h.catalog.recent_marked.borrow().is_empty());
    }

    #[test]
    fn mirror_eviction_drops_oldest_non_pinned_past_grace() {
        let h = harness();
        let mut cfg = RetentionConfig::default();
        cfg.recent.quota_bytes = 100;
        cfg.recent.grace_ms = 0;
        let mut rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let mk = |key: &str, cap: i64, size: u64, pinned: bool, id: i64| MirrorRow {
            seg: MirrorSegment {
                key: key.to_string(),
                capture_ms: cap,
                size,
                first_archived: MonoMs(0),
                pinned,
            },
            id: ArchiveItemId(id),
            source_path: format!("arch/{key}"),
        };
        let facts = RecentFacts {
            visible: Vec::new(),
            archived_keys: HashSet::new(),
            scan_gap_ms: 0,
            candidates: Vec::new(),
            // Total 150 with incoming 0 → evicting the oldest (60) reaches 90 ≤ quota.
            mirror: vec![
                mk("oldest", 1, 60, false, 1),
                mk("mid", 2, 60, false, 2),
                mk("pinned", 3, 30, true, 3),
            ],
            incoming_bytes: 0,
        };
        let out = rl.mirror_recent(&facts);
        assert_eq!(out.evicted.len(), 1);
        assert_eq!(out.evicted[0].id, ArchiveItemId(1));
        assert_eq!(out.evicted[0].bytes_freed, 60);
        // The single-deleter protocol ran through claim → mark_deleted.
        assert!(h.index.log.borrow().contains(&"mark_deleting".to_string()));
    }

    #[test]
    fn drain_already_healthy_is_noop() {
        let mut h = harness();
        h.statfs = fixed_statfs("/data", 800, 1_000);
        let cfg = drain_cfg();
        let rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let should_stop = || false;
        let pet_watchdog = || {};
        let out = rl
            .drain_to_target(&DrainInput {
                data_fs_path: "/data",
                dry_run: false,
                should_stop: &should_stop,
                pet_watchdog: &pet_watchdog,
            })
            .unwrap();
        assert_eq!(out.stop, DrainStop::AlreadyHealthy);
        assert!(out.records.is_empty());
        assert_eq!(out.bytes_freed, 0);
        assert!(h.fs.log.borrow().is_empty());
        assert!(h.index.log.borrow().is_empty());
    }

    #[test]
    fn drain_reaches_target_and_stops() {
        let cfg = drain_cfg();
        let clock = FakeClock(Cell::new(1_000_000));
        let store = FakeStore::new(&[]);
        let handoff = FakeHandoff {
            outcome: HandoffOutcome::Done,
            seen: RefCell::new(Vec::new()),
        };
        let free = Rc::new(Cell::new(100));
        let statfs = RisingStatfs::new(free.clone(), 1_000);
        let fs = FakeFs::new(&[]).with_free_bump(free.clone(), 150);
        let index = FakeIndex::new(ClaimResult::Claimed);
        let (eviction, delete_reqs) = recent_mirror_items_with_reqs(&[1, 2, 3, 4], 150);
        let catalog = FakeCatalog {
            recovery: Vec::new(),
            eviction,
            delete_reqs,
            recorded: RefCell::new(Vec::new()),
            recent_marked: RefCell::new(Vec::new()),
        };
        let rand = SeqRand(Cell::new(0));
        let rl = RetentionLoop::new(
            &cfg,
            Seams {
                clock: &clock,
                store: &store,
                handoff: &handoff,
                statfs: &statfs,
                fs: &fs,
                index: &index,
                catalog: &catalog,
                rand: &rand,
            },
            "trash",
        );
        let should_stop = || false;
        let pet_watchdog = || {};
        let out = rl
            .drain_to_target(&DrainInput {
                data_fs_path: "/data",
                dry_run: false,
                should_stop: &should_stop,
                pet_watchdog: &pet_watchdog,
            })
            .unwrap();
        let ids: Vec<_> = out.records.iter().map(|r| r.id).collect();
        assert_eq!(
            ids,
            vec![
                ArchiveItemId(1),
                ArchiveItemId(2),
                ArchiveItemId(3),
                ArchiveItemId(4)
            ]
        );
        assert_eq!(out.stop, DrainStop::TargetReached);
        assert!(out.free_after >= 700);
    }

    #[test]
    fn drain_deletes_in_catalog_order_not_size() {
        // The live drain (`drain_to_target`) must delete in the order the indexd
        // catalog returns (value-tiered, oldest-first) — NOT re-sorted by size.
        // EvictionItem.size ascends with id here, so a size-descending sort
        // would delete id 4 first; assert the order stays [1,2,3,4].
        let cfg = drain_cfg();
        let clock = FakeClock(Cell::new(1_000_000));
        let store = FakeStore::new(&[]);
        let handoff = FakeHandoff {
            outcome: HandoffOutcome::Done,
            seen: RefCell::new(Vec::new()),
        };
        let free = Rc::new(Cell::new(100));
        let statfs = RisingStatfs::new(free.clone(), 1_000);
        let fs = FakeFs::new(&[]).with_free_bump(free.clone(), 150);
        let index = FakeIndex::new(ClaimResult::Claimed);
        let (mut eviction, delete_reqs) = recent_mirror_items_with_reqs(&[1, 2, 3, 4], 150);
        for (item, size) in eviction.iter_mut().zip([100u64, 200, 300, 400]) {
            item.size = size;
        }
        let catalog = FakeCatalog {
            recovery: Vec::new(),
            eviction,
            delete_reqs,
            recorded: RefCell::new(Vec::new()),
            recent_marked: RefCell::new(Vec::new()),
        };
        let rand = SeqRand(Cell::new(0));
        let rl = RetentionLoop::new(
            &cfg,
            Seams {
                clock: &clock,
                store: &store,
                handoff: &handoff,
                statfs: &statfs,
                fs: &fs,
                index: &index,
                catalog: &catalog,
                rand: &rand,
            },
            "trash",
        );
        let should_stop = || false;
        let pet_watchdog = || {};
        let out = rl
            .drain_to_target(&DrainInput {
                data_fs_path: "/data",
                dry_run: false,
                should_stop: &should_stop,
                pet_watchdog: &pet_watchdog,
            })
            .unwrap();
        let ids: Vec<_> = out.records.iter().map(|r| r.id).collect();
        assert_eq!(
            ids,
            vec![
                ArchiveItemId(1),
                ArchiveItemId(2),
                ArchiveItemId(3),
                ArchiveItemId(4)
            ]
        );
        assert_eq!(out.stop, DrainStop::TargetReached);
    }

    #[test]
    fn drain_respects_count_cap() {
        let mut h = harness();
        h.statfs = fixed_statfs("/data", 100, 1_000);
        let mut cfg = drain_cfg();
        cfg.target_drain.target_free_frac = 0.95;
        cfg.target_drain.target_exit_frac = 0.95;
        cfg.target_drain.per_cycle_evict_count = 2;
        let (eviction, delete_reqs) = recent_mirror_items_with_reqs(&[1, 2, 3, 4], 100);
        h.catalog.eviction = eviction;
        h.catalog.delete_reqs = delete_reqs;
        let rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let should_stop = || false;
        let pet_watchdog = || {};
        let out = rl
            .drain_to_target(&DrainInput {
                data_fs_path: "/data",
                dry_run: false,
                should_stop: &should_stop,
                pet_watchdog: &pet_watchdog,
            })
            .unwrap();
        assert_eq!(out.records.len(), 2);
        assert_eq!(out.stop, DrainStop::CountCapReached);
    }

    #[test]
    fn drain_respects_byte_cap() {
        let mut h = harness();
        h.statfs = fixed_statfs("/data", 100, 1_000);
        let mut cfg = drain_cfg();
        cfg.target_drain.target_free_frac = 0.95;
        cfg.target_drain.target_exit_frac = 0.95;
        cfg.target_drain.per_cycle_evict_bytes = 250;
        let (eviction, delete_reqs) = recent_mirror_items_with_reqs(&[1, 2, 3, 4], 100);
        h.catalog.eviction = eviction;
        h.catalog.delete_reqs = delete_reqs;
        let rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let should_stop = || false;
        let pet_watchdog = || {};
        let out = rl
            .drain_to_target(&DrainInput {
                data_fs_path: "/data",
                dry_run: false,
                should_stop: &should_stop,
                pet_watchdog: &pet_watchdog,
            })
            .unwrap();
        assert_eq!(out.stop, DrainStop::ByteCapReached);
        assert_eq!(out.records.len(), 3);
        assert!(out.bytes_freed >= 250);
    }

    #[test]
    fn drain_anomaly_refuses() {
        let mut h = harness();
        h.statfs = fixed_statfs("/data", 0, 1_000);
        let mut cfg = drain_cfg();
        cfg.target_drain.target_free_frac = 0.90;
        cfg.target_drain.target_exit_frac = 0.95;
        cfg.target_drain.anomaly_free_frac = 0.10;
        let (eviction, delete_reqs) = recent_mirror_items_with_reqs(&[1, 2], 100);
        h.catalog.eviction = eviction;
        h.catalog.delete_reqs = delete_reqs;
        let rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let should_stop = || false;
        let pet_watchdog = || {};
        let out = rl
            .drain_to_target(&DrainInput {
                data_fs_path: "/data",
                dry_run: false,
                should_stop: &should_stop,
                pet_watchdog: &pet_watchdog,
            })
            .unwrap();
        assert_eq!(
            out.stop,
            DrainStop::AnomalyRefused {
                bytes_to_free: 900,
                total_bytes: 1_000
            }
        );
        assert!(out.records.is_empty());
        assert!(h.fs.log.borrow().is_empty());
        assert!(h.index.log.borrow().is_empty());
    }

    #[test]
    fn drain_dry_run_projects_without_mutation() {
        let mut h = harness();
        h.statfs = fixed_statfs("/data", 100, 1_000);
        let mut cfg = drain_cfg();
        cfg.target_drain.target_free_frac = 0.40;
        cfg.target_drain.target_exit_frac = 0.50;
        let (eviction, delete_reqs) = recent_mirror_items_with_reqs(&[1, 2, 3], 150);
        h.catalog.eviction = eviction;
        h.catalog.delete_reqs = delete_reqs;
        let rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let should_stop = || false;
        let pet_watchdog = || {};
        let out = rl
            .drain_to_target(&DrainInput {
                data_fs_path: "/data",
                dry_run: true,
                should_stop: &should_stop,
                pet_watchdog: &pet_watchdog,
            })
            .unwrap();
        assert_eq!(out.stop, DrainStop::TargetReached);
        assert_eq!(out.records.len(), 3);
        assert!(out.records.iter().all(|r| r.dry_run));
        assert_eq!(out.bytes_freed, 450);
        assert_eq!(out.free_after, 550);
        assert!(h.fs.log.borrow().is_empty());
        assert!(h.index.log.borrow().is_empty());
    }

    #[test]
    fn drain_skips_hard_excluded_then_evicts_next() {
        let mut h = harness();
        h.statfs = fixed_statfs("/data", 100, 1_000);
        let mut cfg = drain_cfg();
        cfg.target_drain.target_free_frac = 0.95;
        cfg.target_drain.target_exit_frac = 0.95;
        let mut first = evic(1, EvictionKind::RecentMirror, Durability::Undurable);
        first.pinned = true;
        let second = evic(2, EvictionKind::RecentMirror, Durability::Undurable);
        h.catalog.eviction = vec![first, second];
        h.catalog.delete_reqs.insert(
            1,
            DeleteRequest {
                id: ArchiveItemId(1),
                source_path: "archive/1".to_string(),
                size_bytes: 100,
            },
        );
        h.catalog.delete_reqs.insert(
            2,
            DeleteRequest {
                id: ArchiveItemId(2),
                source_path: "archive/2".to_string(),
                size_bytes: 100,
            },
        );
        let rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let should_stop = || false;
        let pet_watchdog = || {};
        let out = rl
            .drain_to_target(&DrainInput {
                data_fs_path: "/data",
                dry_run: false,
                should_stop: &should_stop,
                pet_watchdog: &pet_watchdog,
            })
            .unwrap();
        let ids: Vec<_> = out.records.iter().map(|r| r.id).collect();
        assert_eq!(ids, vec![ArchiveItemId(2)]);
        assert!(!ids.contains(&ArchiveItemId(1)));
    }

    #[test]
    fn drain_shutdown_stops_cleanly() {
        let mut h = harness();
        h.statfs = fixed_statfs("/data", 100, 1_000);
        let mut cfg = drain_cfg();
        cfg.target_drain.target_free_frac = 0.95;
        cfg.target_drain.target_exit_frac = 0.95;
        let (eviction, delete_reqs) = recent_mirror_items_with_reqs(&[1, 2], 100);
        h.catalog.eviction = eviction;
        h.catalog.delete_reqs = delete_reqs;
        let rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let pet_count = Cell::new(0u32);
        let should_stop = || pet_count.get() > 0;
        let pet_watchdog = || pet_count.set(pet_count.get().saturating_add(1));
        let out = rl
            .drain_to_target(&DrainInput {
                data_fs_path: "/data",
                dry_run: false,
                should_stop: &should_stop,
                pet_watchdog: &pet_watchdog,
            })
            .unwrap();
        assert_eq!(out.stop, DrainStop::ShutdownRequested);
        assert!(out.records.len() <= 1);
    }

    #[test]
    fn drain_stops_on_delete_failed() {
        let mut h = harness();
        h.statfs = fixed_statfs("/data", 100, 1_000);
        let mut cfg = drain_cfg();
        cfg.target_drain.target_free_frac = 0.95;
        cfg.target_drain.target_exit_frac = 0.95;
        let (eviction, delete_reqs) = recent_mirror_items_with_reqs(&[1, 2], 100);
        h.catalog.eviction = eviction;
        h.catalog.delete_reqs = delete_reqs;
        h.fs.fail_rename = true;
        let rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let should_stop = || false;
        let pet_watchdog = || {};
        let out = rl
            .drain_to_target(&DrainInput {
                data_fs_path: "/data",
                dry_run: false,
                should_stop: &should_stop,
                pet_watchdog: &pet_watchdog,
            })
            .unwrap();
        assert!(matches!(out.stop, DrainStop::DeleteFailed { .. }));
        assert!(out.records.is_empty());
        let claim_calls = h
            .index
            .log
            .borrow()
            .iter()
            .filter(|entry| entry.as_str() == "claim")
            .count();
        assert_eq!(claim_calls, 1);
    }

    #[test]
    fn drain_post_delete_statfs_error_returns_partial_bytes_freed() {
        let cfg = drain_cfg();
        let clock = FakeClock(Cell::new(1_000_000));
        let store = FakeStore::new(&[]);
        let handoff = FakeHandoff {
            outcome: HandoffOutcome::Done,
            seen: RefCell::new(Vec::new()),
        };
        let statfs = SequencedStatfs::new(vec![
            Ok(stat(100, 1_000)),
            Ok(stat(150, 1_000)),
            Err(io::Error::other("post-delete restat failed")),
        ]);
        let fs = FakeFs::new(&[]);
        let index = FakeIndex::new(ClaimResult::Claimed);
        let (eviction, delete_reqs) = recent_mirror_items_with_reqs(&[1, 2], 100);
        let catalog = FakeCatalog {
            recovery: Vec::new(),
            eviction,
            delete_reqs,
            recorded: RefCell::new(Vec::new()),
            recent_marked: RefCell::new(Vec::new()),
        };
        let rand = SeqRand(Cell::new(0));
        let rl = RetentionLoop::new(
            &cfg,
            Seams {
                clock: &clock,
                store: &store,
                handoff: &handoff,
                statfs: &statfs,
                fs: &fs,
                index: &index,
                catalog: &catalog,
                rand: &rand,
            },
            "trash",
        );
        let should_stop = || false;
        let pet_watchdog = || {};
        let out = rl
            .drain_to_target(&DrainInput {
                data_fs_path: "/data",
                dry_run: false,
                should_stop: &should_stop,
                pet_watchdog: &pet_watchdog,
            })
            .unwrap();
        assert!(matches!(out.stop, DrainStop::StatCheckFailed { .. }));
        assert!(out.bytes_freed > 0);
        assert_eq!(out.records.len(), 2);
    }

    #[test]
    fn drain_skips_non_recent_mirror_items() {
        let mut h = harness();
        h.statfs = fixed_statfs("/data", 100, 1_000);
        let mut cfg = drain_cfg();
        cfg.target_drain.target_free_frac = 0.95;
        cfg.target_drain.target_exit_frac = 0.95;
        h.catalog.eviction = vec![
            evic(
                10,
                EvictionKind::Event {
                    folder: FolderClass::SavedClips,
                },
                Durability::Durable,
            ),
            evic(20, EvictionKind::RecentMirror, Durability::Durable),
        ];
        h.catalog.delete_reqs.insert(
            10,
            DeleteRequest {
                id: ArchiveItemId(10),
                source_path: "archive/10".to_string(),
                size_bytes: 100,
            },
        );
        h.catalog.delete_reqs.insert(
            20,
            DeleteRequest {
                id: ArchiveItemId(20),
                source_path: "archive/20".to_string(),
                size_bytes: 100,
            },
        );
        let rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let should_stop = || false;
        let pet_watchdog = || {};
        let out = rl
            .drain_to_target(&DrainInput {
                data_fs_path: "/data",
                dry_run: false,
                should_stop: &should_stop,
                pet_watchdog: &pet_watchdog,
            })
            .unwrap();
        let ids: Vec<_> = out.records.iter().map(|r| r.id).collect();
        assert_eq!(ids, vec![ArchiveItemId(20)]);
        assert!(!ids.contains(&ArchiveItemId(10)));
    }

    #[test]
    fn govern_healthy_does_not_evict() {
        let mut h = harness();
        h.catalog.eviction = vec![evic(1, EvictionKind::RecentMirror, Durability::Durable)];
        h.catalog.delete_reqs.insert(
            1,
            DeleteRequest {
                id: ArchiveItemId(1),
                source_path: "p".to_string(),
                size_bytes: 1,
            },
        );
        let cfg = RetentionConfig::default();
        let mut rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let input = GovernInput {
            samples: vec![data_sample(200 * GB)],
            disk_img: full_disk_img(),
        };
        let out = rl.govern(&input).unwrap();
        assert_eq!(out.assessment.tier, Tier::Healthy);
        assert!(out.evicted.is_empty());
    }

    #[test]
    fn govern_under_pressure_evicts_least_valuable() {
        let mut h = harness();
        // A low-value recent mirror and a high-value durable saved event.
        h.catalog.eviction = vec![
            evic(
                2,
                EvictionKind::Event {
                    folder: FolderClass::SavedClips,
                },
                Durability::Durable,
            ),
            evic(1, EvictionKind::RecentMirror, Durability::Durable),
        ];
        h.catalog.delete_reqs.insert(
            1,
            DeleteRequest {
                id: ArchiveItemId(1),
                source_path: "arch/recent1".to_string(),
                size_bytes: 4096,
            },
        );
        let cfg = RetentionConfig::default();
        let mut rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        // Free space well below Critical floor (8 GiB) → eviction warranted.
        let input = GovernInput {
            samples: vec![data_sample(GB)],
            disk_img: full_disk_img(),
        };
        let out = rl.govern(&input).unwrap();
        assert!(out.assessment.tier >= Tier::Critical);
        assert_eq!(out.evicted.len(), 1, "skipped={:?}", out.skipped);
        assert_eq!(out.evicted[0].id, ArchiveItemId(1)); // the low-value mirror
        assert_eq!(out.evicted[0].bytes_freed, 4096);
    }

    #[test]
    fn govern_exhausted_when_no_safe_candidate() {
        let mut h = harness();
        // Only an undurable SavedClips item exists — never a candidate.
        h.catalog.eviction = vec![evic(
            1,
            EvictionKind::Event {
                folder: FolderClass::SavedClips,
            },
            Durability::Undurable,
        )];
        let cfg = RetentionConfig::default();
        let mut rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        // Free space below the Emergency floor (4 GiB) → Emergency, but nothing
        // is safe to evict → the overlay drops us to Exhausted.
        let input = GovernInput {
            samples: vec![data_sample(GB)],
            disk_img: full_disk_img(),
        };
        let out = rl.govern(&input).unwrap();
        assert_eq!(out.assessment.tier, Tier::Exhausted);
        assert!(out.evicted.is_empty());
        assert!(out.skipped.is_some());
    }

    #[test]
    fn recover_continues_a_half_finished_delete() {
        let mut h = harness();
        h.fs = FakeFs::new(&["trash/7.deleting"]); // trash present, original gone
        h.catalog.recovery = vec![RecoveryRow {
            id: ArchiveItemId(7),
            delete_state: DeleteState::Deleting,
            source_path: "arch/ev7".to_string(),
            trash_path: "trash/7.deleting".to_string(),
            size_bytes: 2048,
        }];
        let cfg = RetentionConfig::default();
        let rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let report = rl.recover().unwrap();
        assert_eq!(report.actions.len(), 1);
        let log = h.index.log.borrow();
        assert!(log.contains(&"mark_deleting".to_string()));
        assert!(log.contains(&"mark_deleted 2048".to_string()));
    }

    #[test]
    fn backpressure_blocks_uploads_at_emergency() {
        let mut h = harness();
        h.catalog.eviction = vec![evic(1, EvictionKind::RecentMirror, Durability::Durable)];
        h.catalog.delete_reqs.insert(
            1,
            DeleteRequest {
                id: ArchiveItemId(1),
                source_path: "p".to_string(),
                size_bytes: 1,
            },
        );
        let cfg = RetentionConfig::default();
        let mut rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        // Below Emergency floor but a safe candidate exists → Emergency (not Exhausted).
        let input = GovernInput {
            samples: vec![data_sample(GB)],
            disk_img: full_disk_img(),
        };
        let out = rl.govern(&input).unwrap();
        assert!(out.assessment.tier >= Tier::Emergency);
        assert!(!rl.upload_backpressure(&out.assessment).uploads_allowed);
    }

    #[test]
    fn sample_filesystems_reads_each_path() {
        let mut h = harness();
        let mut stats = HashMap::new();
        stats.insert(
            "/".to_string(),
            FsStat {
                dev_id: 1,
                free_bytes: 5 * GB,
                total_bytes: 16 * GB,
                free_inodes: 1,
                total_inodes: 2,
            },
        );
        stats.insert(
            "/data".to_string(),
            FsStat {
                dev_id: 2,
                free_bytes: 50 * GB,
                total_bytes: 256 * GB,
                free_inodes: 1,
                total_inodes: 2,
            },
        );
        h.statfs = FakeStatfs {
            stats,
            calls: RefCell::new(Vec::new()),
        };
        let cfg = RetentionConfig::default();
        let rl = RetentionLoop::new(&cfg, h.seams(), "trash");
        let samples = rl
            .sample_filesystems(&[
                (FsRole::Root, "/".to_string()),
                (FsRole::Data, "/data".to_string()),
            ])
            .unwrap();
        assert_eq!(samples.len(), 2);
        assert_eq!(h.statfs.calls.borrow().len(), 2);
    }

    #[test]
    fn run_cycle_threads_all_phases() {
        let mut h = harness();
        h.store = FakeStore::new(&[("front.mp4", 10, 1), ("event.json", 2, 7), ("a", 1, 3)]);
        h.catalog.eviction = vec![evic(1, EvictionKind::RecentMirror, Durability::Durable)];
        let cfg = RetentionConfig::default();
        let mut rl = RetentionLoop::new(&cfg, h.seams(), "trash");

        let recent = RecentFacts {
            visible: vec![RecentSegment {
                key: "a".to_string(),
                capture_ms: 1,
                size: 1,
            }],
            archived_keys: HashSet::new(),
            scan_gap_ms: 0,
            candidates: vec![RecentClipCandidate {
                key: "a".to_string(),
                capture_ms: 1,
                event_adjacent: false,
                src_rel: "src/a".to_string(),
                dest_rel: "dst/a".to_string(),
            }],
            mirror: Vec::new(),
            incoming_bytes: 0,
        };
        let inputs = CycleInputs {
            folders: vec![event_fact(ArchiveVerification::Unverified, false)],
            recent: Some(recent),
            govern: GovernInput {
                samples: vec![data_sample(200 * GB)],
                disk_img: full_disk_img(),
            },
            health: health_inputs(),
        };
        let report = rl.run_cycle(inputs).unwrap();
        assert_eq!(report.events.len(), 1);
        // First pass over this folder → observing, not yet archived.
        assert_eq!(
            report.events[0].1,
            EventOutcome::Observing { stable_passes: 1 }
        );
        assert!(report.recent.is_some());
        assert_eq!(report.govern.assessment.tier, Tier::Healthy);
        assert!(report.backpressure.uploads_allowed);
        assert_eq!(report.health.archive_tier, "Healthy");
    }

    fn health_inputs() -> HealthInputs {
        HealthInputs {
            car_writeable: true,
            per_fs: Vec::new(),
            disk_img: full_disk_img(),
            archive_by_class: Vec::new(),
            wal_bytes: 0,
            log_bytes: 0,
            pinned_bytes: 0,
            leased_bytes: 0,
            reclaimable_bytes: 0,
            next_candidate_classes: Vec::new(),
            sacrificing_undurable: false,
            paused_writers: Vec::new(),
            last_eviction: None,
        }
    }
}
