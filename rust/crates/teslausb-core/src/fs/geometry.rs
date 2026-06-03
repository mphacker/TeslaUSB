//! Filesystem geometry abstraction.
//!
//! A *geometry* describes how a synthesized filesystem lays out its
//! on-disk regions for a given total volume size — where the boot
//! sector lives, where the FAT (or `exFAT` allocation bitmap) lives,
//! where the data region starts, and so on. Geometry is pure
//! compute: no I/O, no syscalls, no allocations beyond the
//! [`Region`] list owned by each concrete implementation.
//!
//! The [`Geometry`] trait is the seam consumed by the
//! `synth::read` dispatcher: given an incoming byte offset (from
//! the NBD wire) the dispatcher calls [`Geometry::region_at`] to
//! decide which sub-synthesizer (`crate::fs::exfat::geometry` for
//! the exFAT boot region, the FAT table, directory entries, etc.)
//! should produce the bytes for that offset.
//!
//! ## Why a trait?
//!
//! The region map has a shared shape (header → metadata tables →
//! data), but the *count* and *kind* of regions are exFAT-specific.
//! A trait lets the dispatcher and integration tests be written
//! once and parameterised over `<G: Geometry>` (charter §"Best
//! Architecture Practices" — dependency-inversion seam).
//!
//! ## Region kinds are FS-narrow
//!
//! [`RegionKind`] enumerates the exFAT region variants. The enum
//! is internal and pre-1.0, so extension is fair game; downstream
//! `match` arms get a `clippy::non_exhaustive_omitted_patterns`
//! warning if they forget a new variant.

use core::fmt;
use core::ops::Range;

/// Logical sector size used by every B-1 synthesized filesystem.
///
/// 512 bytes is the universal default — every USB mass-storage
/// peripheral on the planet announces 512-byte logical sectors,
/// `mkfs.vfat` defaults to 512, Linux's `nbd-client` driver pins
/// `BLKSSZGET` to whatever the export reports, and the FAT32
/// cluster-size lookup tables Microsoft published assume 512.
///
/// If a future increment needs a non-512 sector size (Advanced
/// Format 4 KiB physical sectors with 512-byte logical sectors are
/// fine; native 4 KiB logical sectors are not) it must be plumbed
/// through every [`Geometry`] implementation as a constructor
/// parameter and the cluster-lookup table re-derived. Keep the
/// `pub const` here so callers that hard-code 512 (the boot-sector
/// `BPB_BytsPerSec` field in Phase 2.2, for example) have a single
/// source of truth to follow.
pub const SECTOR_SIZE_BYTES: u32 = 512;

/// Errors a [`Geometry`] constructor may return.
///
/// The error set is small on purpose: every variant pins one
/// invariant a concrete geometry must enforce before it can claim
/// to describe a valid on-disk filesystem.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum GeometryError {
    /// The volume is too small to hold a valid filesystem of the
    /// requested kind.
    ///
    /// FAT32 has a hard floor of 65,525 data clusters (otherwise
    /// `mkfs.vfat` will silently produce a FAT16 volume instead);
    /// at the smallest legal cluster size of 1 sector (512 B) that
    /// works out to ≥ 33,554,432 bytes (32 MiB) plus reserved area
    /// plus two FAT copies. The `minimum` field reports the exact
    /// threshold the constructor computed for the chosen FS.
    #[error("volume of {bytes} bytes is too small (minimum {minimum} bytes for this filesystem)")]
    VolumeTooSmall {
        /// The size the caller passed in.
        bytes: u64,
        /// The smallest size for which this filesystem is valid.
        minimum: u64,
    },

    /// The volume is too large to address with the chosen FS.
    ///
    /// FAT32 has a hard ceiling of `0x0FFFFFF5` data clusters (the
    /// remaining 11 of the 28-bit cluster numbers are reserved by
    /// the spec for end-of-chain / bad-cluster markers); at the
    /// largest cluster size Microsoft documents (32 KiB / 64
    /// sectors) that works out to ~2 TiB. Beyond that, `mkfs.vfat`
    /// refuses to format.
    #[error("volume of {bytes} bytes is too large (maximum {maximum} bytes for this filesystem)")]
    VolumeTooLarge {
        /// The size the caller passed in.
        bytes: u64,
        /// The largest size this filesystem can describe.
        maximum: u64,
    },

    /// The volume size is not a whole multiple of the sector size.
    ///
    /// Geometry math assumes integer sector counts; a non-aligned
    /// volume size is a configuration bug that must be caught at
    /// construction rather than silently truncated.
    #[error("volume of {bytes} bytes is not a whole multiple of {sector_size} bytes")]
    UnalignedVolumeSize {
        /// The size the caller passed in.
        bytes: u64,
        /// The sector size this filesystem uses.
        sector_size: u32,
    },
}

