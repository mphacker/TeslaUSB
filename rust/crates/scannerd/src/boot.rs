//! exFAT boot sector / BPB parser (read path).
//!
//! `teslausb-core::fs::exfat::boot_sector` only *synthesizes* a boot
//! sector; parsing an existing one is scannerd's job. Field offsets and
//! the derived cluster math are validated against the live gadget image
//! (p1: partition_offset=2048, fat_offset=2048, fat_length=768,
//! cluster_heap_offset=4096, cluster_count=98208, first_root_cluster=4,
//! bytes_per_sector_shift=9, sectors_per_cluster_shift=6, num_fats=1).
#![allow(clippy::doc_markdown)] // BPB field names are not Rust paths

use teslausb_core::fs::exfat::boot_sector::{BOOT_SIGNATURE, FILE_SYSTEM_NAME, JUMP_BOOT};
use teslausb_core::fs::exfat::geometry::{FIRST_CLUSTER_NUMBER, MAX_EXFAT_CLUSTER_COUNT};

use crate::error::ScannerError;
use crate::reader::BlockReader;

/// Minimum legal `BytesPerSectorShift` (512-byte sectors).
const MIN_BYTES_PER_SECTOR_SHIFT: u8 = 9;
/// Maximum legal `BytesPerSectorShift` (4096-byte sectors).
const MAX_BYTES_PER_SECTOR_SHIFT: u8 = 12;
/// Maximum legal `BytesPerSectorShift + SectorsPerClusterShift` sum
/// (32 MiB clusters) per the exFAT spec.
const MAX_CLUSTER_SHIFT_SUM: u8 = 25;
/// Cap on `bytes_per_cluster` we are willing to buffer on the Pi
/// (1 MiB) — larger is rejected as implausible / memory-hostile.
const MAX_SANE_BYTES_PER_CLUSTER: u64 = 1024 * 1024;

/// Parsed, validated exFAT volume geometry needed to read the volume.
///
/// All `*_sectors` fields are in **logical sectors** (`1 <<
/// bytes_per_sector_shift` bytes each). `partition_offset_sectors` is
/// the absolute LBA of the volume start, so absolute byte offsets are
/// `(partition_offset_sectors + relative_sector) * bytes_per_sector`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExfatParams {
    /// Absolute LBA of the volume (== MBR `start_lba`).
    pub partition_offset_sectors: u64,
    /// Volume length in logical sectors.
    pub volume_length_sectors: u64,
    /// FAT start, in sectors from the volume start.
    pub fat_offset_sectors: u32,
    /// FAT length in sectors (one FAT).
    pub fat_length_sectors: u32,
    /// Cluster heap start, in sectors from the volume start.
    pub cluster_heap_offset_sectors: u32,
    /// Number of clusters in the heap.
    pub cluster_count: u32,
    /// First cluster of the root directory.
    pub first_root_cluster: u32,
    /// Volume serial number (part of volume identity).
    pub volume_serial: u32,
    /// `log2(bytes per sector)`.
    pub bytes_per_sector_shift: u8,
    /// `log2(sectors per cluster)`.
    pub sectors_per_cluster_shift: u8,
    /// Number of FATs (1 for Tesla / B-1 volumes).
    pub number_of_fats: u8,
}

impl ExfatParams {
    /// Bytes per logical sector.
    #[must_use]
    pub fn bytes_per_sector(&self) -> u64 {
        1_u64 << self.bytes_per_sector_shift
    }

    /// Bytes per cluster.
    #[must_use]
    pub fn bytes_per_cluster(&self) -> u64 {
        1_u64 << (self.bytes_per_sector_shift + self.sectors_per_cluster_shift)
    }

    /// Largest valid cluster number (`cluster_count + 1`, since the
    /// first usable cluster index is 2).
    #[must_use]
    pub fn max_valid_cluster(&self) -> u32 {
        self.cluster_count
            .saturating_add(FIRST_CLUSTER_NUMBER)
            .saturating_sub(1)
    }

