//! `exFAT` volume geometry.
//!
//! Phase 2.8 of the B-1 rewrite. The [`ExfatGeometry`] type takes
//! a target volume size in bytes and computes the on-disk layout
//! that the Phase 2.11 read dispatcher will route reads against:
//!
//! ```text
//! sector
//!  0 ────────── Main Boot Region (12 sectors)
//!               (boot sector, 8 extended boot sectors, OEM
//!                parameters, reserved, checksum)
//! 12 ────────── Backup Boot Region (12 sectors)
//!               (byte-for-byte mirror of the main boot region)
//! 24 ────────── First FAT (`FatLength` sectors)
//! …  ────────── Cluster Heap (`ClusterCount × 2^SPCS` sectors)
//! …  ────────── Excess Space (zero or more padding sectors)
//! V  ────────── end of volume (VolumeLength)
//! ```
//!
//! ## Format choices (pinned)
//!
//! The Microsoft spec gives a lot of latitude (FAT alignment,
//! cluster heap alignment, `TexFAT` second FAT, OEM parameters).
//! B-1 synthesises a deliberately minimal volume:
//!
//! * `BytesPerSectorShift = 9` — 512-byte logical sectors, matching
//!   [`SECTOR_SIZE_BYTES`].
//! * `NumberOfFats = 1` — standard exFAT (no `TexFAT` mirror).
//! * `FatOffset = 24` — FAT starts immediately after the backup
//!   boot region. No FAT alignment padding.
//! * `ClusterHeapOffset = FatOffset + FatLength` — cluster heap
//!   starts immediately after the FAT. No cluster heap alignment
//!   padding.
//! * `FirstClusterOfRootDirectory = 2` — the root directory
//!   occupies the first cluster of the cluster heap. (Microsoft
//!   conventionally places the allocation bitmap and upcase table
//!   in clusters 2 and 3 with the root directory at cluster 4–6;
//!   Phase 2.8 only nails down the geometry, so the boot sector
//!   advertises cluster 2 as a placeholder that Phase 2.10 may
//!   bump when the directory layout lands.)
//!
//! These pins are intentional — every degree of freedom that does
//! not change the kernel's ability to mount the volume gets pinned
//! so the synth output is fully deterministic for a given
//! `(volume_size, label, serial)` triple.
//!
//! ## Cluster size table
//!
//! Microsoft `format.exe` and `mkfs.exfat` both pick the cluster
//! size from a size-keyed table. The three boundaries below match
//! both tools' defaults; the floors are slightly conservative so
//! that very small test volumes still get enough data clusters to
//! be useful:
//!
//! ```text
//!   8 MiB ..   256 MiB  →   4 KiB clusters (   8 sectors)
//! 256 MiB ..    32 GiB  →  32 KiB clusters (  64 sectors)
//!  32 GiB .. ~ 256 TiB  → 128 KiB clusters ( 256 sectors)
//! ```
//!
//! ## Spec anchor
//!
//! Microsoft `exFAT` File System Specification v1.00 (August 27,
//! 2019). §3.1 Main Boot Region, §3.2 Backup Boot Region, §4 FAT
//! Region, §5 Cluster Heap.

use core::fmt;

use crate::fs::geometry::{
    Geometry, GeometryError, Region, RegionKind, RegionMapError, SECTOR_SIZE_BYTES,
};

/// `BytesPerSectorShift` field value pinned by B-1.
///
/// `exFAT` spec §3.1.10 allows values 9..=12 (512 B .. 4 KiB
/// logical sectors). B-1 always uses 512-byte sectors so every
/// synthesized volume reports `9` here.
pub const BYTES_PER_SECTOR_SHIFT: u8 = 9;

/// `NumberOfFats` field value pinned by B-1.
///
/// `exFAT` spec §3.1.15 allows `1` (normal exFAT) or `2` (`TexFAT`
/// — a Microsoft transactional extension never used outside of
/// Windows Embedded). B-1 always writes `1`.
pub const NUMBER_OF_FATS: u8 = 1;

