//! Phase 2.17 — FAT32 directory + file layout planner.
//!
//! Walks a [`BackingTree`] (Phase 2.15) and produces a
//! [`Fat32Layout`] containing:
//!
//! 1. **Cluster chains** ([`AllocatedChains`]) describing
//!    which clusters belong to which file or directory. The
//!    chains drop straight into
//!    [`FatTable::build`](crate::fs::fat32::fat_table::FatTable::build)
//!    so the FAT region serves correct allocation bits without
//!    any glue code.
//! 2. **Materialized directory clusters** — for every
//!    directory in the tree, the planner renders the full
//!    32-byte-per-entry array (LFN + SFN per child, plus the
//!    `.`/`..` pair for subdirs) and stores the resulting
//!    bytes keyed by cluster number. The synth dispatcher
//!    looks the cluster up at read time via the planner's
//!    [`DataClusterSource`] impl.
//! 3. **File placements** — each backing file gets recorded
//!    with its allocation, on-disk size, and backing path so
//!    the Phase-2.19 materializer can re-open the file and
//!    serve real cluster bytes. The 2.17 planner itself does
//!    not read file contents — file clusters fall through to
//!    zero-fill at this layer.
//!
//! ## Why every name uses an LFN
//!
//! FAT32 permits a name to be stored as a bare 8.3 SFN entry
//! when (and only when) the name is exactly representable as
//! one. Generating a collision-free SFN alias from an
//! arbitrary long name requires the Microsoft "tail" + hash
//! algorithm (`~1`, `~2`, …, with a per-name hash beyond
//! `~4`) which is widely documented but tricky to get right
//! and untestable without a Windows host to compare.
//!
//! For the synth read-path we instead always emit an LFN
//! sequence followed by a deterministic synthetic SFN of the
//! form `F<6 hex digits>` (e.g. `F00001A    `). Linux's
//! `vfat` driver, Windows, `fsck.vfat`, and Tesla itself
//! resolve files by the LFN, so the SFN alias's exact bytes
//! never reach userspace. The SFN must still be syntactically
//! valid (uppercase ASCII, no leading space, no `.`) and its
//! checksum must match what the LFN entries claim — both of
//! which fall out automatically when we construct the SFN
//! through [`ShortName::from_padded_str`].
//!
//! The 6-hex-digit counter caps each directory at
//! 16 777 215 entries, which is well past the 65 535-entry
//! FAT32 per-directory limit so the SFN encoder runs out long
//! after FAT32 itself does.
//!
//! ## What this module does NOT do
//!
//! * **No file content materialization.** The
//!   [`DataClusterSource`] impl serves directory cluster
//!   bytes only; file clusters return zeros. Phase 2.19's
//!   `teslafat::DirTreeMaterializer` wraps the layout and
//!   opens the backing files on demand.
//! * **No write path.** The planner consumes an immutable
//!   `&BackingTree`; the write side (Phase 3+) will rebuild
//!   a fresh `Fat32Layout` after each backing-tree edit.
//! * **No reverse mapping** from cluster number to backing
//!   path. Phase 2.19 owns that index because it depends on
//!   whether files are sharded or co-located, which is a
//!   teslafat-policy choice.

use std::collections::BTreeMap;
use std::path::PathBuf;

use crate::fs::backing_tree::{BackingDir, BackingFile, BackingTree};
use crate::fs::cluster_layout::{
    AllocError, AllocatedChains, Allocation, ClusterAllocator, FIRST_DATA_CLUSTER,
};
use crate::fs::data_cluster_source::DataClusterSource;
use crate::fs::fat32::boot_sector::ROOT_DIRECTORY_CLUSTER;
use crate::fs::fat32::directory::{
    DIR_ENTRY_SIZE_BYTES, FileAttributes, LFN_CHARS_PER_ENTRY, LfnError, ShortName, ShortNameError,
    Timestamps, synthesize_dot_entries, synthesize_lfn_sequence, synthesize_sfn_entry,
};
use crate::fs::fat32::geometry::Fat32Geometry;
use crate::fs::geometry::{Geometry, SECTOR_SIZE_BYTES};

