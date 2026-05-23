//! FAT32 write-path state machine (Phase 3.5c).
//!
//! Sits between [`teslausb_core::fs::fat32::parse::decode_write`]
//! (which classifies each kernel-issued write byte into a typed
//! per-region chunk) and [`super::dir_tree::DirTreeWriter`]
//! (which materializes file content onto the POSIX backing tree
//! via `.partial`-suffix atomicity).
//!
//! ## Pipeline
//!
//! ```text
//!  NBD write(offset, &[u8])
//!         │
//!         ▼
//!  decode_write(geometry, offset, bytes)  ── per-region chunks
//!         │       │       │
//!   ┌─────┘       │       └────────────┐
//!   ▼             ▼                    ▼
//!  boot/fsinfo   FatTable           DataCluster
//!  swallow       update mirror       │
//!  (we are       try resolve         │
//!   source-      pending chains      │
//!   of-truth)                        ▼
//!                                   route by cluster
//!                                   ├─ directory cluster
//!                                   │     → buffer, decode entries,
//!                                   │       register filenames, try
//!                                   │       resolve chains
//!                                   ├─ known file cluster
//!                                   │     → dir_tree.apply_chunk
//!                                   └─ unknown cluster
//!                                         → stash in pending_data
//! ```
//!
//! ## Why an explicit state machine
//!
//! Tesla's kernel does not guarantee an ordering between writing
//! a directory entry, writing the FAT entries that form the
//! file's cluster chain, and writing the file's data clusters.
//! Any of the three can land in any order. The state machine
//! keeps stashes of partial information and reconciles whenever
//! a new piece arrives:
//!
//! * Dir entry without FAT chain → file path known, extent
//!   unknown → wait for FAT bytes.
//! * FAT chain without dir entry → chain known, owner unknown
//!   → wait for dir entry.
//! * Data cluster without either → bytes stashed, reconciled
//!   when one or both of the above arrive.
//!
//! ## Crash safety
//!
//! Every routed data byte lands in `<relative>.partial` first.
//! [`Fat32WriteState::flush`] (driven by NBD `FLUSH` or
//! `FUA`-tagged writes) finalizes touched files by deleting the
//! pre-existing final (if any) and renaming `.partial → final`.
//! A crash between the unlink and the rename leaves a stranded
//! `.partial` that the Phase 3.6 reaper finalizes on startup.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use teslausb_core::backend::BackendError;
use teslausb_core::fs::cluster_layout::FIRST_DATA_CLUSTER;
use teslausb_core::fs::cluster_map::{ClusterMap, ClusterMapError};
use teslausb_core::fs::fat32::boot_sector::ROOT_DIRECTORY_CLUSTER;
use teslausb_core::fs::fat32::chain::{self, ChainWalkError};
use teslausb_core::fs::fat32::dir_decode::{
    self, DecodeResult, DecodedDirEntry, DirDecodeError, LfnEntry,
};
use teslausb_core::fs::fat32::directory::{ATTR_DIRECTORY, ATTR_VOLUME_ID, ShortName};
use teslausb_core::fs::fat32::geometry::Fat32Geometry;
use teslausb_core::fs::fat32::parse::{DecodeWriteError, DecodedWrite, decode_write};
use teslausb_core::fs::geometry::{Geometry, SECTOR_SIZE_BYTES};

use super::dir_tree::{DirTreeError, DirTreeWriter};
use super::dirty_map::DirtyByteMap;
use crate::retention::DeletedSet;

/// Number of FAT mirrors the FAT32 geometry advertises. Pulled
/// into the state machine so the per-mirror buffer array has a
/// const size; if the spec invariant ever changes the compiler
/// catches every site.
const NUM_FAT_MIRRORS: usize = teslausb_core::fs::fat32::geometry::NUM_FATS as usize;

