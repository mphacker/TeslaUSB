//! FAT32 File Allocation Table synthesizer.
//!
//! Phase 2.4 of the B-1 rewrite. This module turns a directory-tree
//! description (a [`DirTreeBackend`] implementation that enumerates
//! the cluster chains used by every file and directory in the
//! synthesized volume) into the on-disk **FAT** that lives between
//! the reserved area and the data region.
//!
//! A FAT32 volume has *two* identical FAT copies (Microsoft's
//! "mirror copy" — see [`crate::fs::fat32::geometry::NUM_FATS`]).
//! This module only synthesizes *one* copy; the read dispatcher
//! (Phase 2.6) is responsible for emitting the same bytes when
//! either copy is requested.
//!
//! ## Specification anchor
//!
//! Microsoft FAT Specification (fatgen103.pdf), **§4: FAT** —
//! specifically §4.1 (FAT32 entry layout, reserved entries 0 and 1,
//! end-of-chain and bad-cluster sentinels).
//!
//! ## Entry format
//!
//! Every FAT32 entry is a 32-bit little-endian word. Per §4.1
//! only the **low 28 bits** carry a value; the top 4 bits are
//! reserved and must be preserved on read. On a freshly
//! synthesized volume B-1 writes the top 4 bits as zero.
//!
//! | 28-bit value         | Meaning                                  |
//! |----------------------|------------------------------------------|
//! | `0x0000000`          | Free cluster (available for allocation)  |
//! | `0x0000002..=0xFFFFFEF` | Next cluster in chain (cluster number) |
//! | `0xFFFFFF0..=0xFFFFFF6` | Reserved (not allocatable)             |
//! | `0xFFFFFF7`          | Bad cluster                              |
//! | `0xFFFFFF8..=0xFFFFFFF` | End-of-chain marker                    |
//!
//! Cluster numbers `0x0000000` and `0x0000001` are reserved
//! (cluster numbering starts at 2 — see [`ROOT_DIRECTORY_CLUSTER`]).
//!
//! ## Reserved entries 0 and 1
//!
//! * **FAT\[0\]** = `0x0FFFFFF8`. The low byte mirrors
//!   `BPB_Media` ([`MEDIA_DESCRIPTOR_FIXED`] = `0xF8`); the rest is
//!   "all-ones" padding. This is the value `mkfs.vfat` writes for
//!   a fixed-media FAT32 volume, and matches the §4.1 normative
//!   description.
//! * **FAT\[1\]** = `0x0FFFFFFF`. End-of-chain marker; both the
//!   `ClnShutBitMask` (`0x0800_0000`, bit 27 = "clean shutdown")
//!   and `HrdErrBitMask` (`0x0400_0000`, bit 26 = "no I/O errors
//!   seen") are set, which is the canonical "fresh and healthy"
//!   state.
//!
//! ## What "synthesize" produces
//!
//! Callers use a two-step pipeline:
//!
//! 1. [`FatTable::build`] takes a [`Fat32Geometry`] + a
//!    `&dyn DirTreeBackend` and produces a `FatTable` whose
//!    [`FatTable::entries`] slice has one `u32` per cluster index
//!    in `0..(data_cluster_count + 2)`.
//! 2. [`FatTable::synthesize_sector`] takes a sector index in
//!    `0..fat_size_sectors` and serialises the 128 entries that
//!    fall in that sector to a `[u8; 512]`.
//!
//! The build step is `O(total clusters)` time and memory. For the
//! 4 GiB volume the H1 hardware ships with that's ~1.05M `u32`s
//! (~4 MiB). The Phase 2.14 cold-start benchmark must still meet
//! the ≤ 1 s budget on the Pi Zero 2 W, so the build is structured
//! as a single sequential pass with no nested loops over the
//! entire entry table.
//!
//! ## Why a [`DirTreeBackend`] trait?
//!
//! The synthesizer is generic over the source of cluster chains.
//! In tests we use [`InMemoryDirTree`] — a fixed `Vec<Vec<u32>>`
//! with no I/O — to exercise contiguous, fragmented, and
//! pathological chains. The Phase 3.3 production backend will
//! implement the same trait against a POSIX directory tree. The
//! seam matches the charter's "dependency-inversion" rule.

use core::fmt;

use crate::fs::fat32::boot_sector::{MEDIA_DESCRIPTOR_FIXED, ROOT_DIRECTORY_CLUSTER};
use crate::fs::fat32::geometry::Fat32Geometry;
use crate::fs::geometry::{Geometry, SECTOR_SIZE_BYTES};

/// Byte width of a single FAT32 entry on disk (fatgen103 §4.1).
pub const FAT_ENTRY_SIZE_BYTES: u32 = 4;

/// Number of FAT entries that fit in one 512-byte sector.
///
/// `512 / 4 = 128`. Asserted at compile time below.
pub const FAT_ENTRIES_PER_SECTOR: u32 = SECTOR_SIZE_BYTES / FAT_ENTRY_SIZE_BYTES;

const _: () = {
    assert!(
        SECTOR_SIZE_BYTES % FAT_ENTRY_SIZE_BYTES == 0,
        "sector size must be a whole multiple of FAT entry size"
    );
    assert!(
        FAT_ENTRIES_PER_SECTOR == 128,
        "expected 128 FAT32 entries per 512-byte sector"
    );
};

/// Byte width of one FAT sector (= [`SECTOR_SIZE_BYTES`] as `usize`).
pub const FAT_SECTOR_SIZE_BYTES: usize = SECTOR_SIZE_BYTES as usize;

/// Mask of the 28 value-carrying bits in a FAT32 entry (fatgen103 §4.1).
///
/// The top 4 bits (`0xF000_0000`) are reserved and must be preserved
/// on read. On a freshly synthesized volume B-1 writes them as
/// zero. Used internally to mask caller-supplied next-cluster
/// values before serialisation.
pub const FAT32_ENTRY_MASK: u32 = 0x0FFF_FFFF;

