//! `exFAT` write-path state machine (Phase 3.5e).
//!
//! Sits between [`teslausb_core::fs::exfat::parse::decode_write`]
//! (which classifies each kernel-issued write byte into a typed
//! per-region chunk) and [`super::dir_tree::DirTreeWriter`]
//! (which materializes file content onto the POSIX backing tree
//! via `.partial`-suffix atomicity).
//!
//! Parallel to [`super::fat32_write`] (Phase 3.5c); see that
//! module for the shared rationale (per-region-chunk routing,
//! pending stash for out-of-order arrivals, crash safety via
//! `.partial` rename). The differences here mirror the structural
//! differences between FAT32 and `exFAT`:
//!
//! 1. **Single FAT**, not two mirrors — there is no per-mirror
//!    array; the one FAT is the source of truth for chained files.
//! 2. **`NoFatChain` flag** (`exFAT` spec §7.6.3) — most files
//!    the synthesizer produces (and Tesla writes back) are
//!    contiguous, so their extent is fully described by the
//!    Stream Extension's `(first_cluster, data_length)` pair
//!    **without** walking the FAT. Only when `no_fat_chain == 0`
//!    do we need to walk the FAT.
//! 3. **Directory entry decoder** is the entry-set state machine
//!    from Phase 3.5d, not the LFN-chain decoder from Phase
//!    3.5a; `PartialEntrySet` carry replaces `Vec<LfnEntry>`.
//! 4. **End-of-chain marker** is `0xFFFF_FFFF` (vs FAT32's
//!    `0x0FFFFFF8..=0x0FFFFFFF` range).
//! 5. **Root directory cluster** comes from the geometry
//!    (`first_root_directory_cluster`), not a hard-coded
//!    constant.
//! 6. **No `.` / `..` entries** in `exFAT` directories (spec §6.4)
//!    so the "skip the synthetic dot entries" gymnastics from
//!    FAT32 are unnecessary.
//! 7. **Volume label** lives in the root directory as a single
//!    `0x83` entry — same as our `0x81` Allocation Bitmap and
//!    `0x82` `UpCase` Table primaries. They are decoded but not
//!    routed as files.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use teslausb_core::backend::BackendError;
use teslausb_core::fs::cluster_layout::FIRST_DATA_CLUSTER;
use teslausb_core::fs::cluster_map::{ClusterMap, ClusterMapError, FileExtent};
use teslausb_core::fs::exfat::dir_decode::{
    self, DecodedExfatEntry, ExfatDecodeResult, ExfatDirDecodeError, PartialEntrySet,
};
use teslausb_core::fs::exfat::geometry::ExfatGeometry;
use teslausb_core::fs::exfat::parse::{DecodeWriteError, DecodedWrite, decode_write};
use teslausb_core::fs::geometry::{Geometry, SECTOR_SIZE_BYTES};

use super::dir_tree::{DirTreeError, DirTreeWriter};
use super::dirty_map::DirtyByteMap;

/// `exFAT` FAT entry width (spec §4.1).
const FAT_ENTRY_SIZE_BYTES: usize = 4;
/// End-of-chain marker (spec §4.1).
const EXFAT_END_OF_CHAIN: u32 = 0xFFFF_FFFF;
/// Bad-cluster marker (spec §4.1).
const EXFAT_BAD_CLUSTER: u32 = 0xFFFF_FFF7;
/// Maximum chain length the walker will follow before declaring
/// the chain corrupt. Same cap as the FAT32 walker (Phase 3.5b).
const MAX_CHAIN_LENGTH: usize = 1_048_576;

