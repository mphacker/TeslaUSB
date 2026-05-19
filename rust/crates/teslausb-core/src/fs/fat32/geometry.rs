//! FAT32 geometry — region layout for a volume of a given size.
//!
//! Implements [`crate::fs::geometry::Geometry`] using the Microsoft
//! FAT32 specification (a.k.a. **fatgen103**, version 1.03, December
//! 6, 2000). The constructor [`Fat32Geometry::for_volume_size`] takes
//! a byte count and emits an immutable geometry value whose region
//! map lays out:
//!
//! ```text
//! sector  0           BootSector
//! sector  1           FsInfo
//! sector  2..6        Reserved (zero-fill)
//! sector  6           BackupBootSector
//! sector  7..32       Reserved (zero-fill)
//! sector 32..32+F     FatTable { index: 0 }
//! sector ..32+2F      FatTable { index: 1 }
//! sector ..end        Data
//! ```
//!
//! …where `F` is the per-FAT sector count produced by the fatgen103
//! §3 closed-form formula.
//!
//! ## Cluster size choice
//!
//! Microsoft's recommended FAT32 cluster size for a given volume
//! size is a step function published in KB140365 / fatgen103 §3.5.
//! [`Fat32Geometry::for_volume_size`] applies the same table so the
//! synthesized geometry matches what `mkfs.vfat` and Windows
//! `format.com` would produce for the same volume.
//!
//! | Volume size            | Cluster size       |
//! |------------------------|--------------------|
//! | <  32 MiB              | error (too small)  |
//! | 32 MiB  ..  64 MiB     |  512 B (1 sector)  |
//! | 64 MiB  .. 128 MiB     |   1 KiB (2 sec)    |
//! | 128 MiB .. 256 MiB     |   2 KiB (4 sec)    |
//! | 256 MiB ..   8 GiB     |   4 KiB (8 sec)    |
//! |   8 GiB ..  16 GiB     |   8 KiB (16 sec)   |
//! |  16 GiB ..  32 GiB     |  16 KiB (32 sec)   |
//! |  32 GiB .. ≈2 TiB      |  32 KiB (64 sec)   |
//! | > ≈2 TiB                | error (too large)  |
//!
//! Boundaries are half-open (`min ≤ size < max`); a 64 MiB volume
//! falls in the 1 KiB row, not the 512 B row.
//!
//! ## Fixed format choices
//!
//! For Phase 2.1 the geometry pins:
//!
//! * `BPB_BytsPerSec = 512` (universal default — see
//!   [`crate::fs::geometry::SECTOR_SIZE_BYTES`]).
//! * `BPB_RsvdSecCnt = 32` (matches `mkfs.vfat` default + leaves
//!   room for BPB + `FsInfo` + backup BPB + future spec additions).
//! * `BPB_NumFATs = 2` (the mirror copy is what every consumer
//!   expects; Microsoft FAT spec §3.1 calls a single-FAT volume
//!   "highly inadvisable").
//! * `BPB_RootClus = 2` (the first data cluster is the root
//!   directory — fatgen103 §4.1).
//!
//! The constructor takes only the volume size. If a future
//! increment needs to vary reserved sector count or FAT count
//! (e.g. for a smaller-footprint test fixture) the API will gain a
//! `Builder` while preserving the simple `for_volume_size` shortcut.

use crate::fs::geometry::{Geometry, GeometryError, Region, RegionKind, SECTOR_SIZE_BYTES};

/// Default reserved sector count for a Phase 2 FAT32 volume.
///
/// Matches `mkfs.vfat` and Microsoft `format.com` defaults. The
/// boot sector lives at offset 0; `FsInfo` at offset 1; backup
/// boot sector at offset 6 (fatgen103 §3.4). Sectors 2-5 and 7-31
/// are reserved zero-fill.
pub const RESERVED_SECTORS: u32 = 32;

/// Default number of FAT mirror copies.
pub const NUM_FATS: u8 = 2;

/// Sector index where `FsInfo` lives, relative to the start of the
/// volume (fatgen103 §3.4 `BPB_FSInfo` default).
pub const FSINFO_SECTOR_INDEX: u32 = 1;

/// Sector index where the backup boot sector lives (fatgen103 §3.4
/// `BPB_BkBootSec` default).
pub const BACKUP_BOOT_SECTOR_INDEX: u32 = 6;

/// Minimum data-cluster count for a FAT32 volume (fatgen103 §3.5).
///
/// `mkfs.vfat` and Windows `format.com` will silently downgrade to
/// FAT16 for any volume that produces fewer than this many data
/// clusters; B-1 wants a hard error instead so the daemon never
/// advertises FAT32 for what would actually format as FAT16.
pub const MIN_FAT32_DATA_CLUSTERS: u32 = 65_525;