/// Canonical end-of-chain marker written for the final cluster of
/// every chain (fatgen103 §4.1).
///
/// Any value in `0x0FFFFFF8..=0x0FFFFFFF` is a legal EOC marker;
/// B-1 picks the all-ones value to match `mkfs.vfat`.
pub const END_OF_CHAIN_MARKER: u32 = 0x0FFF_FFFF;

/// Inclusive lower bound of the EOC marker range (fatgen103 §4.1).
pub const END_OF_CHAIN_MIN: u32 = 0x0FFF_FFF8;

/// Bad-cluster sentinel (fatgen103 §4.1).
///
/// Marks a cluster whose underlying storage has gone bad and must
/// not be allocated to a file. B-1's storage is virtual so no
/// data cluster will ever carry this value, but [`FatTable::build`]
/// still rejects chains that try to *use* it to catch corrupt
/// backends.
pub const BAD_CLUSTER_MARKER: u32 = 0x0FFF_FFF7;

/// "Free" sentinel — a cluster available for allocation.
pub const FREE_CLUSTER: u32 = 0x0000_0000;

/// Value of the reserved FAT entry 0 for a fixed-media volume
/// (fatgen103 §4.1).
///
/// Low byte = [`MEDIA_DESCRIPTOR_FIXED`] = `0xF8`; high bits
/// = `0x0FFF_FF00`. Constructed at compile time from the boot
/// sector's media descriptor so the two on-disk fields can never
/// disagree.
pub const FAT_ENTRY_0_FIXED_MEDIA: u32 = 0x0FFF_FF00 | MEDIA_DESCRIPTOR_FIXED as u32;

/// Value of the reserved FAT entry 1 for a freshly-formatted,
/// healthy FAT32 volume (fatgen103 §4.1).
///
/// `0x0FFF_FFFF` = end-of-chain marker | `ClnShutBitMask`
/// (`0x0800_0000`, bit 27 = "clean shutdown") | `HrdErrBitMask`
/// (`0x0400_0000`, bit 26 = "no I/O errors").
pub const FAT_ENTRY_1_CLEAN_HEALTHY: u32 = 0x0FFF_FFFF;

const _: () = {
    assert!(
        FAT_ENTRY_0_FIXED_MEDIA == 0x0FFF_FFF8,
        "FAT[0] must equal 0x0FFFFFF8 for fixed-media FAT32 volumes"
    );
    assert!(
        FAT_ENTRY_1_CLEAN_HEALTHY >= END_OF_CHAIN_MIN,
        "FAT[1] must be in the EOC range so it terminates the (empty) root-of-root chain"
    );
    assert!(
        FAT_ENTRY_1_CLEAN_HEALTHY <= END_OF_CHAIN_MARKER,
        "FAT[1] must lie within the canonical EOC range upper bound"
    );
    assert!(
        FAT_ENTRY_1_CLEAN_HEALTHY & 0x0800_0000 != 0,
        "FAT[1] must have ClnShutBitMask (bit 27) set on a cleanly mounted volume"
    );
    assert!(
        FAT_ENTRY_1_CLEAN_HEALTHY & 0x0400_0000 != 0,
        "FAT[1] must have HrdErrBitMask (bit 26) set on a healthy volume"
    );
    assert!(
        END_OF_CHAIN_MARKER >= END_OF_CHAIN_MIN,
        "EOC marker must lie within the canonical EOC range"
    );
    assert!(
        END_OF_CHAIN_MARKER & !FAT32_ENTRY_MASK == 0,
        "EOC marker must not set any reserved top bits"
    );
};

/// Source of allocated cluster chains for the FAT synthesizer.
///
/// A *chain* is the ordered list of clusters that belong to one
/// file or directory. Cluster N+1 in the slice is the "next
/// cluster" pointer written into FAT entry N. The final cluster
/// in the slice receives an end-of-chain marker
/// ([`END_OF_CHAIN_MARKER`]).
///
/// All clusters across all chains must be unique and must fall in
/// `2..=(2 + data_cluster_count - 1)`; [`FatTable::build`]
/// validates these invariants and surfaces them as
/// [`FatTableError`] variants.
///
/// The root directory's chain must be among those visited and
/// must start at [`ROOT_DIRECTORY_CLUSTER`] (cluster 2). This
/// module does *not* validate that constraint — that's the
/// dispatcher's job in Phase 2.6 — but every test in this module
/// produces a backend whose first chain starts at cluster 2.
pub trait DirTreeBackend {
    /// Invoke `visitor` exactly once with each allocated cluster
    /// chain in the volume.
    ///
    /// The visitor receives chains in any order. Each chain slice
    /// must be non-empty (an empty chain represents a directory
    /// with no clusters, which is illegal in FAT32 since every
    /// directory has at least one cluster — the entry table).
    fn for_each_chain(&self, visitor: &mut dyn FnMut(&[u32]));
}

/// In-memory `DirTreeBackend` for unit tests and the Phase 2.6
/// dispatcher's deterministic test fixtures.
///
/// Wraps a `Vec<Vec<u32>>` where each inner vector is one chain.
/// Construction is unchecked; validation happens in
/// [`FatTable::build`] when the chains are walked. Lives in the
/// production source (not behind `#[cfg(test)]`) because the
/// Phase 2.6 dispatcher tests and the Phase 2.7 integration test
/// both consume it.
#[derive(Debug, Clone, Default)]
pub struct InMemoryDirTree {
    chains: Vec<Vec<u32>>,
}