/// One contiguous range of bytes in a synthesized volume.
///
/// Regions tile the entire volume without gaps; the region map's
/// concatenated lengths sum to the volume size and the regions are
/// stored in ascending `start` order. This invariant is enforced
/// by [`Geometry::validate_region_map`] (used by every concrete
/// constructor and verified again from the trait's default
/// [`Geometry::region_at`] implementation).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Region {
    /// Byte offset where this region starts. Always sector-aligned.
    pub start: u64,
    /// Byte length of this region. Always a positive multiple of
    /// [`SECTOR_SIZE_BYTES`] (or whatever sector size the geometry
    /// uses); a zero-length region is never emitted.
    pub len: u64,
    /// Which kind of bytes live in this region.
    pub kind: RegionKind,
}

impl Region {
    /// End offset (exclusive). Convenience for half-open range
    /// arithmetic.
    #[must_use]
    pub const fn end(self) -> u64 {
        self.start.saturating_add(self.len)
    }

    /// Half-open `[start, end)` range, suitable for `contains` /
    /// `intersects` checks.
    #[must_use]
    pub const fn range(self) -> Range<u64> {
        self.start..self.end()
    }

    /// Returns `true` if `offset` lies inside this region.
    ///
    /// The check is inclusive of `start` and exclusive of `end`,
    /// matching the half-open convention.
    #[must_use]
    pub const fn contains(self, offset: u64) -> bool {
        offset >= self.start && offset < self.end()
    }
}

/// Tag identifying which on-disk structure lives in a [`Region`].
///
/// The exFAT boot-region variants are produced by
/// `fs::exfat::geometry`; the allocation bitmap and up-case table
/// live inside the cluster heap and are served from the
/// [`Self::Data`] region by the exFAT synth.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum RegionKind {
    /// exFAT main boot region — 12 contiguous 512-byte sectors at
    /// the start of the volume (sectors 0..12). Contains, in order:
    /// the main boot sector, 8 main extended boot sectors, the main
    /// OEM parameters sector, a reserved sector, and the main boot
    /// checksum sector. Microsoft exFAT spec v1.00 §3.1.
    ExfatMainBootRegion,
    /// exFAT backup boot region — 12 contiguous 512-byte sectors
    /// immediately following the main boot region (sectors 12..24).
    /// Mirror of [`Self::ExfatMainBootRegion`]. Microsoft exFAT
    /// spec v1.00 §3.2.
    ExfatBackupBootRegion,
    /// Reserved sectors that contain no defined structure — must
    /// be zero-filled on read.
    Reserved,
    /// One of the FAT table copies. `index` is 0 for the primary
    /// FAT, 1 for the mirror (FAT32 always has `num_fats = 2`).
    FatTable {
        /// Which mirror copy.
        index: u8,
    },
    /// The data region — cluster-allocated storage that holds file
    /// contents and (for FAT32) the root directory chain.
    Data,
}

impl fmt::Display for RegionKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ExfatMainBootRegion => f.write_str("exfat-main-boot-region"),
            Self::ExfatBackupBootRegion => f.write_str("exfat-backup-boot-region"),
            Self::Reserved => f.write_str("reserved"),
            Self::FatTable { index } => write!(f, "fat-table-{index}"),
            Self::Data => f.write_str("data"),
        }
    }
}