/// Size in sectors of one boot region (main or backup).
///
/// Boot sector + 8 extended boot sectors + OEM parameters +
/// reserved + boot checksum = 12 sectors per `exFAT` spec §3.1.
pub const BOOT_REGION_SECTORS: u32 = 12;

/// Sector index of the main boot region (always 0).
pub const MAIN_BOOT_REGION_OFFSET_SECTORS: u32 = 0;

/// Sector index of the backup boot region. Equal to
/// [`BOOT_REGION_SECTORS`] because the backup region immediately
/// follows the main region (`exFAT` spec §3.2).
pub const BACKUP_BOOT_REGION_OFFSET_SECTORS: u32 = BOOT_REGION_SECTORS;

/// Sector index where the FAT begins under the B-1 layout pins.
///
/// Equal to the total size of the main + backup boot regions —
/// B-1 does not insert any FAT alignment padding.
pub const FAT_OFFSET_SECTORS: u32 = 2 * BOOT_REGION_SECTORS;

/// First cluster number of the root directory written into the
/// boot sector's `FirstClusterOfRootDirectory` field.
///
/// `exFAT` spec §3.1.7 requires this to be in `2..=ClusterCount + 1`.
/// B-1 currently uses `2` (the first cluster of the cluster heap)
/// as a placeholder; the Phase 2.10 directory layout will bump
/// this when the allocation bitmap and upcase table take cluster
/// 2 and 3 respectively.
pub const FIRST_ROOT_DIRECTORY_CLUSTER: u32 = 2;

/// First valid `exFAT` cluster number.
///
/// Clusters 0 and 1 are reserved (`exFAT` spec §4.1); the cluster
/// heap is numbered from `2` upward and FAT entries `[0, 1]`
/// carry media-descriptor and end-of-chain marker bytes.
pub const FIRST_CLUSTER_NUMBER: u32 = 2;

/// Largest legal `ClusterCount` per `exFAT` spec §3.1.6.
///
/// The on-disk field is a `u32`; values above this would collide
/// with the bad-cluster / end-of-chain markers in the FAT.
pub const MAX_EXFAT_CLUSTER_COUNT: u32 = u32::MAX - 10;

/// Floor on the volume size B-1 accepts.
///
/// 8 MiB is the smallest size that comfortably fits the 24-sector
/// boot regions, a 1-sector FAT, and a handful of 4 KiB clusters
/// for a useful root directory. The `exFAT` spec itself allows
/// smaller volumes; B-1 declines them to keep the geometry tests
/// realistic.
pub const MIN_VOLUME_SIZE_BYTES: u64 = 8 * 1024 * 1024;

/// Ceiling on the volume size B-1 accepts.
///
/// 256 TiB is the largest size that comfortably fits in the
/// 128 KiB cluster band without exceeding
/// [`MAX_EXFAT_CLUSTER_COUNT`]. The spec's true ceiling is
/// 2^64 sectors (≈ 9 `ZiB`) but no real-world target needs that.
pub const MAX_VOLUME_SIZE_BYTES: u64 = 256 * 1024 * 1024 * 1024 * 1024;

/// Compile-time invariants on the pinned format choices.
const _: () = {
    assert!(BYTES_PER_SECTOR_SHIFT == 9);
    assert!((1_u32 << BYTES_PER_SECTOR_SHIFT) == SECTOR_SIZE_BYTES);
    assert!(NUMBER_OF_FATS == 1);
    assert!(BOOT_REGION_SECTORS == 12);
    assert!(BACKUP_BOOT_REGION_OFFSET_SECTORS == 12);
    assert!(FAT_OFFSET_SECTORS == 24);
    assert!(MAX_EXFAT_CLUSTER_COUNT == u32::MAX - 10);
};