/// Maximum data-cluster count for a FAT32 volume.
///
/// FAT32 cluster numbers are 28-bit; the top 4 bits of each entry
/// are reserved. Of the 2^28 possible values, `0x0000_0000` is
/// "free" and `0x0FFF_FFF6..=0x0FFF_FFFF` are reserved for
/// "bad cluster" / "end-of-chain" markers. The maximum usable
/// allocatable cluster number is `0x0FFF_FFF5`, and clusters 0 and
/// 1 are reserved-not-allocatable, so the cap on the count of
/// addressable data clusters is `0x0FFF_FFF5 - 1` — exactly this
/// constant.
///
/// fatgen103 §3.5 states the same threshold.
pub const MAX_FAT32_DATA_CLUSTERS: u32 = 0x0FFF_FFF4;

/// Maximum volume size FAT32 can describe at 512-byte sectors.
///
/// Equal to [`u32::MAX`] × [`SECTOR_SIZE_BYTES`] = 4 294 967 295 ×
/// 512 = 2 199 023 255 040 bytes (exactly 2 TiB − 512 bytes).
///
/// The on-disk `BPB_TotSec32` field is a 32-bit count of sectors,
/// so a FAT32 volume cannot describe more sectors than `u32::MAX`
/// regardless of the cluster size. Microsoft separately caps
/// FAT32 volume *creation* at the same effective ceiling
/// (Windows `format.com` refuses larger). Above this, the
/// constructor returns [`GeometryError::VolumeTooLarge`] — there
/// is no representation for it in the on-disk format the
/// [`crate::fs::fat32::boot_sector`] module synthesizes.
pub const MAX_FAT32_VOLUME_BYTES: u64 = (u32::MAX as u64) * (SECTOR_SIZE_BYTES as u64);

/// FAT32 geometry computed from a target volume size.
///
/// Immutable after construction. All fields are derived; the only
/// inputs are the volume size and the fixed format choices
/// documented at the module level.
#[derive(Debug, Clone)]
pub struct Fat32Geometry {
    total_sectors: u64,
    sectors_per_cluster: u32,
    fat_size_sectors: u32,
    data_cluster_count: u32,
    regions: Vec<Region>,
}

impl Fat32Geometry {
    /// Construct a FAT32 geometry for a volume of `volume_size_bytes`.
    ///
    /// The cluster size is chosen from the Microsoft KB140365 /
    /// fatgen103 §3.5 table; the FAT table size is computed using
    /// the fatgen103 §3 closed-form formula.
    ///
    /// # Errors
    ///
    /// * [`GeometryError::UnalignedVolumeSize`] if `volume_size_bytes`
    ///   is not a whole multiple of [`SECTOR_SIZE_BYTES`].
    /// * [`GeometryError::VolumeTooSmall`] if the volume is below
    ///   the 32 MiB FAT32 floor or produces fewer than
    ///   [`MIN_FAT32_DATA_CLUSTERS`] data clusters.
    /// * [`GeometryError::VolumeTooLarge`] if the volume exceeds
    ///   [`MAX_FAT32_VOLUME_BYTES`] or would produce more than
    ///   [`MAX_FAT32_DATA_CLUSTERS`] data clusters at the chosen
    ///   cluster size.
    pub fn for_volume_size(volume_size_bytes: u64) -> Result<Self, GeometryError> {
        if volume_size_bytes % u64::from(SECTOR_SIZE_BYTES) != 0 {
            return Err(GeometryError::UnalignedVolumeSize {
                bytes: volume_size_bytes,
                sector_size: SECTOR_SIZE_BYTES,
            });
        }
        if volume_size_bytes > MAX_FAT32_VOLUME_BYTES {
            return Err(GeometryError::VolumeTooLarge {
                bytes: volume_size_bytes,
                maximum: MAX_FAT32_VOLUME_BYTES,
            });
        }
        let total_sectors = volume_size_bytes / u64::from(SECTOR_SIZE_BYTES);
        let sectors_per_cluster =
            sectors_per_cluster_for(volume_size_bytes).ok_or(GeometryError::VolumeTooSmall {
                bytes: volume_size_bytes,
                minimum: MIN_VOLUME_SIZE_BYTES,
            })?;
        let fat_size_sectors = fat_size_sectors(total_sectors, sectors_per_cluster);
        let data_sectors = total_sectors
            .checked_sub(u64::from(RESERVED_SECTORS))
            .and_then(|n| n.checked_sub(u64::from(NUM_FATS) * u64::from(fat_size_sectors)))
            .ok_or(GeometryError::VolumeTooSmall {
                bytes: volume_size_bytes,
                minimum: MIN_VOLUME_SIZE_BYTES,
            })?;
        let raw_cluster_count = data_sectors / u64::from(sectors_per_cluster);
        let data_cluster_count =
            u32::try_from(raw_cluster_count).map_err(|_| GeometryError::VolumeTooLarge {
                bytes: volume_size_bytes,
                maximum: MAX_FAT32_VOLUME_BYTES,
            })?;
        if data_cluster_count < MIN_FAT32_DATA_CLUSTERS {
            return Err(GeometryError::VolumeTooSmall {
                bytes: volume_size_bytes,
                minimum: MIN_VOLUME_SIZE_BYTES,
            });
        }
        if data_cluster_count > MAX_FAT32_DATA_CLUSTERS {
            return Err(GeometryError::VolumeTooLarge {
                bytes: volume_size_bytes,
                maximum: MAX_FAT32_VOLUME_BYTES,
            });
        }
        let regions = build_region_map(total_sectors, fat_size_sectors)?;
        let geometry = Self {
            total_sectors,
            sectors_per_cluster,
            fat_size_sectors,
            data_cluster_count,
            regions,
        };
        <Self as Geometry>::validate_region_map(&geometry.regions, volume_size_bytes)
            .map_err(|err| panic_invalid_region_map(volume_size_bytes, &err))?;
        Ok(geometry)
    }