/// Shared geometry surface for every B-1 synthesized filesystem.
///
/// See the module-level doc for the rationale; see
/// `crate::fs::exfat::geometry` for the exFAT implementation.
pub trait Geometry {
    /// Bytes per logical sector. Always [`SECTOR_SIZE_BYTES`] for
    /// the current geometries; a future variable-sector geometry
    /// would override.
    fn sector_size_bytes(&self) -> u32 {
        SECTOR_SIZE_BYTES
    }

    /// Total number of logical sectors in the volume.
    ///
    /// `total_sectors() * sector_size_bytes()` is always equal to
    /// the volume size in bytes the geometry was constructed for.
    fn total_sectors(&self) -> u64;

    /// Bytes per data cluster.
    ///
    /// `sectors_per_cluster × sector_size_bytes`.
    fn bytes_per_cluster(&self) -> u32;

    /// Number of allocatable clusters in the data region.
    ///
    /// `crate::fs::exfat::geometry` enforces the exFAT bounds on
    /// construction.
    fn data_cluster_count(&self) -> u32;

    /// Ordered, gap-free region map.
    ///
    /// Invariants enforced by [`Self::validate_region_map`]:
    ///
    /// 1. Slice is non-empty.
    /// 2. `regions()[0].start == 0`.
    /// 3. Each `region.start + region.len == next_region.start`
    ///    (no gaps, no overlaps).
    /// 4. `regions().last().end() == total_sectors() * sector_size_bytes()`.
    /// 5. Every region has `len > 0`.
    fn regions(&self) -> &[Region];

    /// Total volume size in bytes — convenience computed from
    /// [`Self::total_sectors`] × [`Self::sector_size_bytes`].
    #[must_use]
    fn volume_size_bytes(&self) -> u64 {
        u64::from(self.sector_size_bytes()).saturating_mul(self.total_sectors())
    }

    /// Locate the region that contains `byte_offset`.
    ///
    /// Returns `None` for any offset at or beyond
    /// [`Self::volume_size_bytes`].
    ///
    /// The default implementation does a linear scan; that is
    /// `O(r)` over the region count (typically 5–6 for FAT32, ~10
    /// for `exFAT`) and outperforms a binary search at that scale
    /// because it avoids the branch overhead. A concrete geometry
    /// is free to override with a binary search if it ever holds
    /// hundreds of regions.
    fn region_at(&self, byte_offset: u64) -> Option<Region> {
        self.regions()
            .iter()
            .find(|region| region.contains(byte_offset))
            .copied()
    }

    /// Validate that a freshly-constructed region map satisfies
    /// every invariant documented on [`Self::regions`].
    ///
    /// Called by concrete constructors after assembling their
    /// region list. Lives on the trait (rather than a free
    /// function in this module) so the doc + the validation logic
    /// stay co-located with the invariant they enforce.
    ///
    /// # Errors
    ///
    /// Returns [`RegionMapError`] describing the first invariant
    /// the map violates. The variants pin which check failed so a
    /// failing concrete implementation can be diagnosed without
    /// re-running the whole validator.
    fn validate_region_map(
        regions: &[Region],
        expected_total_bytes: u64,
    ) -> Result<(), RegionMapError> {
        if regions.is_empty() {
            return Err(RegionMapError::Empty);
        }
        let Some(first) = regions.first() else {
            return Err(RegionMapError::Empty);
        };
        if first.start != 0 {
            return Err(RegionMapError::DoesNotStartAtZero { start: first.start });
        }
        let mut cursor: u64 = 0;
        for (index, region) in regions.iter().enumerate() {
            if region.len == 0 {
                return Err(RegionMapError::ZeroLengthRegion { index });
            }
            if region.start != cursor {
                return Err(RegionMapError::Gap {
                    index,
                    expected_start: cursor,
                    actual_start: region.start,
                });
            }
            cursor = region
                .start
                .checked_add(region.len)
                .ok_or(RegionMapError::Overflow { index })?;
        }
        if cursor != expected_total_bytes {
            return Err(RegionMapError::WrongTotal {
                covered: cursor,
                expected: expected_total_bytes,
            });
        }
        Ok(())
    }
}