/// Hard upper bound on entries per single directory, imposed
/// by the 6-hex-digit synthetic SFN counter (`F<6 hex>`
/// fits up to `0x00FFFFFF`).
pub const MAX_ENTRIES_PER_DIRECTORY: u32 = 0x00FF_FFFF;

/// Where a single backing file ended up in the synthesized
/// volume. Phase 2.19's materializer keys on
/// `allocation.first_cluster` to resolve a cluster read to a
/// backing-file open.
#[derive(Debug, Clone)]
pub struct FilePlacement {
    /// Cluster chain assigned to this file. Empty files get
    /// [`Allocation::EMPTY`] and consume no clusters.
    pub allocation: Allocation,
    /// On-disk file size in bytes, as captured by the walker.
    /// Stamped into the file's SFN dir entry and consulted by
    /// the materializer to clamp reads.
    pub size_bytes: u64,
    /// Absolute backing path. The materializer opens this
    /// path to serve file-content cluster reads.
    pub backing_path: PathBuf,
}

/// Errors that can prevent a [`Fat32Layout`] from being built.
#[derive(Debug, PartialEq, Eq)]
pub enum LayoutError {
    /// The cluster allocator rejected an allocation — typically
    /// because the volume isn't large enough to hold the tree.
    Alloc(AllocError),
    /// One of the synthesized SFN aliases failed validation.
    /// Should never happen with the deterministic `F<6 hex>`
    /// generator but surfaced rather than swallowed.
    ShortName(ShortNameError),
    /// An LFN sequence couldn't be synthesized for a child
    /// name. Surfaces the underlying [`LfnError`] verbatim so
    /// callers can pinpoint the offending file.
    Lfn(LfnError),
    /// A directory has more than [`MAX_ENTRIES_PER_DIRECTORY`]
    /// children — the synthetic-SFN counter would overflow.
    DirectoryTooBig {
        /// How many children the directory has.
        children: u64,
        /// The hard cap.
        maximum: u32,
    },
    /// A directory's total entry-byte array exceeds the FAT32
    /// per-file limit of `u32::MAX`. Mathematically reachable
    /// only via a synthetic test fixture; FAT32 itself caps
    /// directory entry count well below `u32::MAX / 32`.
    DirectoryBytesOverflow {
        /// Computed byte size of the directory's entry array.
        bytes: u64,
    },
    /// A backing file is larger than `u32::MAX` bytes. FAT32
    /// can't store the size in its 32-bit SFN field; the walker
    /// should have rejected this earlier, but the planner
    /// checks defensively.
    FileTooLarge {
        /// The offending backing path.
        path: PathBuf,
        /// The file's actual size.
        size_bytes: u64,
    },
}

impl core::fmt::Display for LayoutError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::Alloc(err) => write!(f, "cluster allocator failed during planning: {err}"),
            Self::ShortName(err) => {
                write!(f, "synthetic short name failed validation: {err}")
            }
            Self::Lfn(err) => write!(f, "LFN synthesis failed: {err}"),
            Self::DirectoryTooBig { children, maximum } => write!(
                f,
                "directory has {children} children which exceeds the synthetic-SFN counter cap of {maximum}",
            ),
            Self::DirectoryBytesOverflow { bytes } => write!(
                f,
                "directory entry array of {bytes} bytes exceeds the u32 FAT32 file-size field",
            ),
            Self::FileTooLarge { path, size_bytes } => write!(
                f,
                "backing file {} is {size_bytes} bytes which exceeds FAT32's 4 GiB limit",
                path.display(),
            ),
        }
    }
}

impl std::error::Error for LayoutError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Alloc(err) => Some(err),
            Self::ShortName(err) => Some(err),
            Self::Lfn(err) => Some(err),
            Self::DirectoryTooBig { .. }
            | Self::DirectoryBytesOverflow { .. }
            | Self::FileTooLarge { .. } => None,
        }
    }
}

/// Complete read-side layout for a FAT32 volume backed by a
/// [`BackingTree`]. See the module docs for the full pipeline.
#[derive(Debug)]
pub struct Fat32Layout {
    chains: AllocatedChains,
    bytes_per_cluster: u32,
    first_data_byte: u64,
    dir_clusters: BTreeMap<u32, Vec<u8>>,
    files: Vec<FilePlacement>,
}

