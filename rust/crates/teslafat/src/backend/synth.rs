//! `SynthBackend`: production [`BlockBackend`]
//! that serves a real `exFAT` view of an on-disk
//! backing tree.
//!
//! ## Pipeline
//!
//! 1. [`crate::backing_walker::walk`] walks
//!    `cfg.backing_root` and produces a
//!    [`teslausb_core::fs::backing_tree::BackingTree`] describing
//!    every subdirectory + file the synthesizer must surface.
//! 2. [`teslausb_core::fs::exfat::layout::ExfatLayout::plan`]
//!    allocates cluster extents for every directory
//!    and file, materializes the directory cluster bytes, and
//!    records each file's `FilePlacement` (first cluster,
//!    size, backing path).
//! 3. [`teslausb_core::fs::exfat::synth::ExfatSynth::new`] +
//!    `with_data_source` / `with_layout` build the in-memory
//!    metadata + directory-cluster dispatchers.
//! 4. [`SynthBackend::read`] dispatches the NBD read by (a)
//!    asking the synth for the metadata + directory-region
//!    bytes (which return zeros for file clusters by design)
//!    then (b) overlaying file bytes for any extent that
//!    overlaps the requested range by opening the backing file
//!    on demand and seeking to the right offset.
//!
//! ## Why an overlay instead of a `DataClusterSource` wrapper?
//!
//! The layout's own [`teslausb_core::fs::data_cluster_source::DataClusterSource`]
//! impl handles directory clusters and zero-fills file clusters.
//! Wrapping the layout in a materializer that also performs
//! blocking file I/O would force `ExfatSynth::read` (a pure-CPU
//! sync function) to call `File::open` + `seek` + `read` inside
//! the synth dispatcher's loop. That makes the synth's contract
//! (no I/O) leaky and complicates testing.
//!
//! Instead, `SynthBackend::read` runs the pure-CPU synth read
//! into the caller's buffer (which zero-fills file clusters),
//! then walks a sorted list of `FileExtent`s and overlays the
//! real bytes for any overlap. This keeps the synth dispatcher's
//! contract intact and concentrates all I/O in this module.
//!
//! ## Threading
//!
//! `BlockBackend::read` is `async`. The implementation here is
//! synchronous (the Pi Zero 2 W has a single SDIO bus and at
//! most one active NBD client per export — there is no
//! concurrency to exploit). The current-thread tokio runtime
//! locked in by ADR-0003 is also a current-thread runtime;
//! blocking file reads in `read` therefore block the runtime
//! deliberately, matching the serial nature of the underlying
//! medium.
//!
//! ## Write semantics
//!
//! Writes route through the [`ExfatWriteState`] state machine
//! selected at [`SynthBackend::open`] time. Writes to the boot
//! region or reserved sectors are swallowed as metadata that the
//! synth itself owns; FAT-table writes update the in-memory FAT;
//! directory-cluster writes are decoded to discover new files
//! and route subsequent data-cluster writes to `.partial` files
//! on the backing tree, finalized atomically on `flush`. See
//! `backend::exfat_write` for details.

use std::collections::BTreeMap;
use std::fmt;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Mutex, MutexGuard, PoisonError, RwLock, RwLockReadGuard, RwLockWriteGuard};
use std::time::{Duration, SystemTime};

use teslausb_core::backend::{BackendError, BackendResult, BlockBackend, WriteFlags, check_bounds};
use teslausb_core::fs::backing_tree::BackingTree;
use teslausb_core::fs::cluster_layout::Allocation;
use teslausb_core::fs::exfat::geometry::ExfatGeometry;
use teslausb_core::fs::exfat::layout::{
    ExfatLayout, LayoutError as ExfatLayoutError, LayoutMetadata as ExfatLayoutMetadata,
};
use teslausb_core::fs::exfat::synth::{ExfatSynth, ExfatSynthError};
use teslausb_core::fs::exfat::upcase_table::{UPCASE_TABLE_SIZE_BYTES, UpcaseTable};
use teslausb_core::fs::geometry::{Geometry, GeometryError};

use crate::backend::dir_tree::{DirTreeError, DirTreeWriter};
use crate::backend::exfat_write::ExfatWriteState;
use crate::backing_walker::{WalkError, walk};
use crate::config::Config;
use crate::retention;

/// One GiB in bytes; used to convert `volume_size_gb` (the
/// operator-facing knob) into the `u64` size the backend
/// advertises in the NBD handshake.
const BYTES_PER_GIB: u64 = 1024 * 1024 * 1024;

/// Errors that can prevent a [`SynthBackend`] from being opened.
///
/// Each variant wraps the upstream error verbatim so the daemon
/// boundary (`main.rs`, an `anyhow`-using binary) can attach
/// context with `with_context` and the operator gets a useful
/// chain on a startup failure.
#[derive(Debug)]
pub enum SynthBackendError {
    /// Walking `backing_root` failed.
    Walk(WalkError),
    /// Computing the `exFAT` geometry for
    /// `volume_size_gb * 1 GiB` failed.
    Geometry(GeometryError),
    /// The `exFAT` layout planner rejected the tree.
    ExfatLayout(ExfatLayoutError),
    /// The `exFAT` synthesizer rejected the geometry, label,
    /// or layout.
    ExfatSynth(ExfatSynthError),
    /// The configured `volume_label` is too long for the
    /// `exFAT` 11-UTF-16-code-unit limit. (The 11-byte ASCII
    /// limit is already enforced by [`Config::load`]; this
    /// catches the rarer case where 11 ASCII bytes encode to >11
    /// UTF-16 code units, which cannot happen for ASCII but
    /// could happen if the validation rule ever loosens.)
    LabelTooLong {
        /// Number of UTF-16 code units the configured label
        /// encodes to.
        encoded_len: usize,
    },
    /// Constructing the [`DirTreeWriter`] write
    /// path failed — typically because `backing_root` is not a
    /// directory.
    DirTree(DirTreeError),
}

impl fmt::Display for SynthBackendError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Walk(err) => write!(f, "walking backing_root: {err}"),
            Self::Geometry(err) => write!(f, "computing volume geometry: {err}"),
            Self::ExfatLayout(err) => write!(f, "planning exFAT layout: {err}"),
            Self::ExfatSynth(err) => write!(f, "building exFAT synthesizer: {err}"),
            Self::LabelTooLong { encoded_len } => write!(
                f,
                "volume_label encodes to {encoded_len} UTF-16 code units; \
                 exFAT allows at most 11",
            ),
            Self::DirTree(err) => write!(f, "building DirTreeWriter: {err}"),
        }
    }
}

impl std::error::Error for SynthBackendError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Walk(err) => Some(err),
            Self::Geometry(err) => Some(err),
            Self::ExfatLayout(err) => Some(err),
            Self::ExfatSynth(err) => Some(err),
            Self::LabelTooLong { .. } => None,
            Self::DirTree(err) => Some(err),
        }
    }
}

/// Phase 4.4 — outcome of a runtime retention reload.
///
/// Returned by [`SynthBackend::reload_retention`] and echoed back
/// to operators by the IPC layer (Phase 1.5) as a
/// [`teslausb_core::ipc::messages::Response::RetentionReloadAck`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReloadStats {
    /// The threshold applied during this reload, in seconds.
    /// `0` is interpreted as "retention disabled" — see
    /// `SynthBackend::build_retention_policy` (private helper).
    pub hide_after_seconds: u64,
    /// Number of files the new policy hid.
    pub hidden: u32,
    /// Number of files the new policy left visible.
    pub shown: u32,
}

