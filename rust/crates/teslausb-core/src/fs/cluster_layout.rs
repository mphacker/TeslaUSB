//! Filesystem-agnostic cluster allocator + chain layout
//! (Phase 2.16).
//!
//! Both FAT32 and exFAT lay out user data as a sequence of
//! **clusters**, each `bytes_per_cluster` bytes wide, numbered
//! `2..=(2 + cluster_count - 1)`. A *chain* is the ordered list
//! of clusters that belong to one entity (file or directory).
//! This module provides the shared pieces:
//!
//! * [`ClusterAllocator`] — stateful allocator that hands out
//!   contiguous cluster ranges, never reusing a cluster.
//! * [`Allocation`] — what the allocator returns: a starting
//!   cluster number plus how many contiguous clusters belong to
//!   the entity.
//! * [`AllocatedChains`] — a collection of [`Allocation`]s that
//!   implements [`super::fat32::fat_table::DirTreeBackend`] so
//!   it can be passed straight into
//!   [`crate::fs::fat32::fat_table::FatTable::build`].
//!
//! Per-FS code (Phases 2.17 / 2.18) is responsible for:
//!
//! * Pre-reserving any FS-specific clusters (exFAT bitmap +
//!   upcase table) before allocating user data.
//! * Computing per-directory entry-array byte sizes using the
//!   FS-specific entry format.
//! * Walking the [`crate::fs::backing_tree::BackingTree`] in the
//!   correct order (post-order so children are allocated before
//!   their parent's dir-entry array, which references child
//!   first-cluster numbers).
//!
//! ## Why contiguous allocation
//!
//! The B-1 daemon serves a synthesized view; no on-disk FAT
//! fragmentation exists. The allocator therefore hands out
//! contiguous ranges, which:
//!
//! * Makes FAT-entry synthesis a single `next = N+1` formula
//!   in [`crate::fs::fat32::fat_table::FatTable`] (already the
//!   existing implementation's assumption — exercised by the
//!   Phase 2.4 tests).
//! * Lets a file-content read at byte offset `O` of file `F`
//!   compute `(cluster, offset_in_cluster)` in `O(1)` from
//!   `F.first_cluster + O / bytes_per_cluster`, without walking
//!   the FAT.
//! * Simplifies cluster-number → entity lookup in [`AllocatedChains`]
//!   to a sorted-range search.
//!
//! Phase 3.4's `cluster_map` will lift this restriction for
//! write-side allocation; the read-side path stays contiguous.
//!
//! ## Empty-entity convention
//!
//! Calling [`ClusterAllocator::allocate`] with `size_bytes = 0`
//! returns an [`Allocation`] with `first_cluster = 0` and
//! `cluster_count = 0`. FAT32 and exFAT both encode "no chain"
//! as `FstClus = 0` in the directory entry, so the special
//! sentinel is the standard one. The allocator does NOT advance
//! its cursor for a zero-sized entity.

use std::ops::Range;

use crate::fs::fat32::fat_table::DirTreeBackend;

/// First valid data-cluster number in both FAT32 and exFAT.
///
/// Clusters 0 and 1 are reserved (cluster 0 carries the media
/// descriptor, cluster 1 carries the "dirty" / "io-error" bits).
/// User data starts at cluster 2.
pub const FIRST_DATA_CLUSTER: u32 = 2;

/// Sentinel returned by [`ClusterAllocator::allocate`] when
/// asked for zero bytes. Encoded as `FstClus = 0` in the parent
/// directory entry (both FAT32 and exFAT use this convention).
pub const EMPTY_CHAIN_FIRST_CLUSTER: u32 = 0;

/// A contiguous run of clusters assigned to one file or
/// directory.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Allocation {
    /// First cluster number in the chain, or
    /// [`EMPTY_CHAIN_FIRST_CLUSTER`] for an empty entity.
    pub first_cluster: u32,
    /// Number of clusters in the chain. `0` for an empty entity.
    pub cluster_count: u32,
}

impl Allocation {
    /// The empty allocation — zero clusters, `first_cluster` = 0.
    pub const EMPTY: Self = Self {
        first_cluster: EMPTY_CHAIN_FIRST_CLUSTER,
        cluster_count: 0,
    };

    /// Returns `true` if this allocation contains no clusters.
    #[must_use]
    pub const fn is_empty(self) -> bool {
        self.cluster_count == 0
    }

