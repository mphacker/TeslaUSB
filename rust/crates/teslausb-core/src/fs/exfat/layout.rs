//! Phase 2.18 — exFAT directory + file layout planner.
//!
//! Parallel to [`crate::fs::fat32::layout`] (Phase 2.17) but
//! for exFAT: walks a [`BackingTree`] (Phase 2.15), allocates
//! cluster extents via [`ClusterAllocator`] (Phase 2.16), and
//! produces:
//!
//! 1. **A complete root directory cluster** — the three
//!    mandatory special entries (Allocation Bitmap, `UpCase`
//!    Table, Volume Label) followed by file entry sets for the
//!    backing tree's top-level children.
//! 2. **Subdirectory cluster bytes** keyed by cluster number
//!    (single contiguous extent per subdir; spans multiple
//!    clusters if needed).
//! 3. **File placements** — each backing file's allocation,
//!    on-disk size, and backing path, for the Phase-2.19
//!    materializer to consume.
//! 4. **Allocated extents** — every contiguous extent the
//!    planner reserved beyond the bitmap/upcase metadata, so
//!    [`crate::fs::exfat::synth::ExfatSynth::with_layout`] can
//!    mark them in the allocation bitmap.
//!
//! ## `NoFatChain` everywhere
//!
//! Every allocation the planner makes is contiguous (it uses
//! [`ClusterAllocator`] which only hands out contiguous
//! extents), so every directory + file is laid out with the
//! exFAT `NoFatChain` flag set in its stream extension entry.
//! Per exFAT spec §6.3.4.2 the FAT entries for `NoFatChain`
//! extents may stay free (zero), which is exactly what the
//! synth's existing FAT region produces for unmarked clusters
//! — no FAT updates are needed for layout extents.
//!
//! ## Single-cluster root
//!
//! The planner currently restricts the root directory to a
//! single cluster. The three mandatory entries take 96 bytes,
//! leaving `(bytes_per_cluster - 96)` bytes for the top-level
//! children. A 4 KiB cluster fits ~7 file entry sets which is
//! adequate for the `TeslaUSB` use case (~5 top-level children:
//! `RecentClips`, `SavedClips`, `SentryClips`, `ArchivedClips`,
//! plus
//! the occasional config file). Multi-cluster root chains
//! require populating the FAT for the root chain and are
//! left for a later phase.
//!
//! ## What this module does NOT do
//!
//! * **No file content materialization.** The
//!   [`DataClusterSource`] impl serves subdirectory cluster
//!   bytes only; file clusters return zeros. Phase 2.19's
//!   `teslafat::DirTreeMaterializer` wraps the layout and
//!   opens the backing files on demand.
//! * **No FAT chain construction.** The planner picks
//!   `NoFatChain` for every extent so no FAT updates are
//!   required.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::fs::backing_tree::{BackingDir, BackingFile, BackingTree};
use crate::fs::cluster_layout::{AllocError, Allocation, ClusterAllocator};
use crate::fs::data_cluster_source::DataClusterSource;
use crate::fs::exfat::allocation_bitmap::AllocationBitmapError;
use crate::fs::exfat::directory::{
    DIRECTORY_ENTRY_SIZE_BYTES, DirectoryError, FileAttributes, FileEntrySetParams, FileTimestamps,
    MAX_FILE_NAME_CODE_UNITS, MAX_VOLUME_LABEL_CODE_UNITS, NAME_CODE_UNITS_PER_NAME_ENTRY,
    encode_allocation_bitmap_entry, encode_file_entry_set, encode_upcase_table_entry,
    encode_volume_label_entry,
};
use crate::fs::exfat::geometry::{ExfatGeometry, FIRST_CLUSTER_NUMBER};
use crate::fs::exfat::upcase_table::UpcaseTable;
use crate::fs::geometry::{Geometry, SECTOR_SIZE_BYTES};

/// All metadata the planner needs to lay out the root cluster
/// and to know where the free cluster heap begins.
///
/// Built from an [`ExfatSynth`]'s public accessors (see the
/// `phase_2_18` integration tests for a worked example) or
/// assembled manually for tests.
///
/// [`ExfatSynth`]: crate::fs::exfat::synth::ExfatSynth
#[derive(Debug, Clone)]
pub struct LayoutMetadata<'a> {
    /// First cluster of the allocation bitmap stream.
    pub bitmap_first_cluster: u32,
    /// Allocation bitmap stream size in bytes.
    pub bitmap_size_bytes: u64,
    /// First cluster of the upcase table stream.
    pub upcase_first_cluster: u32,
    /// Upcase table size in bytes.
    pub upcase_size_bytes: u64,
    /// Cached upcase table checksum, used by both the root
    /// `UpCase` entry and the per-file `NameHash` calculation.
    pub upcase: &'a UpcaseTable,
    /// UTF-16 volume label (0..=11 code units).
    pub volume_label_utf16: &'a [u16],
    /// First cluster the planner is allowed to allocate
    /// (must be `>= upcase_first_cluster + ceil(upcase_size_bytes / bytes_per_cluster)`).
    pub first_free_cluster: u32,
}