/// A contiguous run of clusters that belongs to a single
/// backing file, expressed as a half-open volume-byte range
/// plus the file's metadata.
///
/// The byte range is fixed at construction by combining the
/// allocation with the volume's `first_data_byte` +
/// `bytes_per_cluster`, so the overlay loop in
/// [`SynthBackend::read`] does only `u64` arithmetic (no
/// per-read multiplications).
#[derive(Debug, Clone)]
struct FileExtent {
    /// First volume byte covered by this extent (inclusive).
    first_byte: u64,
    /// First volume byte past the end of this extent
    /// (exclusive). `end_byte - first_byte == cluster_count *
    /// bytes_per_cluster`.
    end_byte: u64,
    /// File length in bytes. Bytes between `file_size` and the
    /// extent's `end_byte` are zero (cluster-tail padding) and
    /// the overlay deliberately leaves them untouched so the
    /// synth's zero-fill is the source of truth.
    file_size: u64,
    /// Absolute backing path. Opened on each overlapping read.
    backing_path: PathBuf,
}

/// Read-side synthesizer wired to a real on-disk backing tree.
///
/// See the module docs for the pipeline.
pub struct SynthBackend {
    inner: Box<ExfatSynth>,
    /// File extents sorted ascending by `first_byte`. Each
    /// extent is unique (no overlap) because the cluster
    /// allocator hands out disjoint extents.
    ///
    /// Behind an `RwLock` so Phase 4.4's
    /// [`SynthBackend::reload_retention`] can swap the snapshot
    /// atomically at runtime. Reads are common, writes (reloads)
    /// rare; `RwLock` is the right pick. Charter ADR exception:
    /// no new dependency added (std lib only).
    file_extents: RwLock<Vec<FileExtent>>,
    /// Retained for Phase 4.4 reload: the originally-walked
    /// `cfg.backing_root`, so re-walks and re-applications of the
    /// retention filter don't drift from the construction-time
    /// path.
    backing_root: PathBuf,
    size: u64,
    /// exFAT write-side state.
    write_state: Mutex<ExfatWriteState>,
    /// Set once after a poisoned lock has been observed and
    /// recovered, so the recovery path logs the (catastrophic but
    /// recoverable) event exactly once instead of on every
    /// subsequent NBD request at host read/write rate.
    poison_logged: AtomicBool,
    /// Count of in-bounds writes whose backing apply/flush failed and
    /// were dropped to keep the gadget alive (MAJOR 3). An in-bounds
    /// write must never surface as an NBD `EIO` — that makes the
    /// Tesla flag the drive and stop recording. The count is surfaced
    /// for health/diagnostics; the first fault logs loudly, the rest
    /// at `trace` to avoid flooding the log at host write rate.
    write_faults: AtomicU64,
}

impl fmt::Debug for SynthBackend {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SynthBackend")
            .field("fs_type", &"exfat")
            .field("size", &self.size)
            .field(
                "file_count",
                &self
                    .file_extents
                    .read()
                    .map_or_else(|p| p.into_inner().len(), |g| g.len()),
            )
            .field("writable", &true)
            .finish_non_exhaustive()
    }
}

impl SynthBackend {
    /// Build a `SynthBackend` from a parsed [`Config`].
    ///
    /// Walks `cfg.backing_root` once, plans the layout for the
    /// requested `cfg.fs_type`, and assembles the synth +
    /// materializer overlay.
    ///
    /// **Power-cut recovery (Phase 3.6):** before the walk,
    /// `open` scans the backing tree for stale `<path>.partial`
    /// files (in-flight writes from a previous run that did not
    /// complete) and discards them. The number discarded is
    /// logged at INFO level. See
    /// [`crate::backend::dir_tree::DirTreeWriter::recover_partials`]
    /// for the policy rationale.
    ///
    /// # Errors
    ///
    /// Returns [`SynthBackendError`] if any pipeline step fails.
    /// All variants preserve the original error as `source()`.
    pub fn open(cfg: &Config) -> Result<Self, SynthBackendError> {
        let volume_size = u64::from(cfg.volume_size_gb) * BYTES_PER_GIB;
        if cfg.backing_root.is_dir() {
            // Run recovery only if the root exists — if it doesn't
            // the walk below will produce the right error and we
            // don't want to mask it with a recovery I/O failure.
            let recover_writer =
                DirTreeWriter::new(cfg.backing_root.clone()).map_err(SynthBackendError::DirTree)?;
            let discarded = recover_writer
                .recover_partials()
                .map_err(SynthBackendError::DirTree)?;
            if discarded > 0 {
                tracing::info!(
                    discarded,
                    backing_root = %cfg.backing_root.display(),
                    "discarded stale .partial files from previous run"
                );
            }
        }
        let mut tree = walk(&cfg.backing_root).map_err(SynthBackendError::Walk)?;
        // Phase 4.3: apply the retention filter before layout
        // planning. Files aged past `retention.recentclips_hide_after_seconds`
        // (and only those under top-level `RecentClips/`) are
        // dropped from the synthesized view — they don't claim
        // clusters, don't appear in directory listings, and
        // their clusters report free in FSInfo /
        // allocation-bitmap. The backing files stay on disk
        // until the Phase 4b cleanup worker decides whether to
        // reap them.
        //
        // System clock is read here instead of through a Clock
        // trait: `SynthBackend::open` is the single integration
        // point, called once per daemon launch; injecting a
        // mockable clock all the way down to this call site
        // would cost more in plumbing than the test value it
        // would add. Tests that need to exercise hidden files
        // pre-date the file mtimes before constructing the
        // backend (see `retention_hides_aged_recentclips` test
        // below for the pattern).
        let now = SystemTime::now();
        let hide_after = cfg.retention.recentclips_hide_after_seconds;
        let policy = Self::build_retention_policy(hide_after);
        let stats = retention::apply(&mut tree, &cfg.backing_root, now, &policy);
        if stats.hidden > 0 {
            tracing::info!(
                hidden = stats.hidden,
                shown = stats.shown,
                hide_after_seconds = hide_after,
                "retention filter applied at synth open"
            );
        }
        let serial = compute_volume_serial(&cfg.volume_label);
        Self::open_exfat(cfg, volume_size, &tree, serial)
    }