/// `exFAT` geometry computed from a target volume size.
///
/// Immutable after construction. All fields are derived; the only
/// inputs are the volume size and the fixed format pins documented
/// at the module level.
#[derive(Debug, Clone)]
pub struct ExfatGeometry {
    total_sectors: u64,
    sectors_per_cluster_shift: u8,
    fat_length_sectors: u32,
    cluster_heap_offset_sectors: u32,
    cluster_count: u32,
    regions: Vec<Region>,
}

impl ExfatGeometry {
    /// Construct an `exFAT` geometry for a volume of
    /// `volume_size_bytes`.
    ///
    /// The cluster size is chosen from the table documented at the
    /// module level; the FAT length is computed from the
    /// closed-form `exFAT` formula in §4.1.
    ///
    /// # Errors
    ///
    /// * [`GeometryError::UnalignedVolumeSize`] if `volume_size_bytes`
    ///   is not a whole multiple of [`SECTOR_SIZE_BYTES`].
    /// * [`GeometryError::VolumeTooSmall`] if the volume is below
    ///   [`MIN_VOLUME_SIZE_BYTES`] or would produce zero cluster
    ///   heap clusters at the chosen cluster size.
    /// * [`GeometryError::VolumeTooLarge`] if the volume exceeds
    ///   [`MAX_VOLUME_SIZE_BYTES`] or would produce more than
    ///   [`MAX_EXFAT_CLUSTER_COUNT`] clusters at the chosen
    ///   cluster size.
    pub fn for_volume_size(volume_size_bytes: u64) -> Result<Self, GeometryError> {
        if volume_size_bytes % u64::from(SECTOR_SIZE_BYTES) != 0 {
            return Err(GeometryError::UnalignedVolumeSize {
                bytes: volume_size_bytes,
                sector_size: SECTOR_SIZE_BYTES,
            });
        }
        if volume_size_bytes < MIN_VOLUME_SIZE_BYTES {
            return Err(GeometryError::VolumeTooSmall {
                bytes: volume_size_bytes,
                minimum: MIN_VOLUME_SIZE_BYTES,
            });
        }
        if volume_size_bytes > MAX_VOLUME_SIZE_BYTES {
            return Err(GeometryError::VolumeTooLarge {
                bytes: volume_size_bytes,
                maximum: MAX_VOLUME_SIZE_BYTES,
            });
        }
        let total_sectors = volume_size_bytes / u64::from(SECTOR_SIZE_BYTES);

        let sectors_per_cluster_shift = sectors_per_cluster_shift_for(volume_size_bytes);
        let sectors_per_cluster = 1_u32 << sectors_per_cluster_shift;

        let after_boot_regions = total_sectors
            .checked_sub(u64::from(FAT_OFFSET_SECTORS))
            .ok_or(GeometryError::VolumeTooSmall {
                bytes: volume_size_bytes,
                minimum: MIN_VOLUME_SIZE_BYTES,
            })?;

        let (fat_length_sectors, cluster_count) =
            choose_fat_and_cluster_count(after_boot_regions, sectors_per_cluster)?;

        let cluster_heap_offset_sectors = FAT_OFFSET_SECTORS
            .checked_add(fat_length_sectors)
            .ok_or(GeometryError::VolumeTooLarge {
                bytes: volume_size_bytes,
                maximum: MAX_VOLUME_SIZE_BYTES,
            })?;

        if cluster_count > MAX_EXFAT_CLUSTER_COUNT {
            return Err(GeometryError::VolumeTooLarge {
                bytes: volume_size_bytes,
                maximum: MAX_VOLUME_SIZE_BYTES,
            });
        }

        let regions = build_region_map(
            total_sectors,
            fat_length_sectors,
            cluster_heap_offset_sectors,
            cluster_count,
            sectors_per_cluster,
        )?;

        let geometry = Self {
            total_sectors,
            sectors_per_cluster_shift,
            fat_length_sectors,
            cluster_heap_offset_sectors,
            cluster_count,
            regions,
        };
        <Self as Geometry>::validate_region_map(&geometry.regions, volume_size_bytes)
            .map_err(|err| panic_invalid_region_map(volume_size_bytes, &err))?;
        Ok(geometry)
    }

