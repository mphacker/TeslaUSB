//! MBR partition-table parser (read path).
//!
//! `teslausb-core::fs::mbr` only *renders* an MBR (the provisioning
//! path); reading an existing table is scannerd's job. We parse the
//! classic 64-byte primary partition table at offset 446 and the
//! `0x55AA` boot signature, validated against the real gadget image
//! (p1 type=0x07 start_lba=2048; p2 type=0x07 start_lba=6291456).
#![allow(clippy::doc_markdown)] // MBR field names are not Rust paths

use teslausb_core::fs::mbr::PARTITION_TYPE_EXFAT;

use crate::error::ScannerError;
use crate::reader::BlockReader;

/// Byte offset of the first primary partition entry within the MBR.
const PARTITION_TABLE_OFFSET: usize = 446;
/// Size of one primary partition entry, in bytes.
const PARTITION_ENTRY_SIZE: usize = 16;
/// Number of primary partition entries.
const PARTITION_ENTRY_COUNT: usize = 4;
/// Offset of the `0x55AA` boot signature within the 512-byte sector.
const BOOT_SIGNATURE_OFFSET: usize = 510;

/// One primary MBR partition entry that scannerd cares about.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PartitionEntry {
    /// Table slot, 0..=3 (informational; part of file identity).
    pub slot: u8,
    /// Partition type byte (`0x07` = exFAT/NTFS for Tesla volumes).
    pub part_type: u8,
    /// Starting LBA (512-byte sectors) of the partition.
    pub start_lba: u32,
    /// Partition length in 512-byte sectors.
    pub num_sectors: u32,
}

impl PartitionEntry {
    /// `true` if this entry is a non-empty exFAT-typed partition.
    #[must_use]
    pub fn is_exfat(&self) -> bool {
        self.part_type == PARTITION_TYPE_EXFAT && self.num_sectors > 0
    }
}

/// Read and parse the MBR primary partition table.
///
/// Returns all four slots (callers select exFAT volumes and confirm
/// each by parsing its boot sector, rather than trusting the type
/// byte / slot order — `0x07` is also NTFS).
///
/// # Errors
///
/// * [`ScannerError::Reader`] if sector 0 cannot be read.
/// * [`ScannerError::Mbr`] if the boot signature is missing.
pub fn parse_mbr<R: BlockReader + ?Sized>(
    reader: &R,
) -> Result<[PartitionEntry; PARTITION_ENTRY_COUNT], ScannerError> {
    let sector = reader.read_vec_at(0, 512)?;

    let sig = sector
        .get(BOOT_SIGNATURE_OFFSET..BOOT_SIGNATURE_OFFSET + 2)
        .ok_or(ScannerError::Mbr("sector 0 shorter than 512 bytes"))?;
    if sig != [0x55, 0xAA] {
        return Err(ScannerError::Mbr("missing 0x55AA boot signature"));
    }

    let mut entries = [PartitionEntry {
        slot: 0,
        part_type: 0,
        start_lba: 0,
        num_sectors: 0,
    }; PARTITION_ENTRY_COUNT];

    for (i, entry) in entries.iter_mut().enumerate() {
        let base = PARTITION_TABLE_OFFSET + i * PARTITION_ENTRY_SIZE;
        let raw = sector
            .get(base..base + PARTITION_ENTRY_SIZE)
            .ok_or(ScannerError::Mbr("partition entry out of range"))?;
        // raw[0] = boot flag; raw[1..4] = CHS start; raw[4] = type;
        // raw[5..8] = CHS end; raw[8..12] = start_lba LE; raw[12..16]
        // = num_sectors LE.
        let part_type = raw.get(4).copied().unwrap_or(0);
        let start_lba = read_u32_le(raw, 8).unwrap_or(0);
        let num_sectors = read_u32_le(raw, 12).unwrap_or(0);
        *entry = PartitionEntry {
            slot: u8::try_from(i).unwrap_or(0),
            part_type,
            start_lba,
            num_sectors,
        };
    }

    Ok(entries)
}

/// Read a little-endian `u32` at byte `off` within `buf`, if in range.
fn read_u32_le(buf: &[u8], off: usize) -> Option<u32> {
    let bytes = buf.get(off..off + 4)?;
    let arr: [u8; 4] = bytes.try_into().ok()?;
    Some(u32::from_le_bytes(arr))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::indexing_slicing, clippy::panic)]
mod tests {
    use super::*;
    use crate::reader::SliceReader;

    /// Build a 512-byte MBR with the two real Tesla partitions.
    fn real_mbr() -> Vec<u8> {
        let mut s = vec![0_u8; 512];
        // Entry 1 @446: type 0x07, start_lba 2048, num 6289408.
        let e1 = 446;
        s[e1 + 4] = 0x07;
        s[e1 + 8..e1 + 12].copy_from_slice(&2048_u32.to_le_bytes());
        s[e1 + 12..e1 + 16].copy_from_slice(&6_289_408_u32.to_le_bytes());
        // Entry 2 @462: type 0x07, start_lba 6291456, num 2097152.
        let e2 = 446 + 16;
        s[e2 + 4] = 0x07;
        s[e2 + 8..e2 + 12].copy_from_slice(&6_291_456_u32.to_le_bytes());
        s[e2 + 12..e2 + 16].copy_from_slice(&2_097_152_u32.to_le_bytes());
        s[510] = 0x55;
        s[511] = 0xAA;
        s
    }

    #[test]
    fn parses_real_two_partition_layout() {
        let reader = SliceReader::new(real_mbr());
        let parts = parse_mbr(&reader).unwrap();
        assert!(parts[0].is_exfat());
        assert_eq!(parts[0].start_lba, 2048);
        assert_eq!(parts[0].num_sectors, 6_289_408);
        assert!(parts[1].is_exfat());
        assert_eq!(parts[1].start_lba, 6_291_456);
        assert_eq!(parts[1].num_sectors, 2_097_152);
        assert!(!parts[2].is_exfat());
        assert!(!parts[3].is_exfat());
    }

    #[test]
    fn rejects_missing_signature() {
        let mut img = real_mbr();
        img[510] = 0;
        let reader = SliceReader::new(img);
        assert!(matches!(parse_mbr(&reader), Err(ScannerError::Mbr(_))));
    }

    #[test]
    fn rejects_short_image() {
        let reader = SliceReader::new(vec![0_u8; 100]);
        assert!(matches!(parse_mbr(&reader), Err(ScannerError::Reader(_))));
    }
}
