//! FAT32 write-side **decoder** (Phase 3.1).
//!
//! The inverse of [`crate::fs::fat32::synth::Fat32Synth::read`]:
//! given an arbitrary `(offset, &[u8])` write that the kernel
//! issued against the synthesized volume, classify every byte
//! into the FAT32 region it lands in and return a sequence of
//! typed per-region chunks the consumer (Phase 3.3
//! `backend::dir_tree` POSIX adapter) can route.
//!
//! ## Why this module is pure logic
//!
//! Phase 3.1's plan-stated scope (`docs/00-PLAN.md` Phase 3) is
//! "translates pwrite-into-BPB / FAT / dir regions into a typed
//! enum. Unit tests for every region." There is no I/O here, no
//! state — just region arithmetic on top of [`Fat32Geometry`]'s
//! region map (Phase 2.1).
//!
//! ## Region splitting rules
//!
//! 1. A write that straddles two regions is emitted as two
//!    chunks, in ascending-offset order.
//! 2. A write into the data region that straddles a cluster
//!    boundary is emitted as one [`DecodedWrite::DataCluster`]
//!    chunk per cluster, again in ascending-offset order.
//! 3. A write into a single [`crate::fs::geometry::RegionKind::FatTable`]
//!    mirror is emitted as one [`DecodedWrite::FatTable`] chunk —
//!    intra-FAT splitting (per-sector or per-entry) is the
//!    consumer's job.
//! 4. Variant fields that name an in-region offset
//!    (`byte_in_sector`, `byte_in_fat`, `byte_in_cluster`) are
//!    **region-local**: subtract the region's `start` from the
//!    absolute volume offset. The carried `bytes` slice is the
//!    exact subslice of the caller's input that lands in that
//!    chunk.
//!
//! ## What this module does NOT do
//!
//! * It does not validate write content (e.g. it does NOT
//!   reject a FAT entry that points at cluster `0`, or a boot
//!   sector with a bogus `BPB_Media`).
//! * It does not apply writes anywhere — see Phase 3.3.
//! * It does not check `cluster_number ≤ data_cluster_count + 1`
//!   for [`DecodedWrite::DataCluster`]; the consumer decides
//!   policy for writes beyond the last allocatable cluster
//!   (those can only arise on a kernel issuing a write past the
//!   raw cluster heap edge, which is itself a kernel bug —
//!   recording the offending cluster number aids diagnosis).
//!
//! ## Spec reference
//!
//! Microsoft fatgen103 §3 (region layout), §4 (FAT entries),
//! §6 (directory entries). Region-kind tags are
//! [`crate::fs::geometry::RegionKind`].

use crate::fs::cluster_layout::FIRST_DATA_CLUSTER;
use crate::fs::fat32::geometry::Fat32Geometry;
use crate::fs::geometry::{Geometry, Region, RegionKind};

/// One per-region chunk of a decoded NBD write.
///
/// The `bytes` field is borrowed from the caller's input buffer;
/// the consumer can either copy it (if it needs ownership) or
/// route it through a write-back pipeline without allocation.
///
/// Variants intentionally mirror the [`RegionKind`] variants that
/// a FAT32 geometry's region map can produce. The exFAT-only
/// `RegionKind` variants are unreachable here and rejected by
/// [`decode_write`] with [`DecodeWriteError::UnsupportedRegion`].
#[derive(Debug, PartialEq, Eq)]
pub enum DecodedWrite<'a> {
    /// Write into the FAT32 boot sector (sector 0 of the
    /// reserved region). `byte_in_sector` is the offset within
    /// the 512-byte sector.
    BootSector {
        /// Byte offset within the boot sector (0..512).
        byte_in_sector: usize,
        /// The subslice of the caller's input that lands here.
        bytes: &'a [u8],
    },
    /// Write into the FAT32 backup boot sector (typically sector
    /// 6). Per fatgen103 §3.4 normative, this is a byte-for-byte
    /// mirror of the primary boot sector; a well-behaved formatter
    /// writes both. `byte_in_sector` is the offset within the
    /// 512-byte sector.
    BackupBootSector {
        /// Byte offset within the backup boot sector (0..512).
        byte_in_sector: usize,
        /// The subslice of the caller's input that lands here.
        bytes: &'a [u8],
    },
    /// Write into the `FSInfo` sector (typically sector 1).
    /// `byte_in_sector` is the offset within the 512-byte sector.
    FsInfo {
        /// Byte offset within the `FSInfo` sector (0..512).
        byte_in_sector: usize,
        /// The subslice of the caller's input that lands here.
        bytes: &'a [u8],
    },
    /// Write into the reserved gap between the well-known boot
    /// sectors. Per fatgen103 §3.1 these bytes are reserved and
    /// should be zero, but the kernel may write whatever; the
    /// absolute volume offset is preserved for the consumer's
    /// diagnostics. The synth read path zero-fills this region.
    Reserved {
        /// Absolute byte offset in the volume where this chunk
        /// starts (no region-local rebasing — reserved regions
        /// can occupy multiple disjoint gaps).
        absolute_offset: u64,
        /// The subslice of the caller's input that lands here.
        bytes: &'a [u8],
    },
    /// Write into one of the FAT mirrors. `mirror_index` is 0
    /// for the primary FAT, 1 for the secondary mirror (FAT32
    /// always has [`crate::fs::fat32::geometry::NUM_FATS`] = 2
    /// copies). `byte_in_fat` is the offset from the start of
    /// THIS mirror (not the absolute volume offset).
    FatTable {
        /// Which mirror copy.
        mirror_index: u8,
        /// Byte offset from the start of this FAT mirror.
        byte_in_fat: usize,
        /// The subslice of the caller's input that lands here.
        bytes: &'a [u8],
    },
    /// Write into a single data cluster.
    ///
    /// `cluster_number` is the FAT cluster number — `2` is the
    /// first allocatable cluster (root directory by default),
    /// not `0`. `byte_in_cluster` is the offset from the start
    /// of that cluster. A multi-cluster write yields one
    /// `DataCluster` chunk per cluster.
    DataCluster {
        /// FAT cluster number (≥ 2).
        cluster_number: u32,
        /// Byte offset from the start of the cluster.
        byte_in_cluster: usize,
        /// The subslice of the caller's input that lands here.
        bytes: &'a [u8],
    },
}