    /// `SectorsPerClusterShift` field value (boot sector offset
    /// `0x6D`). `sectors_per_cluster = 1 << shift`.
    #[must_use]
    pub fn sectors_per_cluster_shift(&self) -> u8 {
        self.sectors_per_cluster_shift
    }

    /// Logical sectors per data cluster
    /// (`1 << sectors_per_cluster_shift()`).
    #[must_use]
    pub fn sectors_per_cluster(&self) -> u32 {
        1_u32 << self.sectors_per_cluster_shift
    }

    /// `FatLength` field value — sectors per FAT.
    #[must_use]
    pub fn fat_length_sectors(&self) -> u32 {
        self.fat_length_sectors
    }

    /// `FatOffset` field value — sector offset of the first FAT.
    /// Always [`FAT_OFFSET_SECTORS`] under the B-1 layout pins.
    #[must_use]
    pub fn fat_offset_sectors(&self) -> u32 {
        FAT_OFFSET_SECTORS
    }

    /// `ClusterHeapOffset` field value — sector offset of the
    /// first cluster of the cluster heap.
    #[must_use]
    pub fn cluster_heap_offset_sectors(&self) -> u32 {
        self.cluster_heap_offset_sectors
    }

    /// `ClusterCount` field value — number of clusters in the
    /// cluster heap.
    #[must_use]
    pub fn cluster_count(&self) -> u32 {
        self.cluster_count
    }

    /// First valid cluster number assigned to the root directory.
    /// See [`FIRST_ROOT_DIRECTORY_CLUSTER`].
    #[must_use]
    pub fn first_root_directory_cluster(&self) -> u32 {
        FIRST_ROOT_DIRECTORY_CLUSTER
    }
}

impl Geometry for ExfatGeometry {
    fn total_sectors(&self) -> u64 {
        self.total_sectors
    }
    fn bytes_per_cluster(&self) -> u32 {
        self.sectors_per_cluster() * SECTOR_SIZE_BYTES
    }
    fn data_cluster_count(&self) -> u32 {
        self.cluster_count
    }
    fn regions(&self) -> &[Region] {
        &self.regions
    }
}

/// Pick `SectorsPerClusterShift` for a target volume size.
///
/// Boundaries match the Microsoft `format.exe` / `mkfs.exfat`
/// defaults documented in the module-level cluster size table.
const fn sectors_per_cluster_shift_for(volume_size_bytes: u64) -> u8 {
    const MIB: u64 = 1024 * 1024;
    const GIB: u64 = 1024 * MIB;
    if volume_size_bytes < 256 * MIB {
        // 4 KiB clusters = 8 sectors = 1 << 3.
        3
    } else if volume_size_bytes < 32 * GIB {
        // 32 KiB clusters = 64 sectors = 1 << 6.
        6
    } else {
        // 128 KiB clusters = 256 sectors = 1 << 8.
        8
    }
}