/// Failure modes for [`Geometry::validate_region_map`].
///
/// Used internally by concrete geometry constructors as a
/// self-check; if any variant ever escapes the
/// `teslausb-core::fs::*::geometry` test suite it indicates a bug
/// in the constructor, not a user-supplied bad input. Exposed
/// publicly so future filesystems (`exFAT` in Phase 2.8) can re-use
/// the validator.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum RegionMapError {
    /// The region slice is empty.
    #[error("region map is empty")]
    Empty,

    /// The first region does not start at byte 0.
    #[error("region map does not start at offset 0 (starts at {start})")]
    DoesNotStartAtZero {
        /// The offending start offset.
        start: u64,
    },

    /// A region has zero length.
    #[error("region at index {index} has zero length")]
    ZeroLengthRegion {
        /// Index of the offending region in the input slice.
        index: usize,
    },

    /// There is a gap or overlap between two adjacent regions.
    #[error(
        "gap or overlap at region index {index}: expected start {expected_start}, got {actual_start}"
    )]
    Gap {
        /// Index of the offending region.
        index: usize,
        /// What the cumulative-length cursor said the next region
        /// should start at.
        expected_start: u64,
        /// What the region's `start` field actually said.
        actual_start: u64,
    },

    /// A region's `start + len` overflows `u64`.
    #[error("region at index {index} overflows u64 in start + len arithmetic")]
    Overflow {
        /// Index of the offending region.
        index: usize,
    },

    /// The covered byte range does not match the expected total.
    #[error("region map covers {covered} bytes; expected {expected}")]
    WrongTotal {
        /// Sum of region lengths.
        covered: u64,
        /// Total volume size the geometry claimed.
        expected: u64,
    },
}

#[cfg(test)]
mod tests {
    use super::{Geometry, Region, RegionKind, RegionMapError};

    struct StubGeometry {
        total_sectors: u64,
        bytes_per_cluster: u32,
        data_cluster_count: u32,
        regions: Vec<Region>,
    }

    impl Geometry for StubGeometry {
        fn total_sectors(&self) -> u64 {
            self.total_sectors
        }
        fn bytes_per_cluster(&self) -> u32 {
            self.bytes_per_cluster
        }
        fn data_cluster_count(&self) -> u32 {
            self.data_cluster_count
        }
        fn regions(&self) -> &[Region] {
            &self.regions
        }
    }

    fn region(start: u64, len: u64, kind: RegionKind) -> Region {
        Region { start, len, kind }
    }

    #[test]
    fn region_contains_inclusive_of_start_exclusive_of_end() {
        let r = region(100, 50, RegionKind::Data);
        assert!(r.contains(100));
        assert!(r.contains(125));
        assert!(r.contains(149));
        assert!(!r.contains(150), "end is exclusive");
        assert!(!r.contains(99));
        assert!(!r.contains(151));
    }

    #[test]
    fn region_end_and_range_are_consistent() {
        let r = region(512, 1024, RegionKind::Reserved);
        assert_eq!(r.end(), 1536);
        assert_eq!(r.range(), 512..1536);
    }

    #[test]
    fn region_at_returns_some_for_offsets_in_range() {
        let geo = StubGeometry {
            total_sectors: 100,
            bytes_per_cluster: 512,
            data_cluster_count: 0,
            regions: vec![
                region(0, 1024, RegionKind::ExfatMainBootRegion),
                region(1024, 2048, RegionKind::FatTable { index: 0 }),
                region(3072, 100 * 512 - 3072, RegionKind::Data),
            ],
        };
        assert_eq!(
            geo.region_at(0).map(|r| r.kind),
            Some(RegionKind::ExfatMainBootRegion)
        );
        assert_eq!(
            geo.region_at(1023).map(|r| r.kind),
            Some(RegionKind::ExfatMainBootRegion)
        );
        assert_eq!(
            geo.region_at(1024).map(|r| r.kind),
            Some(RegionKind::FatTable { index: 0 })
        );
        assert_eq!(geo.region_at(3072).map(|r| r.kind), Some(RegionKind::Data));
    }

