//! `exFAT` main boot region synthesizer.
//!
//! Phase 2.8 of the B-1 rewrite. This module takes an
//! [`ExfatGeometry`] + a 32-bit volume serial and produces the
//! on-disk **main boot region** that lives at byte offset `0` of
//! an `exFAT` volume. The output is a fixed-size 6144-byte buffer
//! (12 × [`SECTOR_SIZE_BYTES`]) suitable for serving directly from
//! the Phase 2.11 read dispatcher or copying byte-for-byte to the
//! *backup* boot region at sectors 12..24 (`exFAT` spec §3.2).
//!
//! ## Region layout (`exFAT` spec §3.1)
//!
//! ```text
//! Sector  Field                       Source
//! 0       Main Boot Sector            this module — see §3.1.1
//! 1..9    Main Extended Boot Sectors  zero boot code + 0xAA550000
//!                                     extended boot signature
//! 9       Main OEM Parameters         zero-filled (no OEM records)
//! 10      Main Reserved               zero-filled
//! 11      Main Boot Checksum          128 × u32 LE of the boot
//!                                     checksum computed over
//!                                     sectors 0..11 (excluding
//!                                     VolumeFlags + PercentInUse)
//! ```
//!
//! ## Main boot sector field layout (`exFAT` spec §3.1.1)
//!
//! ```text
//! Offset Size Field                          Source / value
//! 0x000   3   JumpBoot                       fixed: EB 76 90
//! 0x003   8   FileSystemName                 fixed: "EXFAT   "
//! 0x00B  53   MustBeZero                     fixed: zero
//! 0x040   8   PartitionOffset (u64 LE)       fixed: 0 (whole-disk)
//! 0x048   8   VolumeLength (u64 LE)          geometry.total_sectors()
//! 0x050   4   FatOffset (u32 LE)             geometry.fat_offset_sectors()
//! 0x054   4   FatLength (u32 LE)             geometry.fat_length_sectors()
//! 0x058   4   ClusterHeapOffset (u32 LE)     geometry.cluster_heap_offset_sectors()
//! 0x05C   4   ClusterCount (u32 LE)          geometry.cluster_count()
//! 0x060   4   FirstClusterOfRootDirectory    geometry.first_root_directory_cluster()
//! 0x064   4   VolumeSerialNumber (u32 LE)    caller-supplied
//! 0x068   2   FileSystemRevision (u16 LE)    fixed: 0x0100 (v1.00)
//! 0x06A   2   VolumeFlags (u16 LE)           fixed: 0
//! 0x06C   1   BytesPerSectorShift            fixed: 9
//! 0x06D   1   SectorsPerClusterShift         geometry.sectors_per_cluster_shift()
//! 0x06E   1   NumberOfFats                   fixed: 1
//! 0x06F   1   DriveSelect                    fixed: 0x80
//! 0x070   1   PercentInUse                   fixed: 0xFF (unknown)
//! 0x071   7   Reserved                       fixed: zero
//! 0x078 390   BootCode                       fixed: zero
//! 0x1FE   2   BootSignature                  fixed: 0x55 0xAA
//! ```
//!
//! ## Why no boot code?
//!
//! The B-1 USB gadget is never booted from. The 390-byte
//! `BootCode` region (`exFAT` spec §3.1.16) is zero-filled and the
//! 8 extended boot sectors (`exFAT` spec §3.1.17–18) carry zero
//! boot code with the 4-byte extended boot signature `0xAA550000`
//! at their last 4 bytes — the minimal valid extended boot sector
//! per the spec.
//!
//! ## Boot checksum
//!
//! The checksum (`exFAT` spec §3.1.19) is a 32-bit value computed
//! over every byte of sectors 0..11 (inclusive) with two
//! exceptions: bytes `0x6A` and `0x6B` (`VolumeFlags`) and byte
//! `0x70` (`PercentInUse`) of the main boot sector are skipped
//! because they are runtime-mutable fields. The checksum sector
//! contains 128 copies of that single 32-bit value, little-endian.
//!
//! ## Spec anchor
//!
//! Microsoft `exFAT` File System Specification v1.00 (August 27,
//! 2019). §3.1 Main Boot Region. §3.1.1 Main Boot Sector.