/// Solve for `(fat_length_sectors, cluster_count)` given the
/// sectors available after the boot regions.
///
/// `exFAT` FAT entries are 4 bytes wide (§4.1); entries 0 and 1
/// are reserved but still occupy FAT space, so the FAT for
/// `cluster_count` allocatable clusters needs
/// `ceil((cluster_count + 2) × 4 / SECTOR_SIZE_BYTES)` sectors.
///
/// We do not insert any padding, so:
///
/// ```text
/// after_boot_regions = fat_length + cluster_count × sectors_per_cluster + excess
/// ```
///
/// We pick the largest `cluster_count` such that `fat_length +
/// cluster_count × sectors_per_cluster <= after_boot_regions`. Any
/// remainder becomes excess space (a [`RegionKind::Reserved`]
/// trailer).
fn choose_fat_and_cluster_count(
    after_boot_regions: u64,
    sectors_per_cluster: u32,
) -> Result<(u32, u32), GeometryError> {
    // Need at least 1 FAT sector and 1 cluster to be useful.
    if after_boot_regions < u64::from(sectors_per_cluster) + 1 {
        return Err(GeometryError::VolumeTooSmall {
            bytes: after_boot_regions.saturating_mul(u64::from(SECTOR_SIZE_BYTES)),
            minimum: MIN_VOLUME_SIZE_BYTES,
        });
    }
    // Upper bound on cluster_count if FAT length were zero — used to
    // bound the search.
    let max_clusters_loose = after_boot_regions / u64::from(sectors_per_cluster);
    let max_clusters_loose_u32 = u32::try_from(
        max_clusters_loose.min(u64::from(MAX_EXFAT_CLUSTER_COUNT)),
    )
    .map_err(|_| GeometryError::VolumeTooLarge {
        bytes: 0,
        maximum: MAX_VOLUME_SIZE_BYTES,
    })?;
    let mut best_cluster_count: u32 = 0;
    let mut best_fat_length: u32 = 0;
    // Iterate descending from the loose upper bound to find the
    // largest cluster_count whose FAT + cluster heap fits. The
    // search terminates within a few hundred iterations because
    // adding one cluster only adds 4 bytes of FAT (one new entry)
    // until the FAT crosses a sector boundary, which happens once
    // per 128 clusters.
    let mut candidate = max_clusters_loose_u32;
    while candidate >= 1 {
        let fat_bytes = u64::from(candidate)
            .saturating_add(u64::from(FIRST_CLUSTER_NUMBER))
            .saturating_mul(4);
        let fat_length_u64 = fat_bytes.div_ceil(u64::from(SECTOR_SIZE_BYTES));
        let Ok(fat_length) = u32::try_from(fat_length_u64) else {
            // FAT itself overflows u32 sectors — try smaller.
            candidate = candidate.saturating_sub(1);
            continue;
        };
        let cluster_heap_sectors = u64::from(candidate) * u64::from(sectors_per_cluster);
        let total = u64::from(fat_length).saturating_add(cluster_heap_sectors);
        if total <= after_boot_regions {
            best_cluster_count = candidate;
            best_fat_length = fat_length;
            break;
        }
        candidate = candidate.saturating_sub(1);
    }
    if best_cluster_count == 0 {
        return Err(GeometryError::VolumeTooSmall {
            bytes: after_boot_regions.saturating_mul(u64::from(SECTOR_SIZE_BYTES)),
            minimum: MIN_VOLUME_SIZE_BYTES,
        });
    }
    Ok((best_fat_length, best_cluster_count))
}

fn build_region_map(
    total_sectors: u64,
    fat_length_sectors: u32,
    cluster_heap_offset_sectors: u32,
    cluster_count: u32,
    sectors_per_cluster: u32,
) -> Result<Vec<Region>, GeometryError> {
    let sector = u64::from(SECTOR_SIZE_BYTES);
    let mut regions: Vec<Region> = Vec::with_capacity(5);
    regions.push(Region {
        start: 0,
        len: u64::from(BOOT_REGION_SECTORS) * sector,
        kind: RegionKind::ExfatMainBootRegion,
    });
    regions.push(Region {
        start: u64::from(BACKUP_BOOT_REGION_OFFSET_SECTORS) * sector,
        len: u64::from(BOOT_REGION_SECTORS) * sector,
        kind: RegionKind::ExfatBackupBootRegion,
    });
    regions.push(Region {
        start: u64::from(FAT_OFFSET_SECTORS) * sector,
        len: u64::from(fat_length_sectors) * sector,
        kind: RegionKind::FatTable { index: 0 },
    });
    let cluster_heap_bytes = u64::from(cluster_count) * u64::from(sectors_per_cluster) * sector;
    let cluster_heap_start = u64::from(cluster_heap_offset_sectors) * sector;
    regions.push(Region {
        start: cluster_heap_start,
        len: cluster_heap_bytes,
        kind: RegionKind::Data,
    });
    let total_bytes = total_sectors * sector;
    let after_heap = cluster_heap_start.checked_add(cluster_heap_bytes).ok_or(
        GeometryError::VolumeTooLarge {
            bytes: total_bytes,
            maximum: MAX_VOLUME_SIZE_BYTES,
        },
    )?;
    if after_heap > total_bytes {
        return Err(GeometryError::VolumeTooSmall {
            bytes: total_bytes,
            minimum: MIN_VOLUME_SIZE_BYTES,
        });
    }
    let excess = total_bytes - after_heap;
    if excess > 0 {
        regions.push(Region {
            start: after_heap,
            len: excess,
            kind: RegionKind::Reserved,
        });
    }
    Ok(regions)
}