/// Where a single backing file ended up in the synthesized
/// volume.
#[derive(Debug, Clone)]
pub struct FilePlacement {
    /// Cluster chain assigned to this file. Empty files get
    /// [`Allocation::EMPTY`] and consume no clusters.
    pub allocation: Allocation,
    /// On-disk file size in bytes, as captured by the walker.
    /// Stamped into the file's stream extension entry and
    /// consulted by the materializer to clamp reads.
    pub size_bytes: u64,
    /// Absolute backing path. The materializer opens this
    /// path to serve file-content cluster reads.
    pub backing_path: PathBuf,
}

/// Where a single directory ended up in the synthesized
/// volume. Used by the write-side resolver to seed every
/// directory's contiguous cluster chain at startup — without
/// this, only the root directory is known and Tesla's writes
/// into subdirectory clusters (every `TeslaCam` clip lives in a
/// subdirectory) are silently dropped after a remount.
#[derive(Debug, Clone)]
pub struct DirPlacement {
    /// Directory path relative to the backing root (the root
    /// directory itself is not included in the placement list;
    /// see [`ExfatLayout::root_directory_bytes`]).
    pub relative_path: PathBuf,
    /// First cluster of the directory's contiguous allocation.
    pub first_cluster: u32,
    /// Number of contiguous clusters the directory occupies.
    pub cluster_count: u32,
}

/// Errors that can prevent an [`ExfatLayout`] from being
/// built.
#[derive(Debug, PartialEq, Eq)]
pub enum LayoutError {
    /// The cluster allocator rejected an allocation — typically
    /// because the volume isn't large enough to hold the tree.
    Alloc(AllocError),
    /// One of the directory-entry encoders rejected the input
    /// (empty name, name too long, label too long, …).
    Directory(DirectoryError),
    /// The root directory's mandatory three entries plus the
    /// top-level children's entry sets don't fit in a single
    /// cluster. Multi-cluster root chains are not yet
    /// supported.
    RootOverflow {
        /// Bytes the root entries would consume.
        needed_bytes: u64,
        /// Bytes available in a single cluster.
        cluster_bytes: u64,
    },
    /// A backing file is larger than `u32::MAX * bytes_per_cluster`
    /// or otherwise exceeds the planner's per-file cluster cap
    /// of `u32::MAX`. exFAT itself allows files up to `2^64 - 1`
    /// bytes but `ClusterAllocator::allocate` saturates at
    /// `u32::MAX` clusters, which is the practical ceiling.
    FileTooLarge {
        /// The offending backing path.
        path: PathBuf,
        /// The file's actual size in bytes.
        size_bytes: u64,
    },
    /// Caller passed metadata that violates the planner's
    /// preconditions (e.g. `first_free_cluster` overlaps the
    /// upcase stream).
    BadMetadata {
        /// A short, human-readable explanation.
        reason: &'static str,
    },
}

impl core::fmt::Display for LayoutError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::Alloc(err) => write!(f, "exFAT cluster allocator failed during planning: {err}"),
            Self::Directory(err) => write!(f, "exFAT directory entry construction failed: {err}"),
            Self::RootOverflow {
                needed_bytes,
                cluster_bytes,
            } => write!(
                f,
                "exFAT root directory needs {needed_bytes} bytes but a single cluster only \
                 holds {cluster_bytes}; multi-cluster root chains are not yet supported",
            ),
            Self::FileTooLarge { path, size_bytes } => write!(
                f,
                "backing file {} is {size_bytes} bytes which exceeds the planner's per-file \
                 cluster cap",
                path.display(),
            ),
            Self::BadMetadata { reason } => write!(f, "exFAT layout metadata invalid: {reason}"),
        }
    }
}

impl std::error::Error for LayoutError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Alloc(err) => Some(err),
            Self::Directory(err) => Some(err),
            Self::RootOverflow { .. } | Self::FileTooLarge { .. } | Self::BadMetadata { .. } => {
                None
            }
        }
    }
}

/// Complete read-side layout for an exFAT volume backed by a
/// [`BackingTree`]. See the module docs for the full pipeline.
#[derive(Debug)]
pub struct ExfatLayout {
    bytes_per_cluster: u32,
    cluster_heap_byte_offset: u64,
    root_directory_cluster: u32,
    root_directory_bytes: Vec<u8>,
    subdir_clusters: BTreeMap<u32, Vec<u8>>,
    file_placements: Vec<FilePlacement>,
    dir_placements: Vec<DirPlacement>,
    allocated_extents: Vec<Allocation>,
}

