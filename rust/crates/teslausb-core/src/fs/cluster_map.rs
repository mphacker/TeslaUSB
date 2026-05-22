//! Cluster → file extent map for the write-side pipeline
//! (Phase 3.4).
//!
//! Phase 3.1 and 3.2 ship the *decoders* — given an NBD write,
//! they classify each byte into a typed per-region chunk
//! ([`crate::fs::fat32::parse::DecodedWrite`] /
//! [`crate::fs::exfat::parse::DecodedWrite`]). Phase 3.3 ships
//! the *POSIX writer*, which routes a `(relative_path,
//! byte_in_file, &[u8])` onto disk. This module is the bridge
//! between them: given a `DataCluster { cluster_number, .. }`
//! chunk, it answers the question *"which file does this cluster
//! belong to, and at what byte offset within that file?"*.
//!
//! ## Extent-based representation
//!
//! Naïvely, the map could be `HashMap<u32, FileExtent>` with one
//! entry per cluster, but a 32 GiB FAT32 volume with 32 KiB
//! clusters holds ~1 M clusters and storing one entry each
//! would burn ~100 MiB of RAM on the Pi Zero 2 W. The
//! cluster-layout planner ([`crate::fs::cluster_layout`]) hands
//! out contiguous cluster ranges to each file, so we represent
//! each file as a single [`FileExtent`] keyed by its
//! `first_cluster` in a [`BTreeMap`]. Lookup is O(log N) where N
//! is the file count, not the cluster count.
//!
//! Phase 3.4's scope is the contiguous case the read-side
//! already needs. Tesla's write-side might (rarely) split a file
//! across non-contiguous extents — the `BTreeMap` layout permits
//! that naturally: each extent gets its own key. Subsequent
//! phases that handle dynamic allocation just call [`ClusterMap::insert`]
//! multiple times for one file path.
//!
//! ## What this module does NOT do
//!
//! * It does not allocate clusters. That is
//!   [`crate::fs::cluster_layout::ClusterAllocator`] on the
//!   read-side and (eventually) Phase 3.5 on the write-side.
//! * It does not interpret directory entries. Phase 3.5's
//!   wiring layer parses dir-entry writes inline to discover
//!   filenames; this module only stores the resulting
//!   `(cluster_number, file_path, byte_in_file)` triples.
//! * It does not enforce file existence on disk. A
//!   [`FileExtent`] holds a [`PathBuf`] that is the *intended*
//!   relative path for the chunk; whether that path actually
//!   exists on the backing tree is the POSIX writer's
//!   ([`crate::fs`] is layer-1; the writer lives in `teslafat`
//!   at layer 3, and `.partial` files materialize on demand).
//!
//! ## Concurrency
//!
//! [`ClusterMap`] holds no internal lock. Plan: at Phase 3.5
//! wire-time it is wrapped in `Arc<ArcSwap<ClusterMap>>` (or a
//! single `Arc<RwLock<...>>` if profiling argues otherwise) so
//! that NBD read requests can dispatch against the current
//! snapshot lock-free while a write request is updating a
//! private clone. The wrapping choice is intentionally NOT
//! baked into this module so the storage layer stays free of
//! synchronization concerns. The cost is that the caller must
//! own the snapshot policy; the benefit is that this module
//! stays pure data.

use std::collections::BTreeMap;
use std::collections::btree_map::Range;
use std::path::{Path, PathBuf};

use crate::fs::cluster_layout::FIRST_DATA_CLUSTER;