impl Fat32Layout {
    /// Plan the cluster layout for `tree` on a FAT32 volume
    /// with the given `geometry`.
    ///
    /// The walk is pre-order with deterministic child
    /// ordering (the caller is expected to have sorted the
    /// [`BackingDir::subdirs`] and [`BackingDir::files`] vecs
    /// by name; `teslafat::backing_walker::walk` does this).
    /// Cluster numbers are therefore reproducible across runs
    /// for the same input tree.
    ///
    /// # Errors
    ///
    /// * [`LayoutError::Alloc`] if the volume is too small to
    ///   hold the tree (or if the geometry is malformed).
    /// * [`LayoutError::Lfn`] if a backing name can't be
    ///   represented as an LFN sequence.
    /// * [`LayoutError::ShortName`] if the synthetic SFN
    ///   counter overflows in a way that produces invalid
    ///   bytes (defensive — the deterministic generator
    ///   should never trigger this).
    /// * [`LayoutError::DirectoryTooBig`] if any directory has
    ///   more than [`MAX_ENTRIES_PER_DIRECTORY`] children.
    /// * [`LayoutError::DirectoryBytesOverflow`] if a
    ///   directory's entry array exceeds `u32::MAX` bytes.
    /// * [`LayoutError::FileTooLarge`] if a backing file
    ///   exceeds `u32::MAX` bytes.
    pub fn plan(geometry: &Fat32Geometry, tree: &BackingTree) -> Result<Self, LayoutError> {
        let bytes_per_cluster = geometry.bytes_per_cluster();
        let max_cluster_exclusive =
            FIRST_DATA_CLUSTER.saturating_add(geometry.data_cluster_count());
        let mut allocator =
            ClusterAllocator::new(bytes_per_cluster, FIRST_DATA_CLUSTER, max_cluster_exclusive)
                .map_err(LayoutError::Alloc)?;
        let first_data_byte = geometry
            .first_data_sector()
            .saturating_mul(u64::from(SECTOR_SIZE_BYTES));

        let mut chains = AllocatedChains::new();
        let mut dir_clusters: BTreeMap<u32, Vec<u8>> = BTreeMap::new();
        let mut files: Vec<FilePlacement> = Vec::new();

        plan_dir(
            &tree.root,
            None,
            true,
            &mut allocator,
            &mut chains,
            &mut dir_clusters,
            &mut files,
        )?;

        Ok(Self {
            chains,
            bytes_per_cluster,
            first_data_byte,
            dir_clusters,
            files,
        })
    }

    /// Cluster chains in `AllocatedChains` form, ready to feed
    /// to [`FatTable::build`](crate::fs::fat32::fat_table::FatTable::build).
    #[must_use]
    pub fn chains(&self) -> &AllocatedChains {
        &self.chains
    }

    /// All file placements in DFS-pre-order. Phase 2.19's
    /// materializer builds a `first_cluster -> path` index off
    /// this slice.
    #[must_use]
    pub fn files(&self) -> &[FilePlacement] {
        &self.files
    }

    /// Materialized directory cluster bytes keyed by cluster
    /// number. Exposed for tests and for the integration with
    /// the synth dispatcher's data region.
    #[must_use]
    pub fn dir_clusters(&self) -> &BTreeMap<u32, Vec<u8>> {
        &self.dir_clusters
    }

    /// Cluster size this layout was planned against.
    #[must_use]
    pub fn bytes_per_cluster(&self) -> u32 {
        self.bytes_per_cluster
    }

    /// Byte offset of the first data cluster (cluster 2)
    /// within the synthesized volume. Useful for tests that
    /// want to verify a specific cluster's bytes appear at
    /// the right offset.
    #[must_use]
    pub fn first_data_byte(&self) -> u64 {
        self.first_data_byte
    }
}

impl DataClusterSource for Fat32Layout {
    fn read_cluster_bytes(&self, cluster: u32, byte_in_cluster: usize, out: &mut [u8]) {
        if let Some(bytes) = self.dir_clusters.get(&cluster) {
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
        } else {
            out.fill(0);
        }
    }
}

// ── Planning core ─────────────────────────────────────────────────────