/// Errors returned by [`Fat32WriteState::apply_write`] and
/// [`Fat32WriteState::flush`].
///
/// Every variant is recoverable from the daemon's point of view —
/// we surface them to the NBD client as `BackendError::Io`, and
/// the client either retries or marks the volume dirty. None of
/// these abort the daemon.
#[derive(Debug, thiserror::Error)]
pub enum Fat32WriteError {
    /// The geometry rejected the write coordinates (out of bounds
    /// or unsupported region).
    #[error("decode_write rejected the write: {0}")]
    Decode(#[from] DecodeWriteError),
    /// Walking a FAT cluster chain failed.
    #[error("FAT chain walk failed: {0}")]
    Chain(#[from] ChainWalkError),
    /// Decoding a directory cluster's entries failed.
    #[error("directory entry decode failed: {0}")]
    DirDecode(#[from] DirDecodeError),
    /// Inserting an extent into the cluster map failed (overlap
    /// with an existing extent — typically a sign of cluster
    /// reuse without a preceding delete).
    #[error("cluster map insert failed: {0}")]
    ClusterMap(#[from] ClusterMapError),
    /// A `DirTreeWriter` operation failed.
    #[error("dir tree writer failed: {0}")]
    DirTree(#[from] DirTreeError),
}

impl From<Fat32WriteError> for BackendError {
    fn from(err: Fat32WriteError) -> Self {
        BackendError::Io(std::io::Error::other(format!("fat32 write: {err}")))
    }
}

/// Per-directory accumulated state. One per dir-first-cluster.
///
/// The buffer carries the **chain-ordered concatenated bytes** of
/// every cluster currently allocated to this directory. When new
/// FAT bytes extend the chain, we resize the buffer (zero-padded).
/// When a data-cluster write hits one of the directory's clusters,
/// we splice the bytes into the right offset.
///
/// On every change we re-decode from byte 0 and replay the
/// resulting entries against the [`Fat32WriteState`]'s
/// reconciliation maps. Re-decoding cheaply on every dir-cluster
/// write is fine for the workloads we care about (dir clusters
/// are small and infrequently written; a Tesla `TeslaCam` tree has
/// at most a few thousand entries spread across a handful of
/// directories).
#[derive(Debug)]
struct DirectoryState {
    /// Cluster chain in FAT order, populated by `walk_chain`.
    /// Empty if the chain hasn't been resolved yet (root dir is
    /// initialized with a single-cluster chain at startup).
    chain: Vec<u32>,
    /// Concatenated cluster bytes, length =
    /// `chain.len() * bytes_per_cluster` (or 0 if chain empty).
    buffer: Vec<u8>,
    /// Tracks which bytes of `buffer` the kernel has written
    /// (Phase 3.5f). Used by the synth read-overlay path so that
    /// pre-existing directory entries (which never enter
    /// `buffer` — it starts zero) are preserved while
    /// kernel-written entries are returned on subsequent reads.
    dirty_buffer: DirtyByteMap,
    /// Parent path relative to backing root. Root directory's
    /// parent is `""` (empty `PathBuf`).
    parent_path: PathBuf,
    /// Last-decoded set of `(first_cluster, relative_path)`
    /// tuples we registered into `pending_filenames` so a
    /// subsequent re-decode can detect deletions (entries that
    /// disappeared between runs).
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

/// One pending dir entry that is awaiting its cluster chain
/// before we can register a `FileExtent`.
#[derive(Debug)]
struct PendingFile {
    relative_path: PathBuf,
    file_size: u64,
    is_directory: bool,
    /// Set of [`DirectoryState`] dir-first-cluster IDs that
    /// currently claim this child. Used to detect deletions: if
    /// the entry disappears from every parent, we drop the
    /// pending entry and (for resolved files) free its extent.
    parents: HashSet<u32>,
}

/// One stashed data-cluster write that arrived before the
/// cluster's owning file was known.
#[derive(Debug)]
struct PendingDataChunk {
    byte_in_cluster: usize,
    bytes: Vec<u8>,
}

/// FAT32 write-side state machine. See module-level docs.
#[derive(Debug)]
pub struct Fat32WriteState {
    geometry: Fat32Geometry,
    dir_tree: DirTreeWriter,
    bytes_per_cluster: u32,
    fat_mirror_size_bytes: usize,

    fat_mirrors: [Vec<u8>; NUM_FAT_MIRRORS],
    /// Per-mirror dirty-byte tracking for the read overlay
    /// (Phase 3.5f). Only the primary mirror's dirty state is
    /// used today (overlay reads the primary), but tracking
    /// both keeps the data structure symmetric and ready for
    /// any future "read from secondary on primary corruption"
    /// recovery path.
    dirty_fat_mirrors: [DirtyByteMap; NUM_FAT_MIRRORS],
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
    /// Relative paths that have ever been written to via
    /// `dir_tree.apply_chunk` and have not yet been finalized.
    /// Flush iterates this set.
    in_flight_files: HashSet<PathBuf>,
    /// Relative paths the caller seeded as already-existing on
    /// the backing tree at construction time. Used to decide
    /// whether [`DirTreeWriter::seed_partial_from_target`] needs
    /// to copy the original on the first write.
    pre_existing_files: HashSet<PathBuf>,
    /// File sizes recorded by directory entries (live or
    /// pre-existing). `flush()` truncates each finalized file
    /// to this size so cluster-tail padding doesn't leak into
    /// the backing tree.
    recorded_file_sizes: HashMap<PathBuf, u64>,
    /// Paths Tesla has marked deleted via directory-entry
    /// mutation (SFN leading byte rewritten to `0xE5`). Phase
    /// 4.2 records the deletion here and leaves the backing
    /// file in place; the Phase 4b cleanup worker decides
    /// whether to actually reap based on GPS/SEI metadata.
    deleted: DeletedSet,
}

/// Describes a pre-existing file extent the [`Fat32WriteState`]
/// should seed into its cluster map at construction time.
///
/// The Phase 2 layout planner hands out contiguous cluster
/// extents for every file already living in the backing tree.
/// Seeding these extents into the writer's cluster map lets the
/// kernel's in-place rewrites (which arrive as bare
/// `DataCluster` writes — Tesla doesn't re-issue the dir entry
/// just to change a few bytes) route to the correct backing
/// file instead of being stashed as orphan `pending_data`.
#[derive(Debug, Clone)]
pub struct PreExistingExtent {
    /// First cluster of the extent.
    pub first_cluster: u32,
    /// Number of contiguous clusters.
    pub cluster_count: u32,
    /// Byte offset within the file at which this extent starts.
    /// `0` for the first extent of a file.
    pub first_byte_in_file: u64,
    /// Total file size in bytes (same value for every extent
    /// of the same file). Used at flush time to truncate the
    /// finalized file to the right length.
    pub file_size_bytes: u64,
    /// Path of the backing file, relative to the backing root.
    pub relative_path: PathBuf,
}

impl Fat32WriteState {
    /// Build a fresh state machine for a FAT32 volume of
    /// `geometry`, routing writes through `dir_tree`.
    ///
    /// `pre_existing_extents` describes every cluster extent
    /// owned by a file that already lives on the backing tree
    /// at startup (as planned by the Phase 2 layout). The
    /// writer seeds its cluster map from these extents so that
    /// in-place rewrites — Tesla writes a fresh data cluster
    /// without re-issuing the directory entry — route to the
    /// correct backing file instead of being orphaned.
    ///
    /// Initializes the root directory at
    /// [`ROOT_DIRECTORY_CLUSTER`] (single cluster); subdirectory
    /// chains are discovered dynamically as their dir-entry
    /// arrives.
    ///
    /// # Panics
    ///
    /// Never panics on valid layout output. Reserved-cluster or
    /// overlapping extents from the layout indicate a bug in
    /// Phase 2 layout planning and result in `tracing::warn` +
    /// the extent being silently skipped (so the daemon stays
    /// up even if the planner is buggy).
    #[must_use]
    pub fn new(
        geometry: Fat32Geometry,
        dir_tree: DirTreeWriter,
        pre_existing_extents: &[PreExistingExtent],
    ) -> Self {
        let bytes_per_cluster = geometry.bytes_per_cluster();
        let fat_mirror_size_bytes =
            (geometry.fat_size_sectors() as usize) * (SECTOR_SIZE_BYTES as usize);
        let fat_mirrors: [Vec<u8>; NUM_FAT_MIRRORS] =
            std::array::from_fn(|_| vec![0u8; fat_mirror_size_bytes]);

        let mut pre_existing_files: HashSet<PathBuf> = HashSet::new();
        let mut recorded_file_sizes: HashMap<PathBuf, u64> = HashMap::new();
        let mut cluster_map = ClusterMap::new();
        for extent in pre_existing_extents {
            pre_existing_files.insert(extent.relative_path.clone());
            recorded_file_sizes.insert(extent.relative_path.clone(), extent.file_size_bytes);
            let file_extent = teslausb_core::fs::cluster_map::FileExtent {
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
            fat_mirror_size_bytes,
            fat_mirrors,
            dirty_fat_mirrors: [DirtyByteMap::new(), DirtyByteMap::new()],
            cluster_map,
            directories: HashMap::new(),
            cluster_to_directory: HashMap::new(),
            pending_files: HashMap::new(),
            pending_data: HashMap::new(),
            in_flight_files: HashSet::new(),
            pre_existing_files,
            recorded_file_sizes,
            deleted: DeletedSet::new(),
        };
        // Bootstrap the root directory with a single-cluster
        // chain. The FAT write that extends it past one cluster
        // will rebuild the chain via try_resolve_directory_chain.
        let mut root = DirectoryState::new(PathBuf::new());
        root.chain = vec![ROOT_DIRECTORY_CLUSTER];
        root.buffer = vec![0u8; bytes_per_cluster as usize];
        state.directories.insert(ROOT_DIRECTORY_CLUSTER, root);
        state
            .cluster_to_directory
            .insert(ROOT_DIRECTORY_CLUSTER, ROOT_DIRECTORY_CLUSTER);
        state
    }

    /// Apply one kernel-issued write to the state machine.
    ///
    /// # Errors
    ///
    /// See [`Fat32WriteError`].
    pub fn apply_write(&mut self, offset: u64, bytes: &[u8]) -> Result<(), Fat32WriteError> {
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
    fn dispatch_chunk(&mut self, chunk: DecodedWrite<'_>) -> Result<(), Fat32WriteError> {
        match chunk {
            DecodedWrite::BootSector { .. }
            | DecodedWrite::BackupBootSector { .. }
            | DecodedWrite::FsInfo { .. }
            | DecodedWrite::Reserved { .. } => {
                // Metadata for which we are the source of truth.
                // Swallow silently — the kernel's writes here are
                // duplicates of what our synth already serves on
                // the read path.
                tracing::trace!(?chunk, "metadata write swallowed");
                Ok(())
            }
            DecodedWrite::FatTable {
                mirror_index,
                byte_in_fat,
                bytes,
            } => self.apply_fat_table_write(mirror_index, byte_in_fat, bytes),
            DecodedWrite::DataCluster {
                cluster_number,
                byte_in_cluster,
                bytes,
            } => self.apply_data_cluster_write(cluster_number, byte_in_cluster, bytes),
        }
    }

    fn apply_fat_table_write(
        &mut self,
        mirror_index: u8,
        byte_in_fat: usize,
        bytes: &[u8],
    ) -> Result<(), Fat32WriteError> {
        let mirror_idx = mirror_index as usize;
        if mirror_idx >= NUM_FAT_MIRRORS {
            tracing::warn!(mirror_index, "FAT table write to unknown mirror index");
            return Ok(());
        }
        let Some(mirror) = self.fat_mirrors.get_mut(mirror_idx) else {
            return Ok(());
        };
        let end = byte_in_fat.saturating_add(bytes.len());
        if end > mirror.len() {
            tracing::warn!(
                mirror_index,
                byte_in_fat,
                len = bytes.len(),
                mirror_len = mirror.len(),
                "FAT table write past end of mirror; truncating"
            );
        }
        let copy_end = end.min(mirror.len());
        let copy_len = copy_end.saturating_sub(byte_in_fat);
        if copy_len > 0 {
            if let (Some(dest), Some(src)) =
                (mirror.get_mut(byte_in_fat..copy_end), bytes.get(..copy_len))
            {
                dest.copy_from_slice(src);
            }
            if let Some(dirty) = self.dirty_fat_mirrors.get_mut(mirror_idx) {
                dirty.mark(byte_in_fat, copy_len);
            }
        }

        // The primary mirror is authoritative for our reconcile
        // logic; the secondary is mirrored on disk only. Skip
        // re-resolve for non-primary mirrors so we don't redo work.
        if mirror_idx == 0 {
            self.resolve_after_fat_change()?;
        }
        Ok(())
    }

    fn apply_data_cluster_write(
        &mut self,
        cluster_number: u32,
        byte_in_cluster: usize,
        bytes: &[u8],
    ) -> Result<(), Fat32WriteError> {
        if cluster_number < FIRST_DATA_CLUSTER {
            tracing::warn!(cluster_number, "data write to reserved cluster ignored");
            return Ok(());
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
    ) -> Result<(), Fat32WriteError> {
        let bytes_per_cluster_usize = self.bytes_per_cluster as usize;
        // Locate the cluster's index in the directory's chain.
        let Some(dir_state) = self.directories.get_mut(&dir_first) else {
            return Ok(());
        };
        let Some(chain_index) = dir_state.chain.iter().position(|&c| c == cluster_number) else {
            // Cluster is mapped to this directory but the chain
            // doesn't yet know about it. Defer: another FAT
            // update will rebuild the chain and replay this
            // write via try_resolve_directory_chain.
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
            // Out-of-range — shouldn't happen because we sized
            // buffer to chain.len() * bytes_per_cluster.
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
    ) -> Result<(), Fat32WriteError> {
        if bytes.is_empty() {
            return Ok(());
        }
        if self.pre_existing_files.contains(relative_path)
            && !self.in_flight_files.contains(relative_path)
        {
            // First touch of a pre-existing file: copy the original
            // into .partial so the un-rewritten bytes survive.
            self.dir_tree.seed_partial_from_target(relative_path)?;
        }
        self.dir_tree
            .apply_chunk(relative_path, byte_in_file, bytes)?;
        self.in_flight_files.insert(relative_path.clone());
        Ok(())
    }

    /// Re-decode the directory rooted at `dir_first` and reconcile
    /// dir-entry additions, modifications, and deletions against
    /// `pending_files`, `cluster_map`, and `directories`.
    fn redecode_directory(&mut self, dir_first: u32) -> Result<(), Fat32WriteError> {
        let (buffer_clone, parent_path) = {
            let Some(dir_state) = self.directories.get(&dir_first) else {
                return Ok(());
            };
            (dir_state.buffer.clone(), dir_state.parent_path.clone())
        };
        if buffer_clone.is_empty() {
            return Ok(());
        }
        let decode_result: DecodeResult =
            dir_decode::decode_directory_cluster(&buffer_clone, Vec::<LfnEntry>::new())?;

        // Build the new (first_cluster -> relative_path) map for
        // this directory.
        let mut new_children: HashMap<u32, (PathBuf, u64, bool)> = HashMap::new();
        for entry in &decode_result.entries {
            match entry {
                DecodedDirEntry::File {
                    long_name,
                    short_name,
                    attributes,
                    first_cluster,
                    file_size,
                    ..
                } => {
                    if *first_cluster < FIRST_DATA_CLUSTER {
                        // Empty file or "." / ".." entry — ignore.
                        continue;
                    }
                    if attributes & ATTR_VOLUME_ID != 0 {
                        continue;
                    }
                    let name = pick_entry_name(long_name.as_deref(), short_name);
                    if is_dot_or_dotdot(&name) {
                        continue;
                    }
                    let relative = parent_path.join(&name);
                    let is_dir = attributes & ATTR_DIRECTORY != 0;
                    new_children.insert(*first_cluster, (relative, u64::from(*file_size), is_dir));
                }
                DecodedDirEntry::ShortNameOnly {
                    short_name,
                    attributes,
                    first_cluster,
                    file_size,
                    ..
                } => {
                    if *first_cluster < FIRST_DATA_CLUSTER {
                        continue;
                    }
                    if attributes & ATTR_VOLUME_ID != 0 {
                        continue;
                    }
                    let name = sfn_to_string(short_name);
                    if is_dot_or_dotdot(&name) {
                        continue;
                    }
                    let relative = parent_path.join(&name);
                    let is_dir = attributes & ATTR_DIRECTORY != 0;
                    new_children.insert(*first_cluster, (relative, u64::from(*file_size), is_dir));
                }
                DecodedDirEntry::Deleted { .. }
                | DecodedDirEntry::VolumeLabel { .. }
                | DecodedDirEntry::Malformed { .. } => {
                    // Deletions are handled by the diff against
                    // registered_children below. VolumeLabel and
                    // Malformed entries are ignored.
                }
            }
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
        for (&first_cluster, (relative_path, file_size, is_directory)) in &new_children {
            self.handle_child_seen(
                dir_first,
                first_cluster,
                relative_path,
                *file_size,
                *is_directory,
            )?;
        }

        // Persist the new registration for the next diff.
        if let Some(dir_state) = self.directories.get_mut(&dir_first) {
            dir_state.registered_children = new_children
                .into_iter()
                .map(|(cluster, (path, _size, _is_dir))| (cluster, path))
                .collect();
        }
        Ok(())
    }

    fn handle_child_seen(
        &mut self,
        parent_dir: u32,
        first_cluster: u32,
        relative_path: &Path,
        file_size: u64,
        is_directory: bool,
    ) -> Result<(), Fat32WriteError> {
        // If Tesla previously deleted this path and is now
        // re-creating it (same relative path, possibly different
        // clusters), the prior deletion mark is stale — drop it
        // so the cleanup worker doesn't reap a now-live file.
        self.deleted.forget(relative_path);

        let pending = self
            .pending_files
            .entry(first_cluster)
            .or_insert_with(|| PendingFile {
                relative_path: relative_path.to_path_buf(),
                file_size,
                is_directory,
                parents: HashSet::new(),
            });
        pending.relative_path = relative_path.to_path_buf();
        pending.file_size = file_size;
        pending.is_directory = is_directory;
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
        // The entry is no longer referenced by any directory we
        // know about. Phase 4.2: treat as a *Tesla* deletion —
        // record the path in the retention `DeletedSet` and leave
        // the backing file present on disk. The Phase 4b cleanup
        // worker decides whether to actually reap based on the
        // file's GPS / SEI metadata; this shim refuses to honor
        // Tesla's round-robin reuse blindly.
        //
        // We still drop the in-flight tracker and the `.partial`
        // companion — those were uncommitted bytes for *this*
        // generation of the dir-entry; if Tesla re-creates a file
        // with the same name we must start that .partial fresh.
        // We also still free the cluster_map extent because the
        // FAT entries those clusters belong to are now legitimately
        // free from the filesystem's perspective; if we kept the
        // extent we'd mis-route a future write that the kernel
        // allocates into the same clusters.
        self.pending_files.remove(&first_cluster);
        let removed = self.cluster_map.remove_at(first_cluster);
        if removed.is_some() {
            tracing::debug!(
                first_cluster,
                ?previous_path,
                "child deleted; recorded in retention set, freed cluster map extent"
            );
        }
        let _ = self.dir_tree.discard(previous_path);
        self.in_flight_files.remove(previous_path);
        self.deleted.mark(previous_path.clone());
    }

    fn ensure_directory_registered(
        &mut self,
        first_cluster: u32,
        relative_path: PathBuf,
    ) -> Result<(), Fat32WriteError> {
        if self.directories.contains_key(&first_cluster) {
            return Ok(());
        }
        let mut dir_state = DirectoryState::new(relative_path);
        // Try to seed the chain from FAT bytes; if not yet
        // resolvable, leave empty and let resolve_after_fat_change
        // populate later.
        if let Some(chain_vec) = self.try_walk_chain(first_cluster) {
            self.adopt_directory_chain(&mut dir_state, first_cluster, &chain_vec);
        } else {
            dir_state.chain = vec![first_cluster];
            dir_state.buffer = vec![0u8; self.bytes_per_cluster as usize];
            self.cluster_to_directory
                .insert(first_cluster, first_cluster);
        }
        self.directories.insert(first_cluster, dir_state);
        // Replay any pending data writes that landed on those clusters
        // before we knew they were dir clusters.
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

    fn try_resolve_file(&mut self, first_cluster: u32) -> Result<(), Fat32WriteError> {
        let pending = match self.pending_files.get(&first_cluster) {
            Some(p) if !p.is_directory => p,
            _ => return Ok(()),
        };
        let relative_path = pending.relative_path.clone();
        let file_size = pending.file_size;
        let Some(chain_vec) = self.try_walk_chain(first_cluster) else {
            return Ok(());
        };
        // If an existing extent for the same first_cluster owns
        // a different file, free it first. This happens when
        // Tesla reuses a cluster after deleting a file.
        if let Some(existing) = self.cluster_map.lookup(first_cluster) {
            if existing.extent.first_cluster == first_cluster
                && existing.extent.file_path != relative_path
            {
                self.cluster_map.remove_at(first_cluster);
            }
        }
        let extents =
            chain::chain_to_extents(&chain_vec, relative_path.clone(), self.bytes_per_cluster);

        // Phase 3.5f (Bug H3-1 fix): replace all extents owned by
        // this path before inserting the new chain. Without this,
        // when the kernel writes the directory entry incrementally
        // (first with a short `file_size`, then later updated to
        // the final size), the earlier short extents would still
        // satisfy `lookup(first_cluster)` for the same path and
        // the new larger extents would be silently skipped by
        // the idempotent-insert path. The result: data-cluster
        // writes for the file's tail fell into `pending_data`
        // forever, producing zero-filled gaps on the backing
        // file. (Symmetric to the exFAT bug fixed in
        // `exfat_write::try_resolve_file`.)
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
        // Bug "fat32-cluster-reuse" (2026-05-22, symmetric to the
        // exFAT fix in `exfat_write::try_resolve_file`): Tesla can
        // free a file's clusters via the FSInfo / FAT-chain writes
        // without our `remove_file` path catching every prior
        // owner, then reallocate those clusters to a different
        // file. The stale extent would block the `insert` below
        // with `ClusterMapError::Overlap`, the warn-and-skip path
        // would orphan the new file's data writes in
        // `pending_data`, and the backing file would be silently
        // zero-filled / truncated.
        //
        // A freshly-arrived directory entry is authoritative for
        // its cluster range, so evict any stale extents that
        // overlap before insertion.
        for extent in &extents {
            let end_excl = extent.first_cluster.saturating_add(extent.cluster_count);
            let evicted = self
                .cluster_map
                .remove_overlapping(extent.first_cluster, end_excl);
            for stale in &evicted {
                tracing::debug!(
                    evicted_first = stale.first_cluster,
                    evicted_count = stale.cluster_count,
                    evicted_path = ?stale.file_path,
                    new_first = extent.first_cluster,
                    new_count = extent.cluster_count,
                    ?relative_path,
                    "evicted stale cluster-map extent for cluster reuse"
                );
            }
        }
        for extent in extents {
            match self.cluster_map.insert(extent.clone()) {
                Ok(()) => {}
                Err(ClusterMapError::Overlap { .. }) => {
                    // Post-eviction this is unreachable unless the
                    // new chain itself contains internally-
                    // overlapping extents (FAT-walker bug).
                    tracing::error!(
                        first_cluster = extent.first_cluster,
                        cluster_count = extent.cluster_count,
                        ?relative_path,
                        "cluster map insert overlapped after eviction \
                         (internal invariant violation)"
                    );
                    debug_assert!(
                        false,
                        "post-eviction overlap is a coding bug; see error log"
                    );
                }
                Err(other) => return Err(other.into()),
            }
        }
        // Cap the file size at the dir entry's reported size so
        // tail-cluster bytes don't get included.
        // We achieve this by trusting the dir entry's file_size
        // at flush time (truncate after rename).
        self.recorded_file_sizes
            .insert(relative_path.clone(), file_size);

        // Replay any pending data writes for the clusters in
        // this chain.
        for &cluster in &chain_vec {
            self.replay_pending_data_for_cluster(cluster)?;
        }
        Ok(())
    }

    fn try_walk_chain(&self, first_cluster: u32) -> Option<Vec<u32>> {
        let mirror = self.fat_mirrors.first()?;
        // Validate that the FAT bytes for first_cluster exist
        // (the chain walker would also fail; we short-circuit
        // for clarity).
        let entry_byte = (first_cluster as usize)
            .checked_mul(teslausb_core::fs::fat32::fat_table::FAT_ENTRY_SIZE_BYTES as usize)?;
        if entry_byte + (teslausb_core::fs::fat32::fat_table::FAT_ENTRY_SIZE_BYTES as usize)
            > mirror.len()
        {
            return None;
        }
        match chain::walk_chain(mirror, first_cluster) {
            Ok(chain_vec) => Some(chain_vec),
            Err(ChainWalkError::PointsToFree { .. }) => None,
            Err(err) => {
                tracing::debug!(first_cluster, ?err, "chain walk failed");
                None
            }
        }
    }

    fn replay_pending_data_for_cluster(
        &mut self,
        cluster_number: u32,
    ) -> Result<(), Fat32WriteError> {
        let Some(chunks) = self.pending_data.remove(&cluster_number) else {
            return Ok(());
        };
        for chunk in chunks {
            self.apply_data_cluster_write(cluster_number, chunk.byte_in_cluster, &chunk.bytes)?;
        }
        Ok(())
    }

    fn replay_pending_data_for_directory(&mut self, dir_first: u32) -> Result<(), Fat32WriteError> {
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

    /// After a FAT mirror write, re-walk every pending file and
    /// directory chain that hadn't fully resolved yet. Newly
    /// resolved chains adopt their clusters and drain pending
    /// data writes.
    fn resolve_after_fat_change(&mut self) -> Result<(), Fat32WriteError> {
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
                    // The directory chain may now expose entries
                    // we hadn't decoded yet.
                    self.redecode_directory(dir_first)?;
                }
            }
        }

        // 2) Pending files: try to resolve their chain now.
        let pending_keys: Vec<u32> = self.pending_files.keys().copied().collect();
        for first_cluster in pending_keys {
            let is_dir = self
                .pending_files
                .get(&first_cluster)
                .is_some_and(|p| p.is_directory);
            if !is_dir {
                self.try_resolve_file(first_cluster)?;
            }
        }
        Ok(())
    }

    /// Finalize every in-flight file by renaming
    /// `<path>.partial → <path>`, replacing any pre-existing
    /// target. Truncates the finalized file to the dir entry's
    /// reported size when known (so tail-cluster padding doesn't
    /// leak into the backing file).
    ///
    /// # Errors
    ///
    /// See [`Fat32WriteError`].
    pub fn flush(&mut self) -> Result<(), Fat32WriteError> {
        let paths: Vec<PathBuf> = self.in_flight_files.drain().collect();
        for path in paths {
            // Find the dir entry that records this path's size
            // (if any) so we can truncate after finalize.
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
            // After finalize the file IS pre-existing.
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
                return Some(pending.file_size);
            }
        }
        None
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

    /// Read-only handle to the set of paths Tesla has marked
    /// deleted via directory-entry mutation in this session. The
    /// Phase 4b cleanup worker reads this to drive reap decisions.
    #[must_use]
    pub fn deleted(&self) -> &DeletedSet {
        &self.deleted
    }

    /// Number of files that have been written to but not yet
    /// finalized via [`Self::flush`].
    #[must_use]
    pub fn in_flight_file_count(&self) -> usize {
        self.in_flight_files.len()
    }

    /// Total bytes of FAT mirror state. Useful for tests that
    /// assert geometry sizing.
    #[must_use]
    pub fn fat_mirror_size_bytes(&self) -> usize {
        self.fat_mirror_size_bytes
    }

    /// Overlay any in-memory write-state updates (kernel-written
    /// FAT entries and directory cluster bytes) onto the bytes
    /// the synth produced for a read of `[offset, offset+buf.len())`
    /// in the volume.
    ///
    /// See [`super::exfat_write::ExfatWriteState::overlay_read`]
    /// for the full rationale (Bug H3-2, Phase 3.5f). FAT32-side
    /// differences: there are `NUM_FAT_MIRRORS` FAT mirrors at
    /// adjacent sector offsets; both are overlaid (the synth
    /// keeps the two in sync, so kernel-written entries in either
    /// mirror need to be visible on subsequent reads of either).
    pub fn overlay_read(&self, offset: u64, buf: &mut [u8]) {
        if buf.is_empty() {
            return;
        }
        let sector = u64::from(SECTOR_SIZE_BYTES);
        let reserved_bytes =
            u64::from(teslausb_core::fs::fat32::geometry::RESERVED_SECTORS).saturating_mul(sector);
        let mirror_bytes = u64::from(self.geometry.fat_size_sectors()).saturating_mul(sector);
        for (idx, mirror) in self.fat_mirrors.iter().enumerate() {
            let mirror_start = reserved_bytes.saturating_add((idx as u64) * mirror_bytes);
            if let Some(dirty) = self.dirty_fat_mirrors.get(idx) {
                overlay_region(offset, buf, mirror_start, mirror, dirty);
            }
        }

        // Per-directory cluster overlays.
        let data_start_bytes = self.geometry.first_data_sector().saturating_mul(sector);
        let bpc = u64::from(self.bytes_per_cluster);
        let bpc_usize = self.bytes_per_cluster as usize;
        for dir_state in self.directories.values() {
            for (chain_idx, &cluster) in dir_state.chain.iter().enumerate() {
                if cluster < FIRST_DATA_CLUSTER {
                    continue;
                }
                let cluster_offset_in_data = u64::from(cluster - FIRST_DATA_CLUSTER) * bpc;
                let cluster_start = data_start_bytes.saturating_add(cluster_offset_in_data);
                let buf_cluster_start = chain_idx.saturating_mul(bpc_usize);
                let buf_cluster_end = buf_cluster_start.saturating_add(bpc_usize);
                let Some(cluster_bytes) = dir_state.buffer.get(buf_cluster_start..buf_cluster_end)
                else {
                    continue;
                };
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

        // Phase 3.5f addendum: overlay file data clusters from
        // the backing tree via the cluster_map. See
        // `exfat_write::ExfatWriteState::overlay_read` for the
        // full rationale.
        super::exfat_write::overlay_data_clusters_from_cluster_map(
            offset,
            buf,
            &self.cluster_map,
            data_start_bytes,
            bpc,
            self.dir_tree.backing_root(),
        );
    }
}

fn overlay_region(
    read_offset: u64,
    read_buf: &mut [u8],
    region_start: u64,
    source: &[u8],
    dirty_map: &DirtyByteMap,
) {
    overlay_region_with_base(read_offset, read_buf, region_start, source, dirty_map, 0);
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

fn pick_entry_name(long_name: Option<&str>, short_name: &ShortName) -> String {
    if let Some(name) = long_name {
        if !name.is_empty() {
            return name.to_string();
        }
    }
    sfn_to_string(short_name)
}

fn sfn_to_string(short_name: &ShortName) -> String {
    let raw = short_name.as_bytes();
    let base: String = raw
        .get(..8)
        .map(|b| String::from_utf8_lossy(b).trim_end_matches(' ').to_string())
        .unwrap_or_default();
    let ext: String = raw
        .get(8..11)
        .map(|b| String::from_utf8_lossy(b).trim_end_matches(' ').to_string())
        .unwrap_or_default();
    if ext.is_empty() {
        base
    } else {
        format!("{base}.{ext}")
    }
}

fn is_dot_or_dotdot(name: &str) -> bool {
    name == "." || name == ".." || name == ".          " || name == "..         "
}

#[cfg(test)]
#[allow(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
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
    use teslausb_core::fs::fat32::directory::{
        FileAttributes, ShortName, Timestamps, synthesize_lfn_sequence, synthesize_sfn_entry,
    };
    use teslausb_core::fs::fat32::geometry::RESERVED_SECTORS;
    use teslausb_core::fs::geometry::SECTOR_SIZE_BYTES;

    /// 34 MiB volume — matches the convention used by every
    /// FAT32-test module in `teslausb-core`.
    const TEST_VOLUME_BYTES: u64 = 34 * 1024 * 1024;
    const SECTOR: u64 = SECTOR_SIZE_BYTES as u64;

    fn geo() -> Fat32Geometry {
        Fat32Geometry::for_volume_size(TEST_VOLUME_BYTES).expect("34 MiB is a valid FAT32 size")
    }

    fn writer(tmp: &TempDir) -> DirTreeWriter {
        DirTreeWriter::new(tmp.path().to_path_buf()).expect("writer construction")
    }

    fn state(tmp: &TempDir) -> Fat32WriteState {
        Fat32WriteState::new(geo(), writer(tmp), &[])
    }

    fn fat1_start() -> u64 {
        u64::from(RESERVED_SECTORS) * SECTOR
    }

    fn data_start(g: &Fat32Geometry) -> u64 {
        g.first_data_sector() * SECTOR
    }

    fn cluster_to_volume_byte(g: &Fat32Geometry, cluster: u32) -> u64 {
        data_start(g) + u64::from(cluster - FIRST_DATA_CLUSTER) * u64::from(g.bytes_per_cluster())
    }

    fn fat_entry_volume_byte(cluster: u32) -> u64 {
        fat1_start() + u64::from(cluster) * 4
    }

    fn write_fat_entry(state: &mut Fat32WriteState, cluster: u32, value: u32) {
        let bytes = value.to_le_bytes();
        state
            .apply_write(fat_entry_volume_byte(cluster), &bytes)
            .expect("fat entry write");
    }

    fn build_file_dir_entry(name: &str, first_cluster: u32, file_size: u32) -> Vec<u8> {
        let short = ShortName::from_padded_str(&name.to_ascii_uppercase()).unwrap();
        let lfn = synthesize_lfn_sequence(name, short.checksum()).unwrap();
        let sfn = synthesize_sfn_entry(
            &short,
            FileAttributes::archive(),
            first_cluster,
            file_size,
            &Timestamps::epoch(),
        );
        let mut bytes = Vec::new();
        for entry in lfn {
            bytes.extend_from_slice(&entry);
        }
        bytes.extend_from_slice(&sfn);
        bytes
    }

    fn build_dir_dir_entry(name: &str, first_cluster: u32) -> Vec<u8> {
        let short = ShortName::from_padded_str(&name.to_ascii_uppercase()).unwrap();
        let lfn = synthesize_lfn_sequence(name, short.checksum()).unwrap();
        let sfn = synthesize_sfn_entry(
            &short,
            FileAttributes::directory(),
            first_cluster,
            0,
            &Timestamps::epoch(),
        );
        let mut bytes = Vec::new();
        for entry in lfn {
            bytes.extend_from_slice(&entry);
        }
        bytes.extend_from_slice(&sfn);
        bytes
    }

    #[test]
    fn new_state_initializes_root_directory() {
        let tmp = TempDir::new().unwrap();
        let s = state(&tmp);
        assert_eq!(s.directory_count(), 1);
        assert_eq!(s.extent_count(), 0);
        assert_eq!(s.in_flight_file_count(), 0);
    }

    #[test]
    fn fat_mirror_size_matches_geometry() {
        let tmp = TempDir::new().unwrap();
        let s = state(&tmp);
        let g = geo();
        let expected = (g.fat_size_sectors() as usize) * (SECTOR_SIZE_BYTES as usize);
        assert_eq!(s.fat_mirror_size_bytes(), expected);
    }

    #[test]
    fn empty_write_is_a_noop() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        s.apply_write(0, &[]).unwrap();
        assert_eq!(s.extent_count(), 0);
        assert_eq!(s.in_flight_file_count(), 0);
    }

    #[test]
    fn metadata_writes_are_swallowed() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        // Boot sector signature write.
        let mut sig = vec![0u8; 2];
        sig[0] = 0x55;
        sig[1] = 0xAA;
        s.apply_write(510, &sig).unwrap();
        // FSInfo lead signature.
        let fsinfo_sig: [u8; 4] = [0x52, 0x52, 0x61, 0x41];
        s.apply_write(512, &fsinfo_sig).unwrap();
        assert_eq!(s.extent_count(), 0);
        assert_eq!(s.in_flight_file_count(), 0);
    }

    #[test]
    fn unknown_cluster_data_write_is_stashed_not_dropped() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();
        // Write into cluster 5 — not known to any file.
        let offset = cluster_to_volume_byte(&g, 5);
        s.apply_write(offset, &[0xAB; 16]).unwrap();
        assert_eq!(s.pending_data.get(&5).map(|v| v.len()), Some(1));
        assert_eq!(s.in_flight_file_count(), 0);
    }

    #[test]
    fn write_dir_entry_then_fat_then_data_creates_file() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();
        let bpc = g.bytes_per_cluster();

        // 1) Tesla writes dir entry for "hello.bin" -> cluster 3,
        //    size = 5 bytes, into the root dir cluster.
        let dir_entry = build_file_dir_entry("hello.bin", 3, 5);
        let root_byte = cluster_to_volume_byte(&g, 2);
        s.apply_write(root_byte, &dir_entry).unwrap();
        // At this point no FAT chain yet for cluster 3 — pending.
        assert!(s.pending_files.contains_key(&3));
        assert_eq!(s.extent_count(), 0);

        // 2) Tesla writes FAT entry for cluster 3 -> EOC.
        write_fat_entry(&mut s, 3, 0x0FFF_FFFF);
        // Now resolved — cluster_map has an extent.
        assert_eq!(s.extent_count(), 1);

        // 3) Tesla writes file data into cluster 3.
        let data_byte = cluster_to_volume_byte(&g, 3);
        s.apply_write(data_byte, b"hello").unwrap();
        assert_eq!(s.in_flight_file_count(), 1);

        // 4) Flush — file appears in backing tree.
        s.flush().unwrap();
        let path = tmp.path().join("hello.bin");
        assert!(path.exists(), "hello.bin should exist after flush");
        let content = fs::read(&path).unwrap();
        assert_eq!(content, b"hello");
        // After flush the in-flight tracker is empty.
        assert_eq!(s.in_flight_file_count(), 0);
        // Truncate-to-dir-size capped the file at 5 bytes (not
        // the full cluster size of bpc bytes).
        assert_eq!(
            content.len() as u32,
            5,
            "file truncated to dir entry's recorded size, not full cluster"
        );
        let _ = bpc;
    }

    #[test]
    fn out_of_order_data_then_fat_then_dir_entry_still_resolves() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();

        // 1) Data arrives first into cluster 4 — stashed.
        let data_byte = cluster_to_volume_byte(&g, 4);
        s.apply_write(data_byte, b"abcdefgh").unwrap();
        assert!(s.pending_data.contains_key(&4));

        // 2) FAT entry for cluster 4 -> EOC.
        write_fat_entry(&mut s, 4, 0x0FFF_FFFF);
        // Still no dir entry — chain head exists in FAT but
        // unowned. Pending data NOT yet replayed.
        assert!(s.pending_data.contains_key(&4));

        // 3) Dir entry for "out.bin" -> cluster 4, size = 8.
        let dir_entry = build_file_dir_entry("out.bin", 4, 8);
        let root_byte = cluster_to_volume_byte(&g, 2);
        s.apply_write(root_byte, &dir_entry).unwrap();

        // Now resolved; pending data replayed; .partial contains
        // the bytes.
        assert!(!s.pending_data.contains_key(&4));
        s.flush().unwrap();
        let content = fs::read(tmp.path().join("out.bin")).unwrap();
        assert_eq!(content, b"abcdefgh");
    }

    #[test]
    fn multi_cluster_contiguous_file_is_routed_across_clusters() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();
        let bpc = g.bytes_per_cluster() as usize;

        // Build a 2-cluster file at clusters 5,6 with a meaningful
        // distinct payload per cluster (chosen small enough that
        // we can compare easily).
        let payload: Vec<u8> = (0..(bpc * 2 - 7)).map(|i| ((i * 31) % 251) as u8).collect();
        // Dir entry at root.
        let dir_entry = build_file_dir_entry("two.bin", 5, payload.len() as u32);
        s.apply_write(cluster_to_volume_byte(&g, 2), &dir_entry)
            .unwrap();
        // FAT chain 5 -> 6 -> EOC.
        write_fat_entry(&mut s, 5, 6);
        write_fat_entry(&mut s, 6, 0x0FFF_FFFF);
        // Data: write the whole payload starting at cluster 5.
        s.apply_write(cluster_to_volume_byte(&g, 5), &payload)
            .unwrap();
        s.flush().unwrap();
        let read = fs::read(tmp.path().join("two.bin")).unwrap();
        assert_eq!(read.len(), payload.len(), "size matches dir entry");
        assert_eq!(read, payload, "byte content matches");
    }

    #[test]
    fn multi_cluster_fragmented_file_extents_are_correct() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();
        let bpc = g.bytes_per_cluster() as usize;

        // File at clusters 5, 7 (skipping 6) — fragmented.
        let payload_cluster_a: Vec<u8> = vec![0x11; bpc];
        let payload_cluster_b: Vec<u8> = vec![0x22; bpc - 3];
        let total_size = payload_cluster_a.len() + payload_cluster_b.len();
        let dir_entry = build_file_dir_entry("frag.bin", 5, total_size as u32);
        s.apply_write(cluster_to_volume_byte(&g, 2), &dir_entry)
            .unwrap();
        write_fat_entry(&mut s, 5, 7);
        write_fat_entry(&mut s, 7, 0x0FFF_FFFF);
        s.apply_write(cluster_to_volume_byte(&g, 5), &payload_cluster_a)
            .unwrap();
        s.apply_write(cluster_to_volume_byte(&g, 7), &payload_cluster_b)
            .unwrap();
        s.flush().unwrap();
        let read = fs::read(tmp.path().join("frag.bin")).unwrap();
        assert_eq!(read.len(), total_size);
        assert!(read[..bpc].iter().all(|&b| b == 0x11));
        assert!(read[bpc..].iter().all(|&b| b == 0x22));
    }

    #[test]
    fn dir_entry_deletion_keeps_backing_file_and_records_in_retention_set() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();

        // Create a file first.
        let dir_entry = build_file_dir_entry("doomed.bin", 3, 4);
        s.apply_write(cluster_to_volume_byte(&g, 2), &dir_entry)
            .unwrap();
        write_fat_entry(&mut s, 3, 0x0FFF_FFFF);
        s.apply_write(cluster_to_volume_byte(&g, 3), b"data")
            .unwrap();
        s.flush().unwrap();
        let backing = tmp.path().join("doomed.bin");
        assert!(backing.exists());
        assert_eq!(s.extent_count(), 1);
        assert!(s.deleted().is_empty());

        // Now Tesla marks the dir entry deleted — zero the whole
        // dir entry chain to simulate a re-write of the dir
        // cluster without the entry.
        let root_byte = cluster_to_volume_byte(&g, 2);
        let zero = vec![0u8; dir_entry.len()];
        s.apply_write(root_byte, &zero).unwrap();

        // Phase 4.2: extent freed, .partial discarded, BUT the
        // committed backing file is preserved and the path is
        // recorded in the retention `DeletedSet` for the cleanup
        // worker to evaluate against its GPS / SEI policy.
        assert_eq!(s.extent_count(), 0);
        assert!(
            backing.exists(),
            "Phase 4.2: backing file must persist past Tesla's dir-entry delete"
        );
        assert_eq!(s.deleted().len(), 1);
        assert!(s.deleted().contains(Path::new("doomed.bin")));
    }

    #[test]
    fn recreating_deleted_path_clears_retention_mark() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();

        // Create, then delete.
        let dir_entry = build_file_dir_entry("respawn.bin", 3, 4);
        s.apply_write(cluster_to_volume_byte(&g, 2), &dir_entry)
            .unwrap();
        write_fat_entry(&mut s, 3, 0x0FFF_FFFF);
        s.apply_write(cluster_to_volume_byte(&g, 3), b"data")
            .unwrap();
        s.flush().unwrap();

        let root_byte = cluster_to_volume_byte(&g, 2);
        let zero = vec![0u8; dir_entry.len()];
        s.apply_write(root_byte, &zero).unwrap();
        assert!(s.deleted().contains(Path::new("respawn.bin")));

        // Tesla re-creates a file with the same name (different
        // cluster). The retention mark must clear so the cleanup
        // worker won't reap the now-live file.
        let new_entry = build_file_dir_entry("respawn.bin", 5, 4);
        s.apply_write(root_byte, &new_entry).unwrap();
        assert!(
            !s.deleted().contains(Path::new("respawn.bin")),
            "re-creating a deleted path must clear its retention mark"
        );
        assert!(s.deleted().is_empty());
    }

    #[test]
    fn subdir_entry_registers_subdirectory_and_decodes_its_children() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();

        // Root has a subdir "TeslaCam" -> cluster 3.
        let sub_entry = build_dir_dir_entry("teslacam", 3);
        s.apply_write(cluster_to_volume_byte(&g, 2), &sub_entry)
            .unwrap();
        write_fat_entry(&mut s, 3, 0x0FFF_FFFF);
        assert_eq!(s.directory_count(), 2);

        // Subdir holds a file "clip.mp4" -> cluster 4, size 6.
        let file_entry = build_file_dir_entry("clip.mp4", 4, 6);
        s.apply_write(cluster_to_volume_byte(&g, 3), &file_entry)
            .unwrap();
        write_fat_entry(&mut s, 4, 0x0FFF_FFFF);
        s.apply_write(cluster_to_volume_byte(&g, 4), b"abcdef")
            .unwrap();
        s.flush().unwrap();
        let expected = tmp.path().join("teslacam").join("clip.mp4");
        assert!(
            expected.exists(),
            "subdir file should land in subdir on disk"
        );
        assert_eq!(fs::read(&expected).unwrap(), b"abcdef");
    }

    #[test]
    fn pre_existing_file_seeded_on_first_overwrite_preserves_unwritten_bytes() {
        let tmp = TempDir::new().unwrap();
        // Seed the backing tree with a 16-byte file before
        // constructing the state.
        let initial: Vec<u8> = (0..16u8).collect();
        let backing_file = tmp.path().join("seed.bin");
        fs::write(&backing_file, &initial).unwrap();
        // Pre-seed the cluster_map with the file's planned
        // extent at cluster 3 (matching the dir entry the test
        // writes below).
        let pre = vec![PreExistingExtent {
            first_cluster: 3,
            cluster_count: 1,
            first_byte_in_file: 0,
            file_size_bytes: 16,
            relative_path: PathBuf::from("seed.bin"),
        }];
        let mut s = Fat32WriteState::new(geo(), writer(&tmp), &pre);
        let g = geo();

        // Tesla overwrites bytes 4..8 with a recognizable pattern.
        // No dir entry / FAT write needed — the writer already
        // knows cluster 3 belongs to seed.bin from the pre-seeded
        // cluster map.
        s.apply_write(cluster_to_volume_byte(&g, 3) + 4, &[0xAA, 0xBB, 0xCC, 0xDD])
            .unwrap();
        s.flush().unwrap();
        let read = fs::read(&backing_file).unwrap();
        assert_eq!(read.len(), 16, "size preserved");
        for (i, &b) in read.iter().enumerate() {
            match i {
                4 => assert_eq!(b, 0xAA),
                5 => assert_eq!(b, 0xBB),
                6 => assert_eq!(b, 0xCC),
                7 => assert_eq!(b, 0xDD),
                _ => assert_eq!(b, i as u8, "byte {i} should be original 0x{:02X}", i as u8),
            }
        }
    }

    #[test]
    fn flush_truncates_to_dir_entry_size_not_full_cluster() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();
        // File whose dir entry reports 3 bytes but whose cluster
        // is much bigger.
        let dir_entry = build_file_dir_entry("small.bin", 3, 3);
        s.apply_write(cluster_to_volume_byte(&g, 2), &dir_entry)
            .unwrap();
        write_fat_entry(&mut s, 3, 0x0FFF_FFFF);
        s.apply_write(cluster_to_volume_byte(&g, 3), b"abc")
            .unwrap();
        s.flush().unwrap();
        let path = tmp.path().join("small.bin");
        let metadata = fs::metadata(&path).unwrap();
        assert_eq!(metadata.len(), 3);
        assert_eq!(fs::read(&path).unwrap(), b"abc");
    }

    #[test]
    fn duplicate_apply_write_is_idempotent() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();
        let dir_entry = build_file_dir_entry("idem.bin", 3, 5);
        s.apply_write(cluster_to_volume_byte(&g, 2), &dir_entry)
            .unwrap();
        write_fat_entry(&mut s, 3, 0x0FFF_FFFF);
        s.apply_write(cluster_to_volume_byte(&g, 3), b"hello")
            .unwrap();
        // Now replay every step a second time — same result.
        s.apply_write(cluster_to_volume_byte(&g, 2), &dir_entry)
            .unwrap();
        write_fat_entry(&mut s, 3, 0x0FFF_FFFF);
        s.apply_write(cluster_to_volume_byte(&g, 3), b"hello")
            .unwrap();
        s.flush().unwrap();
        assert_eq!(fs::read(tmp.path().join("idem.bin")).unwrap(), b"hello");
        assert_eq!(s.extent_count(), 1, "no extent duplication on replay");
    }