    /// Logical sectors per data cluster.
    #[must_use]
    pub fn sectors_per_cluster(&self) -> u32 {
        self.sectors_per_cluster
    }

    /// Sector count occupied by **one** FAT table copy.
    ///
    /// The on-disk layout has [`NUM_FATS`] copies of this size.
    #[must_use]
    pub fn fat_size_sectors(&self) -> u32 {
        self.fat_size_sectors
    }

    /// First sector of the data region.
    ///
    /// Equal to `RESERVED_SECTORS + NUM_FATS × fat_size_sectors`.
    #[must_use]
    pub fn first_data_sector(&self) -> u64 {
        u64::from(RESERVED_SECTORS) + u64::from(NUM_FATS) * u64::from(self.fat_size_sectors)
    }
}

impl Geometry for Fat32Geometry {
    fn total_sectors(&self) -> u64 {
        self.total_sectors
    }
    fn bytes_per_cluster(&self) -> u32 {
        self.sectors_per_cluster * SECTOR_SIZE_BYTES
    }
    fn data_cluster_count(&self) -> u32 {
        self.data_cluster_count
    }
    fn regions(&self) -> &[Region] {
        &self.regions
    }
}

/// 32 MiB — the published minimum for a valid FAT32 volume.
///
/// Volumes smaller than this cannot satisfy the
/// [`MIN_FAT32_DATA_CLUSTERS`] floor even at the smallest cluster
/// size, so the cluster-size table starts at 32 MiB.
pub const MIN_VOLUME_SIZE_BYTES: u64 = 32 * 1024 * 1024;

fn sectors_per_cluster_for(volume_size_bytes: u64) -> Option<u32> {
    // Table boundaries in bytes — half-open ranges `[min, max)`.
    // Microsoft KB140365 / fatgen103 §3.5.
    const MIB: u64 = 1024 * 1024;
    const GIB: u64 = 1024 * MIB;
    const TABLE: &[(u64, u32)] = &[
        (32 * MIB, 1),  //  32 MiB .. 64 MiB  -> 512 B   (1 sector)
        (64 * MIB, 2),  //  64 MiB .. 128 MiB -> 1 KiB   (2 sectors)
        (128 * MIB, 4), // 128 MiB .. 256 MiB -> 2 KiB   (4 sectors)
        (256 * MIB, 8), // 256 MiB .. 8 GiB   -> 4 KiB   (8 sectors)
        (8 * GIB, 16),  //   8 GiB .. 16 GiB  -> 8 KiB   (16 sectors)
        (16 * GIB, 32), //  16 GiB .. 32 GiB  -> 16 KiB  (32 sectors)
        (32 * GIB, 64), //  32 GiB .. u32::MAX  -> 32 KiB  (64 sectors)
    ];
    if volume_size_bytes < MIN_VOLUME_SIZE_BYTES {
        return None;
    }
    let mut chosen: u32 = 0;
    for &(threshold, spc) in TABLE {
        if volume_size_bytes >= threshold {
            chosen = spc;
        }
    }
    if chosen == 0 { None } else { Some(chosen) }
}