impl ExfatLayout {
    /// Plan the cluster layout for `tree` on an exFAT volume
    /// with the given `geometry` and pre-allocated metadata.
    ///
    /// # Errors
    ///
    /// * [`LayoutError::Alloc`] if the volume is too small to
    ///   hold the tree.
    /// * [`LayoutError::Directory`] if a name fails exFAT's
    ///   length / emptiness rules.
    /// * [`LayoutError::RootOverflow`] if the top-level
    ///   children don't fit alongside the three mandatory
    ///   entries in one root cluster.
    /// * [`LayoutError::FileTooLarge`] if a backing file would
    ///   require more than `u32::MAX` clusters.
    /// * [`LayoutError::BadMetadata`] if `metadata` is
    ///   inconsistent (e.g. `first_free_cluster` overlaps the
    ///   upcase stream or the volume label exceeds 11 code
    ///   units).
    pub fn plan(
        geometry: &ExfatGeometry,
        metadata: &LayoutMetadata<'_>,
        tree: &BackingTree,
    ) -> Result<Self, LayoutError> {
        if metadata.volume_label_utf16.len() > MAX_VOLUME_LABEL_CODE_UNITS {
            return Err(LayoutError::Directory(DirectoryError::LabelTooLong {
                max_code_units: MAX_VOLUME_LABEL_CODE_UNITS,
                found: metadata.volume_label_utf16.len(),
            }));
        }

        let bytes_per_cluster = geometry.bytes_per_cluster();
        if bytes_per_cluster == 0 {
            return Err(LayoutError::BadMetadata {
                reason: "geometry reports zero bytes per cluster",
            });
        }
        let cluster_heap_byte_offset = u64::from(geometry.cluster_heap_offset_sectors())
            .saturating_mul(u64::from(SECTOR_SIZE_BYTES));

        let upcase_cluster_count = u32::try_from(
            metadata
                .upcase_size_bytes
                .div_ceil(u64::from(bytes_per_cluster)),
        )
        .unwrap_or(u32::MAX);
        let upcase_end_exclusive = metadata
            .upcase_first_cluster
            .checked_add(upcase_cluster_count)
            .ok_or(LayoutError::BadMetadata {
                reason: "upcase stream end overflows u32",
            })?;
        if metadata.first_free_cluster < upcase_end_exclusive {
            return Err(LayoutError::BadMetadata {
                reason: "first_free_cluster overlaps the upcase stream",
            });
        }

        let max_cluster_exclusive = FIRST_CLUSTER_NUMBER.saturating_add(geometry.cluster_count());
        if metadata.first_free_cluster >= max_cluster_exclusive {
            return Err(LayoutError::BadMetadata {
                reason: "first_free_cluster is at or past the cluster heap end",
            });
        }
        let mut allocator = ClusterAllocator::new(
            bytes_per_cluster,
            metadata.first_free_cluster,
            max_cluster_exclusive,
        )
        .map_err(LayoutError::Alloc)?;

        let mut sink = LayoutSink::default();

        let mut top_level_children: Vec<ChildEntry> =
            Vec::with_capacity(tree.root.subdirs.len() + tree.root.files.len());

        for sub in &tree.root.subdirs {
            let alloc = plan_dir(
                sub,
                &PathBuf::from(sub.name.as_str()),
                &mut allocator,
                &mut sink,
                metadata.upcase,
            )?;
            top_level_children.push(ChildEntry {
                name: sub.name.as_str(),
                attrs: FileAttributes {
                    directory: true,
                    ..FileAttributes::default()
                },
                first_cluster: alloc.first_cluster,
                data_length: u64::from(alloc.cluster_count)
                    .saturating_mul(u64::from(bytes_per_cluster)),
                no_fat_chain: !alloc.is_empty(),
            });
        }

        for f in &tree.root.files {
            let placement = plan_file(f, &mut allocator, &mut sink.allocated_extents)?;
            top_level_children.push(ChildEntry {
                name: f.name.as_str(),
                attrs: FileAttributes {
                    archive: true,
                    ..FileAttributes::default()
                },
                first_cluster: placement.allocation.first_cluster,
                data_length: placement.size_bytes,
                no_fat_chain: !placement.allocation.is_empty(),
            });
            sink.file_placements.push(placement);
        }

        let root_directory_bytes =
            render_root_directory(geometry, metadata, &top_level_children, bytes_per_cluster)?;

        let LayoutSink {
            subdir_clusters,
            file_placements,
            dir_placements,
            allocated_extents,
        } = sink;

        Ok(Self {
            bytes_per_cluster,
            cluster_heap_byte_offset,
            root_directory_cluster: geometry.first_root_directory_cluster(),
            root_directory_bytes,
            subdir_clusters,
            file_placements,
            dir_placements,
            allocated_extents,
        })
    }

    /// Cluster size this layout was planned against.
    #[must_use]
    pub fn bytes_per_cluster(&self) -> u32 {
        self.bytes_per_cluster
    }

    /// Byte offset of the cluster heap (cluster 2) within the
    /// synthesized volume.
    #[must_use]
    pub fn cluster_heap_byte_offset(&self) -> u64 {
        self.cluster_heap_byte_offset
    }

    /// Complete bytes of the root directory cluster — the three
    /// mandatory entries plus the top-level children's entry
    /// sets, zero-padded to one cluster.
    #[must_use]
    pub fn root_directory_bytes(&self) -> &[u8] {
        &self.root_directory_bytes
    }