impl InMemoryDirTree {
    /// Create an empty tree — no chains at all.
    ///
    /// Passing this to [`FatTable::build`] produces a FAT whose
    /// data-cluster entries are all [`FREE_CLUSTER`] and whose
    /// reserved entries 0 and 1 still carry the standard values.
    /// Mainly useful as a baseline for "empty-volume" tests; a
    /// real volume always has at least the root directory chain.
    #[must_use]
    pub fn empty() -> Self {
        Self { chains: Vec::new() }
    }

    /// Append a chain. Cluster ordering inside `chain` is the
    /// chain order — `chain[0] → chain[1] → ... → chain[n-1] → EOC`.
    pub fn push_chain(&mut self, chain: Vec<u32>) {
        self.chains.push(chain);
    }

    /// Convenience constructor: build a tree from a slice of
    /// chains.
    #[must_use]
    pub fn from_chains(chains: Vec<Vec<u32>>) -> Self {
        Self { chains }
    }
}

impl DirTreeBackend for InMemoryDirTree {
    fn for_each_chain(&self, visitor: &mut dyn FnMut(&[u32])) {
        for chain in &self.chains {
            visitor(chain);
        }
    }
}

/// Errors returned by [`FatTable::build`] and
/// [`FatTable::synthesize_sector`].
#[derive(Debug, PartialEq, Eq)]
pub enum FatTableError {
    /// A chain referenced cluster 0 or 1 — the reserved entries
    /// that mean "free" and "FAT entry 1" respectively. Cluster
    /// numbering starts at [`ROOT_DIRECTORY_CLUSTER`] = 2.
    ChainContainsReservedCluster {
        /// The illegal cluster number (always 0 or 1).
        cluster: u32,
    },
    /// A chain referenced a cluster beyond the geometry's
    /// `data_cluster_count + 1` ceiling (the highest legal data
    /// cluster number, since numbering starts at 2).
    ChainContainsOutOfRangeCluster {
        /// The illegal cluster number.
        cluster: u32,
        /// The highest cluster number the geometry permits.
        max_cluster: u32,
    },
    /// A chain referenced [`BAD_CLUSTER_MARKER`] (`0x0FFF_FFF7`).
    /// Bad clusters must not appear in a chain; they must only
    /// appear *as* FAT entry values.
    ChainContainsBadClusterMarker {
        /// The illegal cluster number (`0x0FFF_FFF7`).
        cluster: u32,
    },
    /// A cluster appeared more than once — either twice in the
    /// same chain or in two different chains. Each cluster may
    /// belong to exactly one chain.
    ClusterAllocatedTwice {
        /// The duplicated cluster number.
        cluster: u32,
    },
    /// A chain was empty (zero clusters). Every chain must
    /// contain at least one cluster.
    EmptyChain,
    /// [`FatTable::synthesize_sector`] was called with a sector
    /// index ≥ the geometry's `fat_size_sectors`.
    SectorIndexOutOfRange {
        /// The requested sector index.
        sector_in_fat: u32,
        /// The geometry's `fat_size_sectors` ceiling.
        fat_size_sectors: u32,
    },
}

impl fmt::Display for FatTableError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ChainContainsReservedCluster { cluster } => write!(
                f,
                "cluster chain contains reserved cluster {cluster} (cluster numbering starts at 2)"
            ),
            Self::ChainContainsOutOfRangeCluster {
                cluster,
                max_cluster,
            } => write!(
                f,
                "cluster chain references cluster {cluster}, which exceeds the geometry's max data cluster {max_cluster}"
            ),
            Self::ChainContainsBadClusterMarker { cluster } => write!(
                f,
                "cluster chain contains bad-cluster marker 0x{cluster:08X} (must only appear as a FAT entry value)"
            ),
            Self::ClusterAllocatedTwice { cluster } => write!(
                f,
                "cluster {cluster} is referenced by more than one chain or twice within the same chain"
            ),
            Self::EmptyChain => write!(
                f,
                "directory-tree backend yielded an empty chain (every chain must have at least one cluster)"
            ),
            Self::SectorIndexOutOfRange {
                sector_in_fat,
                fat_size_sectors,
            } => write!(
                f,
                "sector index {sector_in_fat} is out of range for a FAT of {fat_size_sectors} sectors"
            ),
        }
    }
}

impl std::error::Error for FatTableError {}

/// Materialised FAT entry table for a synthesized FAT32 volume.
///
/// The on-disk FAT has [`crate::fs::fat32::geometry::NUM_FATS`] = 2
/// mirror copies of this table; the dispatcher writes the same
/// bytes for either copy.
#[derive(Debug, Clone)]
pub struct FatTable {
    entries: Vec<u32>,
    fat_size_sectors: u32,
}

impl FatTable {
    /// Build the FAT table by walking every chain in `backend`.
    ///
    /// # Errors
    ///
    /// * [`FatTableError::ChainContainsReservedCluster`] if any
    ///   chain references cluster 0 or 1.
    /// * [`FatTableError::ChainContainsOutOfRangeCluster`] if any
    ///   chain references a cluster beyond
    ///   `data_cluster_count + 1`.
    /// * [`FatTableError::ChainContainsBadClusterMarker`] if any
    ///   chain contains `0x0FFF_FFF7`.
    /// * [`FatTableError::ClusterAllocatedTwice`] if the same
    ///   cluster is reached from more than one chain or appears
    ///   twice in one chain.
    /// * [`FatTableError::EmptyChain`] if the backend yields a
    ///   zero-length chain.
    pub fn build(geo: &Fat32Geometry, backend: &dyn DirTreeBackend) -> Result<Self, FatTableError> {
        let data_cluster_count = geo.data_cluster_count();
        // `data_cluster_count` is bounded by MAX_FAT32_DATA_CLUSTERS
        // = 0x0FFF_FFF4, so `data_cluster_count + 2` fits in u32
        // without overflow and trivially in `usize` on every
        // supported target (32-bit and 64-bit alike).
        let max_cluster = ROOT_DIRECTORY_CLUSTER + data_cluster_count - 1;
        let entry_count = (data_cluster_count + ROOT_DIRECTORY_CLUSTER) as usize;
        let mut entries = vec![FREE_CLUSTER; entry_count];
        // The geometry constructor enforces data_cluster_count ≥
        // MIN_FAT32_DATA_CLUSTERS (= 65,525), so entry_count is at
        // least 65,527 > 2 and the reserved-entry writes below
        // cannot index out of bounds.
        write_reserved_entries(&mut entries);

        let mut walk_result: Result<(), FatTableError> = Ok(());
        backend.for_each_chain(&mut |chain| {
            if walk_result.is_err() {
                return;
            }
            walk_result = link_chain(&mut entries, chain, max_cluster);
        });
        walk_result?;

        Ok(Self {
            entries,
            fat_size_sectors: geo.fat_size_sectors(),
        })
    }

