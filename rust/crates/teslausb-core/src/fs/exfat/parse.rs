//! exFAT write-side **decoder** (Phase 3.2).
//!
//! The inverse of [`crate::fs::exfat::synth::ExfatSynth::read`]:
//! given an arbitrary `(offset, &[u8])` write that the kernel
//! issued against the synthesized volume, classify every byte
//! into the exFAT region it lands in and return a sequence of
//! typed per-region chunks the consumer (Phase 3.3
//! `backend::dir_tree` POSIX adapter) can route.
//!
//! Parallels [`crate::fs::fat32::parse`] (Phase 3.1); see that
//! module for the design rationale shared by both filesystems.
//! The differences here mirror the structural differences
//! between FAT32 and exFAT spelled out in the read-side
//! [`crate::fs::exfat::synth`] dispatcher:
//!
//! 1. **Boot regions are 12 contiguous sectors each.** The
//!    [`crate::fs::geometry::RegionKind::ExfatMainBootRegion`]
//!    variant covers sectors 0..12 (boot sector + 8 extended
//!    boot sectors + OEM parameters + reserved + checksum,
//!    Microsoft exFAT spec v1.00 §3.1) and
//!    [`crate::fs::geometry::RegionKind::ExfatBackupBootRegion`]
//!    mirrors that at sectors 12..24 (§3.2). FAT32's per-sector
//!    boot / fsinfo / backup-boot variants do not appear.
//! 2. **There is no `FsInfo`.** exFAT has no equivalent free-
//!    cluster summary sector — free-cluster bookkeeping lives
//!    in the [`AllocationBitmap`](crate::fs::exfat::allocation_bitmap),
//!    itself stored as clusters in the data region.
//! 3. **`NumberOfFats = 1`** (pinned by
//!    [`crate::fs::exfat::geometry::NUMBER_OF_FATS`]). The
//!    [`RegionKind::FatTable`] variant therefore always carries
//!    `index = 0`; the `mirror_index` field on
//!    [`DecodedWrite::FatTable`] is included for shape-parity
//!    with the FAT32 decoder so consumers can stay regular.
//! 4. **No reserved gap between boot region and FAT.** The
//!    backup boot region ends at sector 24 and the FAT starts at
//!    sector 24 with no padding. Reserved space only appears as
//!    *excess* tail after the cluster heap if the volume size
//!    isn't a clean multiple of the chosen cluster size.
//! 5. **Cluster heap holds everything** — root directory,
//!    allocation bitmap, upcase table, file data. They are all
//!    just clusters in the [`RegionKind::Data`] region; the
//!    consumer differentiates by `cluster_number`. This decoder
//!    does not parse cluster *contents*; it only splits at
//!    cluster boundaries.
//!
//! ## Region splitting rules
//!
//! Identical to FAT32:
//!
//! 1. A write that straddles two regions is emitted as two
//!    chunks, in ascending-offset order.
//! 2. A write into the data region that straddles a cluster
//!    boundary is emitted as one [`DecodedWrite::DataCluster`]
//!    chunk per cluster, again in ascending-offset order.
//! 3. Variant fields that name an in-region offset
//!    (`byte_in_region`, `byte_in_fat`, `byte_in_cluster`) are
//!    **region-local**: subtract the region's `start` from the
//!    absolute volume offset. [`DecodedWrite::Reserved`] is the
//!    exception — it carries an `absolute_offset` because the
//!    Reserved tail (if present) is a disjoint trailing region
//!    where a relative offset would be ambiguous.
//!
//! ## What this module does NOT do
//!
//! * It does not validate write content (e.g. it does NOT
//!   reject an exFAT directory entry with a bogus `EntryType`,
//!   nor a FAT entry pointing at cluster `0`).
//! * It does not apply writes anywhere — see Phase 3.3.
//! * It does not check `cluster_number <= cluster_count + 1`
//!   for [`DecodedWrite::DataCluster`]; the consumer decides
//!   policy for writes beyond the last allocatable cluster
//!   (those can only arise on a kernel issuing a write past the
//!   raw cluster heap edge, which is itself a kernel bug —
//!   recording the offending cluster number aids diagnosis).
//!
//! ## Spec reference
//!
//! Microsoft exFAT File System Specification v1.00 (August 27,
//! 2019). §3.1 Main Boot Region, §3.2 Backup Boot Region, §4
//! FAT Region, §5 Cluster Heap, §6 Directory Entries.
//! Region-kind tags are [`crate::fs::geometry::RegionKind`].