    /// `true` if `cluster` is within `2..=cluster_count+1`.
    #[must_use]
    pub fn is_valid_cluster(&self, cluster: u32) -> bool {
        cluster >= FIRST_CLUSTER_NUMBER && cluster <= self.max_valid_cluster()
    }

    /// Absolute byte offset of the start of `cluster` in the image.
    ///
    /// # Errors
    ///
    /// [`ScannerError::InvalidCluster`] if `cluster` is out of range or
    /// the arithmetic overflows.
    pub fn cluster_byte_offset(&self, cluster: u32) -> Result<u64, ScannerError> {
        if !self.is_valid_cluster(cluster) {
            return Err(ScannerError::InvalidCluster {
                cluster,
                reason: "outside 2..=cluster_count+1",
            });
        }
        let heap_base_sectors = u64::from(self.cluster_heap_offset_sectors)
            .checked_add(self.partition_offset_sectors)
            .ok_or(ScannerError::InvalidCluster {
                cluster,
                reason: "heap base overflow",
            })?;
        let heap_base_bytes = heap_base_sectors
            .checked_mul(self.bytes_per_sector())
            .ok_or(ScannerError::InvalidCluster {
                cluster,
                reason: "heap base byte overflow",
            })?;
        let index = u64::from(cluster - FIRST_CLUSTER_NUMBER);
        let within =
            index
                .checked_mul(self.bytes_per_cluster())
                .ok_or(ScannerError::InvalidCluster {
                    cluster,
                    reason: "cluster offset overflow",
                })?;
        heap_base_bytes
            .checked_add(within)
            .ok_or(ScannerError::InvalidCluster {
                cluster,
                reason: "absolute offset overflow",
            })
    }

    /// Absolute byte offset of the FAT entry for `cluster`.
    ///
    /// # Errors
    ///
    /// [`ScannerError::InvalidCluster`] if out of range or overflow.
    pub fn fat_entry_byte_offset(&self, cluster: u32) -> Result<u64, ScannerError> {
        if !self.is_valid_cluster(cluster) {
            return Err(ScannerError::InvalidCluster {
                cluster,
                reason: "FAT lookup outside valid range",
            });
        }
        let fat_base_sectors = u64::from(self.fat_offset_sectors)
            .checked_add(self.partition_offset_sectors)
            .ok_or(ScannerError::InvalidCluster {
                cluster,
                reason: "FAT base overflow",
            })?;
        let fat_base_bytes = fat_base_sectors
            .checked_mul(self.bytes_per_sector())
            .ok_or(ScannerError::InvalidCluster {
                cluster,
                reason: "FAT base byte overflow",
            })?;
        let within = u64::from(cluster)
            .checked_mul(4)
            .ok_or(ScannerError::InvalidCluster {
                cluster,
                reason: "FAT entry offset overflow",
            })?;
        fat_base_bytes
            .checked_add(within)
            .ok_or(ScannerError::InvalidCluster {
                cluster,
                reason: "FAT entry absolute overflow",
            })
    }
}