    /// Materialized subdirectory cluster bytes keyed by cluster
    /// number. Each value is exactly `bytes_per_cluster` long.
    #[must_use]
    pub fn subdir_clusters(&self) -> &BTreeMap<u32, Vec<u8>> {
        &self.subdir_clusters
    }

    /// All file placements in DFS-pre-order.
    #[must_use]
    pub fn file_placements(&self) -> &[FilePlacement] {
        &self.file_placements
    }

    /// All subdirectory placements in DFS-pre-order (the root
    /// directory is excluded — it lives at
    /// [`ExfatGeometry::first_root_directory_cluster`] with bytes
    /// from [`Self::root_directory_bytes`]).
    #[must_use]
    pub fn dir_placements(&self) -> &[DirPlacement] {
        &self.dir_placements
    }

    /// All contiguous extents the planner allocated for
    /// subdirectories and files. The synth marks each one in
    /// the allocation bitmap; the FAT region is unchanged
    /// (`NoFatChain`).
    #[must_use]
    pub fn allocated_extents(&self) -> &[Allocation] {
        &self.allocated_extents
    }
}

impl DataClusterSource for ExfatLayout {
    fn read_cluster_bytes(&self, cluster: u32, byte_in_cluster: usize, out: &mut [u8]) {
        // The root directory cluster is held separately from
        // `subdir_clusters`; serve it here too so the write-side
        // resolver can materialize the root buffer (and its
        // pre-existing-child baseline) from this single source.
        if cluster == self.root_directory_cluster {
            copy_cluster_bytes(&self.root_directory_bytes, byte_in_cluster, out);
        } else if let Some(bytes) = self.subdir_clusters.get(&cluster) {
            copy_cluster_bytes(bytes, byte_in_cluster, out);
        } else {
            out.fill(0);
        }
    }
}

/// Copy `bytes` starting at `byte_in_cluster` into `out`,
/// zero-filling any tail past the end of `bytes`. Shared by the
/// root- and subdirectory-cluster arms of
/// [`ExfatLayout::read_cluster_bytes`].
fn copy_cluster_bytes(bytes: &[u8], byte_in_cluster: usize, out: &mut [u8]) {
    let cluster_len = bytes.len();
    let start = byte_in_cluster.min(cluster_len);
    let available = cluster_len.saturating_sub(start);
    let take = available.min(out.len());
    if take > 0 {
        let src_end = start.saturating_add(take);
        if let (Some(src), Some(dst)) = (bytes.get(start..src_end), out.get_mut(..take)) {
            dst.copy_from_slice(src);
        }
    }
    if let Some(tail) = out.get_mut(take..) {
        tail.fill(0);
    }
}

// Mapping of AllocationBitmapError into LayoutError isn't used
// at plan-time (the planner doesn't touch the bitmap), but
// ExfatSynth::with_layout needs to convert. Exposed for
// completeness.
impl From<AllocationBitmapError> for LayoutError {
    fn from(_err: AllocationBitmapError) -> Self {
        Self::BadMetadata {
            reason: "allocation bitmap rejected a layout extent",
        }
    }
}

// ── Planning core ─────────────────────────────────────────────────────

struct ChildEntry<'a> {
    name: &'a str,
    attrs: FileAttributes,
    first_cluster: u32,
    data_length: u64,
    no_fat_chain: bool,
}

/// Mutable accumulators threaded through the recursive layout
/// planner. They always grow together as the single "plan
/// output", so bundling them keeps [`plan_dir`] within the
/// argument budget and names the clump.
#[derive(Default)]
struct LayoutSink {
    subdir_clusters: BTreeMap<u32, Vec<u8>>,
    file_placements: Vec<FilePlacement>,
    dir_placements: Vec<DirPlacement>,
    allocated_extents: Vec<Allocation>,
}