    /// Borrow the raw entry table.
    ///
    /// `entries[N]` is the FAT entry for cluster N. `entries[0]`
    /// and `entries[1]` are the reserved sentinels;
    /// `entries[2..]` is one slot per data cluster.
    #[must_use]
    pub fn entries(&self) -> &[u32] {
        &self.entries
    }

    /// The geometry's `fat_size_sectors` ceiling, captured at
    /// build time so the synthesizer doesn't need a fresh
    /// geometry reference per sector.
    #[must_use]
    pub fn fat_size_sectors(&self) -> u32 {
        self.fat_size_sectors
    }

    /// Serialise the FAT entries that fall in `sector_in_fat`
    /// (a sector index relative to the start of *one* FAT copy)
    /// to a 512-byte buffer.
    ///
    /// Entries beyond the entry table (when the FAT is larger
    /// than `(data_cluster_count + 2) × 4` bytes, which is the
    /// common case because the FAT size is rounded up) are
    /// emitted as [`FREE_CLUSTER`]. This matches `mkfs.vfat`'s
    /// behaviour and is what fatgen103 §3 implies for the
    /// padding region.
    ///
    /// # Errors
    ///
    /// [`FatTableError::SectorIndexOutOfRange`] if `sector_in_fat`
    /// is ≥ [`FatTable::fat_size_sectors`].
    pub fn synthesize_sector(
        &self,
        sector_in_fat: u32,
    ) -> Result<[u8; FAT_SECTOR_SIZE_BYTES], FatTableError> {
        if sector_in_fat >= self.fat_size_sectors {
            return Err(FatTableError::SectorIndexOutOfRange {
                sector_in_fat,
                fat_size_sectors: self.fat_size_sectors,
            });
        }

        let mut buf = [0u8; FAT_SECTOR_SIZE_BYTES];
        let first_entry_index = (sector_in_fat as usize) * (FAT_ENTRIES_PER_SECTOR as usize);

        for slot in 0..(FAT_ENTRIES_PER_SECTOR as usize) {
            let entry_index = first_entry_index + slot;
            let value = self
                .entries
                .get(entry_index)
                .copied()
                .unwrap_or(FREE_CLUSTER);
            let offset = slot * (FAT_ENTRY_SIZE_BYTES as usize);
            write_u32_le(&mut buf, offset, value);
        }
        Ok(buf)
    }
}

/// Stamp the reserved FAT entries (indices 0 and 1) into the
/// freshly-allocated entry table.
///
/// Splitting this out of [`FatTable::build`] lets the caller's
/// `# Errors` documentation stay focused on the chain-walk
/// failure modes; this helper is infallible and the index access
/// is bounded by the [`FatTable::build`] invariant that
/// `entries.len()` ≥ `MIN_FAT32_DATA_CLUSTERS` (65,527 > 2).
#[inline]
fn write_reserved_entries(entries: &mut [u32]) {
    if let Some((e0, rest)) = entries.split_first_mut() {
        *e0 = FAT_ENTRY_0_FIXED_MEDIA;
        if let Some(e1) = rest.first_mut() {
            *e1 = FAT_ENTRY_1_CLEAN_HEALTHY;
        }
    }
}

fn link_chain(entries: &mut [u32], chain: &[u32], max_cluster: u32) -> Result<(), FatTableError> {
    if chain.is_empty() {
        return Err(FatTableError::EmptyChain);
    }

    for (i, &cluster) in chain.iter().enumerate() {
        validate_cluster_number(cluster, max_cluster)?;
        let slot = cluster as usize;
        // `validate_cluster_number` rejected anything > max_cluster,
        // so `slot` is < entries.len(); the `Option::unwrap_or` is a
        // belt-and-braces measure that keeps the helper panic-free
        // for the clippy gate.
        let current = entries.get(slot).copied().unwrap_or(FREE_CLUSTER);
        if current != FREE_CLUSTER {
            return Err(FatTableError::ClusterAllocatedTwice { cluster });
        }
        let next_value = if i + 1 < chain.len() {
            // chain[i + 1] has not been validated yet — it may
            // exceed max_cluster or equal BAD_CLUSTER_MARKER. The
            // next loop iteration's validate_cluster_number call
            // will surface either case as the appropriate
            // FatTableError variant. The intermediate "possibly
            // invalid" value we write here is never observable
            // because FatTable::build returns Err and drops the
            // entries vec. Writing a real (possibly invalid)
            // next-cluster value here — rather than a sentinel
            // we'd later have to overwrite — keeps the chain walk
            // a single-pass operation, which matters for the
            // cold-start budget (Phase 2.14).
            let raw_next = chain.get(i + 1).copied().unwrap_or(END_OF_CHAIN_MARKER);
            raw_next & FAT32_ENTRY_MASK
        } else {
            END_OF_CHAIN_MARKER
        };
        if let Some(slot_ref) = entries.get_mut(slot) {
            *slot_ref = next_value;
        }
    }

    Ok(())
}