/// fatgen103 §3 closed-form FAT-size formula.
///
/// ```text
/// TmpVal1 = DiskSize - (ReservedSectorCount + RootDirSectors)
///                                              ^^^^^^^^^^^^^^^^ 0 for FAT32
/// TmpVal2 = (256 × SectorsPerCluster + NumFATs) / 2
/// FATSize = ceil(TmpVal1 / TmpVal2)
/// ```
///
/// The result is conservative — it rounds up so the actual data
/// region is always at least one full cluster smaller than the
/// theoretical maximum. This matches `mkfs.vfat`.
fn fat_size_sectors(total_sectors: u64, sectors_per_cluster: u32) -> u32 {
    let tmp1 = total_sectors.saturating_sub(u64::from(RESERVED_SECTORS));
    let tmp2 = (256 * u64::from(sectors_per_cluster) + u64::from(NUM_FATS)) / 2;
    let raw = tmp1.div_ceil(tmp2);
    // Saturating cast: any volume large enough for `raw` to exceed
    // u32::MAX would already have been rejected by the FAT32 size
    // limit, but the cast is defensive.
    u32::try_from(raw).unwrap_or(u32::MAX)
}

fn build_region_map(
    total_sectors: u64,
    fat_size_sectors: u32,
) -> Result<Vec<Region>, GeometryError> {
    let sector = u64::from(SECTOR_SIZE_BYTES);
    // FsInfo / backup boot sector layout: boot=0, fsinfo=1,
    // reserved=2..6, backup=6, reserved=7..32.
    let reserved_end_sector = u64::from(RESERVED_SECTORS);
    let mut regions: Vec<Region> = Vec::with_capacity(8);
    regions.push(Region {
        start: 0,
        len: sector,
        kind: RegionKind::Fat32BootSector,
    });
    regions.push(Region {
        start: u64::from(FSINFO_SECTOR_INDEX) * sector,
        len: sector,
        kind: RegionKind::Fat32FsInfo,
    });
    // Reserved gap between FsInfo and the backup boot sector.
    let gap1_start = (u64::from(FSINFO_SECTOR_INDEX) + 1) * sector;
    let gap1_end = u64::from(BACKUP_BOOT_SECTOR_INDEX) * sector;
    if gap1_end > gap1_start {
        regions.push(Region {
            start: gap1_start,
            len: gap1_end - gap1_start,
            kind: RegionKind::Reserved,
        });
    }
    regions.push(Region {
        start: u64::from(BACKUP_BOOT_SECTOR_INDEX) * sector,
        len: sector,
        kind: RegionKind::Fat32BackupBootSector,
    });
    let gap2_start = (u64::from(BACKUP_BOOT_SECTOR_INDEX) + 1) * sector;
    let gap2_end = reserved_end_sector * sector;
    if gap2_end > gap2_start {
        regions.push(Region {
            start: gap2_start,
            len: gap2_end - gap2_start,
            kind: RegionKind::Reserved,
        });
    }
    // FAT 1
    let fat_bytes = u64::from(fat_size_sectors) * sector;
    let fat1_start = reserved_end_sector * sector;
    regions.push(Region {
        start: fat1_start,
        len: fat_bytes,
        kind: RegionKind::FatTable { index: 0 },
    });
    // FAT 2
    let fat2_start = fat1_start + fat_bytes;
    regions.push(Region {
        start: fat2_start,
        len: fat_bytes,
        kind: RegionKind::FatTable { index: 1 },
    });
    // Data region — through end of volume.
    let data_start = fat2_start + fat_bytes;
    let total_bytes = total_sectors * sector;
    if data_start > total_bytes {
        return Err(GeometryError::VolumeTooSmall {
            bytes: total_bytes,
            minimum: MIN_VOLUME_SIZE_BYTES,
        });
    }
    let data_len = total_bytes - data_start;
    if data_len == 0 {
        return Err(GeometryError::VolumeTooSmall {
            bytes: total_bytes,
            minimum: MIN_VOLUME_SIZE_BYTES,
        });
    }
    regions.push(Region {
        start: data_start,
        len: data_len,
        kind: RegionKind::Data,
    });
    Ok(regions)
}

/// Called from [`Fat32Geometry::for_volume_size`] when the region
/// map validator rejects a freshly-constructed map.
///
/// This is a bug in the constructor (not a user-supplied bad input)
/// because all user-supplied bad inputs are filtered earlier with
/// [`GeometryError`]. Panicking here surfaces the bug in tests
/// without lint suppression — the workspace forbids `unwrap` and
/// `panic!` is allowed (`panic = "warn"`, not `"deny"`); the
/// `#[allow]` below documents the deliberate use.
#[allow(clippy::panic)]
fn panic_invalid_region_map(
    volume_size_bytes: u64,
    err: &crate::fs::geometry::RegionMapError,
) -> GeometryError {
    panic!(
        "Fat32Geometry::for_volume_size({volume_size_bytes}) produced an invalid region map: \
         {err}; this is a bug in fs::fat32::geometry, not a user-supplied bad input"
    );
}