fn plan_dir(
    dir: &BackingDir,
    relative_path: &Path,
    allocator: &mut ClusterAllocator,
    sink: &mut LayoutSink,
    upcase: &UpcaseTable,
) -> Result<Allocation, LayoutError> {
    let bytes_per_cluster = allocator.bytes_per_cluster();
    let dir_bytes = directory_entry_bytes(dir, false)?;
    // At minimum, an empty directory still gets one cluster
    // (so it has a first_cluster to point the parent's entry
    // at, and so the runtime sees a valid directory).
    let alloc_request = dir_bytes.max(1);
    let dir_alloc = allocator
        .allocate(alloc_request)
        .map_err(LayoutError::Alloc)?;
    sink.allocated_extents.push(dir_alloc);
    sink.dir_placements.push(DirPlacement {
        relative_path: relative_path.to_path_buf(),
        first_cluster: dir_alloc.first_cluster,
        cluster_count: dir_alloc.cluster_count,
    });

    // Recurse first so children's cluster numbers are stable.
    let mut child_entries: Vec<ChildEntry<'_>> =
        Vec::with_capacity(dir.subdirs.len() + dir.files.len());

    let mut sub_allocs: Vec<Allocation> = Vec::with_capacity(dir.subdirs.len());
    for sub in &dir.subdirs {
        let sub_alloc = plan_dir(
            sub,
            &relative_path.join(sub.name.as_str()),
            allocator,
            sink,
            upcase,
        )?;
        sub_allocs.push(sub_alloc);
    }
    let mut file_allocs: Vec<(Allocation, u64)> = Vec::with_capacity(dir.files.len());
    for f in &dir.files {
        let placement = plan_file(f, allocator, &mut sink.allocated_extents)?;
        file_allocs.push((placement.allocation, placement.size_bytes));
        sink.file_placements.push(placement);
    }

    for (sub, alloc) in dir.subdirs.iter().zip(sub_allocs.iter()) {
        child_entries.push(ChildEntry {
            name: sub.name.as_str(),
            attrs: FileAttributes {
                directory: true,
                ..FileAttributes::default()
            },
            first_cluster: alloc.first_cluster,
            data_length: u64::from(alloc.cluster_count)
                .saturating_mul(u64::from(bytes_per_cluster)),
            no_fat_chain: !alloc.is_empty(),
        });
    }
    for (f, (alloc, size)) in dir.files.iter().zip(file_allocs.iter()) {
        child_entries.push(ChildEntry {
            name: f.name.as_str(),
            attrs: FileAttributes {
                archive: true,
                ..FileAttributes::default()
            },
            first_cluster: alloc.first_cluster,
            data_length: *size,
            no_fat_chain: !alloc.is_empty(),
        });
    }

    let entry_bytes = render_directory_entries(&child_entries, upcase)?;
    write_into_clusters(
        &entry_bytes,
        dir_alloc,
        bytes_per_cluster,
        &mut sink.subdir_clusters,
    );

    Ok(dir_alloc)
}

fn plan_file(
    file: &BackingFile,
    allocator: &mut ClusterAllocator,
    allocated_extents: &mut Vec<Allocation>,
) -> Result<FilePlacement, LayoutError> {
    // Cap per-file size at u32::MAX clusters worth — beyond
    // that ClusterAllocator saturates anyway.
    let max_file_bytes =
        u64::from(u32::MAX).saturating_mul(u64::from(allocator.bytes_per_cluster()));
    if file.size > max_file_bytes {
        return Err(LayoutError::FileTooLarge {
            path: file.backing_path.clone(),
            size_bytes: file.size,
        });
    }

    let alloc = allocator.allocate(file.size).map_err(LayoutError::Alloc)?;
    if !alloc.is_empty() {
        allocated_extents.push(alloc);
    }
    Ok(FilePlacement {
        allocation: alloc,
        size_bytes: file.size,
        backing_path: file.backing_path.clone(),
    })
}

// ── Sizing ────────────────────────────────────────────────────────────

fn directory_entry_bytes(dir: &BackingDir, is_root: bool) -> Result<u64, LayoutError> {
    // exFAT root has no `.`/`..` and no bitmap/upcase/label
    // overhead is counted here — the root cluster's special
    // entries are appended in `render_root_directory`, not via
    // this function. `is_root` is currently always `false` (the
    // root is rendered through a separate path); the parameter
    // exists so future multi-cluster-root support can reuse the
    // helper.
    let _ = is_root;
    let mut bytes: u64 = 0;
    for sub in &dir.subdirs {
        bytes = bytes.saturating_add(entry_set_bytes_for_name(&sub.name)?);
    }
    for f in &dir.files {
        bytes = bytes.saturating_add(entry_set_bytes_for_name(&f.name)?);
    }
    Ok(bytes)
}

fn entry_set_bytes_for_name(name: &str) -> Result<u64, LayoutError> {
    let units = name.encode_utf16().count();
    if units == 0 {
        return Err(LayoutError::Directory(DirectoryError::EmptyName));
    }
    if units > MAX_FILE_NAME_CODE_UNITS {
        return Err(LayoutError::Directory(DirectoryError::NameTooLong {
            max_code_units: MAX_FILE_NAME_CODE_UNITS,
            found: units,
        }));
    }
    let units_u64 = units as u64;
    let name_entries = units_u64.div_ceil(NAME_CODE_UNITS_PER_NAME_ENTRY as u64);
    // 1 file + 1 stream + N name entries.
    Ok((2 + name_entries) * (DIRECTORY_ENTRY_SIZE_BYTES as u64))
}

// ── Rendering ─────────────────────────────────────────────────────────