/// Read and validate the exFAT boot sector at the volume start.
///
/// `partition_start_lba` is the MBR `start_lba` of the volume.
///
/// # Errors
///
/// * [`ScannerError::Reader`] if the boot sector cannot be read.
/// * [`ScannerError::BootSector`] on any structural/range violation.
pub fn parse_boot_sector<R: BlockReader + ?Sized>(
    reader: &R,
    partition_start_lba: u32,
) -> Result<ExfatParams, ScannerError> {
    // The boot sector is the first 512 bytes of the volume regardless
    // of the (later-parsed) logical sector size; the BPB fields we need
    // all live in the first 113 bytes.
    let base = u64::from(partition_start_lba)
        .checked_mul(512)
        .ok_or(ScannerError::BootSector("partition start overflow"))?;
    let bs = reader.read_vec_at(base, 512)?;

    if bs.get(0..3) != Some(&JUMP_BOOT[..]) {
        return Err(ScannerError::BootSector("bad JUMP_BOOT"));
    }
    if bs.get(3..11) != Some(&FILE_SYSTEM_NAME[..]) {
        return Err(ScannerError::BootSector("FileSystemName != 'EXFAT   '"));
    }
    if bs.get(510..512) != Some(&BOOT_SIGNATURE[..]) {
        return Err(ScannerError::BootSector("missing 0x55AA boot signature"));
    }

    let partition_offset_sectors = read_u64_le(&bs, 64)?;
    let volume_length_sectors = read_u64_le(&bs, 72)?;
    let fat_offset_sectors = read_u32(&bs, 80)?;
    let fat_length_sectors = read_u32(&bs, 84)?;
    let cluster_heap_offset_sectors = read_u32(&bs, 88)?;
    let cluster_count = read_u32(&bs, 92)?;
    let first_root_cluster = read_u32(&bs, 96)?;
    let volume_serial = read_u32(&bs, 100)?;
    let bytes_per_sector_shift = *bs.get(108).ok_or(ScannerError::BootSector("short BPB"))?;
    let sectors_per_cluster_shift = *bs.get(109).ok_or(ScannerError::BootSector("short BPB"))?;
    let number_of_fats = *bs.get(110).ok_or(ScannerError::BootSector("short BPB"))?;

    validate(
        bytes_per_sector_shift,
        sectors_per_cluster_shift,
        number_of_fats,
        cluster_count,
        first_root_cluster,
        volume_length_sectors,
        fat_offset_sectors,
        cluster_heap_offset_sectors,
    )?;

    Ok(ExfatParams {
        partition_offset_sectors,
        volume_length_sectors,
        fat_offset_sectors,
        fat_length_sectors,
        cluster_heap_offset_sectors,
        cluster_count,
        first_root_cluster,
        volume_serial,
        bytes_per_sector_shift,
        sectors_per_cluster_shift,
        number_of_fats,
    })
}

#[allow(clippy::too_many_arguments)]
fn validate(
    bps_shift: u8,
    spc_shift: u8,
    number_of_fats: u8,
    cluster_count: u32,
    first_root_cluster: u32,
    volume_length_sectors: u64,
    fat_offset_sectors: u32,
    cluster_heap_offset_sectors: u32,
) -> Result<(), ScannerError> {
    if !(MIN_BYTES_PER_SECTOR_SHIFT..=MAX_BYTES_PER_SECTOR_SHIFT).contains(&bps_shift) {
        return Err(ScannerError::BootSector(
            "BytesPerSectorShift out of 9..=12",
        ));
    }
    if bps_shift.saturating_add(spc_shift) > MAX_CLUSTER_SHIFT_SUM {
        return Err(ScannerError::BootSector("cluster shift sum > 25"));
    }
    if !(1..=2).contains(&number_of_fats) {
        return Err(ScannerError::BootSector("NumberOfFats not 1 or 2"));
    }
    if cluster_count > MAX_EXFAT_CLUSTER_COUNT {
        return Err(ScannerError::BootSector("ClusterCount too large"));
    }
    let max_valid = cluster_count
        .saturating_add(FIRST_CLUSTER_NUMBER)
        .saturating_sub(1);
    if first_root_cluster < FIRST_CLUSTER_NUMBER || first_root_cluster > max_valid {
        return Err(ScannerError::BootSector("root cluster out of range"));
    }
    let bytes_per_cluster = 1_u64 << (bps_shift + spc_shift);
    if bytes_per_cluster > MAX_SANE_BYTES_PER_CLUSTER {
        return Err(ScannerError::BootSector("implausibly large cluster size"));
    }
    if u64::from(cluster_heap_offset_sectors) >= volume_length_sectors
        || u64::from(fat_offset_sectors) >= volume_length_sectors
    {
        return Err(ScannerError::BootSector("FAT/heap offset beyond volume"));
    }
    Ok(())
}

fn read_u32(buf: &[u8], off: usize) -> Result<u32, ScannerError> {
    let arr: [u8; 4] = buf
        .get(off..off + 4)
        .ok_or(ScannerError::BootSector("short BPB (u32)"))?
        .try_into()
        .map_err(|_| ScannerError::BootSector("u32 slice"))?;
    Ok(u32::from_le_bytes(arr))
}