    fn open_exfat(
        cfg: &Config,
        volume_size: u64,
        tree: &BackingTree,
        serial: u32,
    ) -> Result<Self, SynthBackendError> {
        let geometry =
            ExfatGeometry::for_volume_size(volume_size).map_err(SynthBackendError::Geometry)?;
        let label_utf16: Vec<u16> = cfg.volume_label.encode_utf16().collect();
        if label_utf16.len() > 11 {
            return Err(SynthBackendError::LabelTooLong {
                encoded_len: label_utf16.len(),
            });
        }
        let synth = ExfatSynth::new(geometry.clone(), serial, &label_utf16)
            .map_err(SynthBackendError::ExfatSynth)?;
        let upcase = UpcaseTable::ascii_identity();
        let bytes_per_cluster = geometry.bytes_per_cluster();
        let bitmap_first = synth.bitmap_first_cluster();
        let upcase_first = synth.upcase_first_cluster();
        let upcase_clusters = synth.upcase_cluster_count();
        // Spec-minimal DataLength (ceil(ClusterCount/8)) — what a real
        // mkfs.exfat advertises — not the whole-cluster storage
        // footprint. The bitmap entry is the only consumer of this
        // value (it sizes the directory entry's DataLength, not any
        // allocation), and the synth's own no-layout root uses the
        // same minimal size, so this keeps both paths consistent
        // (ADR-0028 review, MINOR 7).
        let bitmap_size_bytes = synth.bitmap_data_length_bytes();
        let upcase_size_bytes = UPCASE_TABLE_SIZE_BYTES as u64;
        let first_free = upcase_first.saturating_add(upcase_clusters);
        let metadata = ExfatLayoutMetadata {
            bitmap_first_cluster: bitmap_first,
            bitmap_size_bytes,
            upcase_first_cluster: upcase_first,
            upcase_size_bytes,
            upcase: &upcase,
            volume_label_utf16: &label_utf16,
            first_free_cluster: first_free,
        };
        let layout = ExfatLayout::plan(&geometry, &metadata, tree)
            .map_err(SynthBackendError::ExfatLayout)?;
        let first_data_byte = layout.cluster_heap_byte_offset();
        let file_extents = build_file_extents(
            layout.file_placements().iter().map(|f| ExtentInput {
                allocation: f.allocation,
                size_bytes: f.size_bytes,
                backing_path: f.backing_path.clone(),
            }),
            // exFAT cluster numbering starts at 2 —
            // cluster N lives at cluster_heap_byte_offset + (N - 2) * bpc.
            first_data_byte_for_cluster_two(first_data_byte, bytes_per_cluster),
            bytes_per_cluster,
        );
        // Build pre-existing extents for the exFAT
        // write-side cluster map: skip empty files (no clusters) and
        // skip files that can't be relativised against the
        // backing root.
        let mut pre_existing_extents: Vec<crate::backend::exfat_write::PreExistingExfatExtent> =
            Vec::new();
        for f in layout.file_placements() {
            if f.allocation.is_empty() {
                continue;
            }
            let Ok(relative) = f.backing_path.strip_prefix(&cfg.backing_root) else {
                tracing::warn!(
                    backing_path = %f.backing_path.display(),
                    backing_root = %cfg.backing_root.display(),
                    "skipping file: backing_path not under backing_root"
                );
                continue;
            };
            pre_existing_extents.push(crate::backend::exfat_write::PreExistingExfatExtent {
                first_cluster: f.allocation.first_cluster,
                cluster_count: f.allocation.cluster_count,
                first_byte_in_file: 0,
                file_size_bytes: f.size_bytes,
                relative_path: relative.to_path_buf(),
            });
        }
        // Snapshot the (cheap, byte-free) directory placements
        // before the layout is moved into the synth — the resolver
        // seeds every subdirectory's cluster chain from these so
        // Tesla's clip-entry writes resolve from the first one after
        // a remount (2026-06-01 recording-outage fix).
        let dir_placements = layout.dir_placements().to_vec();
        // Snapshot the root directory's full cluster chain too: when
        // the root's entry sets exceed one cluster it is FAT-chained
        // (fixed root cluster -> overflow run -> EOC), and the
        // resolver must seed every root cluster so a host write into
        // an overflow cluster resolves to the root directory.
        let root_chain = layout.root_cluster_chain();
        let synth = synth
            .with_layout(layout)
            .map_err(SynthBackendError::ExfatSynth)?;
        let dir_tree =
            DirTreeWriter::new(cfg.backing_root.clone()).map_err(SynthBackendError::DirTree)?;
        let bitmap_first_cluster = synth.bitmap_first_cluster();
        let bitmap_cluster_count = synth.bitmap_cluster_count();
        let mut exfat_write =
            ExfatWriteState::new(geometry.clone(), dir_tree, &pre_existing_extents)
                .with_allocation_bitmap(bitmap_first_cluster, bitmap_cluster_count);
        if let Some(source) = synth.directory_byte_source() {
            exfat_write = exfat_write.with_directory_seed(source, &root_chain, &dir_placements);
        }
        if let Some(ref spill_dir) = cfg.spill_dir {
            exfat_write = exfat_write.with_disk_spill(
                spill_dir.clone(),
                crate::backend::pending_spill::DEFAULT_DISK_SPILL_BYTES,
            );
        }
        Ok(Self {
            inner: Box::new(synth),
            file_extents: RwLock::new(file_extents),
            backing_root: cfg.backing_root.clone(),
            size: volume_size,
            write_state: Mutex::new(exfat_write),
            poison_logged: AtomicBool::new(false),
            write_faults: AtomicU64::new(0),
        })
    }

    /// Total size of the synthesized volume in bytes. Same value
    /// returned by [`BlockBackend::size`]; exposed as an inherent
    /// method so callers that don't already have `BlockBackend`
    /// in scope (notably the `main.rs` startup logger) can read
    /// it without an extra `use`.
    #[must_use]
    pub fn volume_size(&self) -> u64 {
        self.size
    }

    /// Number of file extents the backend serves.
    ///
    /// Exposed for tests and operator diagnostics; the daemon's
    /// startup logs include this to confirm the walker found
    /// what was expected.
    #[must_use]
    pub fn file_count(&self) -> usize {
        self.file_extents
            .read()
            .map_or_else(|p| p.into_inner().len(), |g| g.len())
    }

    /// Whether the volume is quiescent — no host write is
    /// mid-flight (no unresolved dir entry, no out-of-order data
    /// awaiting an owner, no `.partial` awaiting flush).
    ///
    /// [`crate::backend::reloadable::ReloadableBackend`] consults
    /// this before going live with a re-walked view: a full
    /// layout swap is only safe while no in-flight write is
    /// addressing the *current* layout. A poisoned `write_state`
    /// lock is treated as **not** quiescent (fail-safe: never swap
    /// when the write machine's state is uncertain).
    #[must_use]
    pub fn is_quiescent(&self) -> bool {
        self.write_state
            .lock()
            .is_ok_and(|state| state.is_quiescent())
    }

    /// Test-only: force the write machine to look mid-write so the
    /// quiescence gate in
    /// [`crate::backend::reloadable::ReloadableBackend::try_go_live`]
    /// can be exercised deterministically.
    #[cfg(test)]
    pub(crate) fn mark_inflight_for_test(&self) {
        if let Ok(mut state) = self.write_state.lock() {
            state.mark_inflight_for_test();
        }
    }

    /// Snapshot the current extent table. Test-only helper: the
    /// production read path acquires the lock per-call.
    #[cfg(test)]
    fn extents_snapshot(&self) -> Vec<FileExtent> {
        self.file_extents
            .read()
            .map_or_else(|p| p.into_inner().clone(), |g| g.clone())
    }

    /// First (lowest) byte offset of the first backing-file extent,
    /// or `None` if no files are mapped. Test-only helper for
    /// sibling-module tests that need to read a known file's data
    /// region without reaching into the private extent table.
    #[cfg(test)]
    pub(crate) fn first_file_byte(&self) -> Option<u64> {
        self.file_extents
            .read()
            .map_or_else(|p| p.into_inner().clone(), |g| g.clone())
            .first()
            .map(|e| e.first_byte)
    }