fn render_directory_entries(
    children: &[ChildEntry<'_>],
    upcase: &UpcaseTable,
) -> Result<Vec<u8>, LayoutError> {
    let mut out: Vec<u8> = Vec::new();
    let timestamps = FileTimestamps::default();
    for child in children {
        let utf16: Vec<u16> = child.name.encode_utf16().collect();
        let params = FileEntrySetParams {
            name: &utf16,
            attributes: child.attrs,
            timestamps,
            first_cluster: child.first_cluster,
            valid_data_length: child.data_length,
            data_length: child.data_length,
            no_fat_chain: child.no_fat_chain,
        };
        let bytes = encode_file_entry_set(&params, upcase).map_err(LayoutError::Directory)?;
        out.extend_from_slice(&bytes);
    }
    Ok(out)
}

fn render_root_directory(
    geometry: &ExfatGeometry,
    metadata: &LayoutMetadata<'_>,
    children: &[ChildEntry<'_>],
    bytes_per_cluster: u32,
) -> Result<Vec<u8>, LayoutError> {
    let cluster_bytes = bytes_per_cluster as usize;
    let needed_bytes = 3 * DIRECTORY_ENTRY_SIZE_BYTES;
    if cluster_bytes < needed_bytes {
        return Err(LayoutError::Directory(
            DirectoryError::RootClusterTooSmall {
                needed_bytes,
                cluster_bytes,
            },
        ));
    }
    let _ = geometry;

    let bitmap =
        encode_allocation_bitmap_entry(metadata.bitmap_first_cluster, metadata.bitmap_size_bytes);
    let upcase_entry = encode_upcase_table_entry(
        metadata.upcase.checksum(),
        metadata.upcase_first_cluster,
        metadata.upcase_size_bytes,
    );
    let label =
        encode_volume_label_entry(metadata.volume_label_utf16).map_err(LayoutError::Directory)?;

    let mut child_bytes: u64 = 0;
    for child in children {
        child_bytes = child_bytes.saturating_add(entry_set_bytes_for_name(child.name)?);
    }

    let header_bytes = (3 * DIRECTORY_ENTRY_SIZE_BYTES) as u64;
    let total_root_bytes = header_bytes.saturating_add(child_bytes);
    if total_root_bytes > cluster_bytes as u64 {
        return Err(LayoutError::RootOverflow {
            needed_bytes: total_root_bytes,
            cluster_bytes: cluster_bytes as u64,
        });
    }

    let mut buf = vec![0u8; cluster_bytes];
    #[allow(clippy::indexing_slicing)] // bounds verified above
    {
        buf[0x00..0x20].copy_from_slice(&bitmap);
        buf[0x20..0x40].copy_from_slice(&upcase_entry);
        buf[0x40..0x60].copy_from_slice(&label);
    }

    let mut cursor: usize = 3 * DIRECTORY_ENTRY_SIZE_BYTES;
    let timestamps = FileTimestamps::default();
    for child in children {
        let utf16: Vec<u16> = child.name.encode_utf16().collect();
        let params = FileEntrySetParams {
            name: &utf16,
            attributes: child.attrs,
            timestamps,
            first_cluster: child.first_cluster,
            valid_data_length: child.data_length,
            data_length: child.data_length,
            no_fat_chain: child.no_fat_chain,
        };
        let bytes =
            encode_file_entry_set(&params, metadata.upcase).map_err(LayoutError::Directory)?;
        let end = cursor.saturating_add(bytes.len());
        if end > cluster_bytes {
            return Err(LayoutError::RootOverflow {
                needed_bytes: end as u64,
                cluster_bytes: cluster_bytes as u64,
            });
        }
        if let (Some(dst), Some(src)) = (buf.get_mut(cursor..end), bytes.get(..)) {
            dst.copy_from_slice(src);
        }
        cursor = end;
    }
    Ok(buf)
}

fn write_into_clusters(
    entry_bytes: &[u8],
    alloc: Allocation,
    bytes_per_cluster: u32,
    subdir_clusters: &mut BTreeMap<u32, Vec<u8>>,
) {
    let cluster_size = bytes_per_cluster as usize;
    let range = alloc.cluster_range();
    for (idx, cluster) in range.enumerate() {
        let start = idx.saturating_mul(cluster_size);
        let mut buf = vec![0u8; cluster_size];
        if start < entry_bytes.len() {
            let end = (start.saturating_add(cluster_size)).min(entry_bytes.len());
            let chunk_len = end.saturating_sub(start);
            if let (Some(src), Some(dst)) = (entry_bytes.get(start..end), buf.get_mut(..chunk_len))
            {
                dst.copy_from_slice(src);
            }
        }
        subdir_clusters.insert(cluster, buf);
    }
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
    use std::path::PathBuf;
    use std::time::SystemTime;

    use super::*;
    use crate::fs::backing_tree::{BackingDir, BackingFile, BackingTree};
    use crate::fs::exfat::directory::{
        ENTRY_TYPE_ALLOCATION_BITMAP, ENTRY_TYPE_FILE, ENTRY_TYPE_FILE_NAME,
        ENTRY_TYPE_STREAM_EXTENSION, ENTRY_TYPE_UPCASE_TABLE, ENTRY_TYPE_VOLUME_LABEL,
    };
    use crate::fs::exfat::upcase_table::{UPCASE_TABLE_SIZE_BYTES, UpcaseTable};

    const SIXTY_FOUR_MIB: u64 = 64 * 1024 * 1024;

    fn epoch() -> SystemTime {
        SystemTime::UNIX_EPOCH
    }

    fn empty_dir(name: &str) -> BackingDir {
        BackingDir {
            name: name.to_string(),
            backing_path: PathBuf::from("/").join(name),
            mtime: epoch(),
            subdirs: Vec::new(),
            files: Vec::new(),
        }
    }

    fn file(name: &str, size: u64) -> BackingFile {
        BackingFile {
            name: name.to_string(),
            backing_path: PathBuf::from("/").join(name),
            mtime: epoch(),
            size,
        }
    }

    struct Fixture {
        geo: ExfatGeometry,
        upcase: UpcaseTable,
        label: Vec<u16>,
    }

    impl Fixture {
        fn new() -> Self {
            let geo = ExfatGeometry::for_volume_size(SIXTY_FOUR_MIB).expect("64 MiB geometry");
            let upcase = UpcaseTable::ascii_identity();
            let label: Vec<u16> = "TESLACAM".encode_utf16().collect();
            Self { geo, upcase, label }
        }

        fn metadata(&self) -> LayoutMetadata<'_> {
            let bytes_per_cluster = self.geo.bytes_per_cluster();
            let root_cluster = self.geo.first_root_directory_cluster();
            // Mimic ExfatSynth's reservation order: root @ 2,
            // bitmap @ 3, upcase after bitmap.
            let bitmap_clusters = 1u32; // 64 MiB volume → tiny bitmap
            let bitmap_first = root_cluster + 1;
            let upcase_size = UPCASE_TABLE_SIZE_BYTES as u64;
            let upcase_clusters =
                u32::try_from(upcase_size.div_ceil(u64::from(bytes_per_cluster))).unwrap();
            let upcase_first = bitmap_first + bitmap_clusters;
            let first_free = upcase_first + upcase_clusters;
            LayoutMetadata {
                bitmap_first_cluster: bitmap_first,
                bitmap_size_bytes: u64::from(bitmap_clusters) * u64::from(bytes_per_cluster),
                upcase_first_cluster: upcase_first,
                upcase_size_bytes: upcase_size,
                upcase: &self.upcase,
                volume_label_utf16: &self.label,
                first_free_cluster: first_free,
            }
        }
    }

    // ─── plan ─────────────────────────────────────────────────────────

    #[test]
    fn plan_empty_tree_produces_root_with_three_special_entries() {
        let fx = Fixture::new();
        let tree = BackingTree {
            root: empty_dir("root"),
        };
        let layout = ExfatLayout::plan(&fx.geo, &fx.metadata(), &tree).expect("plan ok");
        let root = layout.root_directory_bytes();
        assert_eq!(root[0x00], ENTRY_TYPE_ALLOCATION_BITMAP);
        assert_eq!(root[0x20], ENTRY_TYPE_UPCASE_TABLE);
        assert_eq!(root[0x40], ENTRY_TYPE_VOLUME_LABEL);
        // First child slot must be zero — end of directory.
        assert_eq!(root[0x60], 0);
        assert!(layout.subdir_clusters().is_empty());
        assert!(layout.file_placements().is_empty());
        assert!(layout.allocated_extents().is_empty());
    }

    #[test]
    fn plan_one_file_appends_entry_set_after_specials() {
        let fx = Fixture::new();
        let mut root = empty_dir("root");
        root.files.push(file("clip.mp4", 1024));
        let tree = BackingTree { root };
        let layout = ExfatLayout::plan(&fx.geo, &fx.metadata(), &tree).expect("plan ok");
        let root_bytes = layout.root_directory_bytes();
        // File entry at offset 0x60.
        assert_eq!(root_bytes[0x60], ENTRY_TYPE_FILE);
        // Stream extension at offset 0x80.
        assert_eq!(root_bytes[0x80], ENTRY_TYPE_STREAM_EXTENSION);
        // First name entry at offset 0xA0.
        assert_eq!(root_bytes[0xA0], ENTRY_TYPE_FILE_NAME);
        assert_eq!(layout.file_placements().len(), 1);
        assert_eq!(layout.file_placements()[0].size_bytes, 1024);
        assert_eq!(layout.allocated_extents().len(), 1);
    }

    #[test]
    fn plan_subdir_creates_subdir_cluster_and_records_extent() {
        let fx = Fixture::new();
        let mut root = empty_dir("root");
        let mut sub = empty_dir("RecentClips");
        sub.files.push(file("a.mp4", 100));
        root.subdirs.push(sub);
        let tree = BackingTree { root };
        let layout = ExfatLayout::plan(&fx.geo, &fx.metadata(), &tree).expect("plan ok");
        // Two extents: one for the subdir directory cluster,
        // one for the file inside it.
        assert_eq!(layout.allocated_extents().len(), 2);
        assert_eq!(layout.subdir_clusters().len(), 1);
        let (cluster, bytes) = layout.subdir_clusters().iter().next().unwrap();
        assert_eq!(bytes.len(), fx.geo.bytes_per_cluster() as usize);
        assert_eq!(bytes[0], ENTRY_TYPE_FILE);
        // Subdir entry first_cluster in the root must match
        // the subdir's actual cluster.
        let root_bytes = layout.root_directory_bytes();
        // Stream extension entry FirstCluster field is bytes 20..24.
        let first_cluster = u32::from_le_bytes([
            root_bytes[0x94],
            root_bytes[0x95],
            root_bytes[0x96],
            root_bytes[0x97],
        ]);
        assert_eq!(first_cluster, *cluster);
    }

    #[test]
    fn plan_root_overflow_returns_error_when_too_many_children() {
        let fx = Fixture::new();
        let mut root = empty_dir("root");
        // 100 short-named files each emit ~96 bytes; way past
        // a 4 KiB cluster's capacity.
        for i in 0..100 {
            root.files.push(file(&format!("f{i:02}.txt"), 1));
        }
        let tree = BackingTree { root };
        let err = ExfatLayout::plan(&fx.geo, &fx.metadata(), &tree).unwrap_err();
        assert!(matches!(err, LayoutError::RootOverflow { .. }));
    }

    #[test]
    fn plan_rejects_empty_name() {
        let fx = Fixture::new();
        let mut root = empty_dir("root");
        root.files.push(file("", 10));
        let tree = BackingTree { root };
        let err = ExfatLayout::plan(&fx.geo, &fx.metadata(), &tree).unwrap_err();
        assert!(matches!(
            err,
            LayoutError::Directory(DirectoryError::EmptyName)
        ));
    }

    #[test]
    fn plan_rejects_metadata_with_first_free_inside_upcase() {
        let fx = Fixture::new();
        let mut meta = fx.metadata();
        meta.first_free_cluster = meta.upcase_first_cluster;
        let tree = BackingTree {
            root: empty_dir("root"),
        };
        let err = ExfatLayout::plan(&fx.geo, &meta, &tree).unwrap_err();
        assert!(matches!(err, LayoutError::BadMetadata { .. }));
    }

    // ─── DataClusterSource ────────────────────────────────────────────

    #[test]
    fn data_source_serves_subdir_cluster_bytes() {
        let fx = Fixture::new();
        let mut root = empty_dir("root");
        root.subdirs.push(empty_dir("RecentClips"));
        let tree = BackingTree { root };
        let layout = ExfatLayout::plan(&fx.geo, &fx.metadata(), &tree).expect("plan ok");
        let (cluster, expected) = {
            let (c, b) = layout.subdir_clusters().iter().next().unwrap();
            (*c, b.clone())
        };
        let mut buf = vec![0u8; expected.len()];
        layout.read_cluster_bytes(cluster, 0, &mut buf);
        assert_eq!(buf, expected);
    }

    #[test]
    fn data_source_unknown_cluster_zero_fills() {
        let fx = Fixture::new();
        let tree = BackingTree {
            root: empty_dir("root"),
        };
        let layout = ExfatLayout::plan(&fx.geo, &fx.metadata(), &tree).expect("plan ok");
        let mut buf = vec![0xFFu8; 64];
        layout.read_cluster_bytes(u32::MAX, 0, &mut buf);
        assert!(buf.iter().all(|&b| b == 0));
    }

    /// Regression: the root directory cluster must be served by the
    /// `DataClusterSource` (not just `root_directory_bytes()`), so the
    /// write-side resolver can materialize the root buffer and its
    /// pre-existing-child baseline from this single source. Before the
    /// fix `read_cluster_bytes(root_cluster, ..)` zero-filled, leaving
    /// the seeded root buffer empty and root deletions undetected.
    #[test]
    fn data_source_serves_root_directory_cluster_bytes() {
        let fx = Fixture::new();
        let mut root = empty_dir("root");
        root.subdirs.push(empty_dir("RecentClips"));
        let tree = BackingTree { root };
        let layout = ExfatLayout::plan(&fx.geo, &fx.metadata(), &tree).expect("plan ok");
        let root_cluster = fx.geo.first_root_directory_cluster();
        let expected = layout.root_directory_bytes().to_vec();
        assert!(
            expected.iter().any(|&b| b != 0),
            "root directory bytes should carry the RecentClips entry"
        );
        let mut buf = vec![0u8; expected.len()];
        layout.read_cluster_bytes(root_cluster, 0, &mut buf);
        assert_eq!(buf, expected);
    }

    // ─── sizing helpers ───────────────────────────────────────────────

    #[test]
    fn entry_set_bytes_matches_spec_for_short_name() {
        // "a" → 1 utf-16 unit → 1 name entry → 3 entries total
        // = 96 bytes.
        assert_eq!(entry_set_bytes_for_name("a").unwrap(), 96);
    }

    #[test]
    fn entry_set_bytes_matches_spec_for_16_unit_name() {
        // 16 units → ceil(16/15) = 2 name entries → 4 entries
        // total = 128 bytes.
        let n = "a".repeat(16);
        assert_eq!(entry_set_bytes_for_name(&n).unwrap(), 128);
    }
}