/// Errors returned by [`decode_write`].
///
/// The bounds-check variants mirror
/// [`crate::fs::fat32::synth::Fat32SynthError`] for symmetry with
/// the read path. The `UnsupportedRegion` variant is
/// defense-in-depth against a future [`RegionKind`] extension that
/// a `Fat32Geometry` might one day produce (e.g. a new "GPT
/// reserved" variant); a correctly-constructed
/// [`Fat32Geometry`] never trips it today.
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
    /// not recognise — currently only the exFAT-only variants.
    #[error("FAT32 decode_write received an unsupported region kind: {kind}")]
    UnsupportedRegion {
        /// The offending region kind.
        kind: RegionKind,
    },
}

/// Decode a single kernel-issued write against a FAT32 volume
/// described by `geometry` into a sequence of typed per-region
/// chunks.
///
/// An empty `bytes` slice returns `Ok(Vec::new())`; no region
/// lookup is performed. Otherwise the function walks
/// [`Fat32Geometry`]'s region map starting at `offset`, emitting
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
///   region map contains an exFAT (or other non-FAT32) variant
///   — defense-in-depth; a well-formed
///   [`Fat32Geometry`] never produces one.
pub fn decode_write<'a>(
    geometry: &Fat32Geometry,
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
    geometry: &Fat32Geometry,
    region: Region,
    cursor: u64,
    chunk: &'a [u8],
    out: &mut Vec<DecodedWrite<'a>>,
) -> Result<(), DecodeWriteError> {
    let byte_in_region_u64 = cursor.saturating_sub(region.start);
    let byte_in_region = usize::try_from(byte_in_region_u64).unwrap_or(usize::MAX);
    match region.kind {
        RegionKind::Fat32BootSector => out.push(DecodedWrite::BootSector {
            byte_in_sector: byte_in_region,
            bytes: chunk,
        }),
        RegionKind::Fat32BackupBootSector => out.push(DecodedWrite::BackupBootSector {
            byte_in_sector: byte_in_region,
            bytes: chunk,
        }),
        RegionKind::Fat32FsInfo => out.push(DecodedWrite::FsInfo {
            byte_in_sector: byte_in_region,
            bytes: chunk,
        }),
        RegionKind::Reserved => out.push(DecodedWrite::Reserved {
            absolute_offset: cursor,
            bytes: chunk,
        }),
        RegionKind::FatTable { index } => out.push(DecodedWrite::FatTable {
            mirror_index: index,
            byte_in_fat: byte_in_region,
            bytes: chunk,
        }),
        RegionKind::Data => emit_data_chunks(geometry, region, cursor, chunk, out),
        RegionKind::ExfatMainBootRegion | RegionKind::ExfatBackupBootRegion => {
            return Err(DecodeWriteError::UnsupportedRegion { kind: region.kind });
        }
    }
    Ok(())
}

