//! Phase 2.19 — `SynthBackend`: production [`BlockBackend`]
//! that serves a real FAT32 or `exFAT` view of an on-disk
//! backing tree.
//!
//! ## Pipeline
//!
//! 1. [`crate::backing_walker::walk`] (Phase 2.15) walks
//!    `cfg.backing_root` and produces a
//!    [`teslausb_core::fs::backing_tree::BackingTree`] describing
//!    every subdirectory + file the synthesizer must surface.
//! 2. [`teslausb_core::fs::fat32::layout::Fat32Layout::plan`]
//!    (Phase 2.17) or
//!    [`teslausb_core::fs::exfat::layout::ExfatLayout::plan`]
//!    (Phase 2.18) allocates cluster extents for every directory
//!    and file, materializes the directory cluster bytes, and
//!    records each file's `FilePlacement` (first cluster,
//!    size, backing path).
//! 3. [`teslausb_core::fs::fat32::synth::Fat32Synth::new`] /
//!    [`teslausb_core::fs::exfat::synth::ExfatSynth::new`] +
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
//! blocking file I/O would force `Fat32Synth::read` (a pure-CPU
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
//! medium. Phase 3+ will revisit this when a write path is
//! added.
//!
//! ## Write semantics
//!
//! As of Phase 3.5e, writes route through an FS-specific state
//! machine ([`Fat32WriteState`] / [`ExfatWriteState`]) selected
//! at [`SynthBackend::open`] time. Writes to the boot region or
//! reserved sectors are swallowed as metadata that the synth
//! itself owns; FAT-table writes update the in-memory FAT;
//! directory-cluster writes are decoded to discover new files
//! and route subsequent data-cluster writes to `.partial` files
//! on the backing tree, finalized atomically on `flush`. See
//! `backend::fat32_write` and `backend::exfat_write` for
//! details.

use std::collections::BTreeMap;
use std::fmt;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::PathBuf;
use std::sync::{Mutex, RwLock};
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
use teslausb_core::fs::fat32::geometry::Fat32Geometry;
use teslausb_core::fs::fat32::layout::{Fat32Layout, LayoutError as Fat32LayoutError};
use teslausb_core::fs::fat32::synth::{Fat32Synth, Fat32SynthError};
use teslausb_core::fs::geometry::{Geometry, GeometryError};

use crate::backend::dir_tree::{DirTreeError, DirTreeWriter};
use crate::backend::exfat_write::ExfatWriteState;
use crate::backend::fat32_write::Fat32WriteState;
use crate::backing_walker::{WalkError, walk};
use crate::config::{Config, FsType};
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
    /// Computing the FAT32 / `exFAT` geometry for
    /// `volume_size_gb * 1 GiB` failed.
    Geometry(GeometryError),
    /// The FAT32 layout planner rejected the tree.
    Fat32Layout(Fat32LayoutError),
    /// The FAT32 synthesizer rejected the geometry or label.
    Fat32Synth(Fat32SynthError),
    /// The `exFAT` layout planner rejected the tree.
    ExfatLayout(ExfatLayoutError),
    /// The `exFAT` synthesizer rejected the geometry, label,
    /// or layout.
    ExfatSynth(ExfatSynthError),
    /// The configured `volume_label` is too long for the
    /// `exFAT` 11-UTF-16-code-unit limit. (FAT32's 11-byte
    /// ASCII limit is already enforced by [`Config::load`]; this
    /// catches the rarer case where 11 ASCII bytes encode to >11
    /// UTF-16 code units, which cannot happen for ASCII but
    /// could happen if the validation rule ever loosens.)
    LabelTooLong {
        /// Number of UTF-16 code units the configured label
        /// encodes to.
        encoded_len: usize,
    },
    /// Constructing the [`DirTreeWriter`] (Phase 3.5c write
    /// path) failed — typically because `backing_root` is not a
    /// directory.
    DirTree(DirTreeError),
    /// Phase 4.4: the `file_extents` `RwLock` was poisoned by a
    /// panic in another thread holding the read or write guard.
    /// Recovery requires a daemon restart.
    LockPoisoned,
}