    /// The half-open range `[first_cluster, first_cluster + cluster_count)`
    /// of cluster numbers in this allocation. Returns an empty
    /// range for [`Self::EMPTY`].
    #[must_use]
    pub const fn cluster_range(self) -> Range<u32> {
        if self.cluster_count == 0 {
            // 0..0 — an empty range, regardless of first_cluster.
            0..0
        } else {
            self.first_cluster..self.first_cluster + self.cluster_count
        }
    }

    /// Returns `true` if `cluster` is one of the clusters in
    /// this allocation.
    #[must_use]
    pub const fn contains(self, cluster: u32) -> bool {
        if self.cluster_count == 0 {
            false
        } else {
            cluster >= self.first_cluster && cluster < self.first_cluster + self.cluster_count
        }
    }
}

/// Errors returned by [`ClusterAllocator::allocate`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AllocError {
    /// The allocator's remaining capacity is smaller than the
    /// caller's request.
    OutOfClusters {
        /// Clusters the caller asked for.
        requested: u32,
        /// Clusters the allocator had left at the time of the
        /// call.
        available: u32,
    },
    /// The allocator was constructed with `first_cluster < 2`,
    /// which is invalid for both FAT32 and exFAT.
    FirstClusterReserved {
        /// The offending first cluster.
        first_cluster: u32,
    },
    /// The allocator was constructed with `bytes_per_cluster = 0`,
    /// which would divide-by-zero in the `cluster_count`
    /// computation.
    ZeroClusterSize,
    /// The allocator was constructed with
    /// `max_cluster_exclusive <= first_cluster`, leaving zero
    /// capacity.
    EmptyCapacity {
        /// The offending starting cluster.
        first_cluster: u32,
        /// The offending exclusive upper bound.
        max_cluster_exclusive: u32,
    },
}

impl core::fmt::Display for AllocError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::OutOfClusters {
                requested,
                available,
            } => write!(
                f,
                "cluster allocator out of capacity: requested {requested}, available {available}",
            ),
            Self::FirstClusterReserved { first_cluster } => write!(
                f,
                "cluster allocator constructed with reserved first_cluster {first_cluster} (clusters 0 and 1 are reserved)",
            ),
            Self::ZeroClusterSize => {
                f.write_str("cluster allocator constructed with bytes_per_cluster = 0")
            }
            Self::EmptyCapacity {
                first_cluster,
                max_cluster_exclusive,
            } => write!(
                f,
                "cluster allocator constructed with empty capacity (first_cluster {first_cluster} >= max_cluster_exclusive {max_cluster_exclusive})",
            ),
        }
    }
}

impl std::error::Error for AllocError {}

/// Stateful cluster allocator.
///
/// Constructed with a cluster size, a starting cluster, and an
/// exclusive upper bound. Each [`Self::allocate`] call advances
/// the internal cursor by the requested cluster count (or zero
/// for an empty-entity request).
///
/// The allocator does NOT track *which* entity each cluster
/// belongs to — that's the per-FS code's job. It only knows
/// "next free cluster" and "remaining capacity".
#[derive(Debug)]
pub struct ClusterAllocator {
    bytes_per_cluster: u32,
    next_cluster: u32,
    max_cluster_exclusive: u32,
}

impl ClusterAllocator {
    /// Construct a new allocator.
    ///
    /// `bytes_per_cluster` is the cluster size in bytes; must be
    /// non-zero (both FAT32 and exFAT enforce a power-of-two
    /// ≥ 512). `first_cluster` is the cluster number the first
    /// allocation will be assigned (must be ≥ [`FIRST_DATA_CLUSTER`]
    /// = 2). `max_cluster_exclusive` is the cluster number one
    /// past the last allocatable cluster (=
    /// `FIRST_DATA_CLUSTER + cluster_count`).
    ///
    /// # Errors
    ///
    /// * [`AllocError::ZeroClusterSize`] if `bytes_per_cluster == 0`.
    /// * [`AllocError::FirstClusterReserved`] if `first_cluster < 2`.
    /// * [`AllocError::EmptyCapacity`] if
    ///   `max_cluster_exclusive <= first_cluster`.
    pub fn new(
        bytes_per_cluster: u32,
        first_cluster: u32,
        max_cluster_exclusive: u32,
    ) -> Result<Self, AllocError> {
        if bytes_per_cluster == 0 {
            return Err(AllocError::ZeroClusterSize);
        }
        if first_cluster < FIRST_DATA_CLUSTER {
            return Err(AllocError::FirstClusterReserved { first_cluster });
        }
        if max_cluster_exclusive <= first_cluster {
            return Err(AllocError::EmptyCapacity {
                first_cluster,
                max_cluster_exclusive,
            });
        }
        Ok(Self {
            bytes_per_cluster,
            next_cluster: first_cluster,
            max_cluster_exclusive,
        })
    }