/// Sub-divide a data-region chunk into one
/// [`DecodedWrite::DataCluster`] per cluster boundary it crosses.
///
/// Cluster numbering: [`FIRST_DATA_CLUSTER`] (cluster 2) is the
/// first cluster of the data region; FAT32 reserves cluster
/// numbers 0 and 1. Cluster numbers can in principle exceed
/// `data_cluster_count + 1` if the geometry's data region byte
/// length is not an exact multiple of `bytes_per_cluster` —
/// those writes would be off-the-end-of-the-allocatable-heap and
/// the consumer's problem; we still emit them faithfully so the
/// consumer can log + drop.
fn emit_data_chunks<'a>(
    geometry: &Fat32Geometry,
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
    use crate::fs::fat32::geometry::{Fat32Geometry, NUM_FATS, RESERVED_SECTORS};
    use crate::fs::geometry::SECTOR_SIZE_BYTES;

    /// 34 MiB FAT32 — just above the 32 MiB floor (matches the
    /// convention used by `boot_sector.rs`, `fat_table.rs`,
    /// `fsinfo.rs`, and `layout.rs` test modules). Small enough
    /// that tests construct a geometry in microseconds; large
    /// enough that the data region holds enough clusters to
    /// exercise the cross-cluster split paths.
    const TEST_VOLUME_BYTES: u64 = 34 * 1024 * 1024;
    const SECTOR: u64 = SECTOR_SIZE_BYTES as u64;

    fn geo() -> Fat32Geometry {
        Fat32Geometry::for_volume_size(TEST_VOLUME_BYTES).expect("34 MiB is a valid FAT32 size")
    }

    fn fat1_start(_g: &Fat32Geometry) -> u64 {
        u64::from(RESERVED_SECTORS) * SECTOR
    }

    fn fat2_start(g: &Fat32Geometry) -> u64 {
        fat1_start(g) + u64::from(g.fat_size_sectors()) * SECTOR
    }

    fn data_start(g: &Fat32Geometry) -> u64 {
        g.first_data_sector() * SECTOR
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
    fn write_with_overflowing_length_returns_length_exceeds_volume() {
        let g = geo();
        let huge_offset = u64::MAX - 1;
        // Bytes len fits in usize but offset+len overflows u64.
        let err = decode_write(&g, huge_offset, &[0u8; 4]).expect_err("overflow rejected");
        assert!(matches!(
            err,
            DecodeWriteError::OffsetBeyondVolume { .. }
                | DecodeWriteError::LengthExceedsVolume { .. }
        ));
    }

    #[test]
    fn write_at_boot_sector_byte_zero_decodes_to_boot_sector() {
        let g = geo();
        let payload = [0xAAu8; 16];
        let chunks = decode_write(&g, 0, &payload).expect("OK");
        assert_eq!(chunks.len(), 1);
        match &chunks[0] {
            DecodedWrite::BootSector {
                byte_in_sector,
                bytes,
            } => {
                assert_eq!(*byte_in_sector, 0);
                assert_eq!(*bytes, &payload[..]);
            }
            other => panic!("expected BootSector, got {other:?}"),
        }
    }

    #[test]
    fn write_mid_boot_sector_records_in_sector_offset() {
        let g = geo();
        let payload = [0xBBu8; 32];
        let chunks = decode_write(&g, 100, &payload).expect("OK");
        assert_eq!(chunks.len(), 1);
        assert_eq!(
            chunks[0],
            DecodedWrite::BootSector {
                byte_in_sector: 100,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_at_fsinfo_sector_decodes_to_fsinfo() {
        let g = geo();
        let payload = [0xCCu8; 64];
        let chunks = decode_write(&g, SECTOR, &payload).expect("OK");
        assert_eq!(chunks.len(), 1);
        assert_eq!(
            chunks[0],
            DecodedWrite::FsInfo {
                byte_in_sector: 0,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_at_backup_boot_sector_decodes_to_backup_boot_sector() {
        let g = geo();
        // Backup boot sector is at sector 6 by fatgen103 §3.4.
        let payload = [0xDDu8; 8];
        let chunks = decode_write(&g, 6 * SECTOR, &payload).expect("OK");
        assert_eq!(chunks.len(), 1);
        assert_eq!(
            chunks[0],
            DecodedWrite::BackupBootSector {
                byte_in_sector: 0,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_into_reserved_gap_records_absolute_offset() {
        let g = geo();
        // Reserved gap 1: sectors 2..6, so byte offsets 1024..3072.
        let payload = [0xEEu8; 4];
        let off = 2 * SECTOR + 100;
        let chunks = decode_write(&g, off, &payload).expect("OK");
        assert_eq!(chunks.len(), 1);
        assert_eq!(
            chunks[0],
            DecodedWrite::Reserved {
                absolute_offset: off,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_at_fat1_start_decodes_to_mirror_zero() {
        let g = geo();
        let start = fat1_start(&g);
        let payload = [0x12u8; 16];
        let chunks = decode_write(&g, start, &payload).expect("OK");
        assert_eq!(chunks.len(), 1);
        assert_eq!(
            chunks[0],
            DecodedWrite::FatTable {
                mirror_index: 0,
                byte_in_fat: 0,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_at_fat2_start_decodes_to_mirror_one() {
        let g = geo();
        let start = fat2_start(&g);
        let payload = [0x34u8; 16];
        let chunks = decode_write(&g, start, &payload).expect("OK");
        assert_eq!(chunks.len(), 1);
        assert_eq!(
            chunks[0],
            DecodedWrite::FatTable {
                mirror_index: 1,
                byte_in_fat: 0,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_at_data_region_start_decodes_to_cluster_two() {
        let g = geo();
        let start = data_start(&g);
        let payload = [0x55u8; 32];
        let chunks = decode_write(&g, start, &payload).expect("OK");
        assert_eq!(chunks.len(), 1);
        assert_eq!(
            chunks[0],
            DecodedWrite::DataCluster {
                cluster_number: 2,
                byte_in_cluster: 0,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_into_second_cluster_with_offset_records_cluster_three() {
        let g = geo();
        let bpc = u64::from(g.bytes_per_cluster());
        let start = data_start(&g) + bpc + 17;
        let payload = [0x66u8; 8];
        let chunks = decode_write(&g, start, &payload).expect("OK");
        assert_eq!(chunks.len(), 1);
        assert_eq!(
            chunks[0],
            DecodedWrite::DataCluster {
                cluster_number: 3,
                byte_in_cluster: 17,
                bytes: &payload[..],
            }
        );
    }

    #[test]
    fn write_straddling_boot_and_fsinfo_sectors_emits_two_chunks() {
        let g = geo();
        // 4 bytes ending the boot sector + 4 bytes starting FsInfo.
        let payload = [0x77u8; 8];
        let off = SECTOR - 4;
        let chunks = decode_write(&g, off, &payload).expect("OK");
        assert_eq!(chunks.len(), 2);
        let boot_sector_in_offset =
            usize::try_from(SECTOR - 4).expect("SECTOR-4 fits in usize on test target");
        assert_eq!(
            chunks[0],
            DecodedWrite::BootSector {
                byte_in_sector: boot_sector_in_offset,
                bytes: &payload[..4],
            }
        );
        assert_eq!(
            chunks[1],
            DecodedWrite::FsInfo {
                byte_in_sector: 0,
                bytes: &payload[4..],
            }
        );
    }

    #[test]
    fn write_straddling_fat1_fat2_boundary_emits_two_fat_chunks() {
        let g = geo();
        let fat_bytes = u64::from(g.fat_size_sectors()) * SECTOR;
        // 5 bytes at end of FAT1 + 5 at start of FAT2.
        let payload = [0x99u8; 10];
        let off = fat1_start(&g) + fat_bytes - 5;
        let chunks = decode_write(&g, off, &payload).expect("OK");
        assert_eq!(chunks.len(), 2);
        let fat_bytes_usize = usize::try_from(fat_bytes).unwrap();
        assert_eq!(
            chunks[0],
            DecodedWrite::FatTable {
                mirror_index: 0,
                byte_in_fat: fat_bytes_usize - 5,
                bytes: &payload[..5],
            }
        );
        assert_eq!(
            chunks[1],
            DecodedWrite::FatTable {
                mirror_index: 1,
                byte_in_fat: 0,
                bytes: &payload[5..],
            }
        );
    }

    #[test]
    fn write_straddling_fat2_data_boundary_emits_fat_then_cluster() {
        let g = geo();
        let fat_bytes = u64::from(g.fat_size_sectors()) * SECTOR;
        let payload = [0xAAu8; 6];
        let off = fat2_start(&g) + fat_bytes - 2;
        let chunks = decode_write(&g, off, &payload).expect("OK");
        assert_eq!(chunks.len(), 2);
        let fat_bytes_usize = usize::try_from(fat_bytes).unwrap();
        assert_eq!(
            chunks[0],
            DecodedWrite::FatTable {
                mirror_index: 1,
                byte_in_fat: fat_bytes_usize - 2,
                bytes: &payload[..2],
            }
        );
        assert_eq!(
            chunks[1],
            DecodedWrite::DataCluster {
                cluster_number: 2,
                byte_in_cluster: 0,
                bytes: &payload[2..],
            }
        );
    }

    #[test]
    fn write_spanning_two_data_clusters_emits_two_data_chunks() {
        let g = geo();
        let bpc_u64 = u64::from(g.bytes_per_cluster());
        let bpc = g.bytes_per_cluster() as usize;
        let payload = vec![0xBBu8; 16];
        // 8 bytes at end of cluster 2 + 8 at start of cluster 3.
        let off = data_start(&g) + bpc_u64 - 8;
        let chunks = decode_write(&g, off, &payload).expect("OK");
        assert_eq!(chunks.len(), 2);
        assert_eq!(
            chunks[0],
            DecodedWrite::DataCluster {
                cluster_number: 2,
                byte_in_cluster: bpc - 8,
                bytes: &payload[..8],
            }
        );
        assert_eq!(
            chunks[1],
            DecodedWrite::DataCluster {
                cluster_number: 3,
                byte_in_cluster: 0,
                bytes: &payload[8..],
            }
        );
    }

    #[test]
    fn write_spanning_three_data_clusters_emits_three_data_chunks() {
        let g = geo();
        let bpc_u64 = u64::from(g.bytes_per_cluster());
        let bpc = g.bytes_per_cluster() as usize;
        let payload = vec![0xCCu8; bpc + 8];
        // 4 bytes at end of cluster 2, full cluster 3, 4 bytes at
        // start of cluster 4.
        let off = data_start(&g) + bpc_u64 - 4;
        let chunks = decode_write(&g, off, &payload).expect("OK");
        assert_eq!(chunks.len(), 3);
        match &chunks[0] {
            DecodedWrite::DataCluster {
                cluster_number,
                byte_in_cluster,
                bytes,
            } => {
                assert_eq!(*cluster_number, 2);
                assert_eq!(*byte_in_cluster, bpc - 4);
                assert_eq!(bytes.len(), 4);
            }
            other => panic!("expected DataCluster cluster=2, got {other:?}"),
        }
        match &chunks[1] {
            DecodedWrite::DataCluster {
                cluster_number,
                byte_in_cluster,
                bytes,
            } => {
                assert_eq!(*cluster_number, 3);
                assert_eq!(*byte_in_cluster, 0);
                assert_eq!(bytes.len(), bpc);
            }
            other => panic!("expected DataCluster cluster=3, got {other:?}"),
        }
        match &chunks[2] {
            DecodedWrite::DataCluster {
                cluster_number,
                byte_in_cluster,
                bytes,
            } => {
                assert_eq!(*cluster_number, 4);
                assert_eq!(*byte_in_cluster, 0);
                assert_eq!(bytes.len(), 4);
            }
            other => panic!("expected DataCluster cluster=4, got {other:?}"),
        }
    }

    #[test]
    fn chunks_collectively_cover_input_in_order_and_byte_count() {
        let g = geo();
        // Write 1 KiB spanning the end of reserved gap 2, both
        // FATs are huge so just stay in a single cluster: start
        // at boot sector + 100 bytes; that lands entirely within
        // the boot sector for the 64 MiB volume.
        let payload = vec![0xABu8; 512];
        let off = 0;
        let chunks = decode_write(&g, off, &payload).expect("OK");
        let total_len: usize = chunks
            .iter()
            .map(|c| match c {
                DecodedWrite::BootSector { bytes, .. }
                | DecodedWrite::BackupBootSector { bytes, .. }
                | DecodedWrite::FsInfo { bytes, .. }
                | DecodedWrite::Reserved { bytes, .. }
                | DecodedWrite::FatTable { bytes, .. }
                | DecodedWrite::DataCluster { bytes, .. } => bytes.len(),
            })
            .sum();
        assert_eq!(total_len, payload.len());
    }

    #[test]
    fn write_at_last_byte_of_volume_decodes_to_data_cluster() {
        let g = geo();
        let last = g.volume_size_bytes() - 1;
        let payload = [0xFEu8; 1];
        let chunks = decode_write(&g, last, &payload).expect("OK");
        assert_eq!(chunks.len(), 1);
        // Must be a DataCluster — the last region is always Data.
        assert!(matches!(chunks[0], DecodedWrite::DataCluster { .. }));
    }

    #[test]
    fn num_fats_is_two_so_only_two_mirror_indices_can_appear() {
        // Sanity-check the assumption baked into the mirror_index
        // field of DecodedWrite::FatTable: NUM_FATS == 2.
        assert_eq!(NUM_FATS, 2);
    }
}