    /// Translate the operator-facing `recentclips_hide_after_seconds`
    /// knob into a [`retention::Policy`].
    ///
    /// `0` is interpreted as "retention disabled" — translated to
    /// [`Duration::MAX`] so the filter never hides. Without this,
    /// a zero threshold would hide every `RecentClips/` file
    /// because [`retention::decide`] is `age > threshold`
    /// strict-greater and any non-zero age beats
    /// [`Duration::ZERO`]. Operator semantics (zero = off)
    /// trump module-internal semantics (zero = strictest).
    fn build_retention_policy(hide_after_seconds: u64) -> retention::Policy {
        if hide_after_seconds == 0 {
            retention::Policy::new(Duration::MAX)
        } else {
            retention::Policy::new(Duration::from_secs(hide_after_seconds))
        }
    }

    /// Phase 4.4 — recompute what the retention filter would do
    /// under a new `hide_after_seconds` threshold.
    ///
    /// Re-walks `backing_root` (captured at construction time),
    /// applies the new policy, and returns the resulting hidden /
    /// shown counts. Intended to be called by the IPC dispatcher
    /// (Phase 1.5) in response to a
    /// [`teslausb_core::ipc::messages::Request::ReloadRetention`].
    ///
    /// # Scope (read this carefully)
    ///
    /// This method **previews** the new policy and atomically
    /// shrinks the live extent table so that hidden files stop
    /// returning content on reads. It does NOT re-render the
    /// underlying FAT/bitmap/directory entries the synth pre-built
    /// at [`SynthBackend::open`] time — that would require swapping
    /// `inner` and `write_state` atomically with respect to
    /// concurrent NBD reads and active writers, which is the
    /// Phase 1.5 daemon-dispatcher's job (full re-open + host-level
    /// `ArcSwap`). Until that lands, the operator-visible effect
    /// of a runtime reload is: previously-visible-but-now-hidden
    /// files stop overlaying their content (reads return synth
    /// zeros for those clusters) while their dir entries and
    /// FAT/bitmap allocation flags remain until the next daemon
    /// restart. Newly-shown (un-hidden) files do NOT come back at
    /// runtime — they only re-appear after a restart, because the
    /// synth would need to re-render the directory tree.
    ///
    /// The returned [`ReloadStats`] reflect what a full re-open
    /// WOULD show, so the operator can confirm the new threshold
    /// has the intended scope before deciding whether to restart.
    ///
    /// # Errors
    ///
    /// Returns [`SynthBackendError::Walk`] if the re-walk fails
    /// (e.g., backing root removed between open and reload).
    pub fn reload_retention(
        &self,
        hide_after_seconds: u64,
    ) -> Result<ReloadStats, SynthBackendError> {
        let mut tree = walk(&self.backing_root).map_err(SynthBackendError::Walk)?;
        let policy = Self::build_retention_policy(hide_after_seconds);
        let now = SystemTime::now();
        let stats = retention::apply(&mut tree, &self.backing_root, now, &policy);
        // Shrink the live extent table to the surviving files so
        // hidden clusters stop returning the old content. We hold
        // the write lock for the swap; reads will block briefly.
        let mut surviving: std::collections::HashSet<PathBuf> = std::collections::HashSet::new();
        collect_file_paths(&tree.root, &mut surviving);
        let mut guard = self.extents_guard_mut();
        guard.retain(|ext| surviving.contains(&ext.backing_path));
        let live_extents = guard.len();
        drop(guard);
        tracing::info!(
            hide_after_seconds,
            hidden = stats.hidden,
            shown = stats.shown,
            live_extents,
            "retention reload preview applied (full effect requires daemon restart \
             until Phase 1.5 dispatcher lands)"
        );
        Ok(ReloadStats {
            hide_after_seconds,
            hidden: u32::try_from(stats.hidden).unwrap_or(u32::MAX),
            shown: u32::try_from(stats.shown).unwrap_or(u32::MAX),
        })
    }

    /// Acquire the `file_extents` read guard, recovering it if a
    /// prior panic poisoned the lock. A poisoned lock must NEVER
    /// become an NBD `EIO`: the same backend type serves the
    /// `TeslaCam` recording partition, and surfacing an error to the
    /// host makes the Tesla flag the drive and stop recording. The
    /// snapshot is overwrite-on-reload and reads tolerate a
    /// partially-updated view (worst case: zero-fill), so serving
    /// the recovered guard is strictly safer than failing.
    fn extents_guard(&self) -> RwLockReadGuard<'_, Vec<FileExtent>> {
        self.file_extents
            .read()
            .unwrap_or_else(|poisoned| self.recover_poison(poisoned, "file_extents"))
    }

    /// Acquire the `file_extents` write guard, recovering a
    /// poisoned lock for the same reason as [`Self::extents_guard`].
    fn extents_guard_mut(&self) -> RwLockWriteGuard<'_, Vec<FileExtent>> {
        self.file_extents
            .write()
            .unwrap_or_else(|poisoned| self.recover_poison(poisoned, "file_extents"))
    }

    /// Acquire the `write_state` guard, recovering a poisoned lock
    /// so a panic on one request can never wedge the write path
    /// into a permanent EIO that stops recording.
    fn write_state_guard(&self) -> MutexGuard<'_, ExfatWriteState> {
        self.write_state
            .lock()
            .unwrap_or_else(|poisoned| self.recover_poison(poisoned, "write_state"))
    }

    /// Recover the inner guard from a [`PoisonError`], logging the
    /// event at most once across the backend's lifetime to avoid
    /// flooding the log at host request rate once a lock is
    /// poisoned (a poisoned lock stays poisoned).
    fn recover_poison<G>(&self, poisoned: PoisonError<G>, lock: &str) -> G {
        if !self.poison_logged.swap(true, Ordering::Relaxed) {
            tracing::error!(
                lock,
                "synth backend lock poisoned by a prior panic; recovering the guard to \
                 keep serving the gadget (a poisoned lock must never become an NBD EIO \
                 that makes the Tesla flag the drive and stop recording)"
            );
        }
        poisoned.into_inner()
    }

    /// Record and swallow a backing write/flush fault on an in-bounds
    /// request so it never becomes an NBD `EIO`.
    ///
    /// MAJOR 3: backing-disk `ENOSPC`/`EACCES`, a missing `.partial`,
    /// or a cluster-map/dir-tree error during `apply_write`/`flush`
    /// must not propagate to the host. The Tesla treats any `EIO` on
    /// the drive as a fault, flags it, and stops recording. Dropping
    /// the write (the file degrades to zero-fill on read via the
    /// tolerant overlay) keeps the gadget alive, which the operator
    /// invariant ranks strictly above MEDIA write fidelity. The
    /// `.partial` finalize path is state-preserving (it re-queues
    /// failed paths), so a dropped flush is retried on the next one.
    fn tolerate_write_fault(&self, op: &str, offset: u64, len: usize, err: &BackendError) {
        let prior = self.write_faults.fetch_add(1, Ordering::Relaxed);
        if prior == 0 {
            tracing::warn!(
                op,
                offset,
                len,
                %err,
                "in-bounds backing write fault dropped to keep the gadget alive; an in-bounds \
                 write must never become an NBD EIO that stops TeslaCam recording"
            );
        } else {
            tracing::trace!(op, offset, len, %err, total_faults = prior + 1, "write fault dropped");
        }
    }

    /// Number of in-bounds write/flush faults dropped to preserve the
    /// gadget (MAJOR 3). Exposed for health/diagnostics surfacing.
    #[must_use]
    pub fn write_fault_count(&self) -> u64 {
        self.write_faults.load(Ordering::Relaxed)
    }

    fn read_sync(&self, offset: u64, buf: &mut [u8]) -> BackendResult<()> {
        self.inner
            .read(offset, buf)
            .map_err(|e| BackendError::Io(std::io::Error::other(format!("exfat synth: {e}"))))?;
        // Overlay file content for every extent that overlaps
        // the requested range. Extents are sorted by first_byte
        // so a binary-search lower bound would let us skip the
        // ones strictly below the read; for typical NBD reads
        // (≤ 256 KiB) the linear scan over hundreds-to-thousands
        // of extents is dominated by the file I/O cost, so leave
        // the optimisation to a future increment if profiling
        // shows it matters.
        let read_end = offset.saturating_add(buf.len() as u64);
        let extents = self.extents_guard();
        for extent in extents.iter() {
            if extent.end_byte <= offset {
                continue;
            }
            if extent.first_byte >= read_end {
                break;
            }
            overlay_file_extent(extent, offset, buf)?;
        }
        drop(extents);

        // Phase 3.5f: overlay any in-memory write-state updates
        // (kernel-written FAT entries and directory cluster
        // bytes) on top of the synth's startup snapshot. Without
        // this, the kernel would see only the pre-existing files
        // after umount/remount even though the write path has
        // already persisted the new files to the backing tree.
        // See `ExfatWriteState::overlay_read` for the full
        // rationale and the hardware bug (H3-2) it fixes.
        let guard = self.write_state_guard();
        guard.overlay_read(offset, buf);
        Ok(())
    }
}