/// Errors returned by [`ExfatWriteState::apply_write`] and
/// [`ExfatWriteState::flush`].
///
/// Every variant is recoverable from the daemon's point of view —
/// we surface them to the NBD client as `BackendError::Io`, and
/// the client either retries or marks the volume dirty. None of
/// these abort the daemon.
#[derive(Debug, thiserror::Error)]
pub enum ExfatWriteError {
    /// The geometry rejected the write coordinates (out of bounds
    /// or unsupported region).
    #[error("decode_write rejected the write: {0}")]
    Decode(#[from] DecodeWriteError),
    /// Decoding a directory cluster's entries failed.
    #[error("directory entry decode failed: {0}")]
    DirDecode(#[from] ExfatDirDecodeError),
    /// Inserting an extent into the cluster map failed (overlap
    /// with an existing extent — typically a sign of cluster
    /// reuse without a preceding delete).
    #[error("cluster map insert failed: {0}")]
    ClusterMap(#[from] ClusterMapError),
    /// A `DirTreeWriter` operation failed.
    #[error("dir tree writer failed: {0}")]
    DirTree(#[from] DirTreeError),
    /// Walking a FAT cluster chain failed.
    #[error("FAT chain walk failed: {0}")]
    Chain(&'static str),
}

impl From<ExfatWriteError> for BackendError {
    fn from(err: ExfatWriteError) -> Self {
        BackendError::Io(std::io::Error::other(format!("exfat write: {err}")))
    }
}

/// Per-directory accumulated state. One per dir-first-cluster.
///
/// Mirror of FAT32's `DirectoryState`; differs in the dir-entry
/// decoder carry type (`PartialEntrySet` instead of `Vec<LfnEntry>`).
#[derive(Debug)]
struct DirectoryState {
    /// Cluster chain in FAT order. Initially `[first_cluster]`
    /// for any directory; extended when FAT writes resolve a
    /// longer chain.
    chain: Vec<u32>,
    /// Concatenated cluster bytes, length =
    /// `chain.len() * bytes_per_cluster`.
    buffer: Vec<u8>,
    /// Tracks which bytes of `buffer` the kernel has written.
    /// Used by the read overlay (Phase 3.5f) so the synth's
    /// pre-existing directory entries (which we never copy into
    /// `buffer` — it starts zero) are preserved while
    /// kernel-written entries are correctly returned on read.
    dirty_buffer: DirtyByteMap,
    /// Parent path relative to backing root. Root directory's
    /// parent is `""`.
    parent_path: PathBuf,
    /// Last-decoded `(first_cluster, relative_path)` map. Used
    /// to diff against the next decode to detect deletions.
    registered_children: HashMap<u32, PathBuf>,
}

impl DirectoryState {
    fn new(parent_path: PathBuf) -> Self {
        Self {
            chain: Vec::new(),
            buffer: Vec::new(),
            dirty_buffer: DirtyByteMap::new(),
            parent_path,
            registered_children: HashMap::new(),
        }
    }
}

/// A pending dir entry awaiting its cluster chain before we can
/// register a `FileExtent`.
#[derive(Debug)]
struct PendingFile {
    relative_path: PathBuf,
    data_length: u64,
    is_directory: bool,
    no_fat_chain: bool,
    /// `cluster_count = ceil(data_length / bytes_per_cluster)`.
    /// Cached so we can route an extent without re-deriving it
    /// at every resolution attempt.
    cluster_count: u32,
    /// Set of parent dir-first-cluster IDs currently claiming
    /// this child. Used to detect deletions.
    parents: HashSet<u32>,
}

/// One stashed data-cluster write that arrived before the
/// cluster's owning file was known.
#[derive(Debug)]
struct PendingDataChunk {
    byte_in_cluster: usize,
    bytes: Vec<u8>,
}

/// `exFAT` write-side state machine. See module-level docs.
#[derive(Debug)]
pub struct ExfatWriteState {
    geometry: ExfatGeometry,
    dir_tree: DirTreeWriter,
    bytes_per_cluster: u32,
    fat_size_bytes: usize,

    fat: Vec<u8>,
    /// Tracks which bytes of `fat` the kernel has written.
    /// Used by the read overlay (Phase 3.5f); see [`DirtyByteMap`].
    dirty_fat: DirtyByteMap,
    cluster_map: ClusterMap,
    /// `first_cluster -> DirectoryState` for every known directory.
    directories: HashMap<u32, DirectoryState>,
    /// Reverse lookup `cluster_number -> dir_first_cluster` so a
    /// data-cluster write can find its owning directory in O(1).
    cluster_to_directory: HashMap<u32, u32>,
    /// `first_cluster -> PendingFile` for entries we've decoded
    /// but whose FAT chain hasn't fully resolved yet.
    pending_files: HashMap<u32, PendingFile>,
    /// `cluster_number -> queued data chunks` for writes that
    /// arrived before the cluster's owner was known.
    pending_data: HashMap<u32, Vec<PendingDataChunk>>,
    /// Relative paths currently in `.partial` waiting on flush.
    in_flight_files: HashSet<PathBuf>,
    /// Relative paths the caller seeded as already-existing on
    /// the backing tree at construction time.
    pre_existing_files: HashSet<PathBuf>,
    /// File sizes recorded by directory entries (live or
    /// pre-existing). `flush()` truncates each finalized file
    /// to this size so cluster-tail padding doesn't leak into
    /// the backing tree.
    recorded_file_sizes: HashMap<PathBuf, u64>,

    /// First cluster of the allocation bitmap stream (set by
    /// [`Self::with_allocation_bitmap`]; `0` means no bitmap
    /// tracking is configured — used by unit tests that don't
    /// need the overlay).
    bitmap_first_cluster: u32,
    /// In-memory mirror of the allocation bitmap. Sized exactly
    /// to `bitmap_cluster_count * bytes_per_cluster`. Kernel
    /// writes to bitmap clusters land here; the read overlay
    /// surfaces only the byte ranges marked dirty in
    /// `dirty_bitmap` (Phase 3.5g — Bug H3-2 part 2).
    bitmap_buf: Vec<u8>,
    /// Dirty-byte tracker for `bitmap_buf` (indices are
    /// `bitmap_buf` byte offsets).
    dirty_bitmap: DirtyByteMap,
}

/// Describes a pre-existing file extent the [`ExfatWriteState`]
/// should seed into its cluster map at construction time.
///
/// Same role as [`super::fat32_write::PreExistingExtent`]; kept
/// as a distinct type so the two state machines stay
/// independently evolvable.
#[derive(Debug, Clone)]
pub struct PreExistingExfatExtent {
    /// First cluster of the extent.
    pub first_cluster: u32,
    /// Number of contiguous clusters.
    pub cluster_count: u32,
    /// Byte offset within the file at which this extent starts.
    pub first_byte_in_file: u64,
    /// Total file size in bytes.
    pub file_size_bytes: u64,
    /// Path of the backing file, relative to the backing root.
    pub relative_path: PathBuf,
}

impl ExfatWriteState {
    /// Build a fresh state machine for an `exFAT` volume of
    /// `geometry`, routing writes through `dir_tree`.
    ///
    /// `pre_existing_extents` describes every cluster extent
    /// owned by a file that already lives on the backing tree
    /// at startup (as planned by the Phase 2 layout). The writer
    /// seeds its cluster map from these extents so that
    /// in-place rewrites — Tesla writes a fresh data cluster
    /// without re-issuing the directory entry — route to the
    /// correct backing file instead of being orphaned.
    ///
    /// Initializes the root directory at
    /// `geometry.first_root_directory_cluster()` (single
    /// cluster); subdirectory chains are discovered dynamically
    /// as their dir-entry arrives.
    #[must_use]
    pub fn new(
        geometry: ExfatGeometry,
        dir_tree: DirTreeWriter,
        pre_existing_extents: &[PreExistingExfatExtent],
    ) -> Self {
        let bytes_per_cluster = geometry.bytes_per_cluster();
        let fat_size_bytes =
            (geometry.fat_length_sectors() as usize) * (SECTOR_SIZE_BYTES as usize);
        let root_cluster = geometry.first_root_directory_cluster();
        let mut pre_existing_files: HashSet<PathBuf> = HashSet::new();
        let mut recorded_file_sizes: HashMap<PathBuf, u64> = HashMap::new();
        let mut cluster_map = ClusterMap::new();
        for extent in pre_existing_extents {
            pre_existing_files.insert(extent.relative_path.clone());
            recorded_file_sizes.insert(extent.relative_path.clone(), extent.file_size_bytes);
            let file_extent = FileExtent {
                first_cluster: extent.first_cluster,
                cluster_count: extent.cluster_count,
                first_byte_in_file: extent.first_byte_in_file,
                file_path: extent.relative_path.clone(),
            };
            if let Err(err) = cluster_map.insert(file_extent) {
                tracing::warn!(
                    ?err,
                    path = %extent.relative_path.display(),
                    "skipping pre-existing extent that overlaps an earlier one"
                );
            }
        }

        let mut state = Self {
            geometry,
            dir_tree,
            bytes_per_cluster,
            fat_size_bytes,
            fat: vec![0u8; fat_size_bytes],
            dirty_fat: DirtyByteMap::new(),
            cluster_map,
            directories: HashMap::new(),
            cluster_to_directory: HashMap::new(),
            pending_files: HashMap::new(),
            pending_data: HashMap::new(),
            in_flight_files: HashSet::new(),
            pre_existing_files,
            recorded_file_sizes,
            bitmap_first_cluster: 0,
            bitmap_buf: Vec::new(),
            dirty_bitmap: DirtyByteMap::new(),
        };
        // Bootstrap the root directory with a single-cluster
        // chain. A FAT write that extends it will rebuild the
        // chain via resolve_after_fat_change.
        let mut root = DirectoryState::new(PathBuf::new());
        root.chain = vec![root_cluster];
        root.buffer = vec![0u8; bytes_per_cluster as usize];
        state.directories.insert(root_cluster, root);
        state
            .cluster_to_directory
            .insert(root_cluster, root_cluster);
        state
    }

    /// Configure the in-memory allocation bitmap mirror that the
    /// read overlay surfaces after the kernel updates cluster
    /// allocations. Must be called immediately after [`Self::new`]
    /// by the production path; tests that don't exercise the
    /// remount path can skip this and the overlay stays disabled.
    ///
    /// `bitmap_first_cluster` and `bitmap_cluster_count` must match
    /// the values the synth surfaces in the allocation-bitmap
    /// directory entry (see `ExfatSynth::bitmap_first_cluster`).
    #[must_use]
    pub fn with_allocation_bitmap(
        mut self,
        bitmap_first_cluster: u32,
        bitmap_cluster_count: u32,
    ) -> Self {
        self.bitmap_first_cluster = bitmap_first_cluster;
        let size = (bitmap_cluster_count as usize) * (self.bytes_per_cluster as usize);
        self.bitmap_buf = vec![0u8; size];
        self.dirty_bitmap = DirtyByteMap::new();
        self
    }

    /// Apply one kernel-issued write to the state machine.
    ///
    /// # Errors
    ///
    /// See [`ExfatWriteError`].
    pub fn apply_write(&mut self, offset: u64, bytes: &[u8]) -> Result<(), ExfatWriteError> {
        if bytes.is_empty() {
            return Ok(());
        }
        let chunks = decode_write(&self.geometry, offset, bytes)?;
        for chunk in chunks {
            self.dispatch_chunk(chunk)?;
        }
        Ok(())
    }

    // DecodedWrite is a small enum (≤4 words) consumed by
    // destructuring; passing by reference would require an
    // extra layer of pattern indirection and gain nothing.
    #[allow(clippy::needless_pass_by_value)]
    fn dispatch_chunk(&mut self, chunk: DecodedWrite<'_>) -> Result<(), ExfatWriteError> {
        match chunk {
            DecodedWrite::MainBootRegion { .. }
            | DecodedWrite::BackupBootRegion { .. }
            | DecodedWrite::Reserved { .. } => {
                // Metadata for which we are the source of truth.
                tracing::trace!(?chunk, "metadata write swallowed");
                Ok(())
            }
            DecodedWrite::FatTable {
                byte_in_fat, bytes, ..
            } => self.apply_fat_table_write(byte_in_fat, bytes),
            DecodedWrite::DataCluster {
                cluster_number,
                byte_in_cluster,
                bytes,
            } => self.apply_data_cluster_write(cluster_number, byte_in_cluster, bytes),
        }
    }

    fn apply_fat_table_write(
        &mut self,
        byte_in_fat: usize,
        bytes: &[u8],
    ) -> Result<(), ExfatWriteError> {
        let end = byte_in_fat.saturating_add(bytes.len());
        if end > self.fat.len() {
            tracing::warn!(
                byte_in_fat,
                len = bytes.len(),
                fat_len = self.fat.len(),
                "FAT table write past end of FAT; truncating"
            );
        }
        let copy_end = end.min(self.fat.len());
        let copy_len = copy_end.saturating_sub(byte_in_fat);
        if copy_len > 0 {
            if let (Some(dest), Some(src)) = (
                self.fat.get_mut(byte_in_fat..copy_end),
                bytes.get(..copy_len),
            ) {
                dest.copy_from_slice(src);
            }
            self.dirty_fat.mark(byte_in_fat, copy_len);
        }
        self.resolve_after_fat_change()?;
        Ok(())
    }

    fn apply_data_cluster_write(
        &mut self,
        cluster_number: u32,
        byte_in_cluster: usize,
        bytes: &[u8],
    ) -> Result<(), ExfatWriteError> {
        if cluster_number < FIRST_DATA_CLUSTER {
            tracing::warn!(cluster_number, "data write to reserved cluster ignored");
            return Ok(());
        }
        // Allocation bitmap takes priority — the kernel writes to
        // it to mark/clear allocated clusters. Without overlaying
        // these writes, a remount would see the synth's startup
        // snapshot (new file's clusters appear FREE) while the
        // dir entry claims them allocated — Linux exFAT driver
        // rejects this inconsistency with EIO on stat
        // (Bug H3-2, hardware-observed 2026-05-20).
        if self.bitmap_first_cluster != 0
            && cluster_number >= self.bitmap_first_cluster
            && !self.bitmap_buf.is_empty()
        {
            let bpc = self.bytes_per_cluster as usize;
            let bitmap_cluster_offset = (cluster_number - self.bitmap_first_cluster) as usize * bpc;
            let dst_start = bitmap_cluster_offset.saturating_add(byte_in_cluster);
            if dst_start < self.bitmap_buf.len() {
                let avail = self.bitmap_buf.len() - dst_start;
                let copy_len = bytes.len().min(avail);
                if copy_len > 0 {
                    if let (Some(dst), Some(src)) = (
                        self.bitmap_buf.get_mut(dst_start..dst_start + copy_len),
                        bytes.get(..copy_len),
                    ) {
                        dst.copy_from_slice(src);
                        self.dirty_bitmap.mark(dst_start, copy_len);
                    }
                }
                return Ok(());
            }
        }
        // Directory clusters take priority — they may carry dir
        // entries that we need to decode before we can route any
        // queued data to the right file.
        if let Some(&dir_first) = self.cluster_to_directory.get(&cluster_number) {
            self.apply_directory_cluster_write(dir_first, cluster_number, byte_in_cluster, bytes)?;
            return Ok(());
        }
        // File-data path: look up the cluster in the cluster_map.
        if let Some(lookup) = self.cluster_map.lookup(cluster_number) {
            let byte_in_file = lookup
                .byte_in_file_at_cluster_start(self.bytes_per_cluster)
                .saturating_add(byte_in_cluster as u64);
            let extent = lookup.extent.clone();
            self.route_to_file(&extent.file_path, byte_in_file, bytes)?;
            return Ok(());
        }
        // Unknown cluster — stash for later reconciliation.
        self.pending_data
            .entry(cluster_number)
            .or_default()
            .push(PendingDataChunk {
                byte_in_cluster,
                bytes: bytes.to_vec(),
            });
        Ok(())
    }

    fn apply_directory_cluster_write(
        &mut self,
        dir_first: u32,
        cluster_number: u32,
        byte_in_cluster: usize,
        bytes: &[u8],
    ) -> Result<(), ExfatWriteError> {
        let bytes_per_cluster_usize = self.bytes_per_cluster as usize;
        let Some(dir_state) = self.directories.get_mut(&dir_first) else {
            return Ok(());
        };
        let Some(chain_index) = dir_state.chain.iter().position(|&c| c == cluster_number) else {
            return Ok(());
        };
        let buffer_offset = chain_index.saturating_mul(bytes_per_cluster_usize);
        let copy_end = byte_in_cluster.saturating_add(bytes.len());
        let cluster_end = bytes_per_cluster_usize.min(copy_end);
        let copy_len = cluster_end.saturating_sub(byte_in_cluster);
        if copy_len == 0 {
            return Ok(());
        }
        let dst_start = buffer_offset.saturating_add(byte_in_cluster);
        let dst_end = dst_start.saturating_add(copy_len);
        if dst_end > dir_state.buffer.len() {
            tracing::warn!(
                dir_first,
                cluster_number,
                dst_end,
                buf_len = dir_state.buffer.len(),
                "directory cluster write out of buffer range"
            );
            return Ok(());
        }
        if let (Some(dst), Some(src)) = (
            dir_state.buffer.get_mut(dst_start..dst_end),
            bytes.get(..copy_len),
        ) {
            dst.copy_from_slice(src);
        }
        dir_state.dirty_buffer.mark(dst_start, copy_len);
        self.redecode_directory(dir_first)?;
        Ok(())
    }

    fn route_to_file(
        &mut self,
        relative_path: &PathBuf,
        byte_in_file: u64,
        bytes: &[u8],
    ) -> Result<(), ExfatWriteError> {
        if bytes.is_empty() {
            return Ok(());
        }
        if self.pre_existing_files.contains(relative_path)
            && !self.in_flight_files.contains(relative_path)
        {
            self.dir_tree.seed_partial_from_target(relative_path)?;
        }
        self.dir_tree
            .apply_chunk(relative_path, byte_in_file, bytes)?;
        self.in_flight_files.insert(relative_path.clone());
        Ok(())
    }

    fn redecode_directory(&mut self, dir_first: u32) -> Result<(), ExfatWriteError> {
        let (buffer_clone, parent_path) = {
            let Some(dir_state) = self.directories.get(&dir_first) else {
                return Ok(());
            };
            (dir_state.buffer.clone(), dir_state.parent_path.clone())
        };
        if buffer_clone.is_empty() {
            return Ok(());
        }
        let decode_result: ExfatDecodeResult =
            dir_decode::decode_directory_cluster(&buffer_clone, None::<PartialEntrySet>)?;

        // Build the new (first_cluster -> (path, size, is_dir,
        // no_fat_chain)) map for this directory.
        let mut new_children: HashMap<u32, (PathBuf, u64, bool, bool)> = HashMap::new();
        for entry in &decode_result.entries {
            if let DecodedExfatEntry::File {
                name,
                attributes,
                first_cluster,
                data_length,
                no_fat_chain,
                ..
            } = entry
            {
                if *first_cluster < FIRST_DATA_CLUSTER {
                    continue;
                }
                let Some(name) = name.as_deref() else {
                    // Name failed to decode; skip rather than
                    // route to a path we can't reproduce.
                    tracing::warn!(
                        first_cluster,
                        ?parent_path,
                        "skipping File entry with non-UTF8 name"
                    );
                    continue;
                };
                let relative = parent_path.join(name);
                let is_dir = attributes.directory;
                new_children.insert(
                    *first_cluster,
                    (relative, *data_length, is_dir, *no_fat_chain),
                );
            }
            // VolumeLabel / AllocationBitmap / UpcaseTable /
            // DeletedFile / Malformed do not participate in
            // child-routing here. Deletions are handled by the
            // diff against registered_children below.
        }

        // Diff against last registration to compute additions +
        // deletions.
        let previous: HashMap<u32, PathBuf> = self
            .directories
            .get(&dir_first)
            .map(|d| d.registered_children.clone())
            .unwrap_or_default();
        for (cluster, prev_path) in &previous {
            if !new_children.contains_key(cluster) {
                self.handle_child_deleted(dir_first, *cluster, prev_path);
            }
        }
        for (&first_cluster, (relative_path, data_length, is_directory, no_fat_chain)) in
            &new_children
        {
            self.handle_child_seen(
                dir_first,
                first_cluster,
                relative_path,
                *data_length,
                *is_directory,
                *no_fat_chain,
            )?;
        }

        // Persist the new registration for the next diff.
        if let Some(dir_state) = self.directories.get_mut(&dir_first) {
            dir_state.registered_children = new_children
                .into_iter()
                .map(|(cluster, (path, _, _, _))| (cluster, path))
                .collect();
        }
        Ok(())
    }

    fn handle_child_seen(
        &mut self,
        parent_dir: u32,
        first_cluster: u32,
        relative_path: &Path,
        data_length: u64,
        is_directory: bool,
        no_fat_chain: bool,
    ) -> Result<(), ExfatWriteError> {
        let cluster_count = self.clusters_for_data_length(data_length);
        let pending = self
            .pending_files
            .entry(first_cluster)
            .or_insert_with(|| PendingFile {
                relative_path: relative_path.to_path_buf(),
                data_length,
                is_directory,
                no_fat_chain,
                cluster_count,
                parents: HashSet::new(),
            });
        pending.relative_path = relative_path.to_path_buf();
        pending.data_length = data_length;
        pending.is_directory = is_directory;
        pending.no_fat_chain = no_fat_chain;
        pending.cluster_count = cluster_count;
        pending.parents.insert(parent_dir);

        if is_directory {
            self.ensure_directory_registered(first_cluster, relative_path.to_path_buf())?;
        } else {
            self.try_resolve_file(first_cluster)?;
        }
        Ok(())
    }

    fn handle_child_deleted(
        &mut self,
        parent_dir: u32,
        first_cluster: u32,
        previous_path: &PathBuf,
    ) {
        let still_referenced = match self.pending_files.get_mut(&first_cluster) {
            Some(p) => {
                p.parents.remove(&parent_dir);
                !p.parents.is_empty()
            }
            None => false,
        };
        if still_referenced {
            return;
        }
        self.pending_files.remove(&first_cluster);
        let removed = self.cluster_map.remove_at(first_cluster);
        if removed.is_some() {
            tracing::debug!(
                first_cluster,
                ?previous_path,
                "child deleted; freed cluster map extent"
            );
        }
        let _ = self.dir_tree.discard(previous_path);
        let _ = self.dir_tree.unlink(previous_path);
        self.in_flight_files.remove(previous_path);
    }

    fn ensure_directory_registered(
        &mut self,
        first_cluster: u32,
        relative_path: PathBuf,
    ) -> Result<(), ExfatWriteError> {
        if self.directories.contains_key(&first_cluster) {
            return Ok(());
        }
        let mut dir_state = DirectoryState::new(relative_path);
        if let Some(chain_vec) = self.try_walk_chain(first_cluster) {
            self.adopt_directory_chain(&mut dir_state, first_cluster, &chain_vec);
        } else {
            dir_state.chain = vec![first_cluster];
            dir_state.buffer = vec![0u8; self.bytes_per_cluster as usize];
            self.cluster_to_directory
                .insert(first_cluster, first_cluster);
        }
        self.directories.insert(first_cluster, dir_state);
        self.replay_pending_data_for_directory(first_cluster)?;
        Ok(())
    }

    fn adopt_directory_chain(
        &mut self,
        dir_state: &mut DirectoryState,
        first_cluster: u32,
        chain_vec: &[u32],
    ) {
        let bytes_per_cluster_usize = self.bytes_per_cluster as usize;
        dir_state.chain = chain_vec.to_vec();
        dir_state.buffer = vec![0u8; chain_vec.len() * bytes_per_cluster_usize];
        for &cluster in chain_vec {
            self.cluster_to_directory.insert(cluster, first_cluster);
        }
    }

    fn try_resolve_file(&mut self, first_cluster: u32) -> Result<(), ExfatWriteError> {
        let (relative_path, data_length, no_fat_chain, cluster_count) =
            match self.pending_files.get(&first_cluster) {
                Some(p) if !p.is_directory => (
                    p.relative_path.clone(),
                    p.data_length,
                    p.no_fat_chain,
                    p.cluster_count,
                ),
                _ => return Ok(()),
            };
        if cluster_count == 0 {
            // Empty file: no extent to register. Still record
            // the size so flush() truncates it to 0.
            self.recorded_file_sizes
                .insert(relative_path.clone(), data_length);
            return Ok(());
        }
        let chain_vec = if no_fat_chain {
            // Contiguous: synthesize the extent without touching
            // the FAT.
            (first_cluster..first_cluster.saturating_add(cluster_count)).collect::<Vec<_>>()
        } else {
            let Some(c) = self.try_walk_chain(first_cluster) else {
                return Ok(());
            };
            c
        };
        if let Some(existing) = self.cluster_map.lookup(first_cluster) {
            if existing.extent.first_cluster == first_cluster
                && existing.extent.file_path != relative_path
            {
                self.cluster_map.remove_at(first_cluster);
            }
        }
        let extents = chain_to_extents(&chain_vec, relative_path.clone(), self.bytes_per_cluster);

        // Phase 3.5f (Bug H3-1): replace all extents owned by this
        // path before inserting the new chain. Without this, when
        // the kernel writes the directory entry incrementally
        // (e.g., first writes a short `data_length`, then later
        // updates it to the final size), the earlier short extents
        // would still satisfy `lookup(first_cluster)` for the same
        // path and the new larger extents would be silently skipped
        // by the idempotent-insert path. The result was that
        // data-cluster writes for the tail of the file fell into
        // `pending_data` forever, producing zero-filled gaps on
        // the backing file (hardware-observed at the 31 MB mark
        // on a 50 MB copy on 2026-05-20).
        let removed = self.cluster_map.remove_file(relative_path.as_path());
        if removed > 0 {
            tracing::debug!(
                first_cluster,
                ?relative_path,
                removed_extent_count = removed,
                new_extent_count = extents.len(),
                "replacing stale extents for updated dir entry"
            );
        }
        for extent in extents {
            match self.cluster_map.insert(extent.clone()) {
                Ok(()) => {}
                Err(ClusterMapError::Overlap { .. }) => {
                    tracing::warn!(
                        first_cluster = extent.first_cluster,
                        cluster_count = extent.cluster_count,
                        ?relative_path,
                        "cluster map insert overlaps a different owner; skipping"
                    );
                }
                Err(other) => return Err(other.into()),
            }
        }
        self.recorded_file_sizes
            .insert(relative_path.clone(), data_length);

        for &cluster in &chain_vec {
            self.replay_pending_data_for_cluster(cluster)?;
        }
        Ok(())
    }

    fn try_walk_chain(&self, first_cluster: u32) -> Option<Vec<u32>> {
        walk_chain(&self.fat, first_cluster)
    }

    fn replay_pending_data_for_cluster(
        &mut self,
        cluster_number: u32,
    ) -> Result<(), ExfatWriteError> {
        let Some(chunks) = self.pending_data.remove(&cluster_number) else {
            return Ok(());
        };
        for chunk in chunks {
            self.apply_data_cluster_write(cluster_number, chunk.byte_in_cluster, &chunk.bytes)?;
        }
        Ok(())
    }

    fn replay_pending_data_for_directory(&mut self, dir_first: u32) -> Result<(), ExfatWriteError> {
        let clusters: Vec<u32> = self
            .directories
            .get(&dir_first)
            .map(|d| d.chain.clone())
            .unwrap_or_default();
        for cluster in clusters {
            self.replay_pending_data_for_cluster(cluster)?;
        }
        Ok(())
    }

    /// After a FAT write, re-walk every pending file and
    /// directory chain that hadn't fully resolved yet.
    fn resolve_after_fat_change(&mut self) -> Result<(), ExfatWriteError> {
        // 1) Directories: extend chain if FAT now reaches further.
        let dir_first_clusters: Vec<u32> = self.directories.keys().copied().collect();
        for dir_first in dir_first_clusters {
            if let Some(chain_vec) = self.try_walk_chain(dir_first) {
                let mut new_clusters: Vec<u32> = Vec::new();
                let mut buf_resize: Option<usize> = None;
                if let Some(dir_state) = self.directories.get(&dir_first) {
                    if chain_vec != dir_state.chain {
                        for c in &chain_vec {
                            if !dir_state.chain.contains(c) {
                                new_clusters.push(*c);
                            }
                        }
                        buf_resize = Some(chain_vec.len() * (self.bytes_per_cluster as usize));
                    }
                }
                if let Some(new_size) = buf_resize {
                    if let Some(dir_state) = self.directories.get_mut(&dir_first) {
                        dir_state.chain.clone_from(&chain_vec);
                        dir_state.buffer.resize(new_size, 0);
                    }
                    for cluster in &new_clusters {
                        self.cluster_to_directory.insert(*cluster, dir_first);
                    }
                    for cluster in new_clusters {
                        self.replay_pending_data_for_cluster(cluster)?;
                    }
                    self.redecode_directory(dir_first)?;
                }
            }
        }

        // 2) Pending files: try to resolve their chain now.
        let pending_keys: Vec<u32> = self.pending_files.keys().copied().collect();
        for first_cluster in pending_keys {
            let needs_walk = self
                .pending_files
                .get(&first_cluster)
                .is_some_and(|p| !p.is_directory && !p.no_fat_chain);
            if needs_walk {
                self.try_resolve_file(first_cluster)?;
            }
        }
        Ok(())
    }

    /// Finalize every in-flight file by renaming
    /// `<path>.partial → <path>`, replacing any pre-existing
    /// target. Truncates the finalized file to the dir entry's
    /// reported size when known.
    ///
    /// # Errors
    ///
    /// See [`ExfatWriteError`].
    pub fn flush(&mut self) -> Result<(), ExfatWriteError> {
        let paths: Vec<PathBuf> = self.in_flight_files.drain().collect();
        for path in paths {
            let truncate_size = self.find_recorded_size(&path);
            self.dir_tree.finalize_with_replace(&path)?;
            if let Some(size) = truncate_size {
                let absolute = self.dir_tree.backing_root().join(&path);
                if let Err(err) = std::fs::OpenOptions::new()
                    .write(true)
                    .open(&absolute)
                    .and_then(|f| f.set_len(size))
                {
                    tracing::warn!(?absolute, ?err, "post-finalize truncate failed");
                }
            }
            self.pre_existing_files.insert(path);
        }
        Ok(())
    }

    fn find_recorded_size(&self, relative_path: &Path) -> Option<u64> {
        if let Some(size) = self.recorded_file_sizes.get(relative_path) {
            return Some(*size);
        }
        for pending in self.pending_files.values() {
            if !pending.is_directory && pending.relative_path.as_path() == relative_path {
                return Some(pending.data_length);
            }
        }
        None
    }

    fn clusters_for_data_length(&self, data_length: u64) -> u32 {
        if data_length == 0 {
            return 0;
        }
        let bpc = u64::from(self.bytes_per_cluster);
        let total = data_length.div_ceil(bpc);
        u32::try_from(total).unwrap_or(u32::MAX)
    }

    // === Test / diagnostic introspection ===

    /// Number of file extents currently tracked in the cluster map.
    #[must_use]
    pub fn extent_count(&self) -> usize {
        self.cluster_map.len()
    }

    /// Number of directories currently tracked.
    #[must_use]
    pub fn directory_count(&self) -> usize {
        self.directories.len()
    }

    /// Number of files in `.partial` waiting on flush.
    #[must_use]
    pub fn in_flight_file_count(&self) -> usize {
        self.in_flight_files.len()
    }

    /// FAT size in bytes (cached).
    #[must_use]
    pub fn fat_size_bytes(&self) -> usize {
        self.fat_size_bytes
    }

    /// Overlay any in-memory write-state updates (kernel-written
    /// FAT entries and directory cluster bytes) onto the bytes
    /// the synth produced for a read of `[offset, offset+buf.len())`
    /// in the volume.
    ///
    /// `SynthBackend::read_sync` calls this AFTER the synth's
    /// own read and the `file_extents` content overlay. The
    /// synth's layout is a snapshot at daemon startup — without
    /// this third overlay, FAT entries the kernel wrote (for a
    /// new file's cluster chain) and directory entries the
    /// kernel wrote (for the new file's `File`/`Stream`/`Name`
    /// entry set) would not be visible to subsequent reads, and
    /// on umount/remount the kernel would see only the original
    /// pre-existing files (Bug H3-2, observed 2026-05-20 on
    /// hardware: a 50 MB file written through Phase 3.5e
    /// appeared in the backing tree but was invisible to a
    /// fresh exFAT mount).
    ///
    /// Only bytes the kernel actually wrote (tracked via
    /// [`DirtyByteMap`]) are overlaid. The pre-existing FAT
    /// chain entries and directory entries that the synth
    /// produces remain intact.
    pub fn overlay_read(&self, offset: u64, buf: &mut [u8]) {
        if buf.is_empty() {
            return;
        }
        let sector = u64::from(SECTOR_SIZE_BYTES);
        // FAT region overlay.
        let fat_start = u64::from(self.geometry.fat_offset_sectors()).saturating_mul(sector);
        overlay_region_with_base(offset, buf, fat_start, &self.fat, &self.dirty_fat, 0);
        // Per-directory cluster overlays.
        let cluster_heap_start =
            u64::from(self.geometry.cluster_heap_offset_sectors()).saturating_mul(sector);
        let bpc = u64::from(self.bytes_per_cluster);
        let bpc_usize = self.bytes_per_cluster as usize;
        for dir_state in self.directories.values() {
            for (chain_idx, &cluster) in dir_state.chain.iter().enumerate() {
                if cluster < FIRST_DATA_CLUSTER {
                    continue;
                }
                let cluster_offset_in_heap = u64::from(cluster - FIRST_DATA_CLUSTER) * bpc;
                let cluster_start = cluster_heap_start.saturating_add(cluster_offset_in_heap);
                // Slice of the buffer for this single cluster.
                let buf_cluster_start = chain_idx.saturating_mul(bpc_usize);
                let buf_cluster_end = buf_cluster_start.saturating_add(bpc_usize);
                let Some(cluster_bytes) = dir_state.buffer.get(buf_cluster_start..buf_cluster_end)
                else {
                    continue;
                };
                // Dirty map is keyed in `buffer` coordinates;
                // shift to per-cluster coordinates by passing the
                // cluster's buf_cluster_start as base.
                overlay_region_with_base(
                    offset,
                    buf,
                    cluster_start,
                    cluster_bytes,
                    &dir_state.dirty_buffer,
                    buf_cluster_start,
                );
            }
        }

        // Phase 3.5g (Bug H3-2 part 2): overlay allocation bitmap
        // writes. Without this, the synth's startup snapshot
        // shows the new file's clusters as FREE while the dir
        // entry claims them allocated, and the Linux exFAT driver
        // rejects the inconsistency with EIO on stat.
        if self.bitmap_first_cluster != 0 && !self.bitmap_buf.is_empty() {
            let bitmap_first_byte = cluster_heap_start
                .saturating_add(u64::from(self.bitmap_first_cluster - FIRST_DATA_CLUSTER) * bpc);
            overlay_region_with_base(
                offset,
                buf,
                bitmap_first_byte,
                &self.bitmap_buf,
                &self.dirty_bitmap,
                0,
            );
        }

        // Phase 3.5f addendum: overlay file data clusters from
        // the backing tree. After a file is finalized to the
        // backing tree, its bytes are NOT in `synth.file_extents`
        // (that snapshot is captured at startup). Without this
        // overlay, a kernel read of the file's data clusters
        // after umount/remount would return zeros even though
        // the directory entry and FAT chain are visible via the
        // dirty-byte overlay above. The cluster_map already
        // tracks extent → file_path; this step turns that into
        // actual byte reads.
        overlay_data_clusters_from_cluster_map(
            offset,
            buf,
            &self.cluster_map,
            cluster_heap_start,
            bpc,
            self.dir_tree.backing_root(),
        );
    }
}

#[allow(clippy::cast_possible_truncation)]
fn overlay_region_with_base(
    read_offset: u64,
    read_buf: &mut [u8],
    region_start: u64,
    source: &[u8],
    dirty_map: &DirtyByteMap,
    dirty_map_base: usize,
) {
    let read_end = read_offset.saturating_add(read_buf.len() as u64);
    let region_end = region_start.saturating_add(source.len() as u64);
    if read_end <= region_start || read_offset >= region_end {
        return;
    }
    let overlap_start = read_offset.max(region_start);
    let overlap_end = read_end.min(region_end);
    if overlap_end <= overlap_start {
        return;
    }
    // Cast safety: `overlap_start - region_start` < `source.len()` (usize),
    // `overlap_end - overlap_start` <= `read_buf.len()` (usize).
    let source_base = (overlap_start - region_start) as usize;
    let overlap_len = (overlap_end - overlap_start) as usize;
    let dirty_start = dirty_map_base.saturating_add(source_base);
    dirty_map.for_each_overlap(dirty_start, overlap_len, |d_start, d_end| {
        let dirty_len = d_end - d_start;
        let src_off = d_start - dirty_map_base;
        // Cast safety: dst_off < read_buf.len() (usize) by construction.
        let dst_off = (region_start + src_off as u64 - read_offset) as usize;
        if let (Some(dst), Some(src)) = (
            read_buf.get_mut(dst_off..dst_off + dirty_len),
            source.get(src_off..src_off + dirty_len),
        ) {
            dst.copy_from_slice(src);
        }
    });
}

/// Overlay file data clusters from the backing tree onto a
/// read buffer. For each extent in `cluster_map` that overlaps
/// `[read_offset, read_offset+read_buf.len())`, opens the file
/// at `backing_root.join(extent.file_path)`, seeks to the
/// correct in-file offset, and reads the overlapping bytes
/// into `read_buf`. Read errors (e.g., file deleted, ENOENT)
/// are logged at debug level and skipped — the synth will
/// continue to return zeros for that region.
///
/// `data_region_start_bytes` is the volume-byte offset where
/// the cluster heap (or data region for FAT32) begins.
/// `bytes_per_cluster` must match what the `cluster_map` was
/// populated with.
#[allow(clippy::cast_possible_truncation)]
pub(super) fn overlay_data_clusters_from_cluster_map(
    read_offset: u64,
    read_buf: &mut [u8],
    cluster_map: &teslausb_core::fs::cluster_map::ClusterMap,
    data_region_start_bytes: u64,
    bytes_per_cluster: u64,
    backing_root: &std::path::Path,
) {
    use std::io::{Read, Seek, SeekFrom};
    if read_buf.is_empty() {
        return;
    }
    let read_end = read_offset.saturating_add(read_buf.len() as u64);
    for extent in cluster_map.extents() {
        let extent_first_byte = data_region_start_bytes.saturating_add(
            u64::from(extent.first_cluster.saturating_sub(FIRST_DATA_CLUSTER)) * bytes_per_cluster,
        );
        let extent_byte_len = u64::from(extent.cluster_count) * bytes_per_cluster;
        let extent_end_byte = extent_first_byte.saturating_add(extent_byte_len);
        if extent_end_byte <= read_offset || extent_first_byte >= read_end {
            continue;
        }
        let overlap_start = read_offset.max(extent_first_byte);
        let overlap_end = read_end.min(extent_end_byte);
        if overlap_end <= overlap_start {
            continue;
        }
        let offset_in_extent = overlap_start - extent_first_byte;
        let offset_in_file = extent.first_byte_in_file.saturating_add(offset_in_extent);
        let overlap_len = (overlap_end - overlap_start) as usize;
        let dst_off = (overlap_start - read_offset) as usize;
        let path = backing_root.join(&extent.file_path);
        let Some(dst) = read_buf.get_mut(dst_off..dst_off + overlap_len) else {
            continue;
        };
        let Ok(mut f) = std::fs::File::open(&path) else {
            tracing::debug!(?path, "backing file unavailable for overlay read");
            continue;
        };
        if f.seek(SeekFrom::Start(offset_in_file)).is_err() {
            tracing::debug!(?path, offset_in_file, "seek failed for overlay read");
            continue;
        }
        if let Err(err) = f.read_exact(dst) {
            tracing::debug!(?path, ?err, "short read in overlay path");
        }
    }
}

// =====================================================================
// exFAT FAT chain walker
// =====================================================================

/// Walk a chain in an `exFAT` FAT buffer starting from
/// `first_cluster`. Returns `Some(chain)` on success, `None`
/// for any walk error (empty / out-of-range FAT, cycle, free
/// pointer, bad cluster) — same lenient behavior as the FAT32
/// walker.
fn walk_chain(fat: &[u8], first_cluster: u32) -> Option<Vec<u32>> {
    if first_cluster < FIRST_DATA_CLUSTER {
        return None;
    }
    let entry_byte = (first_cluster as usize).checked_mul(FAT_ENTRY_SIZE_BYTES)?;
    if entry_byte + FAT_ENTRY_SIZE_BYTES > fat.len() {
        return None;
    }

    let mut chain = Vec::new();
    let mut visited: HashSet<u32> = HashSet::new();
    let mut current = first_cluster;
    for _ in 0..MAX_CHAIN_LENGTH {
        if !visited.insert(current) {
            return None;
        }
        chain.push(current);
        let entry_byte = (current as usize).checked_mul(FAT_ENTRY_SIZE_BYTES)?;
        let next = read_fat_entry(fat, entry_byte)?;
        if next == EXFAT_END_OF_CHAIN {
            return Some(chain);
        }
        if next == EXFAT_BAD_CLUSTER || next < FIRST_DATA_CLUSTER {
            return None;
        }
        current = next;
    }
    None
}

fn read_fat_entry(fat: &[u8], byte_offset: usize) -> Option<u32> {
    let slice = fat.get(byte_offset..byte_offset + FAT_ENTRY_SIZE_BYTES)?;
    let mut buf = [0u8; FAT_ENTRY_SIZE_BYTES];
    buf.copy_from_slice(slice);
    Some(u32::from_le_bytes(buf))
}

/// Collapse a flat list of cluster numbers into one or more
/// `FileExtent` runs. Mirrors `fat32::chain::chain_to_extents`.
fn chain_to_extents(chain: &[u32], file_path: PathBuf, bytes_per_cluster: u32) -> Vec<FileExtent> {
    let mut out = Vec::new();
    let mut byte_offset: u64 = 0;
    let bpc = u64::from(bytes_per_cluster);
    let mut iter = chain.iter().copied();
    let Some(first) = iter.next() else {
        return out;
    };
    let mut run_first = first;
    let mut run_count: u32 = 1;
    let mut prev = first;
    for cluster in iter {
        if cluster == prev.saturating_add(1) {
            run_count = run_count.saturating_add(1);
            prev = cluster;
        } else {
            out.push(FileExtent {
                first_cluster: run_first,
                cluster_count: run_count,
                first_byte_in_file: byte_offset,
                file_path: file_path.clone(),
            });
            byte_offset = byte_offset.saturating_add(u64::from(run_count) * bpc);
            run_first = cluster;
            run_count = 1;
            prev = cluster;
        }
    }
    out.push(FileExtent {
        first_cluster: run_first,
        cluster_count: run_count,
        first_byte_in_file: byte_offset,
        file_path,
    });
    out
}

#[cfg(test)]
#[allow(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_sign_loss,
    clippy::cognitive_complexity,
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::missing_panics_doc,
    clippy::panic,
    clippy::redundant_closure_for_method_calls,
    clippy::too_many_lines,
    clippy::unwrap_used
)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;
    use teslausb_core::fs::exfat::directory::{
        FileAttributes, FileEntrySetParams, FileTimestamps, encode_file_entry_set,
    };
    use teslausb_core::fs::exfat::upcase_table::UpcaseTable;

    /// 64 MiB volume — large enough to comfortably exercise
    /// exFAT defaults without slow allocations in tests.
    const TEST_VOLUME_BYTES: u64 = 64 * 1024 * 1024;
    const SECTOR: u64 = SECTOR_SIZE_BYTES as u64;

    fn geo() -> ExfatGeometry {
        ExfatGeometry::for_volume_size(TEST_VOLUME_BYTES).expect("64 MiB is a valid exFAT size")
    }

    fn writer(tmp: &TempDir) -> DirTreeWriter {
        DirTreeWriter::new(tmp.path().to_path_buf()).expect("writer construction")
    }

    fn state(tmp: &TempDir) -> ExfatWriteState {
        ExfatWriteState::new(geo(), writer(tmp), &[])
    }

    fn fat_start_byte(g: &ExfatGeometry) -> u64 {
        u64::from(g.fat_offset_sectors()) * SECTOR
    }

    fn data_start_byte(g: &ExfatGeometry) -> u64 {
        u64::from(g.cluster_heap_offset_sectors()) * SECTOR
    }

    fn cluster_to_volume_byte(g: &ExfatGeometry, cluster: u32) -> u64 {
        data_start_byte(g)
            + u64::from(cluster - FIRST_DATA_CLUSTER) * u64::from(g.bytes_per_cluster())
    }

    fn fat_entry_volume_byte(g: &ExfatGeometry, cluster: u32) -> u64 {
        fat_start_byte(g) + u64::from(cluster) * (FAT_ENTRY_SIZE_BYTES as u64)
    }

    fn write_fat_entry(s: &mut ExfatWriteState, cluster: u32, value: u32) {
        let bytes = value.to_le_bytes();
        let g = geo();
        s.apply_write(fat_entry_volume_byte(&g, cluster), &bytes)
            .expect("fat entry write");
    }

    fn write_cluster_data(s: &mut ExfatWriteState, cluster: u32, bytes: &[u8]) {
        let g = geo();
        s.apply_write(cluster_to_volume_byte(&g, cluster), bytes)
            .expect("data cluster write");
    }

    fn ts() -> FileTimestamps {
        FileTimestamps {
            create_timestamp: 0x4A21_0000,
            modify_timestamp: 0x4A21_0001,
            access_timestamp: 0x4A21_0002,
            create_10ms: 50,
            modify_10ms: 25,
            create_utc_offset: 0x80,
            modify_utc_offset: 0x80,
            access_utc_offset: 0x80,
        }
    }

    fn build_file_entry(
        name: &str,
        first_cluster: u32,
        data_length: u64,
        no_fat_chain: bool,
    ) -> Vec<u8> {
        let n: Vec<u16> = name.encode_utf16().collect();
        let params = FileEntrySetParams {
            name: &n,
            attributes: FileAttributes::default(),
            timestamps: ts(),
            first_cluster,
            valid_data_length: data_length,
            data_length,
            no_fat_chain,
        };
        let upcase = UpcaseTable::ascii_identity();
        encode_file_entry_set(&params, &upcase).expect("encode")
    }

    fn root_cluster_byte() -> u64 {
        let g = geo();
        cluster_to_volume_byte(&g, g.first_root_directory_cluster())
    }

    #[test]
    fn fresh_state_has_root_directory_and_zero_extents() {
        let tmp = TempDir::new().unwrap();
        let s = state(&tmp);
        assert_eq!(s.directory_count(), 1);
        assert_eq!(s.extent_count(), 0);
        assert_eq!(s.in_flight_file_count(), 0);
    }

    #[test]
    fn empty_write_is_noop() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        s.apply_write(0, &[]).expect("ok");
    }

    #[test]
    fn boot_region_writes_are_swallowed() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        s.apply_write(0, &[0u8; 64]).expect("ok");
        // No state change.
        assert_eq!(s.directory_count(), 1);
        assert_eq!(s.extent_count(), 0);
    }

    #[test]
    fn fat_table_write_lands_in_internal_fat() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        write_fat_entry(&mut s, 5, EXFAT_END_OF_CHAIN);
        // walk_chain(5) should now return Some([5]).
        assert_eq!(s.try_walk_chain(5), Some(vec![5]));
    }