use core::fmt;

use crate::fs::exfat::geometry::{
    BOOT_REGION_SECTORS, BYTES_PER_SECTOR_SHIFT, ExfatGeometry, NUMBER_OF_FATS,
};
use crate::fs::geometry::{Geometry, SECTOR_SIZE_BYTES};

/// Byte width of one `exFAT` boot sector (always 512).
pub const BOOT_SECTOR_SIZE_BYTES: usize = SECTOR_SIZE_BYTES as usize;

/// Byte width of the full main boot region — 12 × 512.
pub const BOOT_REGION_SIZE_BYTES: usize = (BOOT_REGION_SECTORS as usize) * BOOT_SECTOR_SIZE_BYTES;

/// `JumpBoot` value at offset `0x000` (`exFAT` spec §3.1.2).
pub const JUMP_BOOT: [u8; 3] = [0xEB, 0x76, 0x90];

/// `FileSystemName` value at offset `0x003` — 8 ASCII bytes,
/// space-padded (`exFAT` spec §3.1.3).
pub const FILE_SYSTEM_NAME: [u8; 8] = *b"EXFAT   ";

/// `FileSystemRevision` value at offset `0x068` — `0x0100` =
/// v1.00 (`exFAT` spec §3.1.8).
pub const FILE_SYSTEM_REVISION: u16 = 0x0100;

/// `DriveSelect` value at offset `0x06F` — `0x80` = first fixed
/// disk in BIOS / INT 13h convention (`exFAT` spec §3.1.13).
pub const DRIVE_SELECT: u8 = 0x80;

/// `PercentInUse` sentinel meaning "unknown / not computed"
/// (`exFAT` spec §3.1.14).
pub const PERCENT_IN_USE_UNKNOWN: u8 = 0xFF;

/// `BootSignature` value at the last 2 bytes of the boot sector
/// (`exFAT` spec §3.1.15).
pub const BOOT_SIGNATURE: [u8; 2] = [0x55, 0xAA];

/// `ExtendedBootSignature` value at the last 4 bytes of each
/// extended boot sector (`exFAT` spec §3.1.17).
pub const EXTENDED_BOOT_SIGNATURE_LE: [u8; 4] = [0x00, 0x00, 0x55, 0xAA];

/// Sector index of the OEM Parameters sector within the boot
/// region (`exFAT` spec §3.1).
pub const OEM_PARAMETERS_SECTOR_INDEX: usize = 9;

/// Sector index of the reserved sector within the boot region.
pub const RESERVED_SECTOR_INDEX: usize = 10;

/// Sector index of the boot checksum sector within the boot
/// region.
pub const BOOT_CHECKSUM_SECTOR_INDEX: usize = 11;

/// Compile-time invariants — every byte index this module writes
/// must fit in a sector and the field offsets must match the spec.
const _: () = {
    assert!(BOOT_REGION_SIZE_BYTES == 12 * 512);
    assert!(BOOT_SECTOR_SIZE_BYTES == 512);
    assert!(OEM_PARAMETERS_SECTOR_INDEX < BOOT_REGION_SECTORS as usize);
    assert!(RESERVED_SECTOR_INDEX < BOOT_REGION_SECTORS as usize);
    assert!(BOOT_CHECKSUM_SECTOR_INDEX < BOOT_REGION_SECTORS as usize);
};

/// Errors returned by [`synthesize`].
#[derive(Debug, PartialEq, Eq)]
pub enum BootSectorError {
    /// The geometry's `total_sectors` does not fit in the 64-bit
    /// `VolumeLength` field. The `exFAT` spec gives this field 8
    /// bytes, so the only way to hit this is for a geometry that
    /// reports `u64::MAX` or close to it — currently impossible
    /// since [`ExfatGeometry`] caps the volume size at
    /// [`crate::fs::exfat::geometry::MAX_VOLUME_SIZE_BYTES`]. This
    /// variant exists as defense-in-depth.
    VolumeLengthOverflow {
        /// The geometry's reported total sector count.
        total_sectors: u64,
    },
}