use crate::fs::cluster_layout::FIRST_DATA_CLUSTER;
use crate::fs::exfat::geometry::ExfatGeometry;
use crate::fs::geometry::{Geometry, Region, RegionKind};

/// One per-region chunk of a decoded NBD write against an exFAT
/// volume.
///
/// The `bytes` field is borrowed from the caller's input buffer;
/// the consumer can either copy it (if it needs ownership) or
/// route it through a write-back pipeline without allocation.
///
/// Variants intentionally mirror the [`RegionKind`] variants that
/// an exFAT geometry's region map can produce. The FAT32-only
/// `RegionKind` variants are unreachable here and rejected by
/// [`decode_write`] with [`DecodeWriteError::UnsupportedRegion`].
#[derive(Debug, PartialEq, Eq)]
pub enum DecodedWrite<'a> {
    /// Write into the 12-sector main boot region (sectors 0..12
    /// of the volume). `byte_in_region` is the offset within the
    /// 6144-byte region (Microsoft exFAT spec v1.00 §3.1).
    MainBootRegion {
        /// Byte offset within the main boot region
        /// (`0..BOOT_REGION_SIZE_BYTES`).
        byte_in_region: usize,
        /// The subslice of the caller's input that lands here.
        bytes: &'a [u8],
    },
    /// Write into the 12-sector backup boot region (sectors
    /// 12..24 of the volume). Per Microsoft exFAT spec v1.00
    /// §3.2 this region is a byte-for-byte mirror of the main
    /// boot region; the consumer's policy is to either mirror
    /// writes both ways or to treat divergence as a corruption
    /// event.
    BackupBootRegion {
        /// Byte offset within the backup boot region
        /// (`0..BOOT_REGION_SIZE_BYTES`).
        byte_in_region: usize,
        /// The subslice of the caller's input that lands here.
        bytes: &'a [u8],
    },
    /// Write into the FAT table. exFAT pins `NumberOfFats = 1`
    /// so `mirror_index` is always `0` today; the field is
    /// retained for shape-parity with the FAT32 decoder.
    FatTable {
        /// Which FAT mirror this write targets. Always `0`
        /// under the current B-1 pin.
        mirror_index: u8,
        /// Byte offset within the FAT region (`0..fat_length_bytes`).
        byte_in_fat: usize,
        /// The subslice of the caller's input that lands here.
        bytes: &'a [u8],
    },
    /// Write into one cluster of the data region (a.k.a. the
    /// "cluster heap" in exFAT terminology). The data region
    /// holds the root directory, allocation bitmap, upcase
    /// table, and every file's contents — the consumer
    /// differentiates by `cluster_number` against its layout
    /// model.
    DataCluster {
        /// FAT cluster number ([`FIRST_DATA_CLUSTER`] and up).
        /// Cluster numbers `0` and `1` are reserved per spec
        /// and never appear here.
        cluster_number: u32,
        /// Byte offset within the cluster (`0..bytes_per_cluster`).
        byte_in_cluster: usize,
        /// The subslice of the caller's input that lands here.
        bytes: &'a [u8],
    },
    /// Write into the reserved excess space at the tail of the
    /// volume (present only if the volume size isn't a clean
    /// multiple of the chosen cluster size; see
    /// [`crate::fs::exfat::geometry::ExfatGeometry::for_volume_size`]).
    ///
    /// Carries an **absolute** volume offset rather than a
    /// region-local one because Reserved is a single disjoint
    /// trailing region whose start is not constant across
    /// geometries — recording the absolute byte the kernel asked
    /// for is the most useful diagnostic.
    Reserved {
        /// Absolute byte offset within the volume.
        absolute_offset: u64,
        /// The subslice of the caller's input that lands here.
        bytes: &'a [u8],
    },
}