/// A contiguous run of clusters belonging to one file.
///
/// `first_byte_in_file` lets the same struct represent a file
/// split across multiple non-contiguous extents (the second
/// extent would carry `first_byte_in_file == size_of_first_extent`).
/// In the contiguous case the value is `0`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileExtent {
    /// First cluster number in the extent. Must be
    /// `>= FIRST_DATA_CLUSTER` (clusters 0 and 1 are reserved).
    pub first_cluster: u32,
    /// Number of contiguous clusters in the extent. Must be
    /// `>= 1`; the empty extent is represented by the absence of
    /// a [`ClusterMap`] entry, not a zero-count [`FileExtent`].
    pub cluster_count: u32,
    /// Byte offset within `file_path` of the first byte covered
    /// by this extent. `0` for the file's first extent; non-zero
    /// for subsequent extents of a file split across multiple
    /// runs.
    pub first_byte_in_file: u64,
    /// Path of the backing file the extent maps onto, relative to
    /// the backing root. Carried as `PathBuf` so the consumer can
    /// hand it directly to `teslafat::backend::dir_tree::DirTreeWriter::apply_chunk`
    /// (in the layer-3 daemon crate) without copying.
    pub file_path: PathBuf,
}

impl FileExtent {
    /// The half-open `[first_cluster, first_cluster + cluster_count)`
    /// cluster range covered by this extent.
    #[must_use]
    pub const fn cluster_range(&self) -> core::ops::Range<u32> {
        self.first_cluster..(self.first_cluster.saturating_add(self.cluster_count))
    }

    /// `true` if `cluster` falls inside this extent.
    #[must_use]
    pub const fn contains(&self, cluster: u32) -> bool {
        cluster >= self.first_cluster
            && cluster < self.first_cluster.saturating_add(self.cluster_count)
    }
}

/// Errors returned by [`ClusterMap`] mutators.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ClusterMapError {
    /// [`ClusterMap::insert`] received a [`FileExtent`] with
    /// `first_cluster < FIRST_DATA_CLUSTER` — clusters 0 and 1
    /// are reserved and never own file data.
    #[error("first_cluster {first_cluster} is reserved (must be >= {minimum})")]
    ReservedCluster {
        /// The offending value.
        first_cluster: u32,
        /// The minimum allowed value ([`FIRST_DATA_CLUSTER`]).
        minimum: u32,
    },
    /// [`ClusterMap::insert`] received a zero-count extent. The
    /// caller's contract is that empty files have no extent in
    /// the map at all.
    #[error("cluster_count must be >= 1; got 0 for first_cluster {first_cluster}")]
    EmptyExtent {
        /// The offending extent's `first_cluster`.
        first_cluster: u32,
    },
    /// [`ClusterMap::insert`] received an extent that overlaps an
    /// existing one. The map's invariant is that each cluster is
    /// owned by at most one file at a time; the caller must
    /// remove the conflicting extent first.
    #[error(
        "extent [{first_cluster}, {end_cluster_exclusive}) overlaps existing extent \
         [{existing_first}, {existing_end_exclusive}) (owner {existing_path:?})"
    )]
    Overlap {
        /// First cluster of the rejected extent.
        first_cluster: u32,
        /// First cluster past the end of the rejected extent.
        end_cluster_exclusive: u32,
        /// First cluster of the conflicting extent already in the map.
        existing_first: u32,
        /// First cluster past the end of the conflicting extent.
        existing_end_exclusive: u32,
        /// Path of the file that already owns the conflicting extent.
        existing_path: PathBuf,
    },
}

/// Map from cluster number to [`FileExtent`], keyed for
/// O(log N) lookup. See the module docs for context.
#[derive(Debug, Default, Clone)]
pub struct ClusterMap {
    /// `first_cluster` → extent. Sorted iteration of values
    /// yields extents in ascending cluster order.
    extents: BTreeMap<u32, FileExtent>,
}

impl ClusterMap {
    /// Construct an empty map.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Number of extents currently in the map.
    #[must_use]
    pub fn len(&self) -> usize {
        self.extents.len()
    }