#[cfg(test)]
#[allow(
    clippy::cognitive_complexity,
    clippy::expect_used,
    clippy::panic,
    clippy::unwrap_used
)]
mod tests {
    use super::{
        BACKUP_BOOT_SECTOR_INDEX, FSINFO_SECTOR_INDEX, Fat32Geometry, GeometryError,
        MAX_FAT32_DATA_CLUSTERS, MAX_FAT32_VOLUME_BYTES, MIN_FAT32_DATA_CLUSTERS,
        MIN_VOLUME_SIZE_BYTES, NUM_FATS, RESERVED_SECTORS, fat_size_sectors,
        sectors_per_cluster_for,
    };
    use crate::fs::geometry::{Geometry, RegionKind, SECTOR_SIZE_BYTES};

    const MIB: u64 = 1024 * 1024;
    const GIB: u64 = 1024 * MIB;

    // --- Cluster-size table ---------------------------------------------

    #[test]
    fn cluster_size_for_smallest_legal_volume_is_one_sector() {
        assert_eq!(sectors_per_cluster_for(32 * MIB), Some(1));
        assert_eq!(sectors_per_cluster_for(63 * MIB), Some(1));
    }

    #[test]
    fn cluster_size_table_picks_2_sectors_in_64mib_band() {
        assert_eq!(sectors_per_cluster_for(64 * MIB), Some(2));
        assert_eq!(sectors_per_cluster_for(127 * MIB), Some(2));
    }

    #[test]
    fn cluster_size_table_picks_4_sectors_in_128mib_band() {
        assert_eq!(sectors_per_cluster_for(128 * MIB), Some(4));
        assert_eq!(sectors_per_cluster_for(255 * MIB), Some(4));
    }

    #[test]
    fn cluster_size_table_picks_8_sectors_for_4gib() {
        // H1's default volume size.
        assert_eq!(sectors_per_cluster_for(4 * GIB), Some(8));
        assert_eq!(sectors_per_cluster_for(256 * MIB), Some(8));
        // 7.99 GiB still in the 4 KiB band.
        assert_eq!(sectors_per_cluster_for(8 * GIB - 1024), Some(8));
    }

    #[test]
    fn cluster_size_table_picks_16_sectors_for_8gib_band() {
        assert_eq!(sectors_per_cluster_for(8 * GIB), Some(16));
        assert_eq!(sectors_per_cluster_for(15 * GIB), Some(16));
    }

    #[test]
    fn cluster_size_table_picks_32_sectors_for_16gib_band() {
        assert_eq!(sectors_per_cluster_for(16 * GIB), Some(32));
        assert_eq!(sectors_per_cluster_for(31 * GIB), Some(32));
    }

    #[test]
    fn cluster_size_table_picks_64_sectors_for_32gib_and_up() {
        assert_eq!(sectors_per_cluster_for(32 * GIB), Some(64));
        assert_eq!(sectors_per_cluster_for(64 * GIB), Some(64));
        assert_eq!(sectors_per_cluster_for(MAX_FAT32_VOLUME_BYTES), Some(64));
    }

    #[test]
    fn cluster_size_table_rejects_below_32mib() {
        assert_eq!(sectors_per_cluster_for(0), None);
        assert_eq!(sectors_per_cluster_for(32 * MIB - 1), None);
    }

    // --- fatgen103 closed-form FAT size --------------------------------

    /// Hand-computation for 32 MiB:
    ///
    /// ```text
    /// total_sectors = 32*1024*1024 / 512 = 65536
    /// spc = 1
    /// tmp1 = 65536 - 32 = 65504
    /// tmp2 = (256*1 + 2) / 2 = 129
    /// fat_size = ceil(65504 / 129) = 508
    /// ```
    #[test]
    fn fat_size_matches_fatgen103_formula_at_32mib() {
        assert_eq!(fat_size_sectors(65536, 1), 508);
    }

    /// Hand-computation for 4 GiB:
    ///
    /// ```text
    /// total_sectors = 4 * 1024 * 1024 * 1024 / 512 = 8_388_608
    /// spc = 8
    /// tmp1 = 8_388_608 - 32 = 8_388_576
    /// tmp2 = (256*8 + 2) / 2 = 1025
    /// fat_size = ceil(8_388_576 / 1025) = 8184
    /// ```
    #[test]
    fn fat_size_matches_fatgen103_formula_at_4gib() {
        assert_eq!(fat_size_sectors(8_388_608, 8), 8184);
    }