    /// Cluster size this allocator was constructed for.
    #[must_use]
    pub const fn bytes_per_cluster(&self) -> u32 {
        self.bytes_per_cluster
    }

    /// Next cluster number that would be returned by
    /// [`Self::allocate`] for a non-empty request.
    #[must_use]
    pub const fn next_cluster(&self) -> u32 {
        self.next_cluster
    }

    /// Clusters remaining in the allocator's capacity.
    #[must_use]
    pub const fn remaining_clusters(&self) -> u32 {
        self.max_cluster_exclusive.saturating_sub(self.next_cluster)
    }

    /// Allocate enough contiguous clusters for `size_bytes`.
    ///
    /// `size_bytes == 0` returns [`Allocation::EMPTY`] and does
    /// not advance the cursor.
    ///
    /// # Errors
    ///
    /// * [`AllocError::OutOfClusters`] if the required cluster
    ///   count exceeds remaining capacity.
    pub fn allocate(&mut self, size_bytes: u64) -> Result<Allocation, AllocError> {
        if size_bytes == 0 {
            return Ok(Allocation::EMPTY);
        }
        let bytes_per_cluster = u64::from(self.bytes_per_cluster);
        // ceil_div without overflow: size_bytes > 0 and bytes_per_cluster > 0.
        let cluster_count_u64 = size_bytes.div_ceil(bytes_per_cluster);
        let cluster_count = u32::try_from(cluster_count_u64).map_err(|_| {
            // Asking for > u32::MAX clusters is a configuration
            // bug, not a capacity issue. Surface it as
            // OutOfClusters with the requested value saturated
            // to u32::MAX so the operator sees a sensible number.
            AllocError::OutOfClusters {
                requested: u32::MAX,
                available: self.remaining_clusters(),
            }
        })?;
        let available = self.remaining_clusters();
        if cluster_count > available {
            return Err(AllocError::OutOfClusters {
                requested: cluster_count,
                available,
            });
        }
        let first_cluster = self.next_cluster;
        // Safe: `cluster_count <= remaining = max_cluster_exclusive - next_cluster`,
        // so `next_cluster + cluster_count <= max_cluster_exclusive`.
        self.next_cluster += cluster_count;
        Ok(Allocation {
            first_cluster,
            cluster_count,
        })
    }
}

/// A collection of [`Allocation`]s that satisfies the
/// [`DirTreeBackend`] contract.
///
/// Built by the per-FS layout code (Phases 2.17 / 2.18) by
/// repeatedly calling [`ClusterAllocator::allocate`] and pushing
/// each returned [`Allocation`] here. The collection then
/// hands the resulting chain layout to
/// [`crate::fs::fat32::fat_table::FatTable::build`] via the
/// [`DirTreeBackend`] interface.
///
/// Empty allocations ([`Allocation::EMPTY`]) are silently
/// dropped — the FAT table has nothing to mark for them
/// (`FstClus` = 0 ⇒ no chain).
#[derive(Debug, Clone, Default)]
pub struct AllocatedChains {
    allocations: Vec<Allocation>,
}

impl AllocatedChains {
    /// Construct an empty collection.
    #[must_use]
    pub const fn new() -> Self {
        Self {
            allocations: Vec::new(),
        }
    }

    /// Append `allocation`. Empty allocations are dropped so the
    /// underlying `DirTreeBackend` visitor never receives an
    /// empty chain (which `FatTable::build` would reject).
    pub fn push(&mut self, allocation: Allocation) {
        if !allocation.is_empty() {
            self.allocations.push(allocation);
        }
    }

    /// Number of non-empty allocations recorded.
    #[must_use]
    pub fn len(&self) -> usize {
        self.allocations.len()
    }

    /// Returns `true` if no non-empty allocation has been
    /// recorded.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.allocations.is_empty()
    }

    /// All allocations, in insertion order.
    #[must_use]
    pub fn as_slice(&self) -> &[Allocation] {
        &self.allocations
    }
}