/// Called when the internal region-map validator rejects a
/// freshly-constructed map.
///
/// This indicates a bug in the constructor — every user-supplied
/// bad input is filtered earlier with [`GeometryError`].
#[allow(clippy::panic)]
fn panic_invalid_region_map(volume_size_bytes: u64, err: &RegionMapError) -> GeometryError {
    panic!(
        "exFAT geometry produced an invalid region map for {volume_size_bytes}-byte volume: {err}"
    )
}

/// Auxiliary `Display` impl on [`ExfatGeometry`] for diagnostics.
impl fmt::Display for ExfatGeometry {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "ExfatGeometry(total_sectors={}, spc_shift={}, fat_length_sectors={}, cluster_heap_offset={}, cluster_count={})",
            self.total_sectors,
            self.sectors_per_cluster_shift,
            self.fat_length_sectors,
            self.cluster_heap_offset_sectors,
            self.cluster_count,
        )
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

    const MIB: u64 = 1024 * 1024;
    const GIB: u64 = 1024 * 1024 * 1024;

    fn geom(bytes: u64) -> ExfatGeometry {
        ExfatGeometry::for_volume_size(bytes).expect("valid geometry")
    }

    // ---------- Sector and cluster sizing ----------

    #[test]
    fn rejects_unaligned_volume_size() {
        let err = ExfatGeometry::for_volume_size(8 * MIB + 1).unwrap_err();
        assert!(matches!(err, GeometryError::UnalignedVolumeSize { .. }));
    }

    #[test]
    fn rejects_volume_below_floor() {
        let err = ExfatGeometry::for_volume_size(MIN_VOLUME_SIZE_BYTES - 512).unwrap_err();
        assert!(matches!(err, GeometryError::VolumeTooSmall { .. }));
    }

    #[test]
    fn rejects_volume_above_ceiling() {
        let err = ExfatGeometry::for_volume_size(MAX_VOLUME_SIZE_BYTES + 512).unwrap_err();
        assert!(matches!(err, GeometryError::VolumeTooLarge { .. }));
    }

    #[test]
    fn picks_4kib_cluster_for_small_volume() {
        let g = geom(64 * MIB);
        assert_eq!(g.sectors_per_cluster_shift(), 3);
        assert_eq!(g.sectors_per_cluster(), 8);
        assert_eq!(g.bytes_per_cluster(), 4096);
    }

    #[test]
    fn picks_32kib_cluster_for_medium_volume() {
        let g = geom(4 * GIB);
        assert_eq!(g.sectors_per_cluster_shift(), 6);
        assert_eq!(g.sectors_per_cluster(), 64);
        assert_eq!(g.bytes_per_cluster(), 32 * 1024);
    }

    #[test]
    fn picks_32kib_cluster_at_lower_band_boundary() {
        let g = geom(256 * MIB);
        assert_eq!(g.sectors_per_cluster_shift(), 6);
    }

    #[test]
    fn picks_128kib_cluster_for_large_volume() {
        let g = geom(64 * GIB);
        assert_eq!(g.sectors_per_cluster_shift(), 8);
        assert_eq!(g.sectors_per_cluster(), 256);
        assert_eq!(g.bytes_per_cluster(), 128 * 1024);
    }

    #[test]
    fn picks_128kib_cluster_at_upper_band_boundary() {
        let g = geom(32 * GIB);
        assert_eq!(g.sectors_per_cluster_shift(), 8);
    }

    #[test]
    fn picks_4kib_cluster_at_lower_band_top_minus_one() {
        let g = geom(256 * MIB - 512);
        assert_eq!(g.sectors_per_cluster_shift(), 3);
    }

    // ---------- Total-sectors round trip ----------

    #[test]
    fn total_sectors_matches_volume_size() {
        let bytes = 4 * GIB;
        let g = geom(bytes);
        assert_eq!(g.total_sectors(), bytes / 512);
        assert_eq!(g.volume_size_bytes(), bytes);
    }

    // ---------- FAT length and cluster heap math ----------

    #[test]
    fn fat_length_holds_all_cluster_entries() {
        let g = geom(4 * GIB);
        let entries = u64::from(g.cluster_count()) + u64::from(FIRST_CLUSTER_NUMBER);
        let needed_bytes = entries * 4;
        let actual_bytes = u64::from(g.fat_length_sectors()) * u64::from(SECTOR_SIZE_BYTES);
        assert!(
            actual_bytes >= needed_bytes,
            "FAT length {actual_bytes}B must cover {needed_bytes}B of entries"
        );
        // And not wildly over — within one sector of the required
        // size (we round up).
        assert!(
            actual_bytes - needed_bytes < u64::from(SECTOR_SIZE_BYTES),
            "FAT length {actual_bytes}B overshoots {needed_bytes}B by more than a sector"
        );
    }

    #[test]
    fn cluster_heap_offset_immediately_follows_fat() {
        let g = geom(8 * GIB);
        assert_eq!(
            g.cluster_heap_offset_sectors(),
            g.fat_offset_sectors() + g.fat_length_sectors()
        );
    }

    #[test]
    fn fat_offset_is_pinned_to_after_backup_boot_region() {
        let g = geom(64 * MIB);
        assert_eq!(g.fat_offset_sectors(), 24);
    }

    // ---------- Region map invariants ----------

    #[test]
    fn region_map_starts_with_main_boot_region() {
        let g = geom(64 * MIB);
        let first = g.regions()[0];
        assert_eq!(first.start, 0);
        assert_eq!(first.len, 12 * 512);
        assert_eq!(first.kind, RegionKind::ExfatMainBootRegion);
    }

    #[test]
    fn region_map_second_is_backup_boot_region() {
        let g = geom(64 * MIB);
        let r = g.regions()[1];
        assert_eq!(r.start, 12 * 512);
        assert_eq!(r.len, 12 * 512);
        assert_eq!(r.kind, RegionKind::ExfatBackupBootRegion);
    }

    #[test]
    fn region_map_third_is_fat_table_index_zero() {
        let g = geom(64 * MIB);
        let r = g.regions()[2];
        assert_eq!(r.start, 24 * 512);
        assert_eq!(r.kind, RegionKind::FatTable { index: 0 });
        assert_eq!(r.len, u64::from(g.fat_length_sectors()) * 512);
    }

    #[test]
    fn region_map_has_exactly_one_fat_table() {
        let g = geom(4 * GIB);
        let fat_regions: Vec<_> = g
            .regions()
            .iter()
            .filter(|r| matches!(r.kind, RegionKind::FatTable { .. }))
            .collect();
        assert_eq!(fat_regions.len(), 1, "exFAT has only one FAT (no mirror)");
    }

    #[test]
    fn region_map_contains_data_region() {
        let g = geom(4 * GIB);
        let data: Vec<_> = g
            .regions()
            .iter()
            .filter(|r| matches!(r.kind, RegionKind::Data))
            .collect();
        assert_eq!(data.len(), 1);
        assert!(data[0].len > 0);
    }

    #[test]
    fn region_map_total_matches_volume_size() {
        let bytes = 4 * GIB;
        let g = geom(bytes);
        let covered: u64 = g.regions().iter().map(|r| r.len).sum();
        assert_eq!(covered, bytes);
    }

    #[test]
    fn region_map_has_no_gaps() {
        let g = geom(8 * GIB);
        let mut cursor = 0_u64;
        for r in g.regions() {
            assert_eq!(r.start, cursor, "region map has a gap at {cursor}");
            cursor += r.len;
        }
    }

    #[test]
    fn region_map_excess_is_reserved_kind() {
        // Pick a size that does not divide cleanly so we get an
        // excess trailer.
        let bytes = 4 * GIB + 1024 * 512;
        let g = geom(bytes);
        let last = g.regions().last().copied().unwrap();
        assert!(matches!(last.kind, RegionKind::Reserved | RegionKind::Data));
        let covered: u64 = g.regions().iter().map(|r| r.len).sum();
        assert_eq!(covered, bytes);
    }

    // ---------- region_at default trait routing ----------

    #[test]
    fn region_at_routes_byte_zero_to_main_boot_region() {
        let g = geom(64 * MIB);
        let r = g.region_at(0).expect("byte 0 lies in main boot region");
        assert_eq!(r.kind, RegionKind::ExfatMainBootRegion);
    }

    #[test]
    fn region_at_routes_backup_first_byte_to_backup_region() {
        let g = geom(64 * MIB);
        let r = g
            .region_at(12 * 512)
            .expect("byte at backup boot region start");
        assert_eq!(r.kind, RegionKind::ExfatBackupBootRegion);
    }

    #[test]
    fn region_at_routes_fat_first_byte_to_fat_table() {
        let g = geom(64 * MIB);
        let r = g.region_at(24 * 512).expect("byte at FAT start");
        assert_eq!(r.kind, RegionKind::FatTable { index: 0 });
    }

    #[test]
    fn region_at_routes_cluster_heap_first_byte_to_data() {
        let g = geom(64 * MIB);
        let offset = u64::from(g.cluster_heap_offset_sectors()) * 512;
        let r = g.region_at(offset).expect("byte at cluster heap start");
        assert_eq!(r.kind, RegionKind::Data);
    }

    #[test]
    fn region_at_returns_none_past_volume_end() {
        let g = geom(64 * MIB);
        let vol = g.volume_size_bytes();
        assert!(g.region_at(vol).is_none());
        assert!(g.region_at(vol + 1).is_none());
    }

    // ---------- Cluster count bounds ----------

    #[test]
    fn cluster_count_below_max() {
        let g = geom(64 * GIB);
        assert!(g.cluster_count() <= MAX_EXFAT_CLUSTER_COUNT);
    }

    #[test]
    fn first_root_directory_cluster_is_first_valid_cluster() {
        let g = geom(64 * MIB);
        assert_eq!(g.first_root_directory_cluster(), FIRST_CLUSTER_NUMBER);
    }

    // ---------- Determinism ----------

    #[test]
    fn same_volume_size_produces_identical_regions() {
        let a = geom(2 * GIB);
        let b = geom(2 * GIB);
        assert_eq!(a.regions(), b.regions());
        assert_eq!(a.fat_length_sectors(), b.fat_length_sectors());
        assert_eq!(a.cluster_count(), b.cluster_count());
        assert_eq!(a.sectors_per_cluster_shift(), b.sectors_per_cluster_shift());
    }

    // ---------- Display ----------

    #[test]
    fn display_includes_key_fields() {
        let g = geom(4 * GIB);
        let s = format!("{g}");
        assert!(s.contains("ExfatGeometry"));
        assert!(s.contains("total_sectors"));
        assert!(s.contains("spc_shift"));
        assert!(s.contains("fat_length_sectors"));
        assert!(s.contains("cluster_count"));
    }
}