    #[test]
    fn nofat_chain_file_routes_directly_to_backing_tree() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let payload = b"hello exfat world".to_vec();
        let cluster = 5;

        // Write the data first (no FAT needed for no_fat_chain).
        write_cluster_data(&mut s, cluster, &payload);
        // Then the directory entry with no_fat_chain=true.
        let entry = build_file_entry("hello.bin", cluster, payload.len() as u64, true);
        s.apply_write(root_cluster_byte(), &entry).expect("dir");
        s.flush().expect("flush");

        let final_path = tmp.path().join("hello.bin");
        assert!(final_path.exists(), "file should have been finalized");
        let bytes = fs::read(&final_path).unwrap();
        assert_eq!(bytes, payload);
    }

    #[test]
    fn nofat_chain_file_works_when_dir_arrives_first() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let payload = b"order independent".to_vec();
        let cluster = 5;

        let entry = build_file_entry("first.bin", cluster, payload.len() as u64, true);
        s.apply_write(root_cluster_byte(), &entry).expect("dir");
        // Now the data arrives.
        write_cluster_data(&mut s, cluster, &payload);
        s.flush().expect("flush");

        let final_path = tmp.path().join("first.bin");
        assert_eq!(fs::read(&final_path).unwrap(), payload);
    }

    #[test]
    fn fat_chained_file_resolves_after_fat_then_data() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();
        let bpc = g.bytes_per_cluster() as usize;
        // Build a 2-cluster contiguous file but advertise it
        // with no_fat_chain = false so we exercise the FAT walker.
        let payload1 = vec![0xAAu8; bpc];
        let payload2 = vec![0xBBu8; 100]; // partial cluster
        let total_len = (bpc + 100) as u64;

        // Write FAT chain: 5 -> 6 -> EOC.
        write_fat_entry(&mut s, 5, 6);
        write_fat_entry(&mut s, 6, EXFAT_END_OF_CHAIN);

        // Write data clusters.
        write_cluster_data(&mut s, 5, &payload1);
        write_cluster_data(&mut s, 6, &payload2);

        // Dir entry — no_fat_chain = false.
        let entry = build_file_entry("chained.bin", 5, total_len, false);
        s.apply_write(root_cluster_byte(), &entry).expect("dir");
        s.flush().expect("flush");

        let final_path = tmp.path().join("chained.bin");
        let bytes = fs::read(&final_path).unwrap();
        assert_eq!(bytes.len(), total_len as usize);
        assert!(bytes[..bpc].iter().all(|&b| b == 0xAA));
        assert!(bytes[bpc..].iter().all(|&b| b == 0xBB));
    }

    #[test]
    fn fat_chained_file_works_when_data_arrives_before_fat() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();
        let bpc = g.bytes_per_cluster() as usize;
        let payload1 = vec![0xCCu8; bpc];
        let payload2 = vec![0xDDu8; 200];
        let total_len = (bpc + 200) as u64;

        // Data first (should be stashed in pending_data).
        write_cluster_data(&mut s, 5, &payload1);
        write_cluster_data(&mut s, 6, &payload2);

        // Dir entry next (chain unresolved → pending_file).
        let entry = build_file_entry("late.bin", 5, total_len, false);
        s.apply_write(root_cluster_byte(), &entry).expect("dir");

        // FAT last — this should drain everything.
        write_fat_entry(&mut s, 5, 6);
        write_fat_entry(&mut s, 6, EXFAT_END_OF_CHAIN);

        s.flush().expect("flush");
        let final_path = tmp.path().join("late.bin");
        let bytes = fs::read(&final_path).unwrap();
        assert_eq!(bytes.len(), total_len as usize);
        assert!(bytes[..bpc].iter().all(|&b| b == 0xCC));
        assert!(bytes[bpc..].iter().all(|&b| b == 0xDD));
    }

    #[test]
    fn deleting_a_file_via_dir_rewrite_removes_the_partial() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let payload = b"will be gone".to_vec();
        let cluster = 5;

        // Create + (no flush) — file should be in .partial.
        write_cluster_data(&mut s, cluster, &payload);
        let entry = build_file_entry("gone.bin", cluster, payload.len() as u64, true);
        s.apply_write(root_cluster_byte(), &entry).expect("dir");
        assert_eq!(s.in_flight_file_count(), 1);

        // Now rewrite the dir cluster with zeros (Tesla
        // deleted the file).
        let g = geo();
        let bpc = g.bytes_per_cluster() as usize;
        let zeros = vec![0u8; bpc];
        s.apply_write(root_cluster_byte(), &zeros)
            .expect("dir wipe");

        assert_eq!(s.in_flight_file_count(), 0);
        let partial = tmp.path().join("gone.bin.partial");
        assert!(!partial.exists(), ".partial must be discarded on delete");
    }

    #[test]
    fn flush_finalizes_partial_to_final_path() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let payload = b"final me".to_vec();
        write_cluster_data(&mut s, 5, &payload);
        let entry = build_file_entry("final.bin", 5, payload.len() as u64, true);
        s.apply_write(root_cluster_byte(), &entry).expect("dir");
        assert!(tmp.path().join("final.bin.partial").exists());
        s.flush().expect("flush");
        assert!(!tmp.path().join("final.bin.partial").exists());
        assert!(tmp.path().join("final.bin").exists());
    }

    #[test]
    fn pre_existing_file_inplace_rewrite_seeds_then_overwrites() {
        // Create the backing tree with a pre-existing file.
        let tmp = TempDir::new().unwrap();
        fs::write(tmp.path().join("preex.bin"), b"OLDDATA").unwrap();

        let g = geo();
        let pre_extent = PreExistingExfatExtent {
            first_cluster: 5,
            cluster_count: 1,
            first_byte_in_file: 0,
            file_size_bytes: 7,
            relative_path: PathBuf::from("preex.bin"),
        };
        let mut s = ExfatWriteState::new(g, writer(&tmp), &[pre_extent]);

        // Rewrite the first 3 bytes of cluster 5 (in-place edit).
        let g = geo();
        s.apply_write(cluster_to_volume_byte(&g, 5), b"NEW")
            .expect("rewrite");
        s.flush().expect("flush");

        let bytes = fs::read(tmp.path().join("preex.bin")).unwrap();
        assert_eq!(bytes, b"NEWDATA");
    }

    #[test]
    fn empty_file_zero_byte_creation() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let entry = build_file_entry("empty.bin", 5, 0, true);
        s.apply_write(root_cluster_byte(), &entry).expect("dir");
        s.flush().expect("flush");
        // Empty file shouldn't have produced any .partial or
        // final file (no data written → no in-flight tracking).
        assert_eq!(s.in_flight_file_count(), 0);
    }

    #[test]
    fn walker_rejects_cycle() {
        let mut fat = vec![0u8; 1024];
        // 5 -> 6 -> 5 cycle.
        fat[5 * 4..5 * 4 + 4].copy_from_slice(&6u32.to_le_bytes());
        fat[6 * 4..6 * 4 + 4].copy_from_slice(&5u32.to_le_bytes());
        assert_eq!(walk_chain(&fat, 5), None);
    }

    #[test]
    fn walker_returns_chain_for_singleton_eoc() {
        let mut fat = vec![0u8; 1024];
        fat[5 * 4..5 * 4 + 4].copy_from_slice(&EXFAT_END_OF_CHAIN.to_le_bytes());
        assert_eq!(walk_chain(&fat, 5), Some(vec![5]));
    }

    #[test]
    fn walker_returns_chain_for_two_cluster_run() {
        let mut fat = vec![0u8; 1024];
        fat[5 * 4..5 * 4 + 4].copy_from_slice(&6u32.to_le_bytes());
        fat[6 * 4..6 * 4 + 4].copy_from_slice(&EXFAT_END_OF_CHAIN.to_le_bytes());
        assert_eq!(walk_chain(&fat, 5), Some(vec![5, 6]));
    }

    #[test]
    fn chain_to_extents_collapses_consecutive_clusters() {
        let extents = chain_to_extents(&[5, 6, 7], PathBuf::from("x"), 4096);
        assert_eq!(extents.len(), 1);
        assert_eq!(extents[0].first_cluster, 5);
        assert_eq!(extents[0].cluster_count, 3);
        assert_eq!(extents[0].first_byte_in_file, 0);
    }

    #[test]
    fn chain_to_extents_splits_on_gap() {
        let extents = chain_to_extents(&[5, 6, 10, 11], PathBuf::from("x"), 4096);
        assert_eq!(extents.len(), 2);
        assert_eq!(extents[0].first_cluster, 5);
        assert_eq!(extents[0].cluster_count, 2);
        assert_eq!(extents[0].first_byte_in_file, 0);
        assert_eq!(extents[1].first_cluster, 10);
        assert_eq!(extents[1].cluster_count, 2);
        assert_eq!(extents[1].first_byte_in_file, 2 * 4096);
    }

    // === Phase 3.5f regression tests ===

    /// Bug H3-1 regression: when the kernel rewrites the directory
    /// entry with a larger `data_length` (longer cluster chain),
    /// the new tail clusters must be inserted into the cluster
    /// map. The old idempotent-skip path silently dropped them,
    /// stranding the file's tail data writes in `pending_data`
    /// and producing a zero-filled tail on the backing file.
    #[test]
    fn growing_extent_replaces_stale_chain_h3_1() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();
        let bpc = g.bytes_per_cluster() as usize;

        // Build a 5-cluster chain rooted at cluster 5.
        let chain: [u32; 5] = [5, 6, 7, 8, 9];
        for window in chain.windows(2) {
            write_fat_entry(&mut s, window[0], window[1]);
        }
        write_fat_entry(&mut s, *chain.last().unwrap(), EXFAT_END_OF_CHAIN);

        // First dir entry: short — 3 clusters' worth of data.
        let short_len = (3 * bpc) as u64;
        let entry_short = build_file_entry("video.mp4", 5, short_len, false);
        s.apply_write(root_cluster_byte(), &entry_short)
            .expect("short dir");
        assert!(s.extent_count() >= 1, "first resolve should insert chain");
        let extents_after_short = s.extent_count();

        // Now extend the FAT chain (kernel allocated more clusters)
        // and rewrite the dir entry with the final size — full 5
        // clusters. The pre-fix code would see existing extent at
        // first_cluster=5 with same path and `continue`, dropping
        // the extra clusters.
        let final_len = (5 * bpc) as u64;
        let entry_full = build_file_entry("video.mp4", 5, final_len, false);
        s.apply_write(root_cluster_byte(), &entry_full)
            .expect("full dir");

        // Issue data writes to every cluster including the new tail.
        for &c in &chain {
            let mut payload = vec![0u8; bpc];
            payload[0] = c as u8;
            write_cluster_data(&mut s, c, &payload);
        }
        s.flush().expect("flush");

        // After flush, the backing file should be exactly 5 clusters
        // long with NO zero-filled tail.
        let final_path = tmp.path().join("video.mp4");
        let bytes = fs::read(&final_path).expect("file finalized");
        assert_eq!(
            bytes.len(),
            5 * bpc,
            "all 5 clusters must be present (H3-1: tail was silently dropped)"
        );
        for (i, &c) in chain.iter().enumerate() {
            assert_eq!(
                bytes[i * bpc],
                c as u8,
                "cluster {c} payload must be at offset {}",
                i * bpc
            );
        }
        // Sanity: cluster map didn't lose extents (could have
        // collapsed contiguous extents but should be ≥ short count).
        assert!(s.extent_count() >= extents_after_short);
    }

    /// Bug H3-2 regression: a read covering the FAT and root
    /// directory region after a write must return the kernel-
    /// written bytes (not the synth's startup snapshot). Before
    /// Phase 3.5f, `overlay_read` did not exist and a remount
    /// would not see freshly-written files.
    #[test]
    fn overlay_read_returns_kernel_written_fat_and_dir_bytes_h3_2() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();

        // Kernel writes a FAT entry.
        write_fat_entry(&mut s, 5, EXFAT_END_OF_CHAIN);
        // Kernel writes a directory entry into the root cluster.
        let entry = build_file_entry("a.bin", 5, 12, true);
        s.apply_write(root_cluster_byte(), &entry)
            .expect("dir write");

        // Read back the FAT entry via overlay. Buffer starts zeroed.
        let fat_entry_off = fat_entry_volume_byte(&g, 5);
        let mut fat_buf = vec![0u8; FAT_ENTRY_SIZE_BYTES];
        s.overlay_read(fat_entry_off, &mut fat_buf);
        assert_eq!(
            u32::from_le_bytes(fat_buf.as_slice().try_into().unwrap()),
            EXFAT_END_OF_CHAIN,
            "overlay must surface kernel-written FAT entry"
        );

        // Read back the directory cluster start via overlay.
        let mut dir_buf = vec![0u8; entry.len()];
        s.overlay_read(root_cluster_byte(), &mut dir_buf);
        assert_eq!(
            &dir_buf, &entry,
            "overlay must surface kernel-written dir entry bytes"
        );

        // Read of an unrelated region returns the caller's zero
        // buffer unchanged (overlay does not corrupt synth bytes).
        let mut elsewhere = vec![0xAAu8; 32];
        let before = elsewhere.clone();
        s.overlay_read(fat_entry_off + 1024, &mut elsewhere);
        assert_eq!(elsewhere, before, "overlay must skip non-dirty regions");
    }

    #[test]
    fn overlay_read_returns_kernel_written_bitmap_bytes_h3_2_part_2() {
        // Phase 3.5g regression test. Without bitmap overlay, the
        // synth's startup snapshot serves a bitmap with the new
        // file's clusters marked FREE while the dir entry claims
        // them allocated; the Linux exFAT driver rejects this
        // inconsistency with EIO on stat (hardware-observed
        // 2026-05-20 on a 2 MB+ file). With the overlay, the
        // bitmap bytes the kernel wrote are surfaced to subsequent
        // reads (i.e. the post-remount mount scan).
        let tmp = TempDir::new().unwrap();
        let g = geo();
        let bitmap_first_cluster: u32 = 100;
        let bitmap_cluster_count: u32 = 1;
        let mut s = ExfatWriteState::new(g.clone(), writer(&tmp), &[])
            .with_allocation_bitmap(bitmap_first_cluster, bitmap_cluster_count);

        // Kernel marks bit 0 (cluster 2) and bit 7 (cluster 9) as
        // allocated by writing 0x81 at byte 0 of the bitmap stream.
        let bitmap_byte_off = cluster_to_volume_byte(&g, bitmap_first_cluster);
        let pattern = [0x81u8];
        s.apply_write(bitmap_byte_off, &pattern)
            .expect("bitmap write");

        // Read back the bitmap byte via overlay.
        let mut buf = vec![0u8; 1];
        s.overlay_read(bitmap_byte_off, &mut buf);
        assert_eq!(
            buf,
            vec![0x81u8],
            "overlay must surface kernel-written bitmap bytes"
        );

        // Bytes adjacent to but not covered by the kernel write
        // remain whatever the synth produced (zero in this stub).
        let mut elsewhere = vec![0xAAu8; 8];
        let before = elsewhere.clone();
        s.overlay_read(bitmap_byte_off + 1, &mut elsewhere);
        assert_eq!(
            elsewhere, before,
            "overlay must not mark un-written bitmap bytes dirty"
        );
    }
}