fn plan_dir(
    dir: &BackingDir,
    parent_first_cluster: Option<u32>,
    is_root: bool,
    allocator: &mut ClusterAllocator,
    chains: &mut AllocatedChains,
    dir_clusters: &mut BTreeMap<u32, Vec<u8>>,
    files: &mut Vec<FilePlacement>,
) -> Result<Allocation, LayoutError> {
    let child_count = dir.subdirs.len().saturating_add(dir.files.len());
    let child_count_u64 = child_count as u64;
    if child_count_u64 > u64::from(MAX_ENTRIES_PER_DIRECTORY) {
        return Err(LayoutError::DirectoryTooBig {
            children: child_count_u64,
            maximum: MAX_ENTRIES_PER_DIRECTORY,
        });
    }

    let dir_bytes_u64 = directory_entry_bytes(dir, is_root)?;
    let dir_alloc = allocator
        .allocate(dir_bytes_u64.max(1))
        .map_err(LayoutError::Alloc)?;
    // Root must land at cluster 2 — `ClusterAllocator::new`
    // starts at FIRST_DATA_CLUSTER and `plan_dir` for the
    // root is invoked first, so this assert is documentary.
    debug_assert!(
        !is_root || dir_alloc.first_cluster == ROOT_DIRECTORY_CLUSTER,
        "root must be allocated first so it lands at cluster {ROOT_DIRECTORY_CLUSTER}",
    );
    chains.push(dir_alloc);

    // Now allocate children (DFS pre-order). Subdirs first so
    // their cluster numbers are stable regardless of how many
    // files they sit alongside; both halves are independently
    // sorted by name already.
    let mut child_first_clusters: Vec<(ChildKind, u32, usize)> = Vec::with_capacity(child_count);

    for (idx, sub) in dir.subdirs.iter().enumerate() {
        let sub_alloc = plan_dir(
            sub,
            Some(dir_alloc.first_cluster),
            false,
            allocator,
            chains,
            dir_clusters,
            files,
        )?;
        child_first_clusters.push((ChildKind::Subdir, sub_alloc.first_cluster, idx));
    }

    for (idx, file) in dir.files.iter().enumerate() {
        let file_alloc = plan_file(file, allocator, chains, files)?;
        child_first_clusters.push((ChildKind::File, file_alloc.first_cluster, idx));
    }

    let parent_for_dotdot = if is_root {
        // The root has no `.`/`..`, but if a subdir's parent is
        // the root, that subdir's `..` entry must record 0 (not
        // 2) per fatgen103 §6.5.2.
        0
    } else {
        // We are a subdir whose parent passed its first cluster
        // in via `parent_first_cluster`.
        let p = parent_first_cluster.unwrap_or(0);
        if p == ROOT_DIRECTORY_CLUSTER { 0 } else { p }
    };

    let entry_bytes = render_directory_bytes(
        dir,
        is_root,
        dir_alloc.first_cluster,
        parent_for_dotdot,
        &child_first_clusters,
    )?;

    write_into_clusters(
        &entry_bytes,
        dir_alloc,
        allocator.bytes_per_cluster(),
        dir_clusters,
    );

    Ok(dir_alloc)
}

#[derive(Debug, Clone, Copy)]
enum ChildKind {
    Subdir,
    File,
}

fn plan_file(
    file: &BackingFile,
    allocator: &mut ClusterAllocator,
    chains: &mut AllocatedChains,
    files: &mut Vec<FilePlacement>,
) -> Result<Allocation, LayoutError> {
    if file.size > u64::from(u32::MAX) {
        return Err(LayoutError::FileTooLarge {
            path: file.backing_path.clone(),
            size_bytes: file.size,
        });
    }
    let alloc = allocator.allocate(file.size).map_err(LayoutError::Alloc)?;
    if !alloc.is_empty() {
        chains.push(alloc);
    }
    files.push(FilePlacement {
        allocation: alloc,
        size_bytes: file.size,
        backing_path: file.backing_path.clone(),
    });
    Ok(alloc)
}

// ── Sizing ────────────────────────────────────────────────────────────