fn validate_cluster_number(cluster: u32, max_cluster: u32) -> Result<(), FatTableError> {
    if cluster == BAD_CLUSTER_MARKER {
        return Err(FatTableError::ChainContainsBadClusterMarker { cluster });
    }
    if cluster < ROOT_DIRECTORY_CLUSTER {
        return Err(FatTableError::ChainContainsReservedCluster { cluster });
    }
    if cluster > max_cluster {
        return Err(FatTableError::ChainContainsOutOfRangeCluster {
            cluster,
            max_cluster,
        });
    }
    Ok(())
}

/// Write a `u32` in little-endian byte order to `buf[offset..offset+4]`.
///
/// Every call site uses an offset derived from a fixed
/// [`FAT_ENTRIES_PER_SECTOR`] sweep over a 512-byte buffer, so the
/// `indexing_slicing` lint is safe to suppress.
#[inline]
#[allow(clippy::indexing_slicing)]
fn write_u32_le(buf: &mut [u8; FAT_SECTOR_SIZE_BYTES], offset: usize, value: u32) {
    buf[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
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

    const MIB: u64 = 1024 * 1024;
    const GIB: u64 = 1024 * 1024 * 1024;

    fn geo_4gib() -> Fat32Geometry {
        Fat32Geometry::for_volume_size(4 * GIB).expect("valid 4 GiB geometry")
    }

    fn geo_34mib() -> Fat32Geometry {
        // 34 MiB is the smallest volume size that produces ≥ 65,525
        // data clusters at 1-sector clusters; 32 MiB falls just
        // short. See crate::fs::fat32::geometry tests.
        Fat32Geometry::for_volume_size(34 * MIB).expect("valid 34 MiB geometry")
    }

    fn read_u32_le(sector: &[u8; 512], offset: usize) -> u32 {
        u32::from_le_bytes(sector[offset..offset + 4].try_into().unwrap())
    }

    fn root_only_tree() -> InMemoryDirTree {
        InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]])
    }

    // --- Compile-time sanity ---------------------------------------------

    #[test]
    fn fat_entry_size_is_four_bytes() {
        assert_eq!(FAT_ENTRY_SIZE_BYTES, 4);
    }

    #[test]
    fn fat_entries_per_sector_is_128() {
        assert_eq!(FAT_ENTRIES_PER_SECTOR, 128);
    }

    #[test]
    fn entry_mask_is_low_28_bits() {
        assert_eq!(FAT32_ENTRY_MASK, 0x0FFF_FFFF);
        assert_eq!(FAT32_ENTRY_MASK.count_ones(), 28);
    }

    #[test]
    fn fat_entry_0_low_byte_matches_boot_sector_media_descriptor() {
        // Cross-check: the low byte of FAT[0] MUST equal BPB_Media.
        assert_eq!(
            (FAT_ENTRY_0_FIXED_MEDIA & 0xFF) as u8,
            MEDIA_DESCRIPTOR_FIXED
        );
    }

    #[test]
    fn fat_entry_1_is_in_canonical_eoc_range() {
        // Cross-check at runtime that the compile-time constants
        // we ship are consistent with what fatgen103 §4.1 says.
        // (The same comparison is asserted at module load via
        // `const _: () = { ... };`; we keep a runtime test so an
        // accidental change to the consts still surfaces here.)
        let v = FAT_ENTRY_1_CLEAN_HEALTHY;
        assert!(v >= END_OF_CHAIN_MIN, "{v:#010X} below EOC min");
        assert!(
            v <= END_OF_CHAIN_MARKER,
            "{v:#010X} above EOC max {END_OF_CHAIN_MARKER:#010X}"
        );
        assert_eq!(v & !FAT32_ENTRY_MASK, 0, "no reserved top bits set");
    }

    #[test]
    fn end_of_chain_marker_is_in_canonical_range() {
        let v = END_OF_CHAIN_MARKER;
        assert!(v >= END_OF_CHAIN_MIN, "{v:#010X} below EOC min");
        assert_eq!(v & !FAT32_ENTRY_MASK, 0, "no reserved top bits set");
    }

    // --- Reserved entries 0 and 1 ----------------------------------------

    #[test]
    fn build_writes_fat_entry_0_for_fixed_media() {
        let table = FatTable::build(&geo_4gib(), &root_only_tree()).expect("valid build");
        assert_eq!(table.entries()[0], FAT_ENTRY_0_FIXED_MEDIA);
    }

    #[test]
    fn build_writes_fat_entry_1_clean_and_healthy() {
        let table = FatTable::build(&geo_4gib(), &root_only_tree()).expect("valid build");
        assert_eq!(table.entries()[1], FAT_ENTRY_1_CLEAN_HEALTHY);
    }

    #[test]
    fn build_writes_reserved_entries_even_for_empty_tree() {
        // An empty backend (no chains at all) still produces a
        // FAT with the reserved entries set. Useful as a baseline
        // for tests; a real volume always has the root chain.
        let table = FatTable::build(&geo_4gib(), &InMemoryDirTree::empty()).expect("valid build");
        assert_eq!(table.entries()[0], FAT_ENTRY_0_FIXED_MEDIA);
        assert_eq!(table.entries()[1], FAT_ENTRY_1_CLEAN_HEALTHY);
    }

    // --- Chain construction: contiguous ----------------------------------

    #[test]
    fn single_cluster_chain_terminates_with_eoc_marker() {
        let table = FatTable::build(&geo_4gib(), &root_only_tree()).expect("valid build");
        assert_eq!(table.entries()[2], END_OF_CHAIN_MARKER);
    }

    #[test]
    fn contiguous_two_cluster_chain() {
        let tree = InMemoryDirTree::from_chains(vec![vec![2, 3]]);
        let table = FatTable::build(&geo_4gib(), &tree).expect("valid build");
        assert_eq!(table.entries()[2], 3);
        assert_eq!(table.entries()[3], END_OF_CHAIN_MARKER);
    }

    #[test]
    fn contiguous_three_cluster_chain() {
        let tree = InMemoryDirTree::from_chains(vec![vec![2, 3, 4]]);
        let table = FatTable::build(&geo_4gib(), &tree).expect("valid build");
        assert_eq!(table.entries()[2], 3);
        assert_eq!(table.entries()[3], 4);
        assert_eq!(table.entries()[4], END_OF_CHAIN_MARKER);
    }

    #[test]
    fn contiguous_long_chain_links_every_step() {
        let chain: Vec<u32> = (2..=200).collect();
        let last = *chain.last().unwrap();
        let tree = InMemoryDirTree::from_chains(vec![chain]);
        let table = FatTable::build(&geo_4gib(), &tree).expect("valid build");
        for cluster in 2..last {
            assert_eq!(
                table.entries()[cluster as usize],
                cluster + 1,
                "cluster {cluster} should point to {} but pointed to {}",
                cluster + 1,
                table.entries()[cluster as usize]
            );
        }
        assert_eq!(table.entries()[last as usize], END_OF_CHAIN_MARKER);
    }

    // --- Chain construction: fragmented ----------------------------------

    #[test]
    fn fragmented_chain_jumps_anywhere() {
        // Out-of-order, non-contiguous cluster numbers exercise
        // the chain-walk logic — this is the case that
        // distinguishes a real FAT from a "just write i+1" stub.
        let tree = InMemoryDirTree::from_chains(vec![vec![2, 100, 5, 999, 50]]);
        let table = FatTable::build(&geo_4gib(), &tree).expect("valid build");
        assert_eq!(table.entries()[2], 100);
        assert_eq!(table.entries()[100], 5);
        assert_eq!(table.entries()[5], 999);
        assert_eq!(table.entries()[999], 50);
        assert_eq!(table.entries()[50], END_OF_CHAIN_MARKER);
        // And the intermediate "skipped" clusters stay free.
        assert_eq!(table.entries()[3], FREE_CLUSTER);
        assert_eq!(table.entries()[4], FREE_CLUSTER);
        assert_eq!(table.entries()[6], FREE_CLUSTER);
        assert_eq!(table.entries()[51], FREE_CLUSTER);
        assert_eq!(table.entries()[998], FREE_CLUSTER);
    }

    #[test]
    fn two_independent_chains_do_not_interfere() {
        let tree = InMemoryDirTree::from_chains(vec![vec![2], vec![3, 4]]);
        let table = FatTable::build(&geo_4gib(), &tree).expect("valid build");
        assert_eq!(table.entries()[2], END_OF_CHAIN_MARKER, "root chain");
        assert_eq!(table.entries()[3], 4, "file chain link");
        assert_eq!(table.entries()[4], END_OF_CHAIN_MARKER, "file chain end");
    }

    #[test]
    fn chain_ending_at_max_cluster_is_accepted() {
        let geo = geo_4gib();
        let max_cluster = ROOT_DIRECTORY_CLUSTER + geo.data_cluster_count() - 1;
        let tree = InMemoryDirTree::from_chains(vec![vec![2], vec![max_cluster]]);
        let table = FatTable::build(&geo, &tree).expect("valid build at max cluster");
        assert_eq!(table.entries()[max_cluster as usize], END_OF_CHAIN_MARKER);
    }

    // --- Free clusters stay zero -----------------------------------------

    #[test]
    fn unallocated_clusters_are_free_marker() {
        let tree = InMemoryDirTree::from_chains(vec![vec![2]]);
        let table = FatTable::build(&geo_4gib(), &tree).expect("valid build");
        // Every cluster from 3 to data_cluster_count+1 must be free.
        for cluster in 3..(table.entries().len()) {
            assert_eq!(
                table.entries()[cluster],
                FREE_CLUSTER,
                "cluster {cluster} should be free"
            );
        }
    }

    // --- Validation: reserved clusters -----------------------------------

    #[test]
    fn chain_containing_cluster_zero_is_rejected() {
        let tree = InMemoryDirTree::from_chains(vec![vec![0, 2]]);
        let err = FatTable::build(&geo_4gib(), &tree).expect_err("cluster 0 is reserved");
        assert_eq!(
            err,
            FatTableError::ChainContainsReservedCluster { cluster: 0 }
        );
    }

    #[test]
    fn chain_containing_cluster_one_is_rejected() {
        let tree = InMemoryDirTree::from_chains(vec![vec![1, 2]]);
        let err = FatTable::build(&geo_4gib(), &tree).expect_err("cluster 1 is reserved");
        assert_eq!(
            err,
            FatTableError::ChainContainsReservedCluster { cluster: 1 }
        );
    }

    // --- Validation: out of range ----------------------------------------

    #[test]
    fn chain_referencing_cluster_above_max_is_rejected() {
        let geo = geo_4gib();
        let max_cluster = ROOT_DIRECTORY_CLUSTER + geo.data_cluster_count() - 1;
        let bad = max_cluster + 1;
        let tree = InMemoryDirTree::from_chains(vec![vec![2, bad]]);
        let err = FatTable::build(&geo, &tree).expect_err("above max cluster");
        assert_eq!(
            err,
            FatTableError::ChainContainsOutOfRangeCluster {
                cluster: bad,
                max_cluster
            }
        );
    }

    #[test]
    fn chain_referencing_huge_cluster_is_rejected() {
        let geo = geo_4gib();
        let max_cluster = ROOT_DIRECTORY_CLUSTER + geo.data_cluster_count() - 1;
        // Just below the bad-cluster marker so we don't conflate
        // the two error variants.
        let bad = 0x0FFF_FFF6;
        let tree = InMemoryDirTree::from_chains(vec![vec![2, bad]]);
        let err = FatTable::build(&geo, &tree).expect_err("huge cluster");
        assert_eq!(
            err,
            FatTableError::ChainContainsOutOfRangeCluster {
                cluster: bad,
                max_cluster
            }
        );
    }

    // --- Validation: bad-cluster marker ----------------------------------

    #[test]
    fn chain_containing_bad_cluster_marker_is_rejected() {
        let tree = InMemoryDirTree::from_chains(vec![vec![2, BAD_CLUSTER_MARKER]]);
        let err = FatTable::build(&geo_4gib(), &tree).expect_err("bad-cluster marker");
        assert_eq!(
            err,
            FatTableError::ChainContainsBadClusterMarker {
                cluster: BAD_CLUSTER_MARKER
            }
        );
    }

    #[test]
    fn bad_cluster_marker_check_fires_before_out_of_range() {
        // BAD_CLUSTER_MARKER is also > max_cluster for any
        // realistic volume — confirm the more specific variant
        // wins so users get the actionable error.
        let geo = geo_34mib();
        let tree = InMemoryDirTree::from_chains(vec![vec![2, BAD_CLUSTER_MARKER]]);
        let err = FatTable::build(&geo, &tree).expect_err("bad-cluster marker");
        assert_eq!(
            err,
            FatTableError::ChainContainsBadClusterMarker {
                cluster: BAD_CLUSTER_MARKER
            }
        );
    }

    // --- Validation: double allocation -----------------------------------

    #[test]
    fn cluster_used_in_two_chains_is_rejected() {
        let tree = InMemoryDirTree::from_chains(vec![vec![2, 3], vec![3, 4]]);
        let err = FatTable::build(&geo_4gib(), &tree).expect_err("cluster 3 reused");
        assert_eq!(err, FatTableError::ClusterAllocatedTwice { cluster: 3 });
    }

    #[test]
    fn cluster_used_twice_in_same_chain_is_rejected() {
        let tree = InMemoryDirTree::from_chains(vec![vec![2, 3, 2]]);
        let err = FatTable::build(&geo_4gib(), &tree).expect_err("cluster 2 reused");
        assert_eq!(err, FatTableError::ClusterAllocatedTwice { cluster: 2 });
    }

    // --- Validation: empty chain -----------------------------------------

    #[test]
    fn empty_chain_is_rejected() {
        let tree = InMemoryDirTree::from_chains(vec![vec![]]);
        let err = FatTable::build(&geo_4gib(), &tree).expect_err("empty chain");
        assert_eq!(err, FatTableError::EmptyChain);
    }

    // --- Sector serialisation --------------------------------------------

    #[test]
    fn sector_0_holds_entries_0_through_127() {
        let tree = InMemoryDirTree::from_chains(vec![vec![2, 3, 4]]);
        let table = FatTable::build(&geo_4gib(), &tree).expect("valid build");
        let s = table.synthesize_sector(0).expect("valid sector 0");
        assert_eq!(read_u32_le(&s, 0x000), FAT_ENTRY_0_FIXED_MEDIA);
        assert_eq!(read_u32_le(&s, 0x004), FAT_ENTRY_1_CLEAN_HEALTHY);
        assert_eq!(read_u32_le(&s, 0x008), 3); // entry 2
        assert_eq!(read_u32_le(&s, 0x00C), 4); // entry 3
        assert_eq!(read_u32_le(&s, 0x010), END_OF_CHAIN_MARKER); // entry 4
        // Entry 127 lives at the last 4-byte slot of sector 0.
        assert_eq!(read_u32_le(&s, 0x1FC), FREE_CLUSTER);
    }

    #[test]
    fn sector_1_holds_entries_128_through_255() {
        // Put a chain entirely inside sector 1's range to verify
        // the sector→entry-index mapping.
        let chain: Vec<u32> = vec![2, 130, 200];
        let tree = InMemoryDirTree::from_chains(vec![chain]);
        let table = FatTable::build(&geo_4gib(), &tree).expect("valid build");
        let s = table.synthesize_sector(1).expect("valid sector 1");
        // Entry 128 starts at offset 0 of sector 1; entry 130 is at
        // offset (130 - 128) × 4 = 8.
        assert_eq!(read_u32_le(&s, 0x008), 200);
        // Entry 200 → EOC, at offset (200 - 128) × 4 = 288 = 0x120.
        assert_eq!(read_u32_le(&s, 0x120), END_OF_CHAIN_MARKER);
        // Entry 128 itself is unallocated → free.
        assert_eq!(read_u32_le(&s, 0x000), FREE_CLUSTER);
    }

    #[test]
    fn synthesize_writes_little_endian_bytes() {
        // Pick a multi-byte chain target to exercise byte order.
        // Value chosen so its 4 LE bytes are all distinct AND it
        // fits below the 4 GiB geometry's max cluster (1,046,527):
        // 0x000A_BCDE = 703,710 → LE bytes DE BC 0A 00.
        let tree = InMemoryDirTree::from_chains(vec![vec![2, 0x000A_BCDE]]);
        let table = FatTable::build(&geo_4gib(), &tree).expect("valid build");
        let s = table.synthesize_sector(0).expect("valid sector 0");
        assert_eq!(&s[0x008..0x00C], &[0xDE, 0xBC, 0x0A, 0x00]);
    }

    #[test]
    fn synthesize_top_4_bits_of_data_entries_are_zero() {
        // Per fatgen103 §4.1 the top 4 bits of every FAT entry
        // are reserved; on a freshly synthesized volume B-1
        // writes them as zero (the mask is FAT32_ENTRY_MASK).
        let tree = InMemoryDirTree::from_chains(vec![vec![2, 100, 500, 1000]]);
        let table = FatTable::build(&geo_4gib(), &tree).expect("valid build");
        for &cluster in &[2u32, 100, 500, 1000] {
            let value = table.entries()[cluster as usize];
            assert_eq!(
                value & !FAT32_ENTRY_MASK,
                0,
                "cluster {cluster} entry {value:#010X} had reserved top bits set"
            );
        }
    }

    #[test]
    fn synthesize_sector_at_last_index_succeeds() {
        let geo = geo_4gib();
        let table = FatTable::build(&geo, &root_only_tree()).expect("valid build");
        let last = geo.fat_size_sectors() - 1;
        let _ = table
            .synthesize_sector(last)
            .expect("last sector synthesizes");
    }

    #[test]
    fn synthesize_sector_past_end_is_rejected() {
        let geo = geo_4gib();
        let table = FatTable::build(&geo, &root_only_tree()).expect("valid build");
        let past = geo.fat_size_sectors();
        let err = table
            .synthesize_sector(past)
            .expect_err("sector past end rejected");
        assert_eq!(
            err,
            FatTableError::SectorIndexOutOfRange {
                sector_in_fat: past,
                fat_size_sectors: geo.fat_size_sectors()
            }
        );
    }

    #[test]
    fn padding_sectors_beyond_entry_table_are_all_free() {
        // The FAT size is rounded up; sectors beyond the entry
        // table proper are entirely FREE_CLUSTER bytes.
        let geo = geo_4gib();
        let table = FatTable::build(&geo, &root_only_tree()).expect("valid build");
        let last = geo.fat_size_sectors() - 1;
        let s = table.synthesize_sector(last).expect("valid last sector");
        // The very last FAT sector may straddle the entry-table
        // boundary; sample its tail (offset 0x1FC) which is the
        // entry corresponding to the highest index reachable from
        // this sector. For 4 GiB geometry the entry table is
        // 1,046,528 entries (data_cluster_count + 2 = 1,046,528),
        // and 1,046,528 / 128 = 8176 sectors; the FAT itself is
        // 8184 sectors. So sectors 8176..8183 (8 sectors) are
        // pure padding.
        assert_eq!(read_u32_le(&s, 0x000), FREE_CLUSTER);
        assert_eq!(read_u32_le(&s, 0x1FC), FREE_CLUSTER);
    }

    // --- Cross-geometry sanity sweep -------------------------------------

    #[test]
    fn build_succeeds_across_small_medium_large_geometries() {
        // 34 MiB, 4 GiB, and 32 GiB are the three sizes the
        // existing crate::fs::fat32::geometry tests explicitly
        // verify, spanning the 1-sector, 8-sector, and 64-sector
        // cluster-size buckets. Each must build a valid FAT table
        // with the reserved entries populated and the root chain
        // terminated at cluster 2. Intermediate sizes inside each
        // row are exercised by the fsinfo crate's 17 MiB sweep
        // and (after Phase 2.6) the dispatcher integration tests.
        for &size in &[34 * MIB, 4 * GIB, 32 * GIB] {
            let geo = Fat32Geometry::for_volume_size(size)
                .unwrap_or_else(|e| panic!("geometry for {size}: {e:?}"));
            let table =
                FatTable::build(&geo, &root_only_tree()).expect("valid build at every size");
            assert_eq!(
                table.entries()[0],
                FAT_ENTRY_0_FIXED_MEDIA,
                "FAT[0] at size {size}"
            );
            assert_eq!(
                table.entries()[1],
                FAT_ENTRY_1_CLEAN_HEALTHY,
                "FAT[1] at size {size}"
            );
            assert_eq!(
                table.entries()[2],
                END_OF_CHAIN_MARKER,
                "root chain EOC at size {size}"
            );
            // Sanity-check: the entry table size matches geometry.
            let expected_entries = (geo.data_cluster_count() + ROOT_DIRECTORY_CLUSTER) as usize;
            assert_eq!(
                table.entries().len(),
                expected_entries,
                "entry count at size {size}"
            );
        }
    }

    // --- Hand-built full-sector comparison -------------------------------

    #[test]
    fn full_sector_0_matches_hand_built_expected_for_known_chains() {
        // Two chains: root=[2] and a file=[3, 5, 4].
        let tree = InMemoryDirTree::from_chains(vec![vec![2], vec![3, 5, 4]]);
        let table = FatTable::build(&geo_4gib(), &tree).expect("valid build");
        let actual = table.synthesize_sector(0).expect("valid sector 0");

        let mut expected = [0u8; 512];
        // Entry 0: FAT_ENTRY_0_FIXED_MEDIA = 0x0FFFFFF8 (LE: F8 FF FF 0F).
        expected[0x000..0x004].copy_from_slice(&FAT_ENTRY_0_FIXED_MEDIA.to_le_bytes());
        // Entry 1: 0x0FFFFFFF (LE: FF FF FF 0F).
        expected[0x004..0x008].copy_from_slice(&FAT_ENTRY_1_CLEAN_HEALTHY.to_le_bytes());
        // Entry 2: EOC (root chain end).
        expected[0x008..0x00C].copy_from_slice(&END_OF_CHAIN_MARKER.to_le_bytes());
        // Entry 3: → 5.
        expected[0x00C..0x010].copy_from_slice(&5u32.to_le_bytes());
        // Entry 4: EOC (last cluster of the [3,5,4] chain).
        expected[0x010..0x014].copy_from_slice(&END_OF_CHAIN_MARKER.to_le_bytes());
        // Entry 5: → 4.
        expected[0x014..0x018].copy_from_slice(&4u32.to_le_bytes());
        // Everything else (entries 6..127, offsets 0x018..0x200) stays zero.

        assert_eq!(actual, expected);
    }
}