impl fmt::Display for SynthBackendError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Walk(err) => write!(f, "walking backing_root: {err}"),
            Self::Geometry(err) => write!(f, "computing volume geometry: {err}"),
            Self::Fat32Layout(err) => write!(f, "planning FAT32 layout: {err}"),
            Self::Fat32Synth(err) => write!(f, "building FAT32 synthesizer: {err}"),
            Self::ExfatLayout(err) => write!(f, "planning exFAT layout: {err}"),
            Self::ExfatSynth(err) => write!(f, "building exFAT synthesizer: {err}"),
            Self::LabelTooLong { encoded_len } => write!(
                f,
                "volume_label encodes to {encoded_len} UTF-16 code units; \
                 exFAT allows at most 11",
            ),
            Self::DirTree(err) => write!(f, "building DirTreeWriter: {err}"),
            Self::LockPoisoned => write!(f, "file_extents lock poisoned by panicked thread"),
        }
    }
}

impl std::error::Error for SynthBackendError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Walk(err) => Some(err),
            Self::Geometry(err) => Some(err),
            Self::Fat32Layout(err) => Some(err),
            Self::Fat32Synth(err) => Some(err),
            Self::ExfatLayout(err) => Some(err),
            Self::ExfatSynth(err) => Some(err),
            Self::LabelTooLong { .. } | Self::LockPoisoned => None,
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
    inner: SynthInner,
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
    /// Phase 3.5c (FAT32) / Phase 3.5e (exFAT) write-side state.
    write_state: Mutex<WriteState>,
}

/// FS-specific write-side state machine selected at
/// [`SynthBackend::open`] time. Both FAT32 (Phase 3.5c) and
/// `exFAT` (Phase 3.5e) variants own a state machine; there is
/// no read-only fallback because every supported filesystem now
/// has a write path.
#[derive(Debug)]
enum WriteState {
    Fat32(Fat32WriteState),
    Exfat(ExfatWriteState),
}

enum SynthInner {
    Fat32(Box<Fat32Synth>),
    Exfat(Box<ExfatSynth>),
}