fn read_u64_le(buf: &[u8], off: usize) -> Result<u64, ScannerError> {
    let arr: [u8; 8] = buf
        .get(off..off + 8)
        .ok_or(ScannerError::BootSector("short BPB (u64)"))?
        .try_into()
        .map_err(|_| ScannerError::BootSector("u64 slice"))?;
    Ok(u64::from_le_bytes(arr))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::indexing_slicing, clippy::panic)]
mod tests {
    use super::*;
    use crate::reader::SliceReader;

    /// Build a boot sector mirroring the real p1 BPB, placed at
    /// `start_lba * 512` in a backing image large enough to hold it.
    fn image_with_real_bpb() -> (Vec<u8>, u32) {
        let start_lba = 2048_u32;
        let mut img = vec![0_u8; (start_lba as usize) * 512 + 512];
        let base = (start_lba as usize) * 512;
        let bs = &mut img[base..base + 512];
        bs[0..3].copy_from_slice(&[0xEB, 0x76, 0x90]);
        bs[3..11].copy_from_slice(b"EXFAT   ");
        bs[64..72].copy_from_slice(&2048_u64.to_le_bytes());
        bs[72..80].copy_from_slice(&6_289_408_u64.to_le_bytes());
        bs[80..84].copy_from_slice(&2048_u32.to_le_bytes());
        bs[84..88].copy_from_slice(&768_u32.to_le_bytes());
        bs[88..92].copy_from_slice(&4096_u32.to_le_bytes());
        bs[92..96].copy_from_slice(&98_208_u32.to_le_bytes());
        bs[96..100].copy_from_slice(&4_u32.to_le_bytes());
        bs[100..104].copy_from_slice(&0xEEF7_E06A_u32.to_le_bytes());
        bs[108] = 9;
        bs[109] = 6;
        bs[110] = 1;
        bs[510] = 0x55;
        bs[511] = 0xAA;
        (img, start_lba)
    }

    #[test]
    fn parses_real_bpb() {
        let (img, lba) = image_with_real_bpb();
        let reader = SliceReader::new(img);
        let p = parse_boot_sector(&reader, lba).unwrap();
        assert_eq!(p.partition_offset_sectors, 2048);
        assert_eq!(p.volume_length_sectors, 6_289_408);
        assert_eq!(p.fat_offset_sectors, 2048);
        assert_eq!(p.cluster_heap_offset_sectors, 4096);
        assert_eq!(p.cluster_count, 98_208);
        assert_eq!(p.first_root_cluster, 4);
        assert_eq!(p.bytes_per_sector_shift, 9);
        assert_eq!(p.sectors_per_cluster_shift, 6);
        assert_eq!(p.bytes_per_cluster(), 32_768);
        assert_eq!(p.bytes_per_sector(), 512);
        // Root cluster 4 byte offset = (4096 + 2048)*512 + (4-2)*32768.
        let expected = (4096 + 2048) * 512 + (4 - 2) * 32_768;
        assert_eq!(p.cluster_byte_offset(4).unwrap(), expected);
    }

    #[test]
    fn rejects_bad_fs_name() {
        let (mut img, lba) = image_with_real_bpb();
        let base = (lba as usize) * 512;
        img[base + 3] = b'X';
        let reader = SliceReader::new(img);
        assert!(matches!(
            parse_boot_sector(&reader, lba),
            Err(ScannerError::BootSector(_))
        ));
    }

    #[test]
    fn rejects_cluster_zero_and_one() {
        let (img, lba) = image_with_real_bpb();
        let reader = SliceReader::new(img);
        let p = parse_boot_sector(&reader, lba).unwrap();
        assert!(p.cluster_byte_offset(0).is_err());
        assert!(p.cluster_byte_offset(1).is_err());
        assert!(p.cluster_byte_offset(p.max_valid_cluster() + 1).is_err());
        assert!(p.cluster_byte_offset(2).is_ok());
    }
}