    #[test]
    fn second_flush_with_no_writes_is_noop() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        s.flush().unwrap();
        s.flush().unwrap();
    }

    #[test]
    fn write_to_reserved_cluster_is_warned_and_dropped() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        // Cluster 0 doesn't exist as a data cluster — but the
        // decoder shouldn't emit one for offsets in the data
        // region anyway. We invoke the routine directly to
        // confirm defensive handling.
        s.apply_data_cluster_write(0, 0, &[0u8; 4]).unwrap();
        s.apply_data_cluster_write(1, 0, &[0u8; 4]).unwrap();
        assert!(s.pending_data.is_empty());
    }

    // === Phase 3.5f regression tests ===

    /// Bug H3-1 regression (FAT32 mirror of the exFAT bug): when
    /// the kernel rewrites the directory entry with a larger
    /// `file_size` (longer cluster chain), the new tail clusters
    /// must be inserted into the cluster map. The pre-fix
    /// idempotent-skip path silently dropped them.
    #[test]
    fn growing_extent_replaces_stale_chain_h3_1() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);
        let g = geo();
        let bpc = g.bytes_per_cluster() as usize;

        // Build a 4-cluster chain rooted at cluster 5.
        let chain: [u32; 4] = [5, 6, 7, 8];
        for window in chain.windows(2) {
            write_fat_entry(&mut s, window[0], window[1]);
        }
        write_fat_entry(&mut s, *chain.last().unwrap(), 0x0FFF_FFFF);

        // First dir entry: short — 2 clusters' worth of data.
        let short_size = (2 * bpc) as u32;
        let entry_short = build_file_dir_entry("VIDEO.MP4", 5, short_size);
        s.apply_write(cluster_to_volume_byte(&g, 2), &entry_short)
            .expect("short dir");

        // Now extend the dir entry to the final 4-cluster size.
        let final_size = (4 * bpc) as u32;
        let entry_full = build_file_dir_entry("VIDEO.MP4", 5, final_size);
        s.apply_write(cluster_to_volume_byte(&g, 2), &entry_full)
            .expect("full dir");

        // Write data to every cluster including the new tail.
        for &c in &chain {
            let mut payload = vec![0u8; bpc];
            payload[0] = c as u8;
            s.apply_write(cluster_to_volume_byte(&g, c), &payload)
                .expect("data write");
        }
        s.flush().expect("flush");

        // The backing file must be exactly 4 clusters long with
        // no zero-filled tail.
        let final_path = tmp.path().join("VIDEO.MP4");
        let bytes = fs::read(&final_path).expect("file finalized");
        assert_eq!(
            bytes.len(),
            4 * bpc,
            "all 4 clusters must be present (H3-1 regression)"
        );
        for (i, &c) in chain.iter().enumerate() {
            assert_eq!(
                bytes[i * bpc],
                c as u8,
                "cluster {c} payload must be at offset {}",
                i * bpc
            );
        }
    }

    // === Bug "fat32-cluster-reuse" regression tests (2026-05-22) ===
    // Symmetric to the exFAT fix; same root cause.

    /// Tesla can free a file's clusters and reuse them for a
    /// different file without our `remove_file` / `remove_at`
    /// paths catching every prior owner. Pre-fix, the stale
    /// extent blocked the new file's `cluster_map.insert`,
    /// orphaned the new file's data writes in `pending_data`,
    /// and routed any subsequent data writes for the reused
    /// clusters to the old file's backing path (silent
    /// corruption of both files).
    #[test]
    fn fat32_cluster_reuse_for_different_path_evicts_stale_extent_and_writes_correct_bytes() {
        let tmp = TempDir::new().unwrap();
        let g = geo();
        let bpc = g.bytes_per_cluster() as usize;

        // Pre-seed cluster_map with a stale 1-cluster extent for
        // an "old" clip at cluster 3.
        let old_extent = PreExistingExtent {
            first_cluster: 3,
            cluster_count: 1,
            first_byte_in_file: 0,
            file_size_bytes: bpc as u64,
            relative_path: PathBuf::from("OLD.MP4"),
        };
        fs::write(tmp.path().join("OLD.MP4"), vec![0u8; bpc]).unwrap();
        let mut s = Fat32WriteState::new(g.clone(), writer(&tmp), &[old_extent]);
        assert_eq!(s.extent_count(), 1);

        // Tesla advertises NEW.MP4 at the same cluster 3 (bitmap
        // / FSInfo reuse). Sequence: dir entry → FAT → data, with
        // dir-entry first to expose the pre-fix ordering bug.
        let payload = vec![0xBBu8; bpc];
        let entry = build_file_dir_entry("NEW.MP4", 3, payload.len() as u32);
        s.apply_write(cluster_to_volume_byte(&g, 2), &entry)
            .expect("dir");
        write_fat_entry(&mut s, 3, 0x0FFF_FFFF);
        s.apply_write(cluster_to_volume_byte(&g, 3), &payload)
            .expect("data");
        s.flush().expect("flush");

        // Post-fix: NEW.MP4 owns cluster 3, data lands in it.
        let new_bytes = fs::read(tmp.path().join("NEW.MP4")).expect("NEW.MP4 must be finalized");
        assert_eq!(new_bytes, payload);
        // OLD.MP4 is untouched — eviction is in-memory only.
        let old_bytes = fs::read(tmp.path().join("OLD.MP4")).unwrap();
        assert!(
            old_bytes.iter().all(|&b| b == 0),
            "old clip's backing bytes must not be overwritten via the reused cluster"
        );
    }

    /// Partial-overlap variant: the new file's chain only partly
    /// overlaps a stale extent. Eviction is whole-extent, not
    /// per-cluster trim — the dir entry is authoritative.
    #[test]
    fn fat32_cluster_reuse_partial_overlap_evicts_stale_extent() {
        let tmp = TempDir::new().unwrap();
        let g = geo();
        let bpc = g.bytes_per_cluster() as usize;
        // Stale extent at [5, 10).
        let old_extent = PreExistingExtent {
            first_cluster: 5,
            cluster_count: 5,
            first_byte_in_file: 0,
            file_size_bytes: bpc as u64,
            relative_path: PathBuf::from("OLD.MP4"),
        };
        fs::write(tmp.path().join("OLD.MP4"), vec![0u8; bpc]).unwrap();
        let mut s = Fat32WriteState::new(g.clone(), writer(&tmp), &[old_extent]);
        assert_eq!(s.extent_count(), 1);

        // New clip claims only cluster 7 (mid-range of the stale).
        let payload = vec![0xCCu8; bpc];
        let entry = build_file_dir_entry("NEW.MP4", 7, payload.len() as u32);
        s.apply_write(cluster_to_volume_byte(&g, 2), &entry)
            .expect("dir");
        write_fat_entry(&mut s, 7, 0x0FFF_FFFF);
        s.apply_write(cluster_to_volume_byte(&g, 7), &payload)
            .expect("data");
        s.flush().expect("flush");

        let new_bytes = fs::read(tmp.path().join("NEW.MP4")).expect("NEW.MP4 must be finalized");
        assert_eq!(new_bytes, payload);
        assert_eq!(s.extent_count(), 1);
    }

    /// Bug H3-2 regression: a read covering the FAT and root
    /// directory region after a write must return the kernel-
    /// written bytes (not the synth's startup snapshot).
    #[test]
    fn overlay_read_returns_kernel_written_fat_and_dir_bytes_h3_2() {
        let tmp = TempDir::new().unwrap();
        let mut s = state(&tmp);

        // Kernel writes a FAT entry.
        write_fat_entry(&mut s, 5, 0x0FFF_FFFF);
        // Kernel writes a dir entry into the root cluster (2).
        let g = geo();
        let entry = build_file_dir_entry("HI.TXT", 5, 12);
        s.apply_write(cluster_to_volume_byte(&g, 2), &entry)
            .expect("dir write");

        // Read back the FAT entry via overlay.
        let off = fat_entry_volume_byte(5);
        let mut fat_buf = [0u8; 4];
        s.overlay_read(off, &mut fat_buf);
        assert_eq!(
            u32::from_le_bytes(fat_buf),
            0x0FFF_FFFF,
            "overlay must surface kernel-written FAT entry"
        );

        // Read back the dir cluster start via overlay.
        let mut dir_buf = vec![0u8; entry.len()];
        s.overlay_read(cluster_to_volume_byte(&g, 2), &mut dir_buf);
        assert_eq!(
            &dir_buf, &entry,
            "overlay must surface kernel-written dir entry bytes"
        );

        // Read of an unrelated region returns the caller's buffer
        // unchanged (overlay does not corrupt synth bytes).
        let mut elsewhere = vec![0xAAu8; 32];
        let before = elsewhere.clone();
        s.overlay_read(off + 4096, &mut elsewhere);
        assert_eq!(elsewhere, before, "overlay must skip non-dirty regions");
    }
}