impl fmt::Display for BootSectorError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::VolumeLengthOverflow { total_sectors } => write!(
                f,
                "geometry reports {total_sectors} total sectors; exFAT VolumeLength is a 64-bit field and this value would overflow on encode"
            ),
        }
    }
}

impl core::error::Error for BootSectorError {}

/// Synthesize the full 12-sector `exFAT` main boot region.
///
/// The buffer's layout follows `exFAT` spec §3.1; see the
/// module-level doc for the sector breakdown. The output is
/// byte-for-byte identical to what the spec requires for the
/// **backup** boot region at sectors 12..24, so the Phase 2.11
/// dispatcher can serve both regions from one buffer.
///
/// `volume_serial` is written into the `VolumeSerialNumber` field
/// (offset `0x064`) in little-endian order. The caller is
/// responsible for choosing a value — `mkfs.exfat` derives one
/// from the current time; B-1's installer is expected to pick a
/// stable per-volume value so the serial survives synthesizer
/// restarts.
///
/// # Errors
///
/// * [`BootSectorError::VolumeLengthOverflow`] is currently
///   unreachable because [`ExfatGeometry`] caps the volume size
///   below `u64::MAX`. The variant remains as a documented
///   defense against any future geometry implementation that
///   relaxes that cap.
pub fn synthesize(
    geometry: &ExfatGeometry,
    volume_serial: u32,
) -> Result<[u8; BOOT_REGION_SIZE_BYTES], BootSectorError> {
    let mut buf = [0_u8; BOOT_REGION_SIZE_BYTES];
    write_main_boot_sector(&mut buf, geometry, volume_serial)?;
    write_extended_boot_sectors(&mut buf);
    // OEM Parameters sector (sector 9): zero-filled (no OEM
    // records). Already zero from initialisation; no work needed.
    // Reserved sector (sector 10): zero-filled. Already zero.
    write_boot_checksum_sector(&mut buf);
    Ok(buf)
}