impl BlockBackend for SynthBackend {
    fn size(&self) -> u64 {
        self.size
    }

    async fn read(&self, offset: u64, buf: &mut [u8]) -> BackendResult<()> {
        check_bounds(offset, buf.len(), self.size)?;
        if buf.is_empty() {
            return Ok(());
        }
        self.read_sync(offset, buf)
    }

    async fn write(&self, offset: u64, buf: &[u8], flags: WriteFlags) -> BackendResult<()> {
        check_bounds(offset, buf.len(), self.size)?;
        if buf.is_empty() {
            return Ok(());
        }
        let mut guard = self.write_state_guard();
        // In-bounds write faults are swallowed (logged + counted) so a
        // backing ENOSPC/EACCES/etc. never becomes an NBD EIO that
        // stops TeslaCam recording. See `tolerate_write_fault`.
        let result = guard
            .apply_write(offset, buf)
            .map_err(BackendError::from)
            .and_then(|()| {
                if flags.contains(WriteFlags::FUA) {
                    guard.flush().map_err(BackendError::from)
                } else {
                    Ok(())
                }
            });
        if let Err(err) = result {
            self.tolerate_write_fault("write", offset, buf.len(), &err);
        }
        Ok(())
    }

    async fn flush(&self) -> BackendResult<()> {
        let mut guard = self.write_state_guard();
        let result = guard.flush().map_err(BackendError::from);
        if let Err(err) = result {
            self.tolerate_write_fault("flush", 0, 0, &err);
        }
        Ok(())
    }
}

/// Normalized adapter input for the exFAT layout's file
/// placements, so [`build_file_extents`] can share the
/// index-construction logic.
struct ExtentInput {
    allocation: Allocation,
    size_bytes: u64,
    backing_path: PathBuf,
}

fn build_file_extents(
    placements: impl Iterator<Item = ExtentInput>,
    first_data_byte_for_cluster_two: u64,
    bytes_per_cluster: u32,
) -> Vec<FileExtent> {
    let bpc = u64::from(bytes_per_cluster);
    // De-dup + sort: gather into a BTreeMap keyed by first_byte
    // so the resulting Vec is naturally ascending. The allocator
    // returns disjoint extents, so different files never share
    // a `first_byte`.
    let mut by_first_byte: BTreeMap<u64, FileExtent> = BTreeMap::new();
    for p in placements {
        if p.allocation.is_empty() {
            continue;
        }
        let cluster_offset_from_two = u64::from(p.allocation.first_cluster - 2);
        let first_byte = first_data_byte_for_cluster_two + cluster_offset_from_two * bpc;
        let end_byte = first_byte + u64::from(p.allocation.cluster_count) * bpc;
        by_first_byte.insert(
            first_byte,
            FileExtent {
                first_byte,
                end_byte,
                file_size: p.size_bytes,
                backing_path: p.backing_path,
            },
        );
    }
    by_first_byte.into_values().collect()
}

/// `exFAT`'s [`ExfatLayout::cluster_heap_byte_offset`] is the
/// byte offset of cluster 2 (the cluster heap is indexed from 2).
/// This helper exists to make the alignment explicit at the
/// call sites and to give a single place to revisit if the
/// layout's cluster-numbering convention shifts.
const fn first_data_byte_for_cluster_two(
    cluster_heap_byte_offset: u64,
    _bytes_per_cluster: u32,
) -> u64 {
    cluster_heap_byte_offset
}

/// Recursively gather absolute backing paths of every file in
/// `dir` (and its descendants) into `out`. Used by
/// [`SynthBackend::reload_retention`] to compute the set of
/// surviving files after applying a new retention policy.
fn collect_file_paths(
    dir: &teslausb_core::fs::backing_tree::BackingDir,
    out: &mut std::collections::HashSet<PathBuf>,
) {
    for f in &dir.files {
        out.insert(f.backing_path.clone());
    }
    for d in &dir.subdirs {
        collect_file_paths(d, out);
    }
}

fn overlay_file_extent(extent: &FileExtent, read_offset: u64, buf: &mut [u8]) -> BackendResult<()> {
    let read_end = read_offset.saturating_add(buf.len() as u64);
    let overlap_start = read_offset.max(extent.first_byte);
    let overlap_end = read_end.min(extent.end_byte);
    if overlap_start >= overlap_end {
        return Ok(());
    }
    let file_offset_start = overlap_start - extent.first_byte;
    let mut file_offset_end = overlap_end - extent.first_byte;
    // Clamp by the file's actual size. Bytes between the file
    // size and the extent end are cluster-tail padding; the
    // synth already zero-filled them, so leave them alone.
    if file_offset_end > extent.file_size {
        file_offset_end = extent.file_size;
    }
    if file_offset_end <= file_offset_start {
        return Ok(());
    }
    let buf_start = usize::try_from(overlap_start - read_offset).map_err(|_| {
        BackendError::Io(std::io::Error::other(
            "overlay buf_start exceeds usize::MAX",
        ))
    })?;
    let copy_len = usize::try_from(file_offset_end - file_offset_start).map_err(|_| {
        BackendError::Io(std::io::Error::other("overlay copy_len exceeds usize::MAX"))
    })?;
    let Some(target) = buf.get_mut(buf_start..buf_start + copy_len) else {
        return Err(BackendError::Io(std::io::Error::other(
            "overlay target slice out of bounds",
        )));
    };
    // Tolerant overlay: a backing file that vanished, was truncated,
    // or errors mid-read must NEVER fail the NBD read. teslafat must
    // never EIO a valid in-partition read — a clip removed by
    // retention or the privileged delete helper between the walk and
    // this read (or a MEDIA file replaced underfoot) would otherwise
    // surface to the host as `nbd0: Other side returned error (5)`,
    // which makes the Tesla flag the drive and stop recording. Serve
    // the bytes we can read and leave the synth's existing zero-fill
    // in place for any shortfall, so the host always gets a clean read.
    let mut file = match File::open(&extent.backing_path) {
        Ok(f) => f,
        Err(e) => {
            tracing::warn!(
                path = %extent.backing_path.display(),
                error = %e,
                "overlay: backing file open failed; serving zero-fill"
            );
            return Ok(());
        }
    };
    if let Err(e) = file.seek(SeekFrom::Start(file_offset_start)) {
        tracing::warn!(
            path = %extent.backing_path.display(),
            error = %e,
            "overlay: backing file seek failed; serving zero-fill"
        );
        return Ok(());
    }
    let mut done = 0usize;
    while done < target.len() {
        let Some(dst) = target.get_mut(done..) else {
            break;
        };
        match file.read(dst) {
            // Clean EOF: the backing file is shorter than the layout
            // recorded (e.g. it shrank since the walk). The remaining
            // target bytes keep the synth's zero-fill.
            Ok(0) => break,
            Ok(n) => done += n,
            Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => {
                tracing::warn!(
                    path = %extent.backing_path.display(),
                    error = %e,
                    read_bytes = done,
                    "overlay: backing file read failed; serving partial + zero-fill"
                );
                break;
            }
        }
    }
    Ok(())
}