    /// Hand-computation for 32 GiB:
    ///
    /// ```text
    /// total_sectors = 32 * 1024 * 1024 * 1024 / 512 = 67_108_864
    /// spc = 64
    /// tmp1 = 67_108_864 - 32 = 67_108_832
    /// tmp2 = (256*64 + 2) / 2 = 8193
    /// fat_size = ceil(67_108_832 / 8193) = 8191
    /// ```
    #[test]
    fn fat_size_matches_fatgen103_formula_at_32gib() {
        assert_eq!(fat_size_sectors(67_108_864, 64), 8191);
    }

    // --- Fat32Geometry::for_volume_size — known sizes ------------------

    #[test]
    fn geometry_rejects_volume_too_small_for_fat32_minimum_clusters() {
        // 32 MiB is below the cluster floor — verified by computation:
        // total = 32*1024*1024/512 = 65536 sectors
        // fat   = ceil((65536-32) / ((256+2)/2)) = ceil(65504/129) = 508
        // data  = (65536 - 32 - 2*508) / 1 = 64488 clusters
        // 64488 < 65525 (MIN_FAT32_DATA_CLUSTERS), so mkfs.vfat would
        // silently make this FAT16. B-1 wants a hard error.
        let err = Fat32Geometry::for_volume_size(32 * MIB).expect_err(
            "32 MiB FAT32 yields fewer than MIN_FAT32_DATA_CLUSTERS so must be rejected",
        );
        assert!(
            matches!(err, GeometryError::VolumeTooSmall { .. }),
            "expected VolumeTooSmall, got {err:?}"
        );
    }

    /// Smallest size that actually yields >= 65525 data clusters at
    /// 512 B/cluster: ceil((65525 + 32 + 2*FAT) * 512). With spc=1
    /// and a self-consistent FAT size of ~514, that's ~33,857,536 B.
    /// Practical: 34 MiB rounds up nicely and is in the spc=1 band.
    #[test]
    fn geometry_for_34mib_is_valid_smallest_practical_fat32() {
        let geo = Fat32Geometry::for_volume_size(34 * MIB)
            .expect("34 MiB should be above the FAT32 cluster floor");
        assert_eq!(geo.sectors_per_cluster(), 1);
        assert!(
            geo.data_cluster_count() >= MIN_FAT32_DATA_CLUSTERS,
            "data_cluster_count={} must be >= {}",
            geo.data_cluster_count(),
            MIN_FAT32_DATA_CLUSTERS
        );
        assert!(
            geo.data_cluster_count() <= MAX_FAT32_DATA_CLUSTERS,
            "data_cluster_count={} must be <= {}",
            geo.data_cluster_count(),
            MAX_FAT32_DATA_CLUSTERS
        );
    }