    #[test]
    fn region_at_returns_none_past_volume_end() {
        let geo = StubGeometry {
            total_sectors: 2,
            bytes_per_cluster: 512,
            data_cluster_count: 0,
            regions: vec![region(0, 1024, RegionKind::Data)],
        };
        assert_eq!(geo.region_at(1024), None);
        assert_eq!(geo.region_at(u64::MAX), None);
    }

    #[test]
    fn volume_size_bytes_matches_sectors_times_sector_size() {
        let geo = StubGeometry {
            total_sectors: 65536,
            bytes_per_cluster: 512,
            data_cluster_count: 0,
            regions: vec![region(0, 65536 * 512, RegionKind::Data)],
        };
        assert_eq!(geo.volume_size_bytes(), 65536_u64 * 512);
    }

    #[test]
    fn validate_region_map_accepts_a_well_formed_map() {
        let regions = vec![
            region(0, 1024, RegionKind::ExfatMainBootRegion),
            region(1024, 2048, RegionKind::FatTable { index: 0 }),
            region(3072, 5120, RegionKind::Data),
        ];
        assert_eq!(
            <StubGeometry as Geometry>::validate_region_map(&regions, 8192),
            Ok(())
        );
    }

    #[test]
    fn validate_region_map_rejects_empty() {
        assert_eq!(
            <StubGeometry as Geometry>::validate_region_map(&[], 1024),
            Err(RegionMapError::Empty)
        );
    }

    #[test]
    fn validate_region_map_rejects_non_zero_first_start() {
        let regions = vec![region(512, 1024, RegionKind::Data)];
        assert_eq!(
            <StubGeometry as Geometry>::validate_region_map(&regions, 1024),
            Err(RegionMapError::DoesNotStartAtZero { start: 512 })
        );
    }

    #[test]
    fn validate_region_map_rejects_zero_length_region() {
        let regions = vec![
            region(0, 1024, RegionKind::ExfatMainBootRegion),
            region(1024, 0, RegionKind::Reserved),
            region(1024, 1024, RegionKind::Data),
        ];
        assert_eq!(
            <StubGeometry as Geometry>::validate_region_map(&regions, 2048),
            Err(RegionMapError::ZeroLengthRegion { index: 1 })
        );
    }

    #[test]
    fn validate_region_map_rejects_gap() {
        let regions = vec![
            region(0, 1024, RegionKind::ExfatMainBootRegion),
            region(2048, 1024, RegionKind::Data),
        ];
        assert_eq!(
            <StubGeometry as Geometry>::validate_region_map(&regions, 3072),
            Err(RegionMapError::Gap {
                index: 1,
                expected_start: 1024,
                actual_start: 2048,
            })
        );
    }

    #[test]
    fn validate_region_map_rejects_overlap() {
        let regions = vec![
            region(0, 1024, RegionKind::ExfatMainBootRegion),
            region(512, 1024, RegionKind::Data),
        ];
        assert_eq!(
            <StubGeometry as Geometry>::validate_region_map(&regions, 1536),
            Err(RegionMapError::Gap {
                index: 1,
                expected_start: 1024,
                actual_start: 512,
            })
        );
    }

    #[test]
    fn validate_region_map_rejects_wrong_total() {
        let regions = vec![region(0, 1024, RegionKind::Data)];
        assert_eq!(
            <StubGeometry as Geometry>::validate_region_map(&regions, 2048),
            Err(RegionMapError::WrongTotal {
                covered: 1024,
                expected: 2048,
            })
        );
    }

    #[test]
    fn region_kind_display_is_kebab_case() {
        assert_eq!(
            format!("{}", RegionKind::ExfatMainBootRegion),
            "exfat-main-boot-region"
        );
        assert_eq!(
            format!("{}", RegionKind::ExfatBackupBootRegion),
            "exfat-backup-boot-region"
        );
        assert_eq!(format!("{}", RegionKind::Reserved), "reserved");
        assert_eq!(
            format!("{}", RegionKind::FatTable { index: 0 }),
            "fat-table-0"
        );
        assert_eq!(
            format!("{}", RegionKind::FatTable { index: 1 }),
            "fat-table-1"
        );
        assert_eq!(format!("{}", RegionKind::Data), "data");
    }
}