/// Write the 512-byte Main Boot Sector at sector 0 of `buf`.
#[allow(clippy::indexing_slicing)] // compile-time constant offsets per spec
fn write_main_boot_sector(
    buf: &mut [u8; BOOT_REGION_SIZE_BYTES],
    geometry: &ExfatGeometry,
    volume_serial: u32,
) -> Result<(), BootSectorError> {
    let sector = &mut buf[0..BOOT_SECTOR_SIZE_BYTES];

    // 0x000 JumpBoot
    sector[0x000..0x003].copy_from_slice(&JUMP_BOOT);
    // 0x003 FileSystemName
    sector[0x003..0x00B].copy_from_slice(&FILE_SYSTEM_NAME);
    // 0x00B..0x040 MustBeZero — already zero from init.

    // 0x040 PartitionOffset (u64) — 0 for non-partitioned media.
    sector[0x040..0x048].copy_from_slice(&0_u64.to_le_bytes());
    // 0x048 VolumeLength (u64)
    sector[0x048..0x050].copy_from_slice(&geometry.total_sectors().to_le_bytes());
    // 0x050 FatOffset (u32)
    sector[0x050..0x054].copy_from_slice(&geometry.fat_offset_sectors().to_le_bytes());
    // 0x054 FatLength (u32)
    sector[0x054..0x058].copy_from_slice(&geometry.fat_length_sectors().to_le_bytes());
    // 0x058 ClusterHeapOffset (u32)
    sector[0x058..0x05C].copy_from_slice(&geometry.cluster_heap_offset_sectors().to_le_bytes());
    // 0x05C ClusterCount (u32)
    sector[0x05C..0x060].copy_from_slice(&geometry.cluster_count().to_le_bytes());
    // 0x060 FirstClusterOfRootDirectory (u32)
    sector[0x060..0x064].copy_from_slice(&geometry.first_root_directory_cluster().to_le_bytes());
    // 0x064 VolumeSerialNumber (u32)
    sector[0x064..0x068].copy_from_slice(&volume_serial.to_le_bytes());
    // 0x068 FileSystemRevision (u16)
    sector[0x068..0x06A].copy_from_slice(&FILE_SYSTEM_REVISION.to_le_bytes());
    // 0x06A VolumeFlags (u16) — 0 (clean unmount, no media failure).
    sector[0x06A..0x06C].copy_from_slice(&0_u16.to_le_bytes());
    // 0x06C BytesPerSectorShift
    sector[0x06C] = BYTES_PER_SECTOR_SHIFT;
    // 0x06D SectorsPerClusterShift
    sector[0x06D] = geometry.sectors_per_cluster_shift();
    // 0x06E NumberOfFats
    sector[0x06E] = NUMBER_OF_FATS;
    // 0x06F DriveSelect
    sector[0x06F] = DRIVE_SELECT;
    // 0x070 PercentInUse
    sector[0x070] = PERCENT_IN_USE_UNKNOWN;
    // 0x071..0x078 Reserved (7 bytes) — already zero.
    // 0x078..0x1FE BootCode (390 bytes) — already zero (not bootable).
    // 0x1FE..0x200 BootSignature
    sector[0x1FE..0x200].copy_from_slice(&BOOT_SIGNATURE);

    // Defensive: every geometry that constructed successfully has
    // a total_sectors that fits in u64 by definition (we just
    // serialised it above). The error variant exists for future
    // geometry implementations that might tighten or relax that
    // invariant — see `BootSectorError::VolumeLengthOverflow`.
    if geometry.total_sectors() == u64::MAX {
        return Err(BootSectorError::VolumeLengthOverflow {
            total_sectors: geometry.total_sectors(),
        });
    }
    Ok(())
}

/// Write the 8 extended boot sectors at sectors 1..9 of `buf`.
///
/// Each extended boot sector is 512 bytes of (zero) boot code
/// followed by the 4-byte `ExtendedBootSignature` (`0xAA550000`
/// little-endian = `[0x00, 0x00, 0x55, 0xAA]`) at the end.
#[allow(clippy::indexing_slicing)] // compile-time constant offsets per spec
fn write_extended_boot_sectors(buf: &mut [u8; BOOT_REGION_SIZE_BYTES]) {
    // Sectors 1..=8 are the 8 extended boot sectors.
    for sector_index in 1..=8_usize {
        let sector_start = sector_index * BOOT_SECTOR_SIZE_BYTES;
        let sig_start = sector_start + BOOT_SECTOR_SIZE_BYTES - 4;
        buf[sig_start..sig_start + 4].copy_from_slice(&EXTENDED_BOOT_SIGNATURE_LE);
    }
}

/// Write the boot checksum sector (sector 11) of `buf`.
///
/// `exFAT` spec §3.1.19: compute a 32-bit checksum across every
/// byte of sectors 0..=10 with the following exceptions in sector
/// 0:
///
/// * Bytes `0x6A` and `0x6B` (`VolumeFlags`) — skipped.
/// * Byte `0x70` (`PercentInUse`) — skipped.
///
/// Then fill sector 11 with 128 little-endian copies of the
/// resulting `u32` (128 × 4 = 512 bytes).
#[allow(clippy::indexing_slicing)] // compile-time constant offsets per spec
fn write_boot_checksum_sector(buf: &mut [u8; BOOT_REGION_SIZE_BYTES]) {
    let checksum =
        compute_boot_checksum(&buf[..BOOT_CHECKSUM_SECTOR_INDEX * BOOT_SECTOR_SIZE_BYTES]);
    let checksum_le = checksum.to_le_bytes();
    let sector_start = BOOT_CHECKSUM_SECTOR_INDEX * BOOT_SECTOR_SIZE_BYTES;
    let mut offset = sector_start;
    while offset < sector_start + BOOT_SECTOR_SIZE_BYTES {
        buf[offset..offset + 4].copy_from_slice(&checksum_le);
        offset += 4;
    }
}