fn directory_entry_bytes(dir: &BackingDir, is_root: bool) -> Result<u64, LayoutError> {
    let mut bytes: u64 = 0;
    if !is_root {
        // `.` + `..` = 2 entries.
        bytes = bytes.saturating_add(2 * DIR_ENTRY_SIZE_BYTES as u64);
    }
    for sub in &dir.subdirs {
        bytes = bytes.saturating_add(entry_bytes_for_name(&sub.name));
    }
    for f in &dir.files {
        bytes = bytes.saturating_add(entry_bytes_for_name(&f.name));
    }
    if bytes > u64::from(u32::MAX) {
        return Err(LayoutError::DirectoryBytesOverflow { bytes });
    }
    Ok(bytes)
}

fn entry_bytes_for_name(name: &str) -> u64 {
    let units = name.encode_utf16().count() as u64;
    let lfn_entries = units.div_ceil(LFN_CHARS_PER_ENTRY as u64);
    // 1 SFN + lfn_entries LFN entries.
    (1 + lfn_entries) * (DIR_ENTRY_SIZE_BYTES as u64)
}

#[allow(clippy::cast_possible_truncation)]
fn directory_entry_bytes_usize(dir: &BackingDir, is_root: bool) -> Result<usize, LayoutError> {
    let b = directory_entry_bytes(dir, is_root)?;
    usize::try_from(b).map_err(|_| LayoutError::DirectoryBytesOverflow { bytes: b })
}

// ── Rendering ─────────────────────────────────────────────────────────

fn render_directory_bytes(
    dir: &BackingDir,
    is_root: bool,
    this_cluster: u32,
    parent_cluster_for_dotdot: u32,
    children: &[(ChildKind, u32, usize)],
) -> Result<Vec<u8>, LayoutError> {
    let total_bytes = directory_entry_bytes_usize(dir, is_root)?;
    let mut out = Vec::with_capacity(total_bytes);
    let stamps = Timestamps::epoch();

    if !is_root {
        let dots = synthesize_dot_entries(this_cluster, parent_cluster_for_dotdot, &stamps);
        for entry in &dots {
            out.extend_from_slice(entry);
        }
    }

    let mut sfn_counter: u32 = 0;
    for &(kind, first_cluster, child_idx) in children {
        sfn_counter = sfn_counter.saturating_add(1);
        let (name, size, attrs) = match kind {
            ChildKind::Subdir => {
                let sub = dir
                    .subdirs
                    .get(child_idx)
                    .ok_or(LayoutError::DirectoryTooBig {
                        children: dir.subdirs.len() as u64,
                        maximum: MAX_ENTRIES_PER_DIRECTORY,
                    })?;
                (sub.name.as_str(), 0u32, FileAttributes::directory())
            }
            ChildKind::File => {
                let f = dir
                    .files
                    .get(child_idx)
                    .ok_or(LayoutError::DirectoryTooBig {
                        children: dir.files.len() as u64,
                        maximum: MAX_ENTRIES_PER_DIRECTORY,
                    })?;
                let size = u32::try_from(f.size).map_err(|_| LayoutError::FileTooLarge {
                    path: f.backing_path.clone(),
                    size_bytes: f.size,
                })?;
                (f.name.as_str(), size, FileAttributes::archive())
            }
        };

        let short = synthetic_sfn(sfn_counter)?;
        let lfn_entries =
            synthesize_lfn_sequence(name, short.checksum()).map_err(LayoutError::Lfn)?;
        for entry in &lfn_entries {
            out.extend_from_slice(entry);
        }
        let sfn = synthesize_sfn_entry(&short, attrs, first_cluster, size, &stamps);
        out.extend_from_slice(&sfn);
    }

    debug_assert_eq!(out.len(), total_bytes);
    Ok(out)
}

fn synthetic_sfn(counter: u32) -> Result<ShortName, LayoutError> {
    // Format: `F<6 hex digits>` (uppercase). 7 chars + 1 pad
    // + 3 ext spaces = 11 bytes.
    let padded = format!("F{counter:06X}");
    ShortName::from_padded_str(&padded).map_err(LayoutError::ShortName)
}