    #[test]
    fn geometry_for_4gib_matches_h1_expectations() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("4 GiB is a valid FAT32 size");
        assert_eq!(geo.total_sectors(), 8_388_608);
        assert_eq!(geo.sectors_per_cluster(), 8);
        assert_eq!(geo.bytes_per_cluster(), 4096);
        assert_eq!(geo.fat_size_sectors(), 8184);
        // (8_388_608 - 32 - 2*8184) / 8 = 1046784 / 8 = 130,848
        // Actually: 8388608 - 32 - 16368 = 8372208; / 8 = 1046526
        // Both bounds satisfied.
        assert!(
            geo.data_cluster_count() >= MIN_FAT32_DATA_CLUSTERS
                && geo.data_cluster_count() <= MAX_FAT32_DATA_CLUSTERS
        );
    }

    #[test]
    fn geometry_for_32gib_matches_microsoft_expectations() {
        let geo = Fat32Geometry::for_volume_size(32 * GIB).expect("32 GiB is a valid FAT32 size");
        assert_eq!(geo.total_sectors(), 67_108_864);
        assert_eq!(geo.sectors_per_cluster(), 64);
        assert_eq!(geo.bytes_per_cluster(), 32 * 1024);
        assert_eq!(geo.fat_size_sectors(), 8191);
        assert!(
            geo.data_cluster_count() >= MIN_FAT32_DATA_CLUSTERS
                && geo.data_cluster_count() <= MAX_FAT32_DATA_CLUSTERS
        );
    }

    // --- error paths ---------------------------------------------------

    #[test]
    fn geometry_rejects_unaligned_volume_size() {
        // 4 GiB minus a single byte — not a sector boundary.
        let err = Fat32Geometry::for_volume_size(4 * GIB - 1)
            .expect_err("non-512-aligned volume size must be rejected");
        assert!(matches!(err, GeometryError::UnalignedVolumeSize { .. }));
    }

    #[test]
    fn geometry_rejects_volume_below_32mib() {
        let err = Fat32Geometry::for_volume_size(MIN_VOLUME_SIZE_BYTES - 512)
            .expect_err("< 32 MiB must be rejected");
        match err {
            GeometryError::VolumeTooSmall { bytes, minimum } => {
                assert_eq!(bytes, MIN_VOLUME_SIZE_BYTES - 512);
                assert_eq!(minimum, MIN_VOLUME_SIZE_BYTES);
            }
            other => panic!("expected VolumeTooSmall, got {other:?}"),
        }
    }

    #[test]
    fn geometry_at_max_u32_sectors_is_accepted() {
        // The published ceiling is u32::MAX sectors (2 TiB - 512 B,
        // = the largest volume the on-disk BPB_TotSec32 field can
        // describe). Must accept.
        let geo = Fat32Geometry::for_volume_size(MAX_FAT32_VOLUME_BYTES)
            .expect("u32::MAX sectors must be accepted");
        assert_eq!(geo.sectors_per_cluster(), 64);
        assert!(geo.data_cluster_count() <= MAX_FAT32_DATA_CLUSTERS);
        assert_eq!(geo.total_sectors(), u64::from(u32::MAX));
    }

    #[test]
    fn geometry_rejects_total_sectors_above_u32_max() {
        // One sector past u32::MAX cannot be represented in
        // BPB_TotSec32 — must reject.
        let err = Fat32Geometry::for_volume_size(MAX_FAT32_VOLUME_BYTES + 512)
            .expect_err("> u32::MAX sectors must be rejected");
        assert!(matches!(err, GeometryError::VolumeTooLarge { .. }));
    }

    // --- Region map invariants -----------------------------------------

    #[test]
    fn region_map_has_expected_kinds_in_order_for_4gib() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        let kinds: Vec<RegionKind> = geo.regions().iter().map(|r| r.kind).collect();
        assert_eq!(
            kinds,
            vec![
                RegionKind::Fat32BootSector,
                RegionKind::Fat32FsInfo,
                RegionKind::Reserved, // sectors 2..6
                RegionKind::Fat32BackupBootSector,
                RegionKind::Reserved, // sectors 7..32
                RegionKind::FatTable { index: 0 },
                RegionKind::FatTable { index: 1 },
                RegionKind::Data,
            ]
        );
    }

    #[test]
    fn region_map_is_contiguous_and_covers_full_volume_for_known_sizes() {
        for size in [34 * MIB, 4 * GIB, 32 * GIB] {
            let geo = Fat32Geometry::for_volume_size(size).expect("valid");
            let regions = geo.regions();
            let first = regions.first().expect("non-empty region map");
            assert_eq!(first.start, 0);
            let mut cursor = 0_u64;
            for r in regions {
                assert_ne!(r.len, 0, "no zero-length regions");
                assert_eq!(r.start, cursor, "no gaps");
                cursor = r.start + r.len;
            }
            assert_eq!(
                cursor, size,
                "region map for {size} bytes covers only {cursor} bytes"
            );
        }
    }

    #[test]
    fn region_map_places_boot_sector_at_offset_0() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        let boot = geo.regions().first().expect("non-empty");
        assert_eq!(boot.start, 0);
        assert_eq!(boot.len, u64::from(SECTOR_SIZE_BYTES));
        assert_eq!(boot.kind, RegionKind::Fat32BootSector);
    }

    #[test]
    fn region_map_places_fsinfo_at_sector_1() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        let fsinfo_offset = u64::from(FSINFO_SECTOR_INDEX) * u64::from(SECTOR_SIZE_BYTES);
        let r = geo.region_at(fsinfo_offset).expect("offset in map");
        assert_eq!(r.kind, RegionKind::Fat32FsInfo);
        assert_eq!(r.start, fsinfo_offset);
        assert_eq!(r.len, u64::from(SECTOR_SIZE_BYTES));
    }

    #[test]
    fn region_map_places_backup_boot_sector_at_sector_6() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        let backup_offset = u64::from(BACKUP_BOOT_SECTOR_INDEX) * u64::from(SECTOR_SIZE_BYTES);
        let r = geo.region_at(backup_offset).expect("offset in map");
        assert_eq!(r.kind, RegionKind::Fat32BackupBootSector);
        assert_eq!(r.start, backup_offset);
        assert_eq!(r.len, u64::from(SECTOR_SIZE_BYTES));
    }

    #[test]
    fn region_map_places_fat1_at_first_post_reserved_sector() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        let fat1_offset = u64::from(RESERVED_SECTORS) * u64::from(SECTOR_SIZE_BYTES);
        let r = geo.region_at(fat1_offset).expect("offset in map");
        assert_eq!(r.kind, RegionKind::FatTable { index: 0 });
        assert_eq!(r.start, fat1_offset);
        assert_eq!(
            r.len,
            u64::from(geo.fat_size_sectors()) * u64::from(SECTOR_SIZE_BYTES)
        );
    }

    #[test]
    fn region_map_places_fat2_immediately_after_fat1() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        let fat1_len = u64::from(geo.fat_size_sectors()) * u64::from(SECTOR_SIZE_BYTES);
        let fat2_offset = u64::from(RESERVED_SECTORS) * u64::from(SECTOR_SIZE_BYTES) + fat1_len;
        let r = geo.region_at(fat2_offset).expect("offset in map");
        assert_eq!(r.kind, RegionKind::FatTable { index: 1 });
        assert_eq!(r.start, fat2_offset);
        assert_eq!(r.len, fat1_len);
    }

    #[test]
    fn region_map_data_region_starts_after_both_fats() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        let expected_data_start = u64::from(RESERVED_SECTORS) * u64::from(SECTOR_SIZE_BYTES)
            + 2 * u64::from(geo.fat_size_sectors()) * u64::from(SECTOR_SIZE_BYTES);
        let r = geo.region_at(expected_data_start).expect("offset in map");
        assert_eq!(r.kind, RegionKind::Data);
        assert_eq!(r.start, expected_data_start);
        assert_eq!(
            r.start,
            geo.first_data_sector() * u64::from(SECTOR_SIZE_BYTES)
        );
    }

    #[test]
    fn region_at_returns_none_past_end_for_known_sizes() {
        for size in [34 * MIB, 4 * GIB, 32 * GIB] {
            let geo = Fat32Geometry::for_volume_size(size).expect("valid");
            assert_eq!(geo.region_at(size), None);
            assert_eq!(geo.region_at(size + 1), None);
            assert_eq!(geo.region_at(u64::MAX), None);
        }
    }

    // --- fixed constants ----------------------------------------------

    #[test]
    fn fixed_constants_match_microsoft_defaults() {
        assert_eq!(RESERVED_SECTORS, 32);
        assert_eq!(NUM_FATS, 2);
        assert_eq!(FSINFO_SECTOR_INDEX, 1);
        assert_eq!(BACKUP_BOOT_SECTOR_INDEX, 6);
        assert_eq!(MIN_FAT32_DATA_CLUSTERS, 65_525);
        assert_eq!(MAX_FAT32_DATA_CLUSTERS, 0x0FFF_FFF4);
    }

    // --- exhaustive sweep ---------------------------------------------

    /// Sweep across a wide range of volume sizes; every accepted
    /// size must have a valid region map (contiguous, full coverage,
    /// `region_at(0)` → boot sector, `region_at(end-1)` → data).
    #[test]
    fn region_map_invariant_holds_across_volume_size_sweep() {
        // Step in 17 MiB chunks so we don't sample only the
        // power-of-two boundaries.
        let mut size = 34 * MIB;
        let cap = 64 * GIB;
        while size <= cap {
            let geo = Fat32Geometry::for_volume_size(size)
                .unwrap_or_else(|err| panic!("size {size} rejected: {err}"));
            let regions = geo.regions();
            // Contiguity + total coverage.
            let mut cursor = 0_u64;
            for r in regions {
                assert_eq!(r.start, cursor, "gap at size={size}");
                cursor = r
                    .start
                    .checked_add(r.len)
                    .unwrap_or_else(|| panic!("overflow at size={size}"));
            }
            assert_eq!(cursor, size, "coverage mismatch at size={size}");
            // Endpoint sanity.
            assert_eq!(
                geo.region_at(0).map(|r| r.kind),
                Some(RegionKind::Fat32BootSector)
            );
            let last_data_byte = size - 1;
            assert_eq!(
                geo.region_at(last_data_byte).map(|r| r.kind),
                Some(RegionKind::Data),
                "size={size} last byte should land in Data"
            );
            // Cluster bounds.
            assert!(
                geo.data_cluster_count() >= MIN_FAT32_DATA_CLUSTERS,
                "size={size} cluster count too low"
            );
            assert!(
                geo.data_cluster_count() <= MAX_FAT32_DATA_CLUSTERS,
                "size={size} cluster count too high"
            );
            size += 17 * MIB;
        }
    }
}