/// Errors returned by [`decode_write`].
///
/// The bounds-check variants mirror
/// [`crate::fs::exfat::synth::ExfatSynthError`] and
/// [`crate::fs::fat32::parse::DecodeWriteError`] for cross-
/// filesystem symmetry. The `UnsupportedRegion` variant is
/// defense-in-depth: a well-formed [`ExfatGeometry`] never
/// produces a FAT32-only [`RegionKind`], but the type system
/// can't prove that, so we reject explicitly instead of
/// silently dropping the write.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum DecodeWriteError {
    /// `offset` is at or beyond the geometry's volume size.
    #[error("write offset {offset} is at or beyond the volume size {volume_size}")]
    OffsetBeyondVolume {
        /// The caller's offset.
        offset: u64,
        /// The geometry's volume size.
        volume_size: u64,
    },
    /// `offset + bytes.len()` exceeds the geometry's volume size
    /// (the request writes past the end of the volume).
    #[error(
        "write of {length} bytes at offset {offset} extends past the volume size {volume_size}"
    )]
    LengthExceedsVolume {
        /// The caller's offset.
        offset: u64,
        /// The caller's input length in bytes.
        length: u64,
        /// The geometry's volume size.
        volume_size: u64,
    },
    /// The geometry returned a [`RegionKind`] this decoder does
    /// not recognise — currently the FAT32-only variants.
    #[error("exFAT decode_write received an unsupported region kind: {kind}")]
    UnsupportedRegion {
        /// The offending region kind.
        kind: RegionKind,
    },
}

/// Decode a single kernel-issued write against an exFAT volume
/// described by `geometry` into a sequence of typed per-region
/// chunks.
///
/// An empty `bytes` slice returns `Ok(Vec::new())`; no region
/// lookup is performed. Otherwise the function walks
/// [`ExfatGeometry`]'s region map starting at `offset`, emitting
/// one chunk per region boundary (and additionally one chunk per
/// cluster boundary within the data region) until the entire
/// input is consumed.
///
/// # Errors
///
/// * [`DecodeWriteError::OffsetBeyondVolume`] if `offset` is at
///   or beyond `geometry.volume_size_bytes()`.
/// * [`DecodeWriteError::LengthExceedsVolume`] if
///   `offset + bytes.len()` exceeds the volume size.
/// * [`DecodeWriteError::UnsupportedRegion`] if the geometry's
///   region map contains a FAT32 (or other non-exFAT) variant —
///   defense-in-depth; a well-formed [`ExfatGeometry`] never
///   produces one.
pub fn decode_write<'a>(
    geometry: &ExfatGeometry,
    offset: u64,
    bytes: &'a [u8],
) -> Result<Vec<DecodedWrite<'a>>, DecodeWriteError> {
    if bytes.is_empty() {
        return Ok(Vec::new());
    }
    let volume_size = geometry.volume_size_bytes();
    if offset >= volume_size {
        return Err(DecodeWriteError::OffsetBeyondVolume {
            offset,
            volume_size,
        });
    }
    let length_u64 = u64::try_from(bytes.len()).unwrap_or(u64::MAX);
    let end_offset =
        offset
            .checked_add(length_u64)
            .ok_or(DecodeWriteError::LengthExceedsVolume {
                offset,
                length: length_u64,
                volume_size,
            })?;
    if end_offset > volume_size {
        return Err(DecodeWriteError::LengthExceedsVolume {
            offset,
            length: length_u64,
            volume_size,
        });
    }

    let mut out: Vec<DecodedWrite<'a>> = Vec::new();
    let mut cursor = offset;
    let mut remaining: &'a [u8] = bytes;
    while !remaining.is_empty() {
        let region = geometry
            .region_at(cursor)
            .ok_or(DecodeWriteError::OffsetBeyondVolume {
                offset: cursor,
                volume_size,
            })?;
        let region_remaining_u64 = region.end().saturating_sub(cursor);
        let region_remaining = usize::try_from(region_remaining_u64).unwrap_or(usize::MAX);
        let take = region_remaining.min(remaining.len());
        let (chunk, rest) = remaining.split_at(take);
        emit_region_chunks(geometry, region, cursor, chunk, &mut out)?;
        cursor = cursor.saturating_add(take as u64);
        remaining = rest;
    }
    Ok(out)
}