    /// `true` if the map contains zero extents.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.extents.is_empty()
    }

    /// Insert `extent`, rejecting reserved clusters, empty
    /// extents, and overlaps.
    ///
    /// # Errors
    ///
    /// * [`ClusterMapError::ReservedCluster`] if
    ///   `extent.first_cluster < FIRST_DATA_CLUSTER`.
    /// * [`ClusterMapError::EmptyExtent`] if
    ///   `extent.cluster_count == 0`.
    /// * [`ClusterMapError::Overlap`] if `extent` shares any
    ///   cluster with an extent already in the map.
    pub fn insert(&mut self, extent: FileExtent) -> Result<(), ClusterMapError> {
        if extent.first_cluster < FIRST_DATA_CLUSTER {
            return Err(ClusterMapError::ReservedCluster {
                first_cluster: extent.first_cluster,
                minimum: FIRST_DATA_CLUSTER,
            });
        }
        if extent.cluster_count == 0 {
            return Err(ClusterMapError::EmptyExtent {
                first_cluster: extent.first_cluster,
            });
        }
        let end_excl = extent.first_cluster.saturating_add(extent.cluster_count);
        if let Some(conflict) = self.find_overlapping(extent.first_cluster, end_excl) {
            return Err(ClusterMapError::Overlap {
                first_cluster: extent.first_cluster,
                end_cluster_exclusive: end_excl,
                existing_first: conflict.first_cluster,
                existing_end_exclusive: conflict
                    .first_cluster
                    .saturating_add(conflict.cluster_count),
                existing_path: conflict.file_path.clone(),
            });
        }
        self.extents.insert(extent.first_cluster, extent);
        Ok(())
    }

    /// Look up the extent containing `cluster`, returning the
    /// extent and the byte offset within its owning file.
    ///
    /// Returns `None` if `cluster` is outside every extent in
    /// the map (which includes reserved clusters 0 and 1).
    #[must_use]
    pub fn lookup(&self, cluster: u32) -> Option<ClusterLookup<'_>> {
        // BTreeMap::range from the start to `cluster` inclusive
        // gives us every extent whose `first_cluster <= cluster`;
        // the last one is the only candidate.
        let (&first_cluster, extent) = self.extents.range(..=cluster).next_back()?;
        if !extent.contains(cluster) {
            return None;
        }
        let offset_in_extent_clusters = u64::from(cluster - first_cluster);
        Some(ClusterLookup {
            extent,
            offset_in_extent_clusters,
        })
    }

    /// Remove and return the extent whose `first_cluster ==
    /// first_cluster`, if any.
    ///
    /// Used when Tesla deletes a file (the dir-entry is cleared)
    /// or shortens it (the trailing extent's clusters return to
    /// the free pool).
    pub fn remove_at(&mut self, first_cluster: u32) -> Option<FileExtent> {
        self.extents.remove(&first_cluster)
    }

    /// Remove every extent owned by `file_path`, returning the
    /// number removed.
    ///
    /// Used for whole-file delete when the caller has only the
    /// path, not the first cluster.
    pub fn remove_file(&mut self, file_path: &Path) -> usize {
        let keys: Vec<u32> = self
            .extents
            .iter()
            .filter(|(_, e)| e.file_path == file_path)
            .map(|(&k, _)| k)
            .collect();
        for key in &keys {
            self.extents.remove(key);
        }
        keys.len()
    }

    /// Remove and return every extent whose cluster range intersects
    /// the half-open range `[start, end_exclusive)`, in ascending
    /// `first_cluster` order.
    ///
    /// Used when a freshly-arrived directory entry authoritatively
    /// claims a cluster range. Tesla may have freed the prior
    /// owner via the allocation bitmap without rewriting the
    /// directory entry (so [`Self::remove_at`] /
    /// [`Self::remove_file`] never fire), leaving stale extents
    /// in the map. Without eviction those stale extents block
    /// [`Self::insert`] with [`ClusterMapError::Overlap`] and the
    /// new file's data writes end up stashed in `pending_data`
    /// forever (zero-filled gaps on the backing tree, observed
    /// in the wild as ~1,200 "cluster map insert overlaps a
    /// different owner; skipping" warnings per boot on
    /// 2026-05-22).
    pub fn remove_overlapping(&mut self, start: u32, end_exclusive: u32) -> Vec<FileExtent> {
        if end_exclusive <= start {
            return Vec::new();
        }
        let keys: Vec<u32> = self
            .extents_in_range(start, end_exclusive)
            .map(|e| e.first_cluster)
            .collect();
        keys.into_iter()
            .filter_map(|k| self.extents.remove(&k))
            .collect()
    }

    /// Iterate the map's extents in ascending `first_cluster`
    /// order.
    pub fn extents(&self) -> impl Iterator<Item = &FileExtent> {
        self.extents.values()
    }

    /// Iterate the extents whose cluster range intersects the
    /// half-open `[start, end)` range, in ascending order. Used
    /// by Phase 3.5's whole-write dispatcher to find every
    /// extent a multi-cluster write touches in one pass.
    #[must_use]
    pub fn extents_in_range(&self, start: u32, end: u32) -> ExtentsInRange<'_> {
        // Find the first key that's potentially relevant: the
        // last extent whose first_cluster <= start may extend
        // into [start, end). Use range_from = key just before
        // that candidate so the iterator yields it.
        let start_key = self
            .extents
            .range(..=start)
            .next_back()
            .map_or(start, |(&k, _)| k);
        ExtentsInRange {
            inner: self.extents.range(start_key..end),
            range_start: start,
            range_end: end,
        }
    }

    fn find_overlapping(&self, start: u32, end_exclusive: u32) -> Option<&FileExtent> {
        // Candidate 1: the extent whose first_cluster is the
        // largest <= start. If it extends past `start`, overlap.
        if let Some((&_k, extent)) = self.extents.range(..=start).next_back() {
            let existing_end = extent.first_cluster.saturating_add(extent.cluster_count);
            if existing_end > start {
                return Some(extent);
            }
        }
        // Candidate 2: any extent whose first_cluster is in
        // [start + 1, end_exclusive). Those start inside the
        // candidate range and therefore overlap.
        if let Some((_, extent)) = self
            .extents
            .range(start.saturating_add(1)..end_exclusive)
            .next()
        {
            return Some(extent);
        }
        None
    }
}