impl fmt::Debug for SynthBackend {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let kind = match &self.inner {
            SynthInner::Fat32(_) => "Fat32",
            SynthInner::Exfat(_) => "Exfat",
        };
        f.debug_struct("SynthBackend")
            .field("fs_type", &kind)
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
        match cfg.fs_type {
            FsType::Fat32 => Self::open_fat32(cfg, volume_size, &tree, serial),
            FsType::Exfat => Self::open_exfat(cfg, volume_size, &tree, serial),
        }
    }

    fn open_fat32(
        cfg: &Config,
        volume_size: u64,
        tree: &BackingTree,
        serial: u32,
    ) -> Result<Self, SynthBackendError> {
        let geometry =
            Fat32Geometry::for_volume_size(volume_size).map_err(SynthBackendError::Geometry)?;
        let layout = Fat32Layout::plan(&geometry, cfg.volume_label.as_bytes(), tree)
            .map_err(SynthBackendError::Fat32Layout)?;
        let bytes_per_cluster = layout.bytes_per_cluster();
        let first_data_byte = layout.first_data_byte();
        let file_extents = build_file_extents(
            layout.files().iter().map(|f| ExtentInput {
                allocation: f.allocation,
                size_bytes: f.size_bytes,
                backing_path: f.backing_path.clone(),
            }),
            first_data_byte,
            bytes_per_cluster,
        );
        // Build pre-existing extents for the Phase 3.5c
        // write-side cluster map. Skip empty files (no clusters
        // allocated → nothing to seed); skip files we can't
        // relativize against the backing root (defensive — every
        // backing_path is rooted at cfg.backing_root by
        // construction).
        let mut pre_existing_extents: Vec<crate::backend::fat32_write::PreExistingExtent> =
            Vec::new();
        for f in layout.files() {
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
            pre_existing_extents.push(crate::backend::fat32_write::PreExistingExtent {
                first_cluster: f.allocation.first_cluster,
                cluster_count: f.allocation.cluster_count,
                first_byte_in_file: 0,
                file_size_bytes: f.size_bytes,
                relative_path: relative.to_path_buf(),
            });
        }
        let dir_tree =
            DirTreeWriter::new(cfg.backing_root.clone()).map_err(SynthBackendError::DirTree)?;
        let mut fat32_write =
            Fat32WriteState::new(geometry.clone(), dir_tree, &pre_existing_extents);
        if let Some(ref spill_dir) = cfg.spill_dir {
            fat32_write = fat32_write.with_disk_spill(
                spill_dir.clone(),
                crate::backend::pending_spill::DEFAULT_DISK_SPILL_BYTES,
            );
        }
        let synth = Fat32Synth::new(
            geometry,
            cfg.volume_label.as_bytes(),
            serial,
            Some(layout.free_cluster_count()),
            layout.next_free_cluster_hint(),
            layout.chains(),
        )
        .map_err(SynthBackendError::Fat32Synth)?;
        let synth = synth.with_data_source(Box::new(layout));
        Ok(Self {
            inner: SynthInner::Fat32(Box::new(synth)),
            file_extents: RwLock::new(file_extents),
            backing_root: cfg.backing_root.clone(),
            size: volume_size,
            write_state: Mutex::new(WriteState::Fat32(fat32_write)),
        })
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
        let bitmap_clusters = synth.bitmap_cluster_count();
        let upcase_first = synth.upcase_first_cluster();
        let upcase_clusters = synth.upcase_cluster_count();
        let bitmap_size_bytes = u64::from(bitmap_clusters) * u64::from(bytes_per_cluster);
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
            // exFAT cluster numbering starts at 2 just like FAT32 —
            // cluster N lives at cluster_heap_byte_offset + (N - 2) * bpc.
            first_data_byte_for_cluster_two(first_data_byte, bytes_per_cluster),
            bytes_per_cluster,
        );
        // Build pre-existing extents for the Phase 3.5e exFAT
        // write-side cluster map. Same shape and semantics as
        // the FAT32 path: skip empty files (no clusters) and
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
        let synth = synth
            .with_layout(layout)
            .map_err(SynthBackendError::ExfatSynth)?;
        let dir_tree =
            DirTreeWriter::new(cfg.backing_root.clone()).map_err(SynthBackendError::DirTree)?;
        let bitmap_first_cluster = synth.bitmap_first_cluster();
        let bitmap_cluster_count = synth.bitmap_cluster_count();
        let mut exfat_write = ExfatWriteState::new(geometry.clone(), dir_tree, &pre_existing_extents)
            .with_allocation_bitmap(bitmap_first_cluster, bitmap_cluster_count);
        if let Some(ref spill_dir) = cfg.spill_dir {
            exfat_write = exfat_write.with_disk_spill(
                spill_dir.clone(),
                crate::backend::pending_spill::DEFAULT_DISK_SPILL_BYTES,
            );
        }
        Ok(Self {
            inner: SynthInner::Exfat(Box::new(synth)),
            file_extents: RwLock::new(file_extents),
            backing_root: cfg.backing_root.clone(),
            size: volume_size,
            write_state: Mutex::new(WriteState::Exfat(exfat_write)),
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

    /// Whether this backend is serving a FAT32 view (as opposed
    /// to `exFAT`). Useful for startup logging.
    #[must_use]
    pub fn is_fat32(&self) -> bool {
        matches!(self.inner, SynthInner::Fat32(_))
    }

    /// Snapshot the current extent table. Test-only helper: the
    /// production read path acquires the lock per-call.
    #[cfg(test)]
    fn extents_snapshot(&self) -> Vec<FileExtent> {
        self.file_extents
            .read()
            .map_or_else(|p| p.into_inner().clone(), |g| g.clone())
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
        let mut guard = self
            .file_extents
            .write()
            .map_err(|_| SynthBackendError::LockPoisoned)?;
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

    fn read_sync(&self, offset: u64, buf: &mut [u8]) -> BackendResult<()> {
        match &self.inner {
            SynthInner::Fat32(s) => s.read(offset, buf).map_err(|e| {
                BackendError::Io(std::io::Error::other(format!("fat32 synth: {e}")))
            })?,
            SynthInner::Exfat(s) => s.read(offset, buf).map_err(|e| {
                BackendError::Io(std::io::Error::other(format!("exfat synth: {e}")))
            })?,
        }
        // Overlay file content for every extent that overlaps
        // the requested range. Extents are sorted by first_byte
        // so a binary-search lower bound would let us skip the
        // ones strictly below the read; for typical NBD reads
        // (≤ 256 KiB) the linear scan over hundreds-to-thousands
        // of extents is dominated by the file I/O cost, so leave
        // the optimisation to a future increment if profiling
        // shows it matters.
        let read_end = offset.saturating_add(buf.len() as u64);
        let extents = self
            .file_extents
            .read()
            .map_err(|_| BackendError::Io(std::io::Error::other("file_extents lock poisoned")))?;
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
        let guard = self
            .write_state
            .lock()
            .map_err(|_| BackendError::Io(std::io::Error::other("write lock poisoned")))?;
        match &*guard {
            WriteState::Fat32(state) => state.overlay_read(offset, buf),
            WriteState::Exfat(state) => state.overlay_read(offset, buf),
        }
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
        let mut guard = self
            .write_state
            .lock()
            .map_err(|_| BackendError::Io(std::io::Error::other("write lock poisoned")))?;
        match &mut *guard {
            WriteState::Fat32(state) => {
                state.apply_write(offset, buf)?;
                if flags.contains(WriteFlags::FUA) {
                    state.flush()?;
                }
            }
            WriteState::Exfat(state) => {
                state.apply_write(offset, buf)?;
                if flags.contains(WriteFlags::FUA) {
                    state.flush()?;
                }
            }
        }
        Ok(())
    }

    async fn flush(&self) -> BackendResult<()> {
        let mut guard = self
            .write_state
            .lock()
            .map_err(|_| BackendError::Io(std::io::Error::other("write lock poisoned")))?;
        match &mut *guard {
            WriteState::Fat32(state) => state.flush()?,
            WriteState::Exfat(state) => state.flush()?,
        }
        Ok(())
    }
}

/// Adapter input shared between the FAT32 and `exFAT` paths.
/// Both layouts expose the same three fields under different
/// types, so [`build_file_extents`] takes this normalized form
/// to share the index-construction logic.
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

/// FAT32's `first_data_byte` from
/// [`Fat32Layout::first_data_byte`] is already the byte offset
/// of cluster 2. `exFAT`'s
/// [`ExfatLayout::cluster_heap_byte_offset`] is the byte offset
/// of cluster 2 as well (the cluster heap is indexed from 2).
/// This helper exists to make the alignment explicit at the
/// call sites and to give a single place to revisit if either
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
    let mut file = File::open(&extent.backing_path)?;
    file.seek(SeekFrom::Start(file_offset_start))?;
    file.read_exact(target)?;
    Ok(())
}

/// Derive a stable 32-bit volume serial from the configured
/// volume label using FNV-1a.
///
/// Tesla treats the serial as a stable identifier across boots
/// (it caches mount state per-serial), so deriving it from a
/// config value that itself rarely changes keeps the serial
/// stable without persisting state. Zero is forbidden by
/// `mkfs.vfat` convention; the fallback constant is arbitrary.
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

    fn sample_cfg(backing_root: PathBuf, fs_type: FsType) -> Config {
        Config {
            backing_root,
            volume_size_gb: 4,
            volume_label: "TESTLABEL".to_string(),
            cluster_size: None,
            fs_type,
            retention: crate::config::RetentionConfig::default(),
            nbd: crate::config::NbdConfig::default(),
            spill_dir: None,
        }
    }

    #[tokio::test]
    async fn fat32_open_walks_backing_root_and_size_matches_config() {
        let dir = tempfile::tempdir().unwrap();
        write_file(&dir.path().join("alpha.mp4"), &[0x11; 8192]);
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
        let backend = SynthBackend::open(&cfg).expect("open ok");
        assert!(backend.is_fat32());
        assert_eq!(backend.size(), 4 * BYTES_PER_GIB);
        assert!(
            backend.file_count() >= 1,
            "expected at least one file extent, got {}",
            backend.file_count()
        );
    }

    #[tokio::test]
    async fn fat32_read_at_offset_zero_returns_boot_sector_signature() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
        let backend = SynthBackend::open(&cfg).expect("open ok");
        let mut sector = [0u8; 512];
        backend.read(0, &mut sector).await.expect("read ok");
        // Boot sector signature at 0x1FE/0x1FF is 0x55 0xAA per
        // fatgen103 §3. This is the cheapest invariant that
        // proves we wired up the FAT32 synthesizer correctly.
        assert_eq!(sector[510], 0x55, "boot sig byte 0: {:#x}", sector[510]);
        assert_eq!(sector[511], 0xAA, "boot sig byte 1: {:#x}", sector[511]);
    }

    #[tokio::test]
    async fn fat32_read_of_file_cluster_returns_backing_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let payload: Vec<u8> = (0..4096u32).map(|i| (i % 251) as u8).collect();
        write_file(&dir.path().join("data.bin"), &payload);
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
        let backend = SynthBackend::open(&cfg).expect("open ok");
        // Read the first file extent's first cluster from the
        // volume. The single backing file lives at a known
        // first_cluster which we can recover from the backend's
        // own extent table.
        let extent_first = backend
            .extents_snapshot()
            .into_iter()
            .next()
            .expect("at least one extent")
            .first_byte;
        let mut buf = vec![0u8; payload.len()];
        backend.read(extent_first, &mut buf).await.expect("read ok");
        assert_eq!(buf, payload, "file content overlay mismatch");
    }

    #[tokio::test]
    async fn fat32_read_past_end_of_file_returns_zero_padding() {
        let dir = tempfile::tempdir().unwrap();
        // 100 bytes — well below a single cluster (≥ 512 bytes
        // on the smallest FAT32 volume).
        let payload = vec![0xAAu8; 100];
        write_file(&dir.path().join("short.bin"), &payload);
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
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
    async fn fat32_write_to_boot_sector_is_accepted_as_metadata() {
        // Phase 3.5c: FAT32 writes are now wired through
        // Fat32WriteState. Writes to metadata regions (boot
        // sector, FSInfo, reserved) are swallowed silently;
        // they don't fail. The actual file content survives a
        // round-trip via tests in `fat32_write::tests` and
        // tests/synth_write_integration.rs.
        let dir = tempfile::tempdir().unwrap();
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
        let backend = SynthBackend::open(&cfg).expect("open ok");
        backend
            .write(0, &[0u8; 16], WriteFlags::NONE)
            .await
            .expect("metadata write should be accepted");
    }

    #[tokio::test]
    async fn exfat_write_to_boot_sector_is_accepted_as_metadata() {
        // Phase 3.5e shipped exFAT write support. Writes to the
        // boot region are swallowed as metadata (the synth is the
        // source of truth for boot bytes); writes to data
        // clusters / FAT / dirs are exercised by
        // exfat_write::tests.
        let dir = tempfile::tempdir().unwrap();
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Exfat);
        let backend = SynthBackend::open(&cfg).expect("open ok");
        backend
            .write(0, &[0u8; 16], WriteFlags::NONE)
            .await
            .expect("metadata write should be accepted");
    }

    #[tokio::test]
    async fn flush_succeeds_with_no_state() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
        let backend = SynthBackend::open(&cfg).expect("open ok");
        backend.flush().await.expect("flush ok");
    }

    #[tokio::test]
    async fn read_out_of_bounds_returns_out_of_bounds() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
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
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Exfat);
        let backend = SynthBackend::open(&cfg).expect("open ok");
        assert!(!backend.is_fat32());
        assert_eq!(backend.size(), 4 * BYTES_PER_GIB);
    }

    #[tokio::test]
    async fn exfat_read_at_offset_zero_returns_main_boot_sector_signature() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Exfat);
        let backend = SynthBackend::open(&cfg).expect("open ok");
        let mut sector = [0u8; 512];
        backend.read(0, &mut sector).await.expect("read ok");
        // exFAT main boot sector also ends in 0x55 0xAA per
        // exFAT spec §3.1.
        assert_eq!(sector[510], 0x55);
        assert_eq!(sector[511], 0xAA);
    }

    #[tokio::test]
    async fn exfat_read_of_file_cluster_returns_backing_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let payload: Vec<u8> = (0..2048u32).map(|i| ((i * 7) % 251) as u8).collect();
        write_file(&dir.path().join("recording.h265"), &payload);
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Exfat);
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
        let cfg = sample_cfg(missing, FsType::Fat32);
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

        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
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

        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
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

        let mut cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
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
    async fn retention_hides_aged_recentclips_in_exfat_layout_too() {
        // Symmetric assertion for the exFAT path — both layout
        // planners must consume the filtered tree.
        let dir = tempfile::tempdir().unwrap();
        write_file(&dir.path().join("RecentClips/old.mp4"), &[0xAA; 4096]);
        write_file(&dir.path().join("RecentClips/new.mp4"), &[0xBB; 4096]);
        backdate(&dir.path().join("RecentClips/old.mp4"), 7200);

        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Exfat);
        let backend = SynthBackend::open(&cfg).expect("open ok");

        let old_present = backend
            .extents_snapshot()
            .iter()
            .any(|e| e.backing_path.ends_with("RecentClips/old.mp4"));
        let new_present = backend
            .extents_snapshot()
            .iter()
            .any(|e| e.backing_path.ends_with("RecentClips/new.mp4"));
        assert!(!old_present, "exFAT layout must also honor retention");
        assert!(new_present);
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

        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
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

        let mut cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
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

        let mut cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
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
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
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