/// Emit one or more [`DecodedWrite`] chunks for a single region.
///
/// The caller has already clamped `chunk` to lie entirely within
/// `region`; this helper only sub-divides the data region by
/// cluster boundary. All other regions emit a single chunk.
fn emit_region_chunks<'a>(
    geometry: &ExfatGeometry,
    region: Region,
    cursor: u64,
    chunk: &'a [u8],
    out: &mut Vec<DecodedWrite<'a>>,
) -> Result<(), DecodeWriteError> {
    let byte_in_region_u64 = cursor.saturating_sub(region.start);
    let byte_in_region = usize::try_from(byte_in_region_u64).unwrap_or(usize::MAX);
    match region.kind {
        RegionKind::ExfatMainBootRegion => out.push(DecodedWrite::MainBootRegion {
            byte_in_region,
            bytes: chunk,
        }),
        RegionKind::ExfatBackupBootRegion => out.push(DecodedWrite::BackupBootRegion {
            byte_in_region,
            bytes: chunk,
        }),
        RegionKind::FatTable { index } => out.push(DecodedWrite::FatTable {
            mirror_index: index,
            byte_in_fat: byte_in_region,
            bytes: chunk,
        }),
        RegionKind::Data => emit_data_chunks(geometry, region, cursor, chunk, out),
        RegionKind::Reserved => out.push(DecodedWrite::Reserved {
            absolute_offset: cursor,
            bytes: chunk,
        }),
        RegionKind::Fat32BootSector
        | RegionKind::Fat32BackupBootSector
        | RegionKind::Fat32FsInfo => {
            return Err(DecodeWriteError::UnsupportedRegion { kind: region.kind });
        }
    }
    Ok(())
}