/// Result of [`ClusterMap::lookup`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClusterLookup<'a> {
    /// The extent that owns the looked-up cluster.
    pub extent: &'a FileExtent,
    /// Distance in clusters from `extent.first_cluster` to the
    /// looked-up cluster.
    pub offset_in_extent_clusters: u64,
}

impl ClusterLookup<'_> {
    /// Byte offset within [`FileExtent::file_path`] of the start
    /// of the looked-up cluster, given the volume's
    /// `bytes_per_cluster`.
    #[must_use]
    pub fn byte_in_file_at_cluster_start(&self, bytes_per_cluster: u32) -> u64 {
        self.extent.first_byte_in_file
            + self
                .offset_in_extent_clusters
                .saturating_mul(u64::from(bytes_per_cluster))
    }
}

/// Iterator returned by [`ClusterMap::extents_in_range`].
pub struct ExtentsInRange<'a> {
    inner: Range<'a, u32, FileExtent>,
    range_start: u32,
    range_end: u32,
}

impl<'a> Iterator for ExtentsInRange<'a> {
    type Item = &'a FileExtent;

    fn next(&mut self) -> Option<Self::Item> {
        for (_, extent) in self.inner.by_ref() {
            let extent_end = extent.first_cluster.saturating_add(extent.cluster_count);
            // The BTreeMap range guarantees first_cluster < end;
            // also require extent_end > start to skip extents
            // entirely below the requested range.
            if extent_end > self.range_start && extent.first_cluster < self.range_end {
                return Some(extent);
            }
        }
        None
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
    use super::*;

    fn extent(first: u32, count: u32, path: &str) -> FileExtent {
        FileExtent {
            first_cluster: first,
            cluster_count: count,
            first_byte_in_file: 0,
            file_path: PathBuf::from(path),
        }
    }

    #[test]
    fn new_map_is_empty() {
        let m = ClusterMap::new();
        assert!(m.is_empty());
        assert_eq!(m.len(), 0);
        assert!(m.lookup(2).is_none());
    }

    #[test]
    fn insert_then_lookup_inside_extent_returns_extent() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 5, "foo.mp4")).expect("insert OK");
        let found = m.lookup(12).expect("12 is in [10, 15)");
        assert_eq!(found.extent.first_cluster, 10);
        assert_eq!(found.extent.cluster_count, 5);
        assert_eq!(found.extent.file_path, PathBuf::from("foo.mp4"));
        assert_eq!(found.offset_in_extent_clusters, 2);
    }

    #[test]
    fn lookup_at_first_cluster_returns_offset_zero() {
        let mut m = ClusterMap::new();
        m.insert(extent(100, 3, "a.bin")).expect("insert OK");
        let found = m.lookup(100).expect("100 is first cluster");
        assert_eq!(found.offset_in_extent_clusters, 0);
    }

    #[test]
    fn lookup_at_last_cluster_returns_count_minus_one_offset() {
        let mut m = ClusterMap::new();
        m.insert(extent(100, 3, "a.bin")).expect("insert OK");
        let found = m.lookup(102).expect("102 is last cluster");
        assert_eq!(found.offset_in_extent_clusters, 2);
    }

    #[test]
    fn lookup_just_past_extent_returns_none() {
        let mut m = ClusterMap::new();
        m.insert(extent(100, 3, "a.bin")).expect("insert OK");
        assert!(m.lookup(103).is_none());
    }

    #[test]
    fn lookup_below_first_extent_returns_none() {
        let mut m = ClusterMap::new();
        m.insert(extent(100, 3, "a.bin")).expect("insert OK");
        assert!(m.lookup(50).is_none());
    }

    #[test]
    fn lookup_between_two_extents_returns_none() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 5, "a.bin")).expect("insert a");
        m.insert(extent(100, 5, "b.bin")).expect("insert b");
        assert!(m.lookup(50).is_none());
        assert!(m.lookup(15).is_none());
        assert!(m.lookup(99).is_none());
    }

    #[test]
    fn insert_reserved_cluster_zero_is_rejected() {
        let mut m = ClusterMap::new();
        let err = m
            .insert(extent(0, 1, "x"))
            .expect_err("cluster 0 is reserved");
        assert!(matches!(
            err,
            ClusterMapError::ReservedCluster {
                first_cluster: 0,
                minimum: 2,
            }
        ));
    }

    #[test]
    fn insert_reserved_cluster_one_is_rejected() {
        let mut m = ClusterMap::new();
        let err = m
            .insert(extent(1, 1, "x"))
            .expect_err("cluster 1 is reserved");
        assert!(matches!(
            err,
            ClusterMapError::ReservedCluster {
                first_cluster: 1,
                minimum: 2,
            }
        ));
    }

    #[test]
    fn insert_empty_extent_is_rejected() {
        let mut m = ClusterMap::new();
        let err = m
            .insert(extent(10, 0, "x"))
            .expect_err("cluster_count 0 is rejected");
        assert!(matches!(
            err,
            ClusterMapError::EmptyExtent { first_cluster: 10 }
        ));
    }

    #[test]
    fn insert_overlapping_extent_is_rejected_left_overlap() {
        let mut m = ClusterMap::new();
        m.insert(extent(20, 5, "a.bin")).expect("insert a");
        // [18, 22) overlaps [20, 25) on the right of the new one.
        let err = m
            .insert(extent(18, 4, "b.bin"))
            .expect_err("left-overlap rejected");
        assert!(matches!(err, ClusterMapError::Overlap { .. }));
    }

    #[test]
    fn insert_overlapping_extent_is_rejected_right_overlap() {
        let mut m = ClusterMap::new();
        m.insert(extent(20, 5, "a.bin")).expect("insert a");
        // [22, 28) overlaps [20, 25) on the right of the existing one.
        let err = m
            .insert(extent(22, 6, "b.bin"))
            .expect_err("right-overlap rejected");
        assert!(matches!(err, ClusterMapError::Overlap { .. }));
    }

    #[test]
    fn insert_overlapping_extent_is_rejected_inside_existing() {
        let mut m = ClusterMap::new();
        m.insert(extent(20, 10, "a.bin")).expect("insert a");
        let err = m
            .insert(extent(22, 3, "b.bin"))
            .expect_err("inside-overlap rejected");
        assert!(matches!(err, ClusterMapError::Overlap { .. }));
    }

    #[test]
    fn insert_overlapping_extent_is_rejected_containing_existing() {
        let mut m = ClusterMap::new();
        m.insert(extent(20, 3, "a.bin")).expect("insert a");
        let err = m
            .insert(extent(18, 10, "b.bin"))
            .expect_err("containing-overlap rejected");
        assert!(matches!(err, ClusterMapError::Overlap { .. }));
    }

    #[test]
    fn insert_adjacent_extents_no_overlap() {
        let mut m = ClusterMap::new();
        m.insert(extent(20, 5, "a.bin")).expect("insert a");
        // [25, 28) is exactly adjacent; not an overlap.
        m.insert(extent(25, 3, "b.bin"))
            .expect("adjacent insert OK");
        assert_eq!(m.len(), 2);
    }

    #[test]
    fn insert_multiple_disjoint_extents_for_same_file() {
        // File split across two extents (rare but allowed).
        let mut m = ClusterMap::new();
        let e1 = FileExtent {
            first_cluster: 10,
            cluster_count: 3,
            first_byte_in_file: 0,
            file_path: PathBuf::from("split.bin"),
        };
        let e2 = FileExtent {
            first_cluster: 100,
            cluster_count: 2,
            first_byte_in_file: 3 * 4096,
            file_path: PathBuf::from("split.bin"),
        };
        m.insert(e1).expect("insert e1");
        m.insert(e2).expect("insert e2");
        assert_eq!(m.len(), 2);
        let lookup1 = m.lookup(11).expect("11 is in e1");
        assert_eq!(lookup1.byte_in_file_at_cluster_start(4096), 4096);
        let lookup2 = m.lookup(101).expect("101 is in e2");
        assert_eq!(lookup2.byte_in_file_at_cluster_start(4096), 3 * 4096 + 4096);
    }

    #[test]
    fn byte_in_file_at_cluster_start_is_first_byte_for_first_cluster() {
        let mut m = ClusterMap::new();
        m.insert(FileExtent {
            first_cluster: 50,
            cluster_count: 4,
            first_byte_in_file: 8192,
            file_path: PathBuf::from("x"),
        })
        .expect("insert");
        let found = m.lookup(50).expect("50 is first");
        assert_eq!(found.byte_in_file_at_cluster_start(4096), 8192);
    }

    #[test]
    fn remove_at_returns_extent_when_present() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 3, "a")).expect("insert");
        let removed = m.remove_at(10).expect("removal returns extent");
        assert_eq!(removed.first_cluster, 10);
        assert!(m.is_empty());
        assert!(m.lookup(11).is_none());
    }

    #[test]
    fn remove_at_returns_none_for_unknown_first_cluster() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 3, "a")).expect("insert");
        assert!(m.remove_at(20).is_none());
        // Mid-extent cluster is NOT a valid removal key.
        assert!(m.remove_at(11).is_none());
    }

    #[test]
    fn remove_file_removes_every_extent_of_that_path() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 3, "split.bin")).expect("insert 1");
        m.insert(extent(100, 2, "split.bin")).expect("insert 2");
        m.insert(extent(200, 5, "other.bin")).expect("insert other");
        let removed = m.remove_file(Path::new("split.bin"));
        assert_eq!(removed, 2);
        assert_eq!(m.len(), 1);
        assert!(m.lookup(11).is_none());
        assert!(m.lookup(101).is_none());
        assert!(m.lookup(202).is_some());
    }

    #[test]
    fn remove_file_returns_zero_when_path_unknown() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 3, "a")).expect("insert");
        assert_eq!(m.remove_file(Path::new("nope")), 0);
        assert_eq!(m.len(), 1);
    }

    #[test]
    fn extents_iterates_in_ascending_first_cluster_order() {
        let mut m = ClusterMap::new();
        m.insert(extent(100, 3, "c")).expect("c");
        m.insert(extent(10, 3, "a")).expect("a");
        m.insert(extent(50, 3, "b")).expect("b");
        let firsts: Vec<u32> = m.extents().map(|e| e.first_cluster).collect();
        assert_eq!(firsts, vec![10, 50, 100]);
    }

    #[test]
    fn extents_in_range_returns_only_intersecting_extents() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 5, "a")).expect("a");
        m.insert(extent(20, 5, "b")).expect("b");
        m.insert(extent(40, 5, "c")).expect("c");
        m.insert(extent(60, 5, "d")).expect("d");
        // Range [22, 45) intersects b (overlaps tail), c (entirely contained).
        let names: Vec<&str> = m
            .extents_in_range(22, 45)
            .map(|e| e.file_path.to_str().expect("str"))
            .collect();
        assert_eq!(names, vec!["b", "c"]);
    }

    #[test]
    fn extents_in_range_returns_empty_when_no_overlap() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 5, "a")).expect("a");
        m.insert(extent(20, 5, "b")).expect("b");
        let names: Vec<&str> = m
            .extents_in_range(30, 40)
            .map(|e| e.file_path.to_str().expect("str"))
            .collect();
        assert!(names.is_empty());
    }

    #[test]
    fn extents_in_range_includes_partially_overlapping_first_extent() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 10, "a")).expect("a");
        // Range starts inside [10, 20). The first extent should still appear.
        let names: Vec<&str> = m
            .extents_in_range(15, 30)
            .map(|e| e.file_path.to_str().expect("str"))
            .collect();
        assert_eq!(names, vec!["a"]);
    }

    #[test]
    fn file_extent_contains_matches_lookup_semantics() {
        let e = extent(10, 5, "a");
        assert!(!e.contains(9));
        assert!(e.contains(10));
        assert!(e.contains(14));
        assert!(!e.contains(15));
    }

    #[test]
    fn file_extent_cluster_range_is_half_open() {
        let e = extent(10, 5, "a");
        let r = e.cluster_range();
        assert_eq!(r.start, 10);
        assert_eq!(r.end, 15);
    }

    // ---------------------------------------------------------
    // remove_overlapping — stale-extent eviction for the
    // "Tesla freed via bitmap, now reusing the clusters" case.
    // (Production bug: 2026-05-22, ~1,200 overlap warnings/boot.)
    // ---------------------------------------------------------

    #[test]
    fn remove_overlapping_empty_map_returns_empty() {
        let mut m = ClusterMap::new();
        assert!(m.remove_overlapping(10, 20).is_empty());
        assert!(m.is_empty());
    }

    #[test]
    fn remove_overlapping_returns_empty_when_no_intersection() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 5, "a")).expect("a");
        m.insert(extent(30, 5, "b")).expect("b");
        // Range [20, 25) doesn't touch any extent.
        let evicted = m.remove_overlapping(20, 25);
        assert!(evicted.is_empty());
        assert_eq!(m.len(), 2);
    }

    #[test]
    fn remove_overlapping_evicts_extent_starting_before_range() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 10, "a")).expect("a"); // [10, 20)
        // Range [15, 25) clips into the right half of `a`.
        let evicted = m.remove_overlapping(15, 25);
        assert_eq!(evicted.len(), 1);
        assert_eq!(evicted[0].file_path, PathBuf::from("a"));
        assert!(m.is_empty());
    }

    #[test]
    fn remove_overlapping_evicts_extent_starting_inside_range() {
        let mut m = ClusterMap::new();
        m.insert(extent(15, 10, "a")).expect("a"); // [15, 25)
        // Range [10, 20) clips into the left half of `a`.
        let evicted = m.remove_overlapping(10, 20);
        assert_eq!(evicted.len(), 1);
        assert_eq!(evicted[0].file_path, PathBuf::from("a"));
        assert!(m.is_empty());
    }

    #[test]
    fn remove_overlapping_evicts_extent_fully_inside_range() {
        let mut m = ClusterMap::new();
        m.insert(extent(15, 3, "a")).expect("a"); // [15, 18)
        let evicted = m.remove_overlapping(10, 25);
        assert_eq!(evicted.len(), 1);
        assert!(m.is_empty());
    }

    #[test]
    fn remove_overlapping_evicts_extent_fully_containing_range() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 20, "a")).expect("a"); // [10, 30)
        let evicted = m.remove_overlapping(15, 20);
        assert_eq!(evicted.len(), 1);
        assert!(m.is_empty());
    }

    #[test]
    fn remove_overlapping_evicts_multiple_extents_in_order() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 5, "a")).expect("a"); // [10, 15)
        m.insert(extent(20, 5, "b")).expect("b"); // [20, 25)
        m.insert(extent(30, 5, "c")).expect("c"); // [30, 35)
        m.insert(extent(50, 5, "d")).expect("d"); // [50, 55) — outside
        let evicted = m.remove_overlapping(12, 32);
        let paths: Vec<&Path> = evicted.iter().map(|e| e.file_path.as_path()).collect();
        assert_eq!(
            paths,
            vec![Path::new("a"), Path::new("b"), Path::new("c")]
        );
        assert_eq!(m.len(), 1);
        assert!(m.lookup(50).is_some());
    }

    #[test]
    fn remove_overlapping_does_not_evict_adjacent_extent() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 5, "a")).expect("a"); // [10, 15)
        m.insert(extent(15, 5, "b")).expect("b"); // [15, 20) — abuts but doesn't overlap
        let evicted = m.remove_overlapping(10, 15);
        assert_eq!(evicted.len(), 1);
        assert_eq!(evicted[0].file_path, PathBuf::from("a"));
        assert!(m.lookup(15).is_some(), "adjacent extent must survive");
    }

    #[test]
    fn remove_overlapping_empty_range_is_noop() {
        let mut m = ClusterMap::new();
        m.insert(extent(10, 5, "a")).expect("a");
        assert!(m.remove_overlapping(10, 10).is_empty());
        assert!(m.remove_overlapping(20, 5).is_empty());
        assert_eq!(m.len(), 1);
    }

    #[test]
    fn insert_after_remove_overlapping_succeeds_for_stale_extent_reuse() {
        // The regression scenario: stale extent `a` blocks insertion
        // of new extent `b` until `remove_overlapping` evicts it.
        let mut m = ClusterMap::new();
        m.insert(extent(100, 50, "stale_clip.mp4")).expect("stale");
        let new_extent = extent(110, 30, "fresh_clip.mp4"); // overlaps `a`
        let evicted = m.remove_overlapping(
            new_extent.first_cluster,
            new_extent.first_cluster + new_extent.cluster_count,
        );
        assert_eq!(evicted.len(), 1);
        m.insert(new_extent).expect("insert must succeed after eviction");
        assert_eq!(m.lookup(120).expect("hit").extent.file_path, PathBuf::from("fresh_clip.mp4"));
    }
}