/// Compute the `exFAT` boot checksum over the first 11 sectors.
///
/// Algorithm (`exFAT` spec §3.4):
///
/// ```text
/// checksum = 0
/// for index in 0..(11 * 512):
///     if index == 0x6A || index == 0x6B || index == 0x70:
///         continue   // skip VolumeFlags + PercentInUse
///     checksum = ((checksum >> 1) | (checksum << 31)) + byte
///     checksum &= 0xFFFFFFFF
/// ```
///
/// `prefix` must be the first 11 sectors of the boot region
/// (`11 * 512 = 5632` bytes).
fn compute_boot_checksum(prefix: &[u8]) -> u32 {
    let mut checksum: u32 = 0;
    for (index, &byte) in prefix.iter().enumerate() {
        if index == 0x6A || index == 0x6B || index == 0x70 {
            continue;
        }
        checksum = checksum.rotate_right(1).wrapping_add(u32::from(byte));
    }
    checksum
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
    use crate::fs::exfat::geometry::{
        BACKUP_BOOT_REGION_OFFSET_SECTORS, FAT_OFFSET_SECTORS, FIRST_ROOT_DIRECTORY_CLUSTER,
    };

    const MIB: u64 = 1024 * 1024;
    const GIB: u64 = 1024 * 1024 * 1024;
    const VOL_SERIAL: u32 = 0xDEAD_BEEF;

    fn synth_4gib() -> ([u8; BOOT_REGION_SIZE_BYTES], ExfatGeometry) {
        let geo = ExfatGeometry::for_volume_size(4 * GIB).expect("valid 4 GiB exFAT geometry");
        let buf = synthesize(&geo, VOL_SERIAL).expect("valid synthesis");
        (buf, geo)
    }

    // ---------- Output sizing ----------

    #[test]
    fn output_is_exactly_12_sectors() {
        let (buf, _) = synth_4gib();
        assert_eq!(buf.len(), 12 * 512);
    }

    #[test]
    fn boot_region_size_constant_matches_array_size() {
        let (buf, _) = synth_4gib();
        assert_eq!(buf.len(), BOOT_REGION_SIZE_BYTES);
    }

    // ---------- Main boot sector fixed bytes ----------

    #[test]
    fn jump_boot_is_eb_76_90() {
        let (buf, _) = synth_4gib();
        assert_eq!(&buf[0x000..0x003], &[0xEB, 0x76, 0x90]);
    }

    #[test]
    fn file_system_name_is_exfat_padded() {
        let (buf, _) = synth_4gib();
        assert_eq!(&buf[0x003..0x00B], b"EXFAT   ");
    }

    #[test]
    fn must_be_zero_region_is_zero() {
        let (buf, _) = synth_4gib();
        for (i, &b) in buf[0x00B..0x040].iter().enumerate() {
            assert_eq!(b, 0, "MustBeZero byte at +{i:#04x} = {b:#04x}");
        }
    }

    #[test]
    fn partition_offset_is_zero() {
        let (buf, _) = synth_4gib();
        assert_eq!(&buf[0x040..0x048], &[0; 8]);
    }

    #[test]
    fn volume_length_matches_geometry_total_sectors() {
        let (buf, geo) = synth_4gib();
        let mut expected = [0_u8; 8];
        expected.copy_from_slice(&geo.total_sectors().to_le_bytes());
        assert_eq!(&buf[0x048..0x050], &expected);
    }

    #[test]
    fn fat_offset_matches_geometry() {
        let (buf, geo) = synth_4gib();
        assert_eq!(&buf[0x050..0x054], &geo.fat_offset_sectors().to_le_bytes());
    }

    #[test]
    fn fat_length_matches_geometry() {
        let (buf, geo) = synth_4gib();
        assert_eq!(&buf[0x054..0x058], &geo.fat_length_sectors().to_le_bytes());
    }

    #[test]
    fn cluster_heap_offset_matches_geometry() {
        let (buf, geo) = synth_4gib();
        assert_eq!(
            &buf[0x058..0x05C],
            &geo.cluster_heap_offset_sectors().to_le_bytes()
        );
    }

    #[test]
    fn cluster_count_matches_geometry() {
        let (buf, geo) = synth_4gib();
        assert_eq!(&buf[0x05C..0x060], &geo.cluster_count().to_le_bytes());
    }

    #[test]
    fn first_root_cluster_is_2() {
        let (buf, _) = synth_4gib();
        let expected = FIRST_ROOT_DIRECTORY_CLUSTER.to_le_bytes();
        assert_eq!(&buf[0x060..0x064], &expected);
    }

    #[test]
    fn volume_serial_is_written_le() {
        let (buf, _) = synth_4gib();
        assert_eq!(&buf[0x064..0x068], &VOL_SERIAL.to_le_bytes());
    }

    #[test]
    fn file_system_revision_is_v1_00() {
        let (buf, _) = synth_4gib();
        // 0x0100 LE = [0x00, 0x01]
        assert_eq!(&buf[0x068..0x06A], &[0x00, 0x01]);
    }

    #[test]
    fn volume_flags_are_zero() {
        let (buf, _) = synth_4gib();
        assert_eq!(&buf[0x06A..0x06C], &[0x00, 0x00]);
    }

    #[test]
    fn bytes_per_sector_shift_is_9() {
        let (buf, _) = synth_4gib();
        assert_eq!(buf[0x06C], 9);
    }

    #[test]
    fn sectors_per_cluster_shift_matches_geometry() {
        let (buf, geo) = synth_4gib();
        assert_eq!(buf[0x06D], geo.sectors_per_cluster_shift());
    }

    #[test]
    fn number_of_fats_is_1() {
        let (buf, _) = synth_4gib();
        assert_eq!(buf[0x06E], 1);
    }

    #[test]
    fn drive_select_is_0x80() {
        let (buf, _) = synth_4gib();
        assert_eq!(buf[0x06F], 0x80);
    }

    #[test]
    fn percent_in_use_is_0xff() {
        let (buf, _) = synth_4gib();
        assert_eq!(buf[0x070], 0xFF);
    }

    #[test]
    fn reserved_after_percent_in_use_is_zero() {
        let (buf, _) = synth_4gib();
        for (i, &b) in buf[0x071..0x078].iter().enumerate() {
            assert_eq!(b, 0, "Reserved byte at +{i} = {b:#04x}");
        }
    }

    #[test]
    fn boot_code_area_is_zero() {
        let (buf, _) = synth_4gib();
        for (i, &b) in buf[0x078..0x1FE].iter().enumerate() {
            assert_eq!(b, 0, "BootCode byte at +{i} = {b:#04x}");
        }
    }

    #[test]
    fn boot_signature_is_55_aa() {
        let (buf, _) = synth_4gib();
        assert_eq!(&buf[0x1FE..0x200], &[0x55, 0xAA]);
    }

    // ---------- Extended boot sectors ----------

    #[test]
    fn extended_boot_sectors_have_valid_signature() {
        let (buf, _) = synth_4gib();
        for sector_index in 1..=8_usize {
            let sig_start = sector_index * 512 + 508;
            assert_eq!(
                &buf[sig_start..sig_start + 4],
                &EXTENDED_BOOT_SIGNATURE_LE,
                "extended boot sector {sector_index} signature mismatch"
            );
        }
    }

    #[test]
    fn extended_boot_sectors_have_zero_boot_code() {
        let (buf, _) = synth_4gib();
        for sector_index in 1..=8_usize {
            let body_start = sector_index * 512;
            let body_end = body_start + 508;
            for (i, &b) in buf[body_start..body_end].iter().enumerate() {
                assert_eq!(
                    b, 0,
                    "extended boot sector {sector_index} byte +{i} = {b:#04x} (expected zero)"
                );
            }
        }
    }

    // ---------- OEM Parameters / Reserved sector ----------

    #[test]
    fn oem_parameters_sector_is_zero() {
        let (buf, _) = synth_4gib();
        let start = OEM_PARAMETERS_SECTOR_INDEX * 512;
        for (i, &b) in buf[start..start + 512].iter().enumerate() {
            assert_eq!(b, 0, "OEM parameters byte +{i} = {b:#04x}");
        }
    }

    #[test]
    fn reserved_sector_is_zero() {
        let (buf, _) = synth_4gib();
        let start = RESERVED_SECTOR_INDEX * 512;
        for (i, &b) in buf[start..start + 512].iter().enumerate() {
            assert_eq!(b, 0, "Reserved sector byte +{i} = {b:#04x}");
        }
    }

    // ---------- Boot checksum sector ----------

    #[test]
    fn checksum_sector_is_128_copies_of_one_u32() {
        let (buf, _) = synth_4gib();
        let start = BOOT_CHECKSUM_SECTOR_INDEX * 512;
        let first: [u8; 4] = buf[start..start + 4].try_into().expect("4 bytes");
        for chunk_index in 0..128_usize {
            let off = start + chunk_index * 4;
            assert_eq!(
                &buf[off..off + 4],
                &first,
                "checksum copy {chunk_index} differs from copy 0"
            );
        }
    }

    #[test]
    fn checksum_matches_independent_implementation() {
        let (buf, _) = synth_4gib();
        let start = BOOT_CHECKSUM_SECTOR_INDEX * 512;
        let written: u32 = u32::from_le_bytes(buf[start..start + 4].try_into().expect("4 bytes"));
        let recomputed = reference_checksum(&buf[..BOOT_CHECKSUM_SECTOR_INDEX * 512]);
        assert_eq!(written, recomputed);
    }

    /// Independent reference implementation of the `exFAT` spec
    /// §3.4 boot checksum algorithm. Distinct from the production
    /// `compute_boot_checksum` so the test does not rubber-stamp
    /// the production code.
    fn reference_checksum(prefix: &[u8]) -> u32 {
        assert_eq!(prefix.len(), 11 * 512);
        let mut sum: u32 = 0;
        for (i, &byte) in prefix.iter().enumerate() {
            if i == 0x6A || i == 0x6B || i == 0x70 {
                continue;
            }
            let rotated = if sum & 1 != 0 {
                (sum >> 1) | 0x8000_0000
            } else {
                sum >> 1
            };
            sum = rotated.wrapping_add(u32::from(byte));
        }
        sum
    }

    #[test]
    fn checksum_skips_volume_flags_bytes() {
        // Two volumes that differ ONLY in VolumeFlags (bytes
        // 0x6A..0x6C) must yield identical checksums.
        let geo = ExfatGeometry::for_volume_size(64 * MIB).expect("valid geometry");
        let buf_a = synthesize(&geo, 1).unwrap();
        let mut buf_b = buf_a;
        buf_b[0x06A] = 0xAA;
        buf_b[0x06B] = 0xBB;
        let sum_a = reference_checksum(&buf_a[..BOOT_CHECKSUM_SECTOR_INDEX * 512]);
        let sum_b = reference_checksum(&buf_b[..BOOT_CHECKSUM_SECTOR_INDEX * 512]);
        assert_eq!(sum_a, sum_b, "checksum must ignore VolumeFlags bytes");
    }

    #[test]
    fn checksum_skips_percent_in_use_byte() {
        let geo = ExfatGeometry::for_volume_size(64 * MIB).expect("valid geometry");
        let buf_a = synthesize(&geo, 1).unwrap();
        let mut buf_b = buf_a;
        buf_b[0x070] = 0x42;
        let sum_a = reference_checksum(&buf_a[..BOOT_CHECKSUM_SECTOR_INDEX * 512]);
        let sum_b = reference_checksum(&buf_b[..BOOT_CHECKSUM_SECTOR_INDEX * 512]);
        assert_eq!(sum_a, sum_b, "checksum must ignore PercentInUse byte");
    }

    #[test]
    fn checksum_includes_other_bytes() {
        // Changing any non-excluded byte should change the checksum.
        let geo = ExfatGeometry::for_volume_size(64 * MIB).expect("valid geometry");
        let buf_a = synthesize(&geo, 1).unwrap();
        let mut buf_b = buf_a;
        buf_b[0x100] ^= 0x55;
        let sum_a = reference_checksum(&buf_a[..BOOT_CHECKSUM_SECTOR_INDEX * 512]);
        let sum_b = reference_checksum(&buf_b[..BOOT_CHECKSUM_SECTOR_INDEX * 512]);
        assert_ne!(sum_a, sum_b);
    }

    // ---------- Determinism ----------

    #[test]
    fn same_inputs_produce_identical_buffer() {
        let geo = ExfatGeometry::for_volume_size(4 * GIB).unwrap();
        let buf_a = synthesize(&geo, 0xCAFE_BABE).unwrap();
        let buf_b = synthesize(&geo, 0xCAFE_BABE).unwrap();
        assert_eq!(&buf_a[..], &buf_b[..]);
    }

    #[test]
    fn different_volume_serial_changes_only_serial_field_and_checksum() {
        let geo = ExfatGeometry::for_volume_size(4 * GIB).unwrap();
        let buf_a = synthesize(&geo, 0x1111_1111).unwrap();
        let buf_b = synthesize(&geo, 0x2222_2222).unwrap();
        // Bytes 0..0x064 should match.
        assert_eq!(&buf_a[0..0x064], &buf_b[0..0x064]);
        // Bytes 0x068 (rev) onward through end of boot code should match.
        assert_eq!(&buf_a[0x068..0x1FE], &buf_b[0x068..0x1FE]);
        // Serial differs.
        assert_ne!(&buf_a[0x064..0x068], &buf_b[0x064..0x068]);
        // Checksum sector differs (because input bytes differ).
        let cs_start = BOOT_CHECKSUM_SECTOR_INDEX * 512;
        assert_ne!(
            &buf_a[cs_start..cs_start + 4],
            &buf_b[cs_start..cs_start + 4]
        );
    }

    // ---------- Geometry-derived field round-trips ----------

    #[test]
    fn small_volume_records_correct_sectors_per_cluster_shift() {
        let geo = ExfatGeometry::for_volume_size(64 * MIB).unwrap();
        let buf = synthesize(&geo, 0).unwrap();
        assert_eq!(buf[0x06D], 3); // 4 KiB cluster band
    }

    #[test]
    fn large_volume_records_correct_sectors_per_cluster_shift() {
        let geo = ExfatGeometry::for_volume_size(64 * GIB).unwrap();
        let buf = synthesize(&geo, 0).unwrap();
        assert_eq!(buf[0x06D], 8); // 128 KiB cluster band
    }

    #[test]
    fn fat_offset_field_equals_pinned_constant() {
        let geo = ExfatGeometry::for_volume_size(64 * MIB).unwrap();
        let buf = synthesize(&geo, 0).unwrap();
        let fat_offset = u32::from_le_bytes(buf[0x050..0x054].try_into().unwrap());
        assert_eq!(fat_offset, FAT_OFFSET_SECTORS);
        assert_eq!(fat_offset, 24);
    }

    #[test]
    fn backup_boot_region_constant_consistent_with_layout() {
        // Defensive check that the public constant matches what
        // the layout actually emits (the dispatcher in Phase 2.11
        // will rely on this equality).
        assert_eq!(BACKUP_BOOT_REGION_OFFSET_SECTORS, 12);
        assert_eq!(BOOT_REGION_SIZE_BYTES, 12 * 512);
    }
}