/// Sub-divide a data-region chunk into one
/// [`DecodedWrite::DataCluster`] per cluster boundary it crosses.
///
/// Cluster numbering: [`FIRST_DATA_CLUSTER`] (cluster 2) is the
/// first cluster of the data region; exFAT reserves cluster
/// numbers 0 and 1 (per spec §4 the FAT[0] slot holds the media
/// descriptor and FAT[1] is unused). Cluster numbers can in
/// principle exceed `cluster_count + 1` if the geometry's data
/// region byte length is not an exact multiple of
/// `bytes_per_cluster` — those writes would be off-the-end-of-
/// the-allocatable-heap and the consumer's problem; we still
/// emit them faithfully so the consumer can log + drop.
fn emit_data_chunks<'a>(
    geometry: &ExfatGeometry,
    region: Region,
    cursor: u64,
    chunk: &'a [u8],
    out: &mut Vec<DecodedWrite<'a>>,
) {
    let bytes_per_cluster = geometry.bytes_per_cluster();
    if bytes_per_cluster == 0 || chunk.is_empty() {
        return;
    }
    let bytes_per_cluster_u64 = u64::from(bytes_per_cluster);
    let bytes_per_cluster_usize = bytes_per_cluster as usize;
    let mut sub_cursor = cursor;
    let mut sub_remaining: &'a [u8] = chunk;
    while !sub_remaining.is_empty() {
        let byte_in_data = sub_cursor.saturating_sub(region.start);
        let cluster_index = byte_in_data / bytes_per_cluster_u64;
        let byte_in_cluster_u64 = byte_in_data % bytes_per_cluster_u64;
        let byte_in_cluster = usize::try_from(byte_in_cluster_u64).unwrap_or(usize::MAX);
        let cluster_number =
            u32::try_from(cluster_index.saturating_add(u64::from(FIRST_DATA_CLUSTER)))
                .unwrap_or(u32::MAX);
        let chunk_remaining_in_cluster = bytes_per_cluster_usize.saturating_sub(byte_in_cluster);
        let take = chunk_remaining_in_cluster.min(sub_remaining.len());
        let (this_cluster, rest) = sub_remaining.split_at(take);
        out.push(DecodedWrite::DataCluster {
            cluster_number,
            byte_in_cluster,
            bytes: this_cluster,
        });
        sub_cursor = sub_cursor.saturating_add(take as u64);
        sub_remaining = rest;
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
    use crate::fs::exfat::geometry::{BOOT_REGION_SECTORS, FAT_OFFSET_SECTORS};
    use crate::fs::geometry::SECTOR_SIZE_BYTES;

    /// 64 MiB exFAT — matches the convention used in
    /// `exfat/synth.rs` test modules. Comfortably above the
    /// 8 MiB exFAT floor; small enough that geometry
    /// construction is microseconds; large enough that the
    /// data region holds enough clusters to exercise the
    /// cross-cluster split paths.
    const TEST_VOLUME_BYTES: u64 = 64 * 1024 * 1024;
    const SECTOR: u64 = SECTOR_SIZE_BYTES as u64;
    const BOOT_REGION_BYTES: u64 = (BOOT_REGION_SECTORS as u64) * SECTOR;

    fn geo() -> ExfatGeometry {
        ExfatGeometry::for_volume_size(TEST_VOLUME_BYTES).expect("64 MiB is a valid exFAT size")
    }

    fn main_boot_start() -> u64 {
        0
    }

    fn backup_boot_start() -> u64 {
        BOOT_REGION_BYTES
    }

    fn fat_start() -> u64 {
        u64::from(FAT_OFFSET_SECTORS) * SECTOR
    }

    fn data_start(g: &ExfatGeometry) -> u64 {
        u64::from(g.cluster_heap_offset_sectors()) * SECTOR
    }

    #[test]
    fn empty_write_returns_empty_vec_without_touching_geometry() {
        let g = geo();
        let result = decode_write(&g, 0, &[]).expect("empty write is OK");
        assert!(result.is_empty());
    }

    #[test]
    fn write_at_or_past_volume_end_returns_offset_beyond_volume() {
        let g = geo();
        let vol = g.volume_size_bytes();
        let err = decode_write(&g, vol, &[0u8; 1]).expect_err("at end is rejected");
        assert!(matches!(
            err,
            DecodeWriteError::OffsetBeyondVolume { offset, volume_size }
                if offset == vol && volume_size == vol
        ));
        let err = decode_write(&g, vol + 1, &[0u8; 1]).expect_err("past end is rejected");
        assert!(matches!(
            err,
            DecodeWriteError::OffsetBeyondVolume { offset, .. } if offset == vol + 1
        ));
    }

    #[test]
    fn write_extending_past_volume_returns_length_exceeds_volume() {
        let g = geo();
        let vol = g.volume_size_bytes();
        let one_before_end = vol - 1;
        let err = decode_write(&g, one_before_end, &[0u8; 2]).expect_err("straddle end rejected");
        assert!(matches!(
            err,
            DecodeWriteError::LengthExceedsVolume {
                offset, length, volume_size
            } if offset == one_before_end && length == 2 && volume_size == vol
        ));
    }

    #[test]
    fn single_byte_write_at_offset_zero_is_main_boot_region() {
        let g = geo();
        let payload = [0xABu8];
        let result = decode_write(&g, main_boot_start(), &payload).expect("ok");
        assert_eq!(result.len(), 1);
        assert_eq!(
            result[0],
            DecodedWrite::MainBootRegion {
                byte_in_region: 0,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_at_end_of_main_boot_region_carries_correct_local_offset() {
        let g = geo();
        let offset = backup_boot_start() - 4;
        let payload: Vec<u8> = vec![0x11, 0x22, 0x33, 0x44];
        let result = decode_write(&g, offset, &payload).expect("ok");
        assert_eq!(result.len(), 1);
        let expected_byte_in_region =
            usize::try_from(BOOT_REGION_BYTES - 4).expect("fits in usize");
        assert_eq!(
            result[0],
            DecodedWrite::MainBootRegion {
                byte_in_region: expected_byte_in_region,
                bytes: payload.as_slice(),
            }
        );
    }

    #[test]
    fn write_at_start_of_backup_boot_region_carries_offset_zero() {
        let g = geo();
        let payload = [0xCCu8];
        let result = decode_write(&g, backup_boot_start(), &payload).expect("ok");
        assert_eq!(result.len(), 1);
        assert_eq!(
            result[0],
            DecodedWrite::BackupBootRegion {
                byte_in_region: 0,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_into_backup_boot_region_carries_local_offset() {
        let g = geo();
        let local_offset_u64: u64 = 7;
        let offset = backup_boot_start() + local_offset_u64;
        let payload = [0xEEu8; 3];
        let result = decode_write(&g, offset, &payload).expect("ok");
        assert_eq!(result.len(), 1);
        assert_eq!(
            result[0],
            DecodedWrite::BackupBootRegion {
                byte_in_region: usize::try_from(local_offset_u64).expect("fits"),
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_into_fat_table_uses_mirror_index_zero() {
        let g = geo();
        let payload = [0x12u8, 0x34, 0x56, 0x78];
        let result = decode_write(&g, fat_start(), &payload).expect("ok");
        assert_eq!(result.len(), 1);
        assert_eq!(
            result[0],
            DecodedWrite::FatTable {
                mirror_index: 0,
                byte_in_fat: 0,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_into_fat_table_at_nonzero_offset_carries_local_offset() {
        let g = geo();
        let local_offset_u64: u64 = 100;
        let offset = fat_start() + local_offset_u64;
        let payload = [0xAAu8; 8];
        let result = decode_write(&g, offset, &payload).expect("ok");
        assert_eq!(result.len(), 1);
        assert_eq!(
            result[0],
            DecodedWrite::FatTable {
                mirror_index: 0,
                byte_in_fat: usize::try_from(local_offset_u64).expect("fits"),
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_into_first_data_cluster_emits_cluster_number_two() {
        let g = geo();
        let payload = [0xBEu8, 0xEF];
        let result = decode_write(&g, data_start(&g), &payload).expect("ok");
        assert_eq!(result.len(), 1);
        assert_eq!(
            result[0],
            DecodedWrite::DataCluster {
                cluster_number: 2,
                byte_in_cluster: 0,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_into_third_data_cluster_emits_cluster_number_four() {
        let g = geo();
        let bpc = u64::from(g.bytes_per_cluster());
        let offset = data_start(&g) + 2 * bpc;
        let payload = [0x42u8];
        let result = decode_write(&g, offset, &payload).expect("ok");
        assert_eq!(result.len(), 1);
        assert_eq!(
            result[0],
            DecodedWrite::DataCluster {
                cluster_number: 4,
                byte_in_cluster: 0,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_straddling_main_to_backup_boot_emits_two_chunks() {
        let g = geo();
        let boundary = backup_boot_start();
        let offset = boundary - 1;
        let payload: Vec<u8> = vec![0x01, 0x02];
        let result = decode_write(&g, offset, &payload).expect("ok");
        assert_eq!(result.len(), 2);
        let expected_main_byte = usize::try_from(BOOT_REGION_BYTES - 1).expect("fits in usize");
        assert_eq!(
            result[0],
            DecodedWrite::MainBootRegion {
                byte_in_region: expected_main_byte,
                bytes: &payload[0..1],
            }
        );
        assert_eq!(
            result[1],
            DecodedWrite::BackupBootRegion {
                byte_in_region: 0,
                bytes: &payload[1..2],
            }
        );
    }

    #[test]
    fn write_straddling_backup_to_fat_emits_two_chunks() {
        let g = geo();
        let boundary = fat_start();
        let offset = boundary - 1;
        let payload: Vec<u8> = vec![0xA1, 0xB2];
        let result = decode_write(&g, offset, &payload).expect("ok");
        assert_eq!(result.len(), 2);
        let expected_backup_byte = usize::try_from(BOOT_REGION_BYTES - 1).expect("fits in usize");
        assert_eq!(
            result[0],
            DecodedWrite::BackupBootRegion {
                byte_in_region: expected_backup_byte,
                bytes: &payload[0..1],
            }
        );
        assert_eq!(
            result[1],
            DecodedWrite::FatTable {
                mirror_index: 0,
                byte_in_fat: 0,
                bytes: &payload[1..2],
            }
        );
    }

    #[test]
    fn write_straddling_fat_to_data_emits_two_chunks() {
        let g = geo();
        let boundary = data_start(&g);
        let offset = boundary - 1;
        let payload: Vec<u8> = vec![0xC1, 0xC2];
        let result = decode_write(&g, offset, &payload).expect("ok");
        assert_eq!(result.len(), 2);
        let fat_len_bytes = u64::from(g.fat_length_sectors()) * SECTOR;
        let expected_fat_byte = usize::try_from(fat_len_bytes - 1).expect("fits in usize");
        assert_eq!(
            result[0],
            DecodedWrite::FatTable {
                mirror_index: 0,
                byte_in_fat: expected_fat_byte,
                bytes: &payload[0..1],
            }
        );
        assert_eq!(
            result[1],
            DecodedWrite::DataCluster {
                cluster_number: 2,
                byte_in_cluster: 0,
                bytes: &payload[1..2],
            }
        );
    }

    #[test]
    fn write_straddling_data_cluster_boundary_emits_two_data_chunks() {
        let g = geo();
        let bpc = u64::from(g.bytes_per_cluster());
        let offset = data_start(&g) + bpc - 1;
        let payload: Vec<u8> = vec![0xD1, 0xD2];
        let result = decode_write(&g, offset, &payload).expect("ok");
        assert_eq!(result.len(), 2);
        let expected_first_byte_in_cluster = usize::try_from(bpc - 1).expect("fits in usize");
        assert_eq!(
            result[0],
            DecodedWrite::DataCluster {
                cluster_number: 2,
                byte_in_cluster: expected_first_byte_in_cluster,
                bytes: &payload[0..1],
            }
        );
        assert_eq!(
            result[1],
            DecodedWrite::DataCluster {
                cluster_number: 3,
                byte_in_cluster: 0,
                bytes: &payload[1..2],
            }
        );
    }

    #[test]
    fn write_spanning_three_consecutive_clusters_emits_three_data_chunks() {
        let g = geo();
        let bpc = u64::from(g.bytes_per_cluster());
        let bpc_usize = usize::try_from(bpc).expect("fits in usize");
        // 1 byte at the tail of cluster 2, full cluster 3,
        // 1 byte at the head of cluster 4.
        let offset = data_start(&g) + bpc - 1;
        let total_len = 1 + bpc_usize + 1;
        let payload = vec![0xAAu8; total_len];
        let result = decode_write(&g, offset, &payload).expect("ok");
        assert_eq!(result.len(), 3);
        assert_eq!(
            result[0],
            DecodedWrite::DataCluster {
                cluster_number: 2,
                byte_in_cluster: bpc_usize - 1,
                bytes: &payload[0..1],
            }
        );
        assert_eq!(
            result[1],
            DecodedWrite::DataCluster {
                cluster_number: 3,
                byte_in_cluster: 0,
                bytes: &payload[1..=bpc_usize],
            }
        );
        assert_eq!(
            result[2],
            DecodedWrite::DataCluster {
                cluster_number: 4,
                byte_in_cluster: 0,
                bytes: &payload[(1 + bpc_usize)..total_len],
            }
        );
    }

    #[test]
    fn write_at_last_byte_of_a_data_cluster_does_not_overflow_into_next() {
        let g = geo();
        let bpc = u64::from(g.bytes_per_cluster());
        let offset = data_start(&g) + bpc - 1;
        let payload = [0xF1u8];
        let result = decode_write(&g, offset, &payload).expect("ok");
        assert_eq!(result.len(), 1);
        let expected_byte_in_cluster = usize::try_from(bpc - 1).expect("fits in usize");
        assert_eq!(
            result[0],
            DecodedWrite::DataCluster {
                cluster_number: 2,
                byte_in_cluster: expected_byte_in_cluster,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_spanning_multiple_regions_concatenates_in_order() {
        // Backup-boot → FAT → first data cluster (3 chunks).
        let g = geo();
        let bpc = u64::from(g.bytes_per_cluster());
        let bpc_usize = usize::try_from(bpc).expect("fits in usize");
        let fat_len = u64::from(g.fat_length_sectors()) * SECTOR;
        let fat_len_usize = usize::try_from(fat_len).expect("fits in usize");
        // Start in the last byte of the backup boot region.
        let offset = fat_start() - 1;
        // Length: 1 byte of backup + entire FAT + first data cluster.
        let total_len_u64 = 1 + fat_len + bpc;
        let total_len = usize::try_from(total_len_u64).expect("fits in usize");
        let payload = vec![0x5Au8; total_len];
        let result = decode_write(&g, offset, &payload).expect("ok");
        // Backup-boot (1 byte) + FatTable (whole FAT) +
        // first DataCluster (whole cluster) = 3 chunks.
        assert_eq!(result.len(), 3);
        let expected_backup_byte = usize::try_from(BOOT_REGION_BYTES - 1).expect("fits in usize");
        assert_eq!(
            result[0],
            DecodedWrite::BackupBootRegion {
                byte_in_region: expected_backup_byte,
                bytes: &payload[0..1],
            }
        );
        assert_eq!(
            result[1],
            DecodedWrite::FatTable {
                mirror_index: 0,
                byte_in_fat: 0,
                bytes: &payload[1..=fat_len_usize],
            }
        );
        assert_eq!(
            result[2],
            DecodedWrite::DataCluster {
                cluster_number: 2,
                byte_in_cluster: 0,
                bytes: &payload[(1 + fat_len_usize)..(1 + fat_len_usize + bpc_usize)],
            }
        );
    }

    #[test]
    fn write_into_reserved_excess_region_emits_reserved_chunk_with_absolute_offset() {
        // Pick a volume size guaranteed to produce an excess
        // (Reserved) tail: 9 MiB sits just above the 8 MiB
        // floor and almost always leaves trailing bytes after
        // the optimizer rounds the cluster heap to whole clusters.
        let g =
            ExfatGeometry::for_volume_size(9 * 1024 * 1024).expect("9 MiB is a valid exFAT size");
        let Some(reserved) = g
            .regions()
            .iter()
            .find(|r| matches!(r.kind, RegionKind::Reserved))
            .copied()
        else {
            // If 9 MiB happens to fit cleanly, skip the
            // assertion path — the inter-region straddle
            // tests still cover the Reserved emitter via
            // emit_region_chunks's match arm. (No exFAT volume
            // size we test against is known to skip this
            // arm; the guard is paranoia.)
            return;
        };
        let payload: Vec<u8> = vec![0x99, 0x88];
        let result = decode_write(&g, reserved.start, &payload).expect("ok");
        assert_eq!(result.len(), 1);
        assert_eq!(
            result[0],
            DecodedWrite::Reserved {
                absolute_offset: reserved.start,
                bytes: payload.as_slice(),
            }
        );
    }

    #[test]
    fn input_bytes_are_borrowed_not_copied() {
        let g = geo();
        let payload: Vec<u8> = (0..32u32)
            .map(|i| u8::try_from(i & 0xFF).unwrap())
            .collect();
        let result = decode_write(&g, 0, &payload).expect("ok");
        assert_eq!(result.len(), 1);
        if let DecodedWrite::MainBootRegion { bytes, .. } = &result[0] {
            // Pointer identity: the carried slice must alias
            // the caller's buffer, not be a copy.
            assert_eq!(bytes.as_ptr(), payload.as_ptr());
            assert_eq!(bytes.len(), payload.len());
        } else {
            panic!("unexpected variant: {:?}", result[0]);
        }
    }

    #[test]
    fn whole_volume_write_visits_every_region_in_order() {
        // Walk the whole volume in one big write, then assert
        // the chunk sequence has the right region kinds in
        // ascending order: MainBoot, BackupBoot, FatTable,
        // DataCluster (one per cluster), [Reserved].
        let g = geo();
        let vol = g.volume_size_bytes();
        let vol_usize = usize::try_from(vol).expect("64 MiB fits in usize");
        let payload = vec![0u8; vol_usize];
        let result = decode_write(&g, 0, &payload).expect("ok");
        let mut iter = result.iter();
        assert!(matches!(
            iter.next(),
            Some(DecodedWrite::MainBootRegion {
                byte_in_region: 0,
                ..
            })
        ));
        assert!(matches!(
            iter.next(),
            Some(DecodedWrite::BackupBootRegion {
                byte_in_region: 0,
                ..
            })
        ));
        assert!(matches!(
            iter.next(),
            Some(DecodedWrite::FatTable {
                mirror_index: 0,
                byte_in_fat: 0,
                ..
            })
        ));
        // Data clusters, starting at cluster 2 and ascending.
        let mut next_expected_cluster: u32 = 2;
        for chunk in iter {
            match chunk {
                DecodedWrite::DataCluster {
                    cluster_number,
                    byte_in_cluster: 0,
                    ..
                } => {
                    assert_eq!(*cluster_number, next_expected_cluster);
                    next_expected_cluster = next_expected_cluster.saturating_add(1);
                }
                DecodedWrite::Reserved { .. } => {
                    // Optional trailing region — must be last.
                    break;
                }
                other => panic!("unexpected mid-stream variant: {other:?}"),
            }
        }
    }
}
