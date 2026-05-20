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
//! ## Write semantics (Phase 2.19)
//!
//! Writes return [`teslausb_core::backend::BackendError::Io`]
//! with [`std::io::ErrorKind::PermissionDenied`]. The Phase 2
//! contract is read-only: the synth has no write path, and the
//! NBD handshake advertises FUA + FLUSH only because the
//! transmission loop drives them through to the backend.
//! Phase 3 will add write support and either gate it behind a
//! config flag or advertise the export as read-only via
//! `NBD_FLAG_READ_ONLY`.

use std::collections::BTreeMap;
use std::fmt;
use std::fs::File;
use std::io::{ErrorKind, Read, Seek, SeekFrom};
use std::path::PathBuf;

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

use crate::backing_walker::{WalkError, walk};
use crate::config::{Config, FsType};

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
            Self::LabelTooLong { .. } => None,
        }
    }
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
#[derive(Debug)]
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
    file_extents: Vec<FileExtent>,
    size: u64,
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
            .field("file_count", &self.file_extents.len())
            .finish()
    }
}

impl SynthBackend {
    /// Build a `SynthBackend` from a parsed [`Config`].
    ///
    /// Walks `cfg.backing_root` once, plans the layout for the
    /// requested `cfg.fs_type`, and assembles the synth +
    /// materializer overlay.
    ///
    /// # Errors
    ///
    /// Returns [`SynthBackendError`] if any pipeline step fails.
    /// All variants preserve the original error as `source()`.
    pub fn open(cfg: &Config) -> Result<Self, SynthBackendError> {
        let volume_size = u64::from(cfg.volume_size_gb) * BYTES_PER_GIB;
        let tree = walk(&cfg.backing_root).map_err(SynthBackendError::Walk)?;
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
        let layout = Fat32Layout::plan(&geometry, tree).map_err(SynthBackendError::Fat32Layout)?;
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
        let synth = Fat32Synth::new(
            geometry,
            cfg.volume_label.as_bytes(),
            serial,
            None,
            None,
            layout.chains(),
        )
        .map_err(SynthBackendError::Fat32Synth)?;
        let synth = synth.with_data_source(Box::new(layout));
        Ok(Self {
            inner: SynthInner::Fat32(Box::new(synth)),
            file_extents,
            size: volume_size,
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
        let upcase_first = synth.upcase_first_cluster();
        let bitmap_clusters = upcase_first.saturating_sub(bitmap_first);
        let bitmap_size_bytes = u64::from(bitmap_clusters) * u64::from(bytes_per_cluster);
        let upcase_size_bytes = UPCASE_TABLE_SIZE_BYTES as u64;
        let upcase_clusters = u32::try_from(
            upcase_size_bytes.div_ceil(u64::from(bytes_per_cluster)),
        )
        .map_err(|_| {
            SynthBackendError::ExfatLayout(ExfatLayoutError::BadMetadata {
                reason: "upcase cluster count exceeds u32::MAX",
            })
        })?;
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
        let synth = synth
            .with_layout(layout)
            .map_err(SynthBackendError::ExfatSynth)?;
        Ok(Self {
            inner: SynthInner::Exfat(Box::new(synth)),
            file_extents,
            size: volume_size,
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
        self.file_extents.len()
    }

    /// Whether this backend is serving a FAT32 view (as opposed
    /// to `exFAT`). Useful for startup logging.
    #[must_use]
    pub fn is_fat32(&self) -> bool {
        matches!(self.inner, SynthInner::Fat32(_))
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
        for extent in &self.file_extents {
            if extent.end_byte <= offset {
                continue;
            }
            if extent.first_byte >= read_end {
                break;
            }
            overlay_file_extent(extent, offset, buf)?;
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

    async fn write(&self, offset: u64, buf: &[u8], _flags: WriteFlags) -> BackendResult<()> {
        check_bounds(offset, buf.len(), self.size)?;
        Err(BackendError::Io(std::io::Error::new(
            ErrorKind::PermissionDenied,
            "SynthBackend is read-only (Phase 2); writes are rejected",
        )))
    }

    async fn flush(&self) -> BackendResult<()> {
        // SynthBackend has no persistent state to flush — every
        // read is synthesised on the fly from the immutable
        // layout + backing files.
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
            .file_extents
            .first()
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
        let extent = backend.file_extents.first().expect("at least one extent");
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
    async fn write_returns_permission_denied() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
        let backend = SynthBackend::open(&cfg).expect("open ok");
        let err = backend
            .write(0, &[0u8; 16], WriteFlags::NONE)
            .await
            .expect_err("write should be rejected");
        match err {
            BackendError::Io(io_err) => {
                assert_eq!(io_err.kind(), ErrorKind::PermissionDenied);
            }
            other => panic!("expected Io(PermissionDenied), got {other:?}"),
        }
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
            .file_extents
            .first()
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
}