/// Derive a deterministic 32-bit volume serial from the configured
/// volume label using FNV-1a.
///
/// Deriving the serial from the label makes it reproducible across
/// boots without persisting any state, so the same backing volume
/// presents the same serial each time it is synthesized. Zero is
/// avoided by `mkfs.vfat` convention; the fallback constant is
/// arbitrary.
fn compute_volume_serial(label: &str) -> u32 {
    let mut hash: u32 = 0x811c_9dc5;
    for &b in label.as_bytes() {
        hash ^= u32::from(b);
        hash = hash.wrapping_mul(0x0100_0193);
    }
    if hash == 0 { 0xCAFE_BABE } else { hash }
}

#[cfg(test)]
#[allow(
    clippy::cognitive_complexity,
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::panic,
    clippy::unwrap_used
)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::Path;
    use teslausb_core::backend::BackendError;

    fn write_file(path: &Path, bytes: &[u8]) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, bytes).unwrap();
    }

    fn sample_cfg(backing_root: PathBuf) -> Config {
        Config {
            backing_root,
            volume_size_gb: 4,
            volume_label: "TESTLABEL".to_string(),
            cluster_size: None,
            fs_type: crate::config::FsType::Exfat,
            retention: crate::config::RetentionConfig::default(),
            spill_dir: None,
            reload_on_sighup: true,
        }
    }

    /// A poisoned lock must NEVER surface as an NBD `EIO`: that
    /// makes the Tesla flag the drive and stop recording. After a
    /// panic poisons both inner locks, reads/writes/flushes must
    /// still return `Ok` (recovered guard), not `BackendError::Io`.
    #[tokio::test]
    async fn poisoned_locks_recover_instead_of_returning_eio() {
        let dir = tempfile::tempdir().unwrap();
        write_file(&dir.path().join("alpha.mp4"), &[0x11; 8192]);
        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");

        // Poison write_state by panicking while its guard is held.
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _g = backend.write_state.lock().unwrap();
            panic!("intentional poison of write_state");
        }));
        // Poison file_extents the same way.
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _g = backend.file_extents.write().unwrap();
            panic!("intentional poison of file_extents");
        }));

        let mut buf = vec![0u8; 512];
        backend
            .read(0, &mut buf)
            .await
            .expect("read must recover a poisoned lock, not EIO");
        backend
            .write(0, &[0u8; 512], WriteFlags::NONE)
            .await
            .expect("write must recover a poisoned lock, not EIO");
        backend
            .flush()
            .await
            .expect("flush must recover a poisoned lock, not EIO");
    }

    #[tokio::test]
    async fn read_past_end_of_file_returns_zero_padding() {
        let dir = tempfile::tempdir().unwrap();
        // 100 bytes — well below a single cluster.
        let payload = vec![0xAAu8; 100];
        write_file(&dir.path().join("short.bin"), &payload);
        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");
        let extent = backend
            .extents_snapshot()
            .first()
            .cloned()
            .expect("at least one extent");
        let cluster_bytes = extent.end_byte - extent.first_byte;
        let mut buf = vec![0xFFu8; usize::try_from(cluster_bytes).unwrap()];
        backend
            .read(extent.first_byte, &mut buf)
            .await
            .expect("read ok");
        // First 100 bytes are the file content.
        assert_eq!(&buf[..100], payload.as_slice(), "file head overlay");
        // Cluster tail (bytes 100..cluster_size) must be zero,
        // because the synth zero-fills and the overlay declined
        // to touch them.
        assert!(
            buf[100..].iter().all(|&b| b == 0),
            "cluster tail must be zero-padded, got {:?}",
            &buf[100..120]
        );
    }

    #[tokio::test]
    async fn read_of_vanished_backing_file_returns_zeros_not_eio() {
        // Regression (INCIDENT 2026-06-02): a backing file removed by
        // retention or the privileged delete helper BETWEEN the walk
        // and an on-demand overlay read must NOT surface as NBD_EIO
        // (`nbd0: Other side returned error (5)`), which makes the
        // Tesla flag the drive and stop recording. The vanished file's
        // region must read back as the synth's zero-fill.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("data.bin");
        write_file(&path, &[0x33u8; 4096]);
        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");
        let extent_first = backend
            .extents_snapshot()
            .into_iter()
            .next()
            .expect("at least one extent")
            .first_byte;
        // Simulate the file disappearing after the walk indexed it.
        fs::remove_file(&path).unwrap();
        let mut buf = vec![0xFFu8; 4096];
        backend
            .read(extent_first, &mut buf)
            .await
            .expect("read must not EIO on a vanished backing file");
        assert!(
            buf.iter().all(|&b| b == 0),
            "vanished backing file must read back as zero-fill, not an error"
        );
    }

    #[tokio::test]
    async fn read_of_shrunk_backing_file_serves_available_then_zero() {
        // Regression: a backing file truncated/shrunk after the walk
        // recorded its size must serve the bytes that remain and
        // zero-fill the shortfall — never EIO the whole read.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("data.bin");
        write_file(&path, &[0x44u8; 4096]);
        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");
        let extent_first = backend
            .extents_snapshot()
            .into_iter()
            .next()
            .expect("at least one extent")
            .first_byte;
        // File shrinks to 100 bytes after the walk recorded 4096.
        write_file(&path, &[0x44u8; 100]);
        let mut buf = vec![0xFFu8; 4096];
        backend
            .read(extent_first, &mut buf)
            .await
            .expect("read must not EIO on a shrunk backing file");
        assert_eq!(
            &buf[..100],
            &[0x44u8; 100],
            "available backing bytes must be served"
        );
        assert!(
            buf[100..].iter().all(|&b| b == 0),
            "shortfall must be zero-filled, not an error"
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn read_of_unreadable_backing_file_returns_zeros_not_eio() {
        // Regression (INCIDENT 2026-06-02/03, confirmed root cause): the
        // MEDIA partition's LightShow/ files were mode 0600 owned by `pi`,
        // while teslafat runs as the `teslausb` service user. The startup
        // walk stats them through the 0755 directories and records an
        // extent, but the on-demand overlay `File::open` fails with EACCES
        // at read time. The deployed build mapped that to
        // `nbd0: Other side returned error (5)` at the SAME sector every
        // boot — a deterministic, fixed-LBA EIO. A present-but-unreadable
        // backing file must read back as the synth's zero-fill, never EIO.
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("lightshow.bin");
        write_file(&path, &[0x5Au8; 4096]);
        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");
        let extent_first = backend
            .extents_snapshot()
            .into_iter()
            .next()
            .expect("at least one extent")
            .first_byte;
        // Drop all permission bits so a non-privileged service user cannot
        // open it — exactly the 0600-pi-vs-teslausb mismatch on the device.
        fs::set_permissions(&path, fs::Permissions::from_mode(0o000)).unwrap();
        // Skip when the process bypasses mode bits (running as root, or a
        // filesystem that ignores permissions): the EACCES path is not
        // reproducible here and the assertion would be vacuous.
        if fs::File::open(&path).is_ok() {
            return;
        }
        let mut buf = vec![0xFFu8; 4096];
        backend
            .read(extent_first, &mut buf)
            .await
            .expect("read must not EIO on an unreadable (0600) backing file");
        assert!(
            buf.iter().all(|&b| b == 0),
            "unreadable backing file must read back as zero-fill, not an error"
        );
    }

    #[tokio::test]
    async fn exfat_write_to_boot_sector_is_accepted_as_metadata() {
        // Phase 3.5e shipped exFAT write support. Writes to the
        // boot region are swallowed as metadata (the synth is the
        // source of truth for boot bytes); writes to data
        // clusters / FAT / dirs are exercised by
        // exfat_write::tests.
        let dir = tempfile::tempdir().unwrap();
        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");
        backend
            .write(0, &[0u8; 16], WriteFlags::NONE)
            .await
            .expect("metadata write should be accepted");
    }

    #[tokio::test]
    async fn flush_succeeds_with_no_state() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");
        backend.flush().await.expect("flush ok");
    }

    #[tokio::test]
    async fn read_out_of_bounds_returns_out_of_bounds() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");
        let mut buf = [0u8; 16];
        let err = backend
            .read(backend.size() - 8, &mut buf)
            .await
            .expect_err("read past end should fail");
        assert!(matches!(err, BackendError::OutOfBounds { .. }));
    }

    #[tokio::test]
    async fn exfat_open_walks_backing_root_and_size_matches_config() {
        let dir = tempfile::tempdir().unwrap();
        write_file(&dir.path().join("clip.mp4"), &[0x22; 4096]);
        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");
        assert_eq!(backend.size(), 4 * BYTES_PER_GIB);
    }

    #[tokio::test]
    async fn exfat_read_at_offset_zero_returns_main_boot_sector_signature() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");
        let mut sector = [0u8; 512];
        backend.read(0, &mut sector).await.expect("read ok");
        // exFAT main boot sector also ends in 0x55 0xAA per
        // exFAT spec §3.1.
        assert_eq!(sector[510], 0x55);
        assert_eq!(sector[511], 0xAA);
    }

    #[tokio::test]
    async fn exfat_allocation_bitmap_entry_advertises_spec_minimal_data_length() {
        // MINOR 7 regression: the allocation-bitmap directory entry's
        // DataLength must be ceil(ClusterCount/8) — what a real
        // mkfs.exfat advertises — NOT the whole-cluster storage
        // footprint. Pre-fix it advertised a full cluster.
        let dir = tempfile::tempdir().unwrap();
        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");

        let geometry =
            ExfatGeometry::for_volume_size(4 * BYTES_PER_GIB).expect("4 GiB exFAT geometry");
        let sector = u64::from(teslausb_core::fs::geometry::SECTOR_SIZE_BYTES);
        let root_offset = u64::from(geometry.cluster_heap_offset_sectors()) * sector;

        // The first root-directory entry is the allocation bitmap
        // (type 0x81); its DataLength is an 8-byte LE field at 0x18.
        let mut entry = [0u8; 32];
        backend
            .read(root_offset, &mut entry)
            .await
            .expect("read root bitmap entry");
        assert_eq!(entry[0], 0x81, "first root entry is the allocation bitmap");
        let data_length = u64::from_le_bytes(entry[0x18..0x20].try_into().unwrap());

        let expected = u64::from(geometry.cluster_count()).div_ceil(8);
        assert_eq!(
            data_length, expected,
            "bitmap DataLength must be ceil(ClusterCount/8)"
        );
        assert!(
            data_length < u64::from(geometry.bytes_per_cluster()),
            "for this geometry the spec-minimal bitmap is smaller than one \
             cluster, proving the entry is no longer over-stated to a full cluster"
        );
    }

    #[tokio::test]
    async fn exfat_read_of_file_cluster_returns_backing_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let payload: Vec<u8> = (0..2048u32).map(|i| ((i * 7) % 251) as u8).collect();
        write_file(&dir.path().join("recording.h265"), &payload);
        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");
        let extent_first = backend
            .extents_snapshot()
            .into_iter()
            .next()
            .expect("at least one extent")
            .first_byte;
        let mut buf = vec![0u8; payload.len()];
        backend.read(extent_first, &mut buf).await.expect("read ok");
        assert_eq!(buf, payload);
    }

    #[test]
    fn compute_volume_serial_is_stable_for_same_label() {
        assert_eq!(
            compute_volume_serial("TESLACAM"),
            compute_volume_serial("TESLACAM")
        );
        assert_ne!(
            compute_volume_serial("TESLACAM"),
            compute_volume_serial("DASHCAM")
        );
    }

    #[test]
    fn compute_volume_serial_never_returns_zero() {
        // FNV-1a on an empty string returns 0x811c9dc5, not 0,
        // so this is hard to trigger naturally — exercise the
        // fallback branch directly by asserting the constant
        // can never be zero.
        assert_ne!(compute_volume_serial(""), 0);
    }

    #[tokio::test]
    async fn backing_root_must_exist() {
        let dir = tempfile::tempdir().unwrap();
        let missing = dir.path().join("does-not-exist");
        let cfg = sample_cfg(missing);
        let err = SynthBackend::open(&cfg).expect_err("open should fail");
        assert!(matches!(err, SynthBackendError::Walk(_)));
    }

    // Phase 4.3 — retention filter wiring at synth open

    /// Helper: backdate a file's mtime so it appears older than
    /// `seconds` to the retention filter. Uses `filetime` via
    /// `std::fs::File::set_modified` (Rust 1.75+) — `TeslaUSB`'s
    /// MSRV is 1.86 per workspace `rust-version`.
    fn backdate(path: &Path, seconds: u64) {
        // Need write access to update mtime on Windows;
        // open-for-read fails with PermissionDenied here.
        let f = fs::OpenOptions::new().write(true).open(path).unwrap();
        let mtime = SystemTime::now() - Duration::from_secs(seconds);
        f.set_modified(mtime).unwrap();
    }

    #[tokio::test]
    async fn retention_hides_aged_recentclips_from_synth_layout() {
        // Two RecentClips files: one fresh, one older than the
        // default 1-hour threshold. The aged one must not appear
        // in the synth's file_extents (i.e., it claimed no
        // clusters), so Tesla's view of free space reflects the
        // hidden file's clusters as free.
        let dir = tempfile::tempdir().unwrap();
        write_file(
            &dir.path().join("RecentClips/2026-01-01_12-00-00-front.mp4"),
            &[0xAA; 4096],
        );
        write_file(
            &dir.path().join("RecentClips/2026-05-19_12-00-00-front.mp4"),
            &[0xBB; 4096],
        );
        // Backdate the first clip past the default 3600s window.
        backdate(
            &dir.path().join("RecentClips/2026-01-01_12-00-00-front.mp4"),
            7200,
        );

        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");

        let aged_present = backend.extents_snapshot().iter().any(|e| {
            e.backing_path
                .ends_with("RecentClips/2026-01-01_12-00-00-front.mp4")
        });
        let fresh_present = backend.extents_snapshot().iter().any(|e| {
            e.backing_path
                .ends_with("RecentClips/2026-05-19_12-00-00-front.mp4")
        });
        assert!(
            !aged_present,
            "Phase 4.3: aged RecentClips file must be hidden from the synth layout"
        );
        assert!(
            fresh_present,
            "Phase 4.3: fresh RecentClips file must remain visible"
        );
    }

    #[tokio::test]
    async fn retention_does_not_hide_sentry_or_saved_regardless_of_age() {
        // Sentry and Saved are outside the retention scope —
        // even ancient files must remain in the synth layout.
        let dir = tempfile::tempdir().unwrap();
        write_file(
            &dir.path().join("SentryClips/2020-01-01_00-00-00/event.mp4"),
            &[0xCC; 4096],
        );
        write_file(
            &dir.path().join("SavedClips/2020-01-01_00-00-00/event.mp4"),
            &[0xDD; 4096],
        );
        backdate(
            &dir.path().join("SentryClips/2020-01-01_00-00-00/event.mp4"),
            86_400 * 365,
        );
        backdate(
            &dir.path().join("SavedClips/2020-01-01_00-00-00/event.mp4"),
            86_400 * 365,
        );

        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");

        let sentry_present = backend
            .extents_snapshot()
            .iter()
            .any(|e| e.backing_path.to_string_lossy().contains("SentryClips"));
        let saved_present = backend
            .extents_snapshot()
            .iter()
            .any(|e| e.backing_path.to_string_lossy().contains("SavedClips"));
        assert!(sentry_present, "SentryClips must never be hidden by mtime");
        assert!(saved_present, "SavedClips must never be hidden by mtime");
    }

    #[tokio::test]
    async fn retention_zero_seconds_means_disabled_not_hide_all() {
        // `recentclips_hide_after_seconds = 0` is the operator's
        // "disable retention" knob — it must not flip into the
        // strictest possible hide.
        let dir = tempfile::tempdir().unwrap();
        write_file(
            &dir.path().join("RecentClips/2026-05-19_12-00-00-front.mp4"),
            &[0xEE; 4096],
        );
        // Backdate by a year. Under the literal `Duration::ZERO`
        // interpretation this would be hidden; under the
        // operator-facing `0 = disabled` interpretation it must
        // be shown.
        backdate(
            &dir.path().join("RecentClips/2026-05-19_12-00-00-front.mp4"),
            86_400 * 365,
        );

        let mut cfg = sample_cfg(dir.path().to_path_buf());
        cfg.retention.recentclips_hide_after_seconds = 0;
        let backend = SynthBackend::open(&cfg).expect("open ok");

        let present = backend.extents_snapshot().iter().any(|e| {
            e.backing_path
                .ends_with("RecentClips/2026-05-19_12-00-00-front.mp4")
        });
        assert!(
            present,
            "hide_after_seconds = 0 must mean retention disabled"
        );
    }

    #[tokio::test]
    async fn retention_frees_clusters_that_a_new_write_can_reuse() {
        // The user-visible payoff: a hidden file's clusters
        // become free, which means a new file Tesla writes can
        // legitimately receive those cluster numbers without
        // colliding with the (still-on-disk) backing file. We
        // can't easily exercise Tesla's allocator here, but we
        // can prove the invariant the allocator relies on: the
        // hidden file's first cluster is no longer claimed by
        // any extent in the synth's view.
        let dir = tempfile::tempdir().unwrap();
        write_file(
            &dir.path().join("RecentClips/hidden.mp4"),
            &[0xAA; 16384], // 4 clusters at 4 KiB
        );
        write_file(&dir.path().join("RecentClips/kept.mp4"), &[0xBB; 4096]);
        backdate(&dir.path().join("RecentClips/hidden.mp4"), 7200);

        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");

        // No extent in the layout names the hidden file.
        for extent in &backend.extents_snapshot() {
            assert!(
                !extent.backing_path.ends_with("hidden.mp4"),
                "hidden file's extent leaked into the layout: {:?}",
                extent.backing_path
            );
        }
        // The kept file is still represented.
        assert!(
            backend
                .extents_snapshot()
                .iter()
                .any(|e| e.backing_path.ends_with("kept.mp4"))
        );
    }

    #[tokio::test]
    async fn reload_retention_with_stricter_threshold_drops_aged_extents() {
        // Start with retention disabled, then reload with a 1 h
        // threshold and verify aged files lose their extents.
        let dir = tempfile::tempdir().unwrap();
        write_file(&dir.path().join("RecentClips/old.mp4"), &[0xAA; 4096]);
        write_file(&dir.path().join("RecentClips/fresh.mp4"), &[0xBB; 4096]);
        backdate(&dir.path().join("RecentClips/old.mp4"), 7200);

        let mut cfg = sample_cfg(dir.path().to_path_buf());
        cfg.retention.recentclips_hide_after_seconds = 0;
        let backend = SynthBackend::open(&cfg).expect("open ok");
        assert!(
            backend
                .extents_snapshot()
                .iter()
                .any(|e| e.backing_path.ends_with("old.mp4")),
            "old.mp4 must be visible before reload"
        );

        let stats = backend.reload_retention(3600).expect("reload ok");
        assert_eq!(stats.hide_after_seconds, 3600);
        assert!(
            stats.hidden >= 1,
            "expected at least 1 hidden, got {stats:?}"
        );

        assert!(
            !backend
                .extents_snapshot()
                .iter()
                .any(|e| e.backing_path.ends_with("old.mp4")),
            "old.mp4 must be evicted from extents after reload"
        );
        assert!(
            backend
                .extents_snapshot()
                .iter()
                .any(|e| e.backing_path.ends_with("fresh.mp4")),
            "fresh.mp4 must still be present"
        );
    }

    #[tokio::test]
    async fn reload_retention_with_zero_disables_filter() {
        // Open with a tight threshold (everything aged is hidden),
        // then reload with 0 and verify hidden=0.
        let dir = tempfile::tempdir().unwrap();
        write_file(&dir.path().join("RecentClips/clip.mp4"), &[0xAA; 4096]);
        backdate(&dir.path().join("RecentClips/clip.mp4"), 7200);

        let mut cfg = sample_cfg(dir.path().to_path_buf());
        cfg.retention.recentclips_hide_after_seconds = 3600;
        let backend = SynthBackend::open(&cfg).expect("open ok");

        let stats = backend.reload_retention(0).expect("reload ok");
        assert_eq!(stats.hide_after_seconds, 0);
        assert_eq!(
            stats.hidden, 0,
            "0 = disabled must produce zero hidden, got {stats:?}"
        );
    }

    #[tokio::test]
    async fn reload_retention_returns_walk_error_if_root_removed() {
        let dir = tempfile::tempdir().unwrap();
        write_file(&dir.path().join("RecentClips/x.mp4"), &[0xAA; 1024]);
        let cfg = sample_cfg(dir.path().to_path_buf());
        let backend = SynthBackend::open(&cfg).expect("open ok");
        let kept = dir; // keep alive
        // Force a walk failure by handing the backend a now-empty
        // path. Simulate this by deleting the backing root.
        std::fs::remove_dir_all(kept.path()).expect("rm root");
        let err = backend
            .reload_retention(3600)
            .expect_err("walk should fail with root gone");
        assert!(matches!(err, SynthBackendError::Walk(_)));
    }
}