impl DirTreeBackend for AllocatedChains {
    fn for_each_chain(&self, visitor: &mut dyn FnMut(&[u32])) {
        for alloc in &self.allocations {
            // Materialize each contiguous range as a Vec<u32>
            // because the DirTreeBackend trait passes chains by
            // `&[u32]`. The vector is short-lived (dropped at
            // the end of each visit) and the total bytes across
            // all chains equals 4 * total_clusters — for a
            // 64 GiB volume at 32 KiB clusters that's ~8 MiB,
            // which fits comfortably in even the Pi Zero 2 W's
            // budget. Phase 3 may swap to an iterator if profiling
            // shows this matters, but the read-side allocator's
            // synth path only walks chains once at FAT-build time.
            let chain: Vec<u32> =
                (alloc.first_cluster..alloc.first_cluster + alloc.cluster_count).collect();
            visitor(&chain);
        }
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

    // ---- Allocation ------------------------------------------

    #[test]
    fn allocation_empty_constant_matches_documented_sentinel() {
        assert_eq!(Allocation::EMPTY.first_cluster, 0);
        assert_eq!(Allocation::EMPTY.cluster_count, 0);
        assert!(Allocation::EMPTY.is_empty());
    }

    #[test]
    fn allocation_cluster_range_for_nonempty() {
        let a = Allocation {
            first_cluster: 5,
            cluster_count: 3,
        };
        assert_eq!(a.cluster_range(), 5..8);
    }

    #[test]
    fn allocation_cluster_range_for_empty_is_empty() {
        let r = Allocation::EMPTY.cluster_range();
        assert!(r.is_empty(), "empty range expected, got {r:?}");
    }

    #[test]
    fn allocation_contains_only_the_clusters_in_the_range() {
        let a = Allocation {
            first_cluster: 5,
            cluster_count: 3,
        };
        assert!(!a.contains(4));
        assert!(a.contains(5));
        assert!(a.contains(6));
        assert!(a.contains(7));
        assert!(!a.contains(8));
    }

    #[test]
    fn allocation_contains_returns_false_for_empty() {
        // An empty allocation never contains any cluster, not
        // even the (meaningless) first_cluster = 0.
        assert!(!Allocation::EMPTY.contains(0));
        assert!(!Allocation::EMPTY.contains(1));
        assert!(!Allocation::EMPTY.contains(2));
    }

    // ---- ClusterAllocator::new validation --------------------

    #[test]
    fn rejects_zero_bytes_per_cluster() {
        assert_eq!(
            ClusterAllocator::new(0, 2, 100).unwrap_err(),
            AllocError::ZeroClusterSize,
        );
    }

    #[test]
    fn rejects_first_cluster_zero() {
        assert_eq!(
            ClusterAllocator::new(512, 0, 100).unwrap_err(),
            AllocError::FirstClusterReserved { first_cluster: 0 },
        );
    }

    #[test]
    fn rejects_first_cluster_one() {
        assert_eq!(
            ClusterAllocator::new(512, 1, 100).unwrap_err(),
            AllocError::FirstClusterReserved { first_cluster: 1 },
        );
    }

    #[test]
    fn accepts_first_cluster_two() {
        let a = ClusterAllocator::new(512, FIRST_DATA_CLUSTER, 100).unwrap();
        assert_eq!(a.next_cluster(), 2);
    }

    #[test]
    fn rejects_empty_capacity() {
        assert_eq!(
            ClusterAllocator::new(512, 2, 2).unwrap_err(),
            AllocError::EmptyCapacity {
                first_cluster: 2,
                max_cluster_exclusive: 2,
            },
        );
        assert_eq!(
            ClusterAllocator::new(512, 5, 4).unwrap_err(),
            AllocError::EmptyCapacity {
                first_cluster: 5,
                max_cluster_exclusive: 4,
            },
        );
    }

    // ---- ClusterAllocator::allocate happy paths --------------

    #[test]
    fn allocate_zero_bytes_returns_empty_and_does_not_advance() {
        let mut a = ClusterAllocator::new(4096, 2, 100).unwrap();
        let alloc = a.allocate(0).unwrap();
        assert_eq!(alloc, Allocation::EMPTY);
        assert_eq!(a.next_cluster(), 2);
        assert_eq!(a.remaining_clusters(), 98);
    }

    #[test]
    fn allocate_exact_cluster_uses_one_cluster() {
        let mut a = ClusterAllocator::new(4096, 2, 100).unwrap();
        let alloc = a.allocate(4096).unwrap();
        assert_eq!(
            alloc,
            Allocation {
                first_cluster: 2,
                cluster_count: 1,
            }
        );
        assert_eq!(a.next_cluster(), 3);
    }

    #[test]
    fn allocate_one_byte_uses_one_cluster() {
        let mut a = ClusterAllocator::new(4096, 2, 100).unwrap();
        let alloc = a.allocate(1).unwrap();
        assert_eq!(alloc.cluster_count, 1);
    }

    #[test]
    fn allocate_just_past_cluster_boundary_uses_two_clusters() {
        let mut a = ClusterAllocator::new(4096, 2, 100).unwrap();
        let alloc = a.allocate(4097).unwrap();
        assert_eq!(alloc.cluster_count, 2);
    }

    #[test]
    fn allocate_consecutive_calls_are_contiguous() {
        let mut a = ClusterAllocator::new(4096, 2, 100).unwrap();
        let a1 = a.allocate(8192).unwrap(); // 2 clusters: 2..4
        let a2 = a.allocate(4096).unwrap(); // 1 cluster:  4
        let a3 = a.allocate(16_384).unwrap(); // 4 clusters: 5..9
        assert_eq!(
            a1,
            Allocation {
                first_cluster: 2,
                cluster_count: 2,
            }
        );
        assert_eq!(
            a2,
            Allocation {
                first_cluster: 4,
                cluster_count: 1,
            }
        );
        assert_eq!(
            a3,
            Allocation {
                first_cluster: 5,
                cluster_count: 4,
            }
        );
        assert_eq!(a.next_cluster(), 9);
    }

    #[test]
    fn allocate_zero_in_between_does_not_skip_a_cluster() {
        let mut a = ClusterAllocator::new(4096, 2, 100).unwrap();
        a.allocate(4096).unwrap();
        a.allocate(0).unwrap();
        let alloc = a.allocate(4096).unwrap();
        // Empty allocation must not advance the cursor.
        assert_eq!(alloc.first_cluster, 3);
    }

    // ---- ClusterAllocator::allocate failure paths -----------

    #[test]
    fn allocate_out_of_capacity_returns_error_with_counts() {
        let mut a = ClusterAllocator::new(4096, 2, 5).unwrap();
        // Capacity: 5 - 2 = 3 clusters.
        let err = a.allocate(4096 * 4).unwrap_err();
        assert_eq!(
            err,
            AllocError::OutOfClusters {
                requested: 4,
                available: 3,
            }
        );
        // Cursor must not have moved on the failed allocation.
        assert_eq!(a.next_cluster(), 2);
        assert_eq!(a.remaining_clusters(), 3);
    }

    #[test]
    fn allocate_to_exactly_capacity_succeeds_and_drains() {
        let mut a = ClusterAllocator::new(4096, 2, 5).unwrap();
        // Capacity 3.
        let alloc = a.allocate(4096 * 3).unwrap();
        assert_eq!(alloc.cluster_count, 3);
        assert_eq!(a.remaining_clusters(), 0);
        // Subsequent zero-sized allocation still succeeds.
        assert_eq!(a.allocate(0).unwrap(), Allocation::EMPTY);
        // Subsequent non-zero allocation fails.
        assert_eq!(
            a.allocate(1).unwrap_err(),
            AllocError::OutOfClusters {
                requested: 1,
                available: 0,
            },
        );
    }

    #[test]
    fn allocate_overflowing_u32_count_returns_out_of_clusters() {
        // Use a 1-byte cluster size so size_bytes > u32::MAX
        // triggers the u32::try_from failure.
        let mut a = ClusterAllocator::new(1, 2, u32::MAX).unwrap();
        let err = a.allocate(u64::from(u32::MAX) + 1).unwrap_err();
        match err {
            AllocError::OutOfClusters { requested, .. } => {
                assert_eq!(requested, u32::MAX);
            }
            other => panic!("expected OutOfClusters, got {other:?}"),
        }
    }

    // ---- AllocatedChains -------------------------------------

    #[test]
    fn allocated_chains_starts_empty() {
        let c = AllocatedChains::new();
        assert!(c.is_empty());
        assert_eq!(c.len(), 0);
        assert!(c.as_slice().is_empty());
    }

    #[test]
    fn allocated_chains_drops_empty_allocations_silently() {
        let mut c = AllocatedChains::new();
        c.push(Allocation::EMPTY);
        c.push(Allocation::EMPTY);
        assert!(c.is_empty());
        assert_eq!(c.len(), 0);
    }

    #[test]
    fn allocated_chains_preserves_insertion_order() {
        let mut c = AllocatedChains::new();
        let a1 = Allocation {
            first_cluster: 2,
            cluster_count: 1,
        };
        let a2 = Allocation {
            first_cluster: 3,
            cluster_count: 4,
        };
        let a3 = Allocation {
            first_cluster: 7,
            cluster_count: 2,
        };
        c.push(a1);
        c.push(a2);
        c.push(a3);
        assert_eq!(c.as_slice(), &[a1, a2, a3]);
    }

    #[test]
    fn dir_tree_backend_visits_each_chain_with_expanded_clusters() {
        let mut c = AllocatedChains::new();
        c.push(Allocation {
            first_cluster: 2,
            cluster_count: 1,
        });
        c.push(Allocation {
            first_cluster: 3,
            cluster_count: 4,
        });
        let mut visited: Vec<Vec<u32>> = Vec::new();
        c.for_each_chain(&mut |chain| {
            visited.push(chain.to_vec());
        });
        assert_eq!(visited, vec![vec![2], vec![3, 4, 5, 6]]);
    }

    #[test]
    fn dir_tree_backend_skips_empty_allocations_internally() {
        // Push doesn't add empty allocations, so the visitor
        // should never see an empty chain. This pins the
        // FatTable::build precondition (empty chains are
        // rejected by FatTable validation).
        let mut c = AllocatedChains::new();
        c.push(Allocation {
            first_cluster: 2,
            cluster_count: 1,
        });
        c.push(Allocation::EMPTY);
        c.push(Allocation {
            first_cluster: 3,
            cluster_count: 2,
        });
        let mut visited: Vec<Vec<u32>> = Vec::new();
        c.for_each_chain(&mut |chain| {
            assert!(!chain.is_empty(), "empty chain leaked to visitor");
            visited.push(chain.to_vec());
        });
        assert_eq!(visited.len(), 2);
    }

    #[test]
    fn dir_tree_backend_integrates_with_fat_table_build() {
        // End-to-end check: a chain layout produced via
        // ClusterAllocator + AllocatedChains feeds straight into
        // FatTable::build without any glue code. Catches any
        // future drift between the planner's chain shape and
        // FatTable's expectations.
        use crate::fs::fat32::fat_table::{FREE_CLUSTER, FatTable};
        use crate::fs::fat32::geometry::Fat32Geometry;
        use crate::fs::geometry::Geometry;

        let geometry = Fat32Geometry::for_volume_size(34 * 1024 * 1024).expect("34 MiB geometry");
        let max_cluster_exclusive =
            FIRST_DATA_CLUSTER.saturating_add(geometry.data_cluster_count());
        let mut allocator = ClusterAllocator::new(
            geometry.bytes_per_cluster(),
            FIRST_DATA_CLUSTER,
            max_cluster_exclusive,
        )
        .unwrap();

        let mut chains = AllocatedChains::new();
        let bytes_per_cluster = u64::from(geometry.bytes_per_cluster());
        let root = allocator.allocate(bytes_per_cluster).unwrap(); // 1 cluster
        let file_a = allocator.allocate(bytes_per_cluster * 2).unwrap(); // 2 clusters
        let file_b = allocator.allocate(bytes_per_cluster * 4).unwrap(); // 4 clusters
        let total_clusters_allocated =
            root.cluster_count + file_a.cluster_count + file_b.cluster_count;
        chains.push(root);
        chains.push(file_a);
        chains.push(file_b);

        let table = FatTable::build(&geometry, &chains).expect("fat table builds");
        let entries = table.entries();
        // FAT entries 0 and 1 are reserved (media descriptor and
        // dirty/IO-error flags); they are never FREE_CLUSTER. The
        // remaining non-free entries should match the cluster
        // count we allocated.
        let allocated_data_entries = entries
            .iter()
            .skip(2)
            .filter(|&&e| e != FREE_CLUSTER)
            .count();
        assert_eq!(
            u32::try_from(allocated_data_entries).unwrap(),
            total_clusters_allocated,
            "FAT table should mark exactly the clusters from AllocatedChains as non-free",
        );
    }

    // ---- Display / Error -------------------------------------

    #[test]
    fn alloc_error_implements_std_error() {
        fn assert_error<E: std::error::Error>(_: &E) {}
        assert_error(&AllocError::ZeroClusterSize);
    }

    #[test]
    fn alloc_error_display_includes_counts() {
        let s = AllocError::OutOfClusters {
            requested: 17,
            available: 5,
        }
        .to_string();
        assert!(s.contains("17"));
        assert!(s.contains('5'));
    }
}