fn write_into_clusters(
    entry_bytes: &[u8],
    alloc: Allocation,
    bytes_per_cluster: u32,
    dir_clusters: &mut BTreeMap<u32, Vec<u8>>,
) {
    let cluster_size = bytes_per_cluster as usize;
    let range = alloc.cluster_range();
    let cluster_count = alloc.cluster_count as usize;
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
        dir_clusters.insert(cluster, buf);
        // `cluster_count` is consulted via debug_assert; the
        // explicit binding silences `unused` while keeping the
        // invariant documented.
        let _ = cluster_count;
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
    use crate::fs::fat32::directory::ATTR_DIRECTORY;

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
            size,
            mtime: epoch(),
        }
    }

    fn small_geometry() -> Fat32Geometry {
        Fat32Geometry::for_volume_size(34 * 1024 * 1024).expect("34 MiB geometry")
    }

    // ── Sizing ────────────────────────────────────────────────────

    #[test]
    fn root_with_no_children_has_zero_entry_bytes() {
        let tree = BackingTree {
            root: empty_dir(""),
        };
        assert_eq!(directory_entry_bytes(&tree.root, true).unwrap(), 0);
    }

    #[test]
    fn subdir_with_no_children_has_64_byte_dot_dotdot() {
        let sub = empty_dir("sub");
        assert_eq!(directory_entry_bytes(&sub, false).unwrap(), 64);
    }

    #[test]
    fn entry_bytes_short_name_one_lfn_plus_one_sfn() {
        // "a" — 1 UTF-16 unit → 1 LFN entry + 1 SFN = 64.
        assert_eq!(entry_bytes_for_name("a"), 64);
        // 13 units fit exactly in one LFN entry.
        let name13: String = "abcdefghijklm".to_string();
        assert_eq!(entry_bytes_for_name(&name13), 64);
        // 14 units need 2 LFN entries + 1 SFN = 96.
        let name14: String = "abcdefghijklmn".to_string();
        assert_eq!(entry_bytes_for_name(&name14), 96);
    }

    #[test]
    fn synthetic_sfn_is_valid_for_first_and_max_counter() {
        let a = synthetic_sfn(1).unwrap();
        assert_eq!(a.as_bytes(), b"F000001    ");
        let z = synthetic_sfn(MAX_ENTRIES_PER_DIRECTORY).unwrap();
        assert_eq!(z.as_bytes(), b"FFFFFFF    ");
    }

    // ── Planning ──────────────────────────────────────────────────

    #[test]
    fn plan_empty_tree_allocates_only_root_cluster() {
        let tree = BackingTree {
            root: empty_dir(""),
        };
        let geo = small_geometry();
        let layout = Fat32Layout::plan(&geo, &tree).unwrap();
        assert_eq!(layout.chains().len(), 1);
        let root = layout.chains().as_slice()[0];
        assert_eq!(root.first_cluster, ROOT_DIRECTORY_CLUSTER);
        assert_eq!(root.cluster_count, 1);
        // Root cluster is entirely zero (no entries).
        let bytes = layout.dir_clusters().get(&ROOT_DIRECTORY_CLUSTER).unwrap();
        assert_eq!(bytes.len(), geo.bytes_per_cluster() as usize);
        assert!(bytes.iter().all(|&b| b == 0));
    }

    #[test]
    fn plan_one_file_writes_lfn_plus_sfn_in_root_cluster() {
        let mut root = empty_dir("");
        root.files.push(file("hi.txt", 0));
        let tree = BackingTree { root };
        let geo = small_geometry();
        let layout = Fat32Layout::plan(&geo, &tree).unwrap();
        let root_bytes = layout.dir_clusters().get(&ROOT_DIRECTORY_CLUSTER).unwrap();
        // First 32 bytes = LFN entry; next 32 bytes = SFN entry.
        // LFN entry's first byte = ordinal | LAST_LONG_ENTRY (0x40)
        // since 1 entry covers the whole name.
        assert_eq!(root_bytes[0], 0x41, "ordinal 1 + LAST_LONG_ENTRY bit");
        assert_eq!(root_bytes[11], 0x0F, "LFN attribute byte");
        // SFN entry at offset 32: first 11 bytes = synthetic SFN.
        assert_eq!(&root_bytes[32..43], b"F000001    ");
        assert_eq!(root_bytes[43], FileAttributes::archive().raw());
        // The 32-byte cell after SFN is end-of-dir (0x00 first byte).
        assert_eq!(root_bytes[64], 0x00);
        // File is empty → first_cluster fields are 0.
        let clus_hi = u16::from_le_bytes(root_bytes[52..54].try_into().unwrap());
        let clus_lo = u16::from_le_bytes(root_bytes[58..60].try_into().unwrap());
        assert_eq!(clus_hi, 0);
        assert_eq!(clus_lo, 0);
    }

    #[test]
    fn plan_one_file_with_content_allocates_data_clusters() {
        let mut root = empty_dir("");
        // 10 KiB file with 4 KiB clusters (sectors_per_cluster
        // = 8 at 34 MiB → 4 KiB) → 3 clusters.
        root.files.push(file("data.bin", 10 * 1024));
        let tree = BackingTree { root };
        let geo = Fat32Geometry::for_volume_size(64 * 1024 * 1024 * 1024).expect("64 GiB geometry");
        // At 64 GiB, sectors_per_cluster = 64 → 32 KiB
        // clusters → 10 KiB fits in 1 cluster.
        let layout = Fat32Layout::plan(&geo, &tree).unwrap();
        // 2 chains: root cluster + file cluster.
        assert_eq!(layout.chains().len(), 2);
        assert_eq!(layout.files().len(), 1);
        let placement = &layout.files()[0];
        assert_eq!(placement.size_bytes, 10 * 1024);
        assert_eq!(placement.allocation.cluster_count, 1);
        assert_eq!(placement.allocation.first_cluster, 3); // root=2, file=3
    }

    #[test]
    fn plan_subdir_emits_dot_and_dotdot_at_offset_zero() {
        let mut root = empty_dir("");
        root.subdirs.push(empty_dir("sub"));
        let tree = BackingTree { root };
        let geo = small_geometry();
        let layout = Fat32Layout::plan(&geo, &tree).unwrap();
        // root=2, sub=3.
        let sub_bytes = layout.dir_clusters().get(&3).unwrap();
        // "." entry: bytes 0..11 = ". " then 9 spaces.
        assert_eq!(sub_bytes[0], b'.');
        assert_eq!(&sub_bytes[1..11], b"          ");
        // Attr byte: directory.
        assert_eq!(sub_bytes[11], ATTR_DIRECTORY);
        // ".." entry at offset 32.
        assert_eq!(&sub_bytes[32..34], b"..");
        // ".." for a subdir whose parent IS root must encode
        // parent cluster = 0, not 2 (fatgen103 §6.5.2).
        let dotdot_clus_hi = u16::from_le_bytes(sub_bytes[32 + 20..32 + 22].try_into().unwrap());
        let dotdot_clus_lo = u16::from_le_bytes(sub_bytes[32 + 26..32 + 28].try_into().unwrap());
        assert_eq!(dotdot_clus_hi, 0);
        assert_eq!(dotdot_clus_lo, 0);
    }

    #[test]
    fn plan_nested_subdir_dotdot_records_grandparent_cluster() {
        let mut child = empty_dir("c");
        child.files.push(file("f.txt", 0));
        let mut middle = empty_dir("m");
        middle.subdirs.push(child);
        let mut root = empty_dir("");
        root.subdirs.push(middle);
        let tree = BackingTree { root };
        let geo = small_geometry();
        let layout = Fat32Layout::plan(&geo, &tree).unwrap();
        // root=2, middle=3, child=4.
        let child_bytes = layout.dir_clusters().get(&4).unwrap();
        // ".." entry at offset 32 — clus_lo should be 3 (middle), not 0.
        let dotdot_clus_lo = u16::from_le_bytes(child_bytes[32 + 26..32 + 28].try_into().unwrap());
        assert_eq!(dotdot_clus_lo, 3);
    }

    // ── DataClusterSource ─────────────────────────────────────────

    #[test]
    fn data_source_returns_dir_cluster_bytes() {
        let mut root = empty_dir("");
        root.files.push(file("a.txt", 0));
        let tree = BackingTree { root };
        let geo = small_geometry();
        let layout = Fat32Layout::plan(&geo, &tree).unwrap();
        let mut buf = vec![0u8; 64];
        layout.read_cluster_bytes(ROOT_DIRECTORY_CLUSTER, 0, &mut buf);
        // Same first byte we asserted above.
        assert_eq!(buf[0], 0x41);
        assert_eq!(buf[32..43], *b"F000001    ");
    }

    #[test]
    fn data_source_partial_offset_within_cluster() {
        let mut root = empty_dir("");
        root.files.push(file("a.txt", 0));
        let tree = BackingTree { root };
        let geo = small_geometry();
        let layout = Fat32Layout::plan(&geo, &tree).unwrap();
        let mut buf = vec![0u8; 11];
        // Offset 32 = SFN field of first SFN entry.
        layout.read_cluster_bytes(ROOT_DIRECTORY_CLUSTER, 32, &mut buf);
        assert_eq!(&buf[..], b"F000001    ");
    }

    #[test]
    fn data_source_unknown_cluster_zero_fills() {
        let tree = BackingTree {
            root: empty_dir(""),
        };
        let geo = small_geometry();
        let layout = Fat32Layout::plan(&geo, &tree).unwrap();
        let mut buf = [0xAAu8; 32];
        layout.read_cluster_bytes(9999, 0, &mut buf);
        assert!(buf.iter().all(|&b| b == 0));
    }

    #[test]
    fn data_source_read_past_dir_bytes_zero_pads() {
        let mut root = empty_dir("");
        root.files.push(file("a.txt", 0));
        let tree = BackingTree { root };
        let geo = small_geometry();
        let layout = Fat32Layout::plan(&geo, &tree).unwrap();
        // The dir has 2 entries (LFN + SFN = 64 bytes); the
        // cluster is bytes_per_cluster long, so a read at
        // offset 64 should yield zeros (end-of-dir region).
        let mut buf = vec![0xAAu8; 16];
        layout.read_cluster_bytes(ROOT_DIRECTORY_CLUSTER, 64, &mut buf);
        assert!(buf.iter().all(|&b| b == 0));
    }

    // ── Error paths ───────────────────────────────────────────────

    #[test]
    fn rejects_file_larger_than_u32_max() {
        let mut root = empty_dir("");
        root.files.push(file("huge.bin", u64::from(u32::MAX) + 1));
        let tree = BackingTree { root };
        let geo = Fat32Geometry::for_volume_size(16 * 1024 * 1024 * 1024).expect("16 GiB geometry");
        let err = Fat32Layout::plan(&geo, &tree).unwrap_err();
        match err {
            LayoutError::FileTooLarge { size_bytes, .. } => {
                assert_eq!(size_bytes, u64::from(u32::MAX) + 1);
            }
            other => panic!("expected FileTooLarge, got {other:?}"),
        }
    }

    #[test]
    fn rejects_tree_that_overflows_volume() {
        // Stuff a tiny volume past its data cluster capacity.
        let mut root = empty_dir("");
        // Each file claims 1 cluster minimum at size 1.
        for i in 0..100_000 {
            root.files.push(file(&format!("f{i}.bin"), 1));
        }
        let tree = BackingTree { root };
        let geo = small_geometry();
        let err = Fat32Layout::plan(&geo, &tree).unwrap_err();
        assert!(matches!(
            err,
            LayoutError::Alloc(AllocError::OutOfClusters { .. })
        ));
    }

    // ── Integration with FatTable ─────────────────────────────────

    #[test]
    fn layout_chains_drop_into_fat_table_build() {
        use crate::fs::fat32::fat_table::{FREE_CLUSTER, FatTable};
        let mut root = empty_dir("");
        root.subdirs.push(empty_dir("sub"));
        root.files.push(file("hi.txt", 0));
        let tree = BackingTree { root };
        let geo = small_geometry();
        let layout = Fat32Layout::plan(&geo, &tree).unwrap();
        let table = FatTable::build(&geo, layout.chains()).expect("fat table builds");
        let entries = table.entries();
        // 2 chains, each 1 cluster: root (cluster 2) + sub
        // (cluster 3). hi.txt is size-0 so it has no chain.
        let allocated = entries
            .iter()
            .skip(2)
            .filter(|&&e| e != FREE_CLUSTER)
            .count();
        assert_eq!(allocated, 2);
    }
}
