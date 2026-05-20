//! FAT32 boot sector synthesizer.
//!
//! Phase 2.2 of the B-1 rewrite. This module takes a
//! [`Fat32Geometry`] + a volume label + a 32-bit volume serial and
//! produces the on-disk **boot sector** that lives at byte offset
//! 0 of a FAT32 volume. The output is a fixed-size `[u8; 512]`
//! suitable for serving directly from the read dispatcher
//! (Phase 2.6) or copying to the *backup* boot sector at sector 6.
//!
//! ## Specification anchors
//!
//! Every magic number in this module is sourced from one of:
//!
//! * Microsoft FAT Specification (fatgen103.pdf), §3.1 (BPB) and
//!   §3.3 (FAT32-specific BPB extensions, "EBPB").
//! * Microsoft FAT Specification §6.1 (short-filename / volume
//!   label character rules).
//!
//! ## Field layout
//!
//! ```text
//! Offset Size Field             Source
//! 0x000   3   BS_jmpBoot        fixed: EB 58 90 (short jmp past BPB + nop)
//! 0x003   8   BS_OEMName        fixed: "MSWIN4.1"
//! 0x00B   2   BPB_BytsPerSec    fixed: 512 (SECTOR_SIZE_BYTES)
//! 0x00D   1   BPB_SecPerClus    geometry.sectors_per_cluster()
//! 0x00E   2   BPB_RsvdSecCnt    fixed: 32 (RESERVED_SECTORS)
//! 0x010   1   BPB_NumFATs       fixed: 2 (NUM_FATS)
//! 0x011   2   BPB_RootEntCnt    fixed: 0 (FAT32: must be zero)
//! 0x013   2   BPB_TotSec16      fixed: 0 (FAT32: must be zero)
//! 0x015   1   BPB_Media         fixed: 0xF8 (fixed disk)
//! 0x016   2   BPB_FATSz16       fixed: 0 (FAT32: must be zero)
//! 0x018   2   BPB_SecPerTrk     fixed: 0 (no CHS — virtual USB)
//! 0x01A   2   BPB_NumHeads      fixed: 0 (no CHS — virtual USB)
//! 0x01C   4   BPB_HiddSec       fixed: 0 (no partition table)
//! 0x020   4   BPB_TotSec32      geometry.total_sectors() as u32
//! 0x024   4   BPB_FATSz32       geometry.fat_size_sectors()
//! 0x028   2   BPB_ExtFlags      fixed: 0 (FAT mirroring active)
//! 0x02A   2   BPB_FSVer         fixed: 0 (FAT32 v0.0)
//! 0x02C   4   BPB_RootClus      fixed: 2 (root dir at first data cluster)
//! 0x030   2   BPB_FSInfo        fixed: 1 (FSINFO_SECTOR_INDEX)
//! 0x032   2   BPB_BkBootSec     fixed: 6 (BACKUP_BOOT_SECTOR_INDEX)
//! 0x034  12   BPB_Reserved      fixed: zero
//! 0x040   1   BS_DrvNum         fixed: 0x80 (first fixed disk)
//! 0x041   1   BS_Reserved1      fixed: 0
//! 0x042   1   BS_BootSig        fixed: 0x29 (EBPB present)
//! 0x043   4   BS_VolID          caller-supplied volume_serial
//! 0x047  11   BS_VolLab         caller-supplied label, space-padded
//! 0x052   8   BS_FilSysType     fixed: "FAT32   "
//! 0x05A 420   (boot code area)  zero (B-1 is not bootable)
//! 0x1FE   2   (boot signature)  fixed: 55 AA
//! ```
//!
//! ## Why no x86 boot code?
//!
//! `mkfs.vfat` writes a small x86 stub at offset `0x5A` that
//! prints "This is not a bootable disk" if BIOS tries to boot from
//! the volume. B-1 advertises a USB mass-storage gadget — Tesla's
//! infotainment doesn't boot the drive, so the stub is omitted and
//! the area is zero-filled. fatgen103 makes the area optional; the
//! `55 AA` signature is what matters to the kernel's FAT driver.
//!
//! ## Volume label rules
//!
//! Per fatgen103 §6.1, FAT volume labels follow the short-filename
//! character set (minus the `.` separator). [`synthesize`] enforces
//! this strictly: lowercase, control characters, and the explicit
//! forbidden punctuation (`"`, `*`, `+`, `,`, `.`, `/`, `:`, `;`,
//! `<`, `=`, `>`, `?`, `[`, `\`, `]`, `|`) all return
//! [`BootSectorError::LabelHasInvalidByte`]. Labels longer than
//! 11 bytes are rejected; shorter labels are right-padded with
//! ASCII space (`0x20`) to the full 11-byte field width.
//!
//! ## What this module does NOT do
//!
//! * It does **not** generate a volume serial number. Callers are
//!   expected to provide one (e.g. derived from current time as
//!   `mkfs.vfat` does, or a fixed value for deterministic tests).
//! * It does **not** synthesize the `FsInfo` sector (sector 1) or
//!   the backup boot sector (sector 6). Those land in Phase 2.3
//!   and Phase 2.6 (the read dispatcher) respectively.

use core::fmt;

use crate::fs::fat32::geometry::{
    BACKUP_BOOT_SECTOR_INDEX, FSINFO_SECTOR_INDEX, Fat32Geometry, NUM_FATS, RESERVED_SECTORS,
};
use crate::fs::geometry::{Geometry, SECTOR_SIZE_BYTES};

/// Byte width of a FAT32 boot sector.
///
/// Equal to [`SECTOR_SIZE_BYTES`] and to the size of the array
/// [`synthesize`] returns. Re-exported as a `usize` so callers can
/// dimension their own buffers without a cast.
pub const BOOT_SECTOR_SIZE_BYTES: usize = SECTOR_SIZE_BYTES as usize;

/// Width of the `BS_VolLab` field (fatgen103 §3.3).
pub const VOLUME_LABEL_LEN_BYTES: usize = 11;

/// Default OEM-name string emitted at offset `0x03`.
///
/// fatgen103 §3.1 recommends this exact 8-byte ASCII string for
/// maximum interoperability with legacy DOS tools, even though
/// modern FAT drivers ignore the field. Matches what `mkfs.vfat`
/// writes by default.
pub const DEFAULT_OEM_NAME: [u8; 8] = *b"MSWIN4.1";

/// Default volume label used when the caller passes an empty
/// slice.
///
/// Microsoft `format.com` writes this exact 11-byte string for
/// "label not set" volumes (spaces are padding; the field is
/// fixed-width ASCII per fatgen103 §3.3).
pub const DEFAULT_VOLUME_LABEL: [u8; VOLUME_LABEL_LEN_BYTES] = *b"NO NAME    ";

/// Filesystem-type tag at offset `0x52` (fatgen103 §3.3,
/// `BS_FilSysType`).
///
/// 8 ASCII bytes, space-padded. Informational only — FAT drivers
/// determine the type from the geometry, not this field. Matches
/// `mkfs.vfat` output.
pub const FAT32_FILESYSTEM_TYPE_TAG: [u8; 8] = *b"FAT32   ";

/// Media descriptor for fixed (non-removable) disks.
///
/// fatgen103 §3.1: `BPB_Media = 0xF8` for fixed media. The same
/// byte is also written into the low 8 bits of FAT entry 0
/// (see Phase 2.4). USB mass storage emulating a fixed disk
/// (which is what the gadget exposes) uses `0xF8`.
pub const MEDIA_DESCRIPTOR_FIXED: u8 = 0xF8;

/// Default drive number (fatgen103 §3.3, `BS_DrvNum`).
///
/// `0x80` = first fixed disk in BIOS / INT 13h convention. Not
/// used by modern OSes but `mkfs.vfat` writes it for legacy
/// compatibility, and so do we.
pub const DEFAULT_DRIVE_NUMBER: u8 = 0x80;

/// Extended boot signature (fatgen103 §3.3, `BS_BootSig`).
///
/// Set to `0x29` to signal that the three trailing fields
/// (`BS_VolID`, `BS_VolLab`, `BS_FilSysType`) are populated.
pub const EXTENDED_BOOT_SIGNATURE: u8 = 0x29;

/// Final two bytes of every valid FAT32 boot sector
/// (fatgen103 §3.1).
///
/// `0x55, 0xAA` at offsets `0x1FE`/`0x1FF`. Without this signature
/// the Linux FAT driver refuses to mount the volume.
pub const BOOT_SECTOR_END_SIGNATURE: [u8; 2] = [0x55, 0xAA];

/// First data cluster index in FAT32 (fatgen103 §4.1).
///
/// Clusters 0 and 1 are reserved; the root directory always
/// starts at cluster 2. Written to `BPB_RootClus` at offset
/// `0x2C`.
pub const ROOT_DIRECTORY_CLUSTER: u32 = 2;

/// Errors returned by [`synthesize`].
#[derive(Debug, PartialEq, Eq)]
pub enum BootSectorError {
    /// The caller-supplied volume label exceeds the 11-byte
    /// `BS_VolLab` field width.
    LabelTooLong {
        /// Actual length of the caller's slice in bytes.
        actual: usize,
        /// Maximum allowed: [`VOLUME_LABEL_LEN_BYTES`].
        maximum: usize,
    },
    /// The caller-supplied volume label contains a byte that is
    /// not allowed in a FAT volume label per fatgen103 §6.1.
    ///
    /// Disallowed bytes include lowercase ASCII (`a..=z`), all
    /// control characters (`< 0x20`), and the explicit forbidden
    /// punctuation set (`"`, `*`, `+`, `,`, `.`, `/`, `:`, `;`,
    /// `<`, `=`, `>`, `?`, `[`, `\`, `]`, `|`).
    LabelHasInvalidByte {
        /// Zero-based offset of the offending byte within the
        /// caller's slice.
        offset: usize,
        /// The byte that failed validation.
        byte: u8,
    },
    /// The geometry's total sector count does not fit in the
    /// 32-bit `BPB_TotSec32` field. The
    /// [`Fat32Geometry`] constructor should reject this case via
    /// `MAX_FAT32_VOLUME_BYTES`; if it ever propagates here it
    /// indicates an internal invariant violation.
    TotalSectorsExceedU32 {
        /// The geometry's reported total sector count.
        total_sectors: u64,
    },
}

impl fmt::Display for BootSectorError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::LabelTooLong { actual, maximum } => write!(
                f,
                "volume label is {actual} bytes; FAT32 BS_VolLab field is {maximum} bytes wide"
            ),
            Self::LabelHasInvalidByte { offset, byte } => write!(
                f,
                "volume label byte at offset {offset} ({byte:#04x}) is not allowed in a FAT volume label (fatgen103 §6.1)"
            ),
            Self::TotalSectorsExceedU32 { total_sectors } => write!(
                f,
                "geometry reports {total_sectors} total sectors; FAT32 BPB_TotSec32 is a 32-bit field"
            ),
        }
    }
}

impl core::error::Error for BootSectorError {}

/// Synthesize the 512-byte FAT32 boot sector for `geometry`.
///
/// `label` may be at most [`VOLUME_LABEL_LEN_BYTES`] bytes long
/// and must satisfy the fatgen103 §6.1 character rules (see
/// [`BootSectorError::LabelHasInvalidByte`] for the disallowed
/// set). An empty `label` slice is replaced with
/// [`DEFAULT_VOLUME_LABEL`] (`"NO NAME    "`). Shorter labels are
/// right-padded with ASCII space (`0x20`) to the full 11-byte
/// field width.
///
/// `volume_serial` is written to `BS_VolID` (offset `0x43`) in
/// little-endian order. The caller is responsible for choosing a
/// value — `mkfs.vfat` derives one from the current time; B-1's
/// installer is expected to pick a stable per-volume value so the
/// serial survives synthesizer restarts.
///
/// # Errors
///
/// * [`BootSectorError::LabelTooLong`] — `label.len() > 11`.
/// * [`BootSectorError::LabelHasInvalidByte`] — `label` contains
///   a byte not allowed in FAT volume labels.
/// * [`BootSectorError::TotalSectorsExceedU32`] — `geometry`
///   reports a total sector count that does not fit in a `u32`
///   (the on-disk `BPB_TotSec32` field width). This indicates an
///   invariant violation in [`Fat32Geometry`] and should never
///   occur in practice.
pub fn synthesize(
    geometry: &Fat32Geometry,
    label: &[u8],
    volume_serial: u32,
) -> Result<[u8; BOOT_SECTOR_SIZE_BYTES], BootSectorError> {
    let padded_label = pad_volume_label(label)?;
    let total_sectors_u32 = u32::try_from(geometry.total_sectors()).map_err(|_| {
        BootSectorError::TotalSectorsExceedU32 {
            total_sectors: geometry.total_sectors(),
        }
    })?;
    let sectors_per_cluster_u8 = u8::try_from(geometry.sectors_per_cluster()).map_err(|_| {
        // Geometry's cluster table tops out at 64 (one byte). If
        // it ever returns > 255 it's the same kind of invariant
        // breach as TotalSectorsExceedU32 — re-use that error
        // surface rather than invent a third one.
        BootSectorError::TotalSectorsExceedU32 {
            total_sectors: geometry.total_sectors(),
        }
    })?;

    let mut sector = [0_u8; BOOT_SECTOR_SIZE_BYTES];

    sector[0x00..0x03].copy_from_slice(&[0xEB, 0x58, 0x90]);
    sector[0x03..0x0B].copy_from_slice(&DEFAULT_OEM_NAME);

    write_u16_le(&mut sector, 0x0B, SECTOR_SIZE_BYTES_U16);
    sector[0x0D] = sectors_per_cluster_u8;
    write_u16_le(&mut sector, 0x0E, RESERVED_SECTORS_U16);
    sector[0x10] = NUM_FATS;
    write_u16_le(&mut sector, 0x11, 0);
    write_u16_le(&mut sector, 0x13, 0);
    sector[0x15] = MEDIA_DESCRIPTOR_FIXED;
    write_u16_le(&mut sector, 0x16, 0);
    write_u16_le(&mut sector, 0x18, 0);
    write_u16_le(&mut sector, 0x1A, 0);
    write_u32_le(&mut sector, 0x1C, 0);
    write_u32_le(&mut sector, 0x20, total_sectors_u32);

    write_u32_le(&mut sector, 0x24, geometry.fat_size_sectors());
    write_u16_le(&mut sector, 0x28, 0);
    write_u16_le(&mut sector, 0x2A, 0);
    write_u32_le(&mut sector, 0x2C, ROOT_DIRECTORY_CLUSTER);
    write_u16_le(&mut sector, 0x30, FSINFO_SECTOR_INDEX_U16);
    write_u16_le(&mut sector, 0x32, BACKUP_BOOT_SECTOR_INDEX_U16);
    // 0x34..0x40: BPB_Reserved (12 bytes) — already zero.

    sector[0x40] = DEFAULT_DRIVE_NUMBER;
    sector[0x41] = 0;
    sector[0x42] = EXTENDED_BOOT_SIGNATURE;
    write_u32_le(&mut sector, 0x43, volume_serial);
    sector[0x47..0x52].copy_from_slice(&padded_label);
    sector[0x52..0x5A].copy_from_slice(&FAT32_FILESYSTEM_TYPE_TAG);

    // 0x5A..0x1FE: boot code area — left zero (B-1 is not bootable).

    sector[0x1FE..0x200].copy_from_slice(&BOOT_SECTOR_END_SIGNATURE);

    Ok(sector)
}

// Compile-time checks: every fixed-width BPB field this module
// writes is wider than (or equal to) the geometry constant whose
// value it carries. If a future edit shrinks the BPB field or
// grows the geometry constant past `u16::MAX`, the build breaks
// here rather than producing a silently truncated boot sector.
const _: () = {
    assert!(SECTOR_SIZE_BYTES <= u16::MAX as u32);
    assert!(RESERVED_SECTORS <= u16::MAX as u32);
    assert!(FSINFO_SECTOR_INDEX <= u16::MAX as u32);
    assert!(BACKUP_BOOT_SECTOR_INDEX <= u16::MAX as u32);
};

#[allow(clippy::cast_possible_truncation)] // const_asserted above
const SECTOR_SIZE_BYTES_U16: u16 = SECTOR_SIZE_BYTES as u16;
#[allow(clippy::cast_possible_truncation)] // const_asserted above
const RESERVED_SECTORS_U16: u16 = RESERVED_SECTORS as u16;
#[allow(clippy::cast_possible_truncation)] // const_asserted above
const FSINFO_SECTOR_INDEX_U16: u16 = FSINFO_SECTOR_INDEX as u16;
#[allow(clippy::cast_possible_truncation)] // const_asserted above
const BACKUP_BOOT_SECTOR_INDEX_U16: u16 = BACKUP_BOOT_SECTOR_INDEX as u16;

/// Write `value` as a little-endian u16 at byte `offset` in
/// `buf`.
///
/// The caller is responsible for ensuring `offset + 2 <=
/// BOOT_SECTOR_SIZE_BYTES`; this helper is `#[allow]`-ed for
/// `indexing_slicing` because every call site in this module uses
/// a compile-time constant offset taken straight from the
/// fatgen103 §3.1 / §3.3 field table.
#[inline]
#[allow(clippy::indexing_slicing)]
fn write_u16_le(buf: &mut [u8; BOOT_SECTOR_SIZE_BYTES], offset: usize, value: u16) {
    buf[offset..offset + 2].copy_from_slice(&value.to_le_bytes());
}

/// Write `value` as a little-endian u32 at byte `offset` in
/// `buf`. See [`write_u16_le`] for the safety reasoning.
#[inline]
#[allow(clippy::indexing_slicing)]
fn write_u32_le(buf: &mut [u8; BOOT_SECTOR_SIZE_BYTES], offset: usize, value: u32) {
    buf[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
}

/// Validate `label` and right-pad with ASCII space to
/// [`VOLUME_LABEL_LEN_BYTES`].
///
/// Empty labels are replaced with [`DEFAULT_VOLUME_LABEL`]. The
/// returned bytes are suitable for both the boot sector
/// `BS_VolLab` field at offset `0x47` and the 11-byte name field
/// of the root-directory volume-label entry that fatgen103 §6.1
/// requires to mirror the boot-sector label.
///
/// # Errors
///
/// * [`BootSectorError::LabelTooLong`] — `label.len() > 11`.
/// * [`BootSectorError::LabelHasInvalidByte`] — `label` contains
///   a byte not allowed in FAT volume labels per fatgen103 §6.1.
#[allow(clippy::indexing_slicing)] // bounds checked above the slice op
pub fn pad_volume_label(label: &[u8]) -> Result<[u8; VOLUME_LABEL_LEN_BYTES], BootSectorError> {
    if label.is_empty() {
        return Ok(DEFAULT_VOLUME_LABEL);
    }
    if label.len() > VOLUME_LABEL_LEN_BYTES {
        return Err(BootSectorError::LabelTooLong {
            actual: label.len(),
            maximum: VOLUME_LABEL_LEN_BYTES,
        });
    }
    for (offset, &byte) in label.iter().enumerate() {
        if !is_valid_volume_label_byte(byte) {
            return Err(BootSectorError::LabelHasInvalidByte { offset, byte });
        }
    }
    let mut padded = [b' '; VOLUME_LABEL_LEN_BYTES];
    padded[..label.len()].copy_from_slice(label);
    Ok(padded)
}

/// fatgen103 §6.1 short-filename / volume-label character rules.
///
/// The spec is given as a negative list (the "disallowed" set);
/// implementing it as a positive predicate avoids the off-by-one
/// risk of an exclusion check. The two formulations are
/// equivalent; tests cover both directions.
const fn is_valid_volume_label_byte(b: u8) -> bool {
    matches!(
        b,
        b' '
        | b'!'
        | b'#'..=b')'
        | b'-'
        | b'0'..=b'9'
        | b'@'..=b'Z'
        | b'^'..=b'`'
        | b'{'
        | b'}'
        | b'~'
    )
}

#[cfg(test)]
#[allow(
    clippy::cognitive_complexity,
    clippy::expect_used,
    clippy::panic,
    clippy::unwrap_used
)]
mod tests {
    use super::*;
    use crate::fs::fat32::geometry::Fat32Geometry;

    const MIB: u64 = 1024 * 1024;
    const GIB: u64 = 1024 * 1024 * 1024;
    const VALID_LABEL_4GIB: &[u8] = b"TESLACAM";
    const VOL_SERIAL_4GIB: u32 = 0xDEAD_BEEF;

    fn synth_4gib() -> [u8; 512] {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid 4 GiB geometry");
        synthesize(&geo, VALID_LABEL_4GIB, VOL_SERIAL_4GIB).expect("valid synthesis")
    }

    // --- Fixed bytes at known offsets ----------------------------------

    #[test]
    fn jmp_boot_is_eb_58_90() {
        let s = synth_4gib();
        assert_eq!(&s[0x00..0x03], &[0xEB, 0x58, 0x90]);
    }

    #[test]
    fn oem_name_is_mswin41() {
        let s = synth_4gib();
        assert_eq!(&s[0x03..0x0B], b"MSWIN4.1");
    }

    #[test]
    fn bytes_per_sector_is_512_little_endian() {
        let s = synth_4gib();
        // 0x0200 LE = [0x00, 0x02].
        assert_eq!(&s[0x0B..0x0D], &[0x00, 0x02]);
    }

    #[test]
    fn sectors_per_cluster_matches_geometry_for_4gib() {
        // 4 GiB FAT32 uses 8 sectors/cluster per KB140365.
        let s = synth_4gib();
        assert_eq!(s[0x0D], 8);
    }

    #[test]
    fn reserved_sector_count_is_32_little_endian() {
        let s = synth_4gib();
        // 0x0020 LE = [0x20, 0x00].
        assert_eq!(&s[0x0E..0x10], &[0x20, 0x00]);
    }

    #[test]
    fn num_fats_is_2() {
        let s = synth_4gib();
        assert_eq!(s[0x10], 2);
    }

    #[test]
    fn root_entry_count_is_zero_for_fat32() {
        let s = synth_4gib();
        assert_eq!(&s[0x11..0x13], &[0x00, 0x00]);
    }

    #[test]
    fn total_sec_16_is_zero_for_fat32() {
        let s = synth_4gib();
        assert_eq!(&s[0x13..0x15], &[0x00, 0x00]);
    }

    #[test]
    fn media_descriptor_is_fixed_disk_f8() {
        let s = synth_4gib();
        assert_eq!(s[0x15], 0xF8);
    }

    #[test]
    fn fat_size_16_is_zero_for_fat32() {
        let s = synth_4gib();
        assert_eq!(&s[0x16..0x18], &[0x00, 0x00]);
    }

    #[test]
    fn chs_geometry_fields_are_zero() {
        let s = synth_4gib();
        assert_eq!(&s[0x18..0x1A], &[0x00, 0x00], "BPB_SecPerTrk");
        assert_eq!(&s[0x1A..0x1C], &[0x00, 0x00], "BPB_NumHeads");
    }

    #[test]
    fn hidden_sectors_is_zero_for_unpartitioned_volume() {
        let s = synth_4gib();
        assert_eq!(&s[0x1C..0x20], &[0x00, 0x00, 0x00, 0x00]);
    }

    #[test]
    fn total_sec_32_equals_geometry_for_4gib() {
        // 4 GiB / 512 = 8_388_608 sectors = 0x0080_0000 LE.
        let s = synth_4gib();
        assert_eq!(
            u32::from_le_bytes(s[0x20..0x24].try_into().unwrap()),
            8_388_608
        );
    }

    #[test]
    fn fat_size_32_equals_geometry_for_4gib() {
        // From fatgen103 hand-computation: 4 GiB → 8184 sectors.
        let s = synth_4gib();
        assert_eq!(u32::from_le_bytes(s[0x24..0x28].try_into().unwrap()), 8184);
    }

    #[test]
    fn ext_flags_zero_means_fat_mirroring_active() {
        let s = synth_4gib();
        assert_eq!(&s[0x28..0x2A], &[0x00, 0x00]);
    }

    #[test]
    fn fs_version_is_zero() {
        let s = synth_4gib();
        assert_eq!(&s[0x2A..0x2C], &[0x00, 0x00]);
    }

    #[test]
    fn root_cluster_is_two() {
        let s = synth_4gib();
        assert_eq!(
            u32::from_le_bytes(s[0x2C..0x30].try_into().unwrap()),
            ROOT_DIRECTORY_CLUSTER
        );
    }

    #[test]
    fn fsinfo_sector_index_is_one() {
        let s = synth_4gib();
        assert_eq!(
            u16::from_le_bytes(s[0x30..0x32].try_into().unwrap()),
            FSINFO_SECTOR_INDEX_U16
        );
        assert_eq!(FSINFO_SECTOR_INDEX_U16, 1);
    }

    #[test]
    fn backup_boot_sector_index_is_six() {
        let s = synth_4gib();
        assert_eq!(
            u16::from_le_bytes(s[0x32..0x34].try_into().unwrap()),
            BACKUP_BOOT_SECTOR_INDEX_U16
        );
        assert_eq!(BACKUP_BOOT_SECTOR_INDEX_U16, 6);
    }

    #[test]
    fn bpb_reserved_12_bytes_are_zero() {
        let s = synth_4gib();
        assert_eq!(&s[0x34..0x40], &[0_u8; 12]);
    }

    #[test]
    fn drive_number_is_0x80() {
        let s = synth_4gib();
        assert_eq!(s[0x40], 0x80);
    }

    #[test]
    fn bs_reserved1_is_zero() {
        let s = synth_4gib();
        assert_eq!(s[0x41], 0);
    }

    #[test]
    fn boot_sig_is_0x29() {
        let s = synth_4gib();
        assert_eq!(s[0x42], 0x29);
    }

    #[test]
    fn volume_serial_round_trips_little_endian() {
        let s = synth_4gib();
        assert_eq!(
            u32::from_le_bytes(s[0x43..0x47].try_into().unwrap()),
            VOL_SERIAL_4GIB
        );
        // Explicit byte order check: 0xDEADBEEF LE = EF BE AD DE.
        assert_eq!(&s[0x43..0x47], &[0xEF, 0xBE, 0xAD, 0xDE]);
    }

    #[test]
    fn volume_label_is_space_padded_to_11_bytes() {
        let s = synth_4gib();
        // b"TESLACAM" is 8 bytes; padded to 11 with three spaces.
        assert_eq!(&s[0x47..0x52], b"TESLACAM   ");
    }

    #[test]
    fn filesystem_type_tag_is_fat32_with_spaces() {
        let s = synth_4gib();
        assert_eq!(&s[0x52..0x5A], b"FAT32   ");
    }

    #[test]
    fn boot_code_area_is_zero() {
        let s = synth_4gib();
        // 420 bytes from 0x5A to 0x1FE.
        for (i, &b) in s[0x5A..0x1FE].iter().enumerate() {
            assert_eq!(b, 0, "boot code area byte at offset {:#x} is {b}", 0x5A + i);
        }
    }

    #[test]
    fn end_signature_is_55_aa() {
        let s = synth_4gib();
        assert_eq!(&s[0x1FE..0x200], &[0x55, 0xAA]);
    }

    // --- Full-buffer comparison against hand-computed expected ----------

    #[test]
    fn full_boot_sector_matches_hand_computed_expected_for_4gib() {
        let s = synth_4gib();
        let mut expected = [0_u8; 512];
        // Fixed prefix.
        expected[0x00..0x03].copy_from_slice(&[0xEB, 0x58, 0x90]);
        expected[0x03..0x0B].copy_from_slice(b"MSWIN4.1");
        expected[0x0B..0x0D].copy_from_slice(&512_u16.to_le_bytes());
        expected[0x0D] = 8;
        expected[0x0E..0x10].copy_from_slice(&32_u16.to_le_bytes());
        expected[0x10] = 2;
        // 0x11..0x15: zeros (root entry count, total sec 16).
        expected[0x15] = 0xF8;
        // 0x16..0x1C: zeros.
        // 0x1C..0x20: zeros (hidden sec).
        expected[0x20..0x24].copy_from_slice(&8_388_608_u32.to_le_bytes());
        expected[0x24..0x28].copy_from_slice(&8184_u32.to_le_bytes());
        // 0x28..0x2C: zeros (ext flags, fs version).
        expected[0x2C..0x30].copy_from_slice(&2_u32.to_le_bytes());
        expected[0x30..0x32].copy_from_slice(&1_u16.to_le_bytes());
        expected[0x32..0x34].copy_from_slice(&6_u16.to_le_bytes());
        // 0x34..0x40: zeros (BPB reserved).
        expected[0x40] = 0x80;
        // 0x41: zero.
        expected[0x42] = 0x29;
        expected[0x43..0x47].copy_from_slice(&VOL_SERIAL_4GIB.to_le_bytes());
        expected[0x47..0x52].copy_from_slice(b"TESLACAM   ");
        expected[0x52..0x5A].copy_from_slice(b"FAT32   ");
        // 0x5A..0x1FE: zeros (boot code).
        expected[0x1FE..0x200].copy_from_slice(&[0x55, 0xAA]);
        assert_eq!(s, expected, "byte-by-byte mismatch vs hand-computed");
    }

    // --- Geometry-dependent fields vary correctly -----------------------

    #[test]
    fn sectors_per_cluster_varies_with_volume_size() {
        let geo_small = Fat32Geometry::for_volume_size(34 * MIB).expect("valid");
        let s = synthesize(&geo_small, b"S", 1).expect("valid");
        assert_eq!(s[0x0D], 1, "34 MiB uses 1 sector/cluster");

        let geo_large = Fat32Geometry::for_volume_size(32 * GIB).expect("valid");
        let s = synthesize(&geo_large, b"S", 1).expect("valid");
        assert_eq!(s[0x0D], 64, "32 GiB uses 64 sectors/cluster");
    }

    #[test]
    fn total_sec_32_varies_with_volume_size() {
        let geo = Fat32Geometry::for_volume_size(34 * MIB).expect("valid");
        let s = synthesize(&geo, b"S", 1).expect("valid");
        let expected = u32::try_from(34 * MIB / 512).expect("fits in u32");
        assert_eq!(
            u32::from_le_bytes(s[0x20..0x24].try_into().unwrap()),
            expected
        );
    }

    #[test]
    fn fat_size_32_varies_with_volume_size() {
        // 32 MiB → fat_size 508 per fatgen103 hand-computation.
        // 34 MiB is the smallest accepted; verify the fat size
        // grows with the volume.
        let geo_small = Fat32Geometry::for_volume_size(34 * MIB).expect("valid");
        let s = synthesize(&geo_small, b"S", 1).expect("valid");
        let fat_small = u32::from_le_bytes(s[0x24..0x28].try_into().unwrap());

        let geo_huge = Fat32Geometry::for_volume_size(64 * GIB).expect("valid");
        let s = synthesize(&geo_huge, b"S", 1).expect("valid");
        let fat_huge = u32::from_le_bytes(s[0x24..0x28].try_into().unwrap());

        assert!(
            fat_small < fat_huge,
            "fat_size_sectors must grow with volume: small={fat_small}, huge={fat_huge}"
        );
    }

    // --- Volume label handling ------------------------------------------

    #[test]
    fn empty_label_falls_back_to_no_name() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        let s = synthesize(&geo, b"", VOL_SERIAL_4GIB).expect("empty label is replaced");
        assert_eq!(&s[0x47..0x52], b"NO NAME    ");
    }

    #[test]
    fn label_exactly_11_bytes_is_not_padded() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        let s = synthesize(&geo, b"TESLA12345!", VOL_SERIAL_4GIB).expect("valid");
        assert_eq!(&s[0x47..0x52], b"TESLA12345!");
    }

    #[test]
    fn label_longer_than_11_bytes_is_rejected() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        let err = synthesize(&geo, b"TESLA12345!!", VOL_SERIAL_4GIB)
            .expect_err("12-byte label must be rejected");
        match err {
            BootSectorError::LabelTooLong { actual, maximum } => {
                assert_eq!(actual, 12);
                assert_eq!(maximum, 11);
            }
            other => panic!("expected LabelTooLong, got {other:?}"),
        }
    }

    #[test]
    fn label_with_lowercase_is_rejected() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        let err =
            synthesize(&geo, b"teslacam", VOL_SERIAL_4GIB).expect_err("lowercase must be rejected");
        match err {
            BootSectorError::LabelHasInvalidByte { offset, byte } => {
                assert_eq!(offset, 0);
                assert_eq!(byte, b't');
            }
            other => panic!("expected LabelHasInvalidByte, got {other:?}"),
        }
    }

    #[test]
    fn label_with_control_char_is_rejected() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        let err = synthesize(&geo, b"TES\x01LA", VOL_SERIAL_4GIB)
            .expect_err("control char must be rejected");
        match err {
            BootSectorError::LabelHasInvalidByte { offset, byte } => {
                assert_eq!(offset, 3);
                assert_eq!(byte, 0x01);
            }
            other => panic!("expected LabelHasInvalidByte, got {other:?}"),
        }
    }

    #[test]
    fn label_rejects_each_forbidden_punctuation_byte() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        let forbidden: &[u8] = b"\"*+,./:;<=>?[\\]|";
        for &bad in forbidden {
            let mut buf = [b'A'; 5];
            buf[2] = bad;
            let err = synthesize(&geo, &buf, 1)
                .expect_err(&format!("forbidden byte {bad:#04x} must be rejected"));
            match err {
                BootSectorError::LabelHasInvalidByte { offset, byte } => {
                    assert_eq!(offset, 2, "wrong offset for byte {bad:#04x}");
                    assert_eq!(byte, bad, "wrong byte reported");
                }
                other => panic!("expected LabelHasInvalidByte for byte {bad:#04x}, got {other:?}"),
            }
        }
    }

    #[test]
    fn label_accepts_full_allowed_charset() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        // Pick one representative from each allowed range / set
        // so the test fails if `is_valid_volume_label_byte` is
        // tightened by accident.
        let samples: &[(&[u8], &str)] = &[
            (b" AAAA", "leading space"),
            (b"!", "0x21 bang"),
            (b"#$%&'()", "0x23..0x29 range"),
            (b"-", "dash"),
            (b"0123456789", "digits"),
            (b"@", "at-sign"),
            (b"ABCXYZ", "uppercase letters"),
            (b"^_`", "0x5E..0x60 range"),
            (b"{", "left brace"),
            (b"}", "right brace"),
            (b"~", "tilde"),
        ];
        for (label, descr) in samples {
            synthesize(&geo, label, 1)
                .unwrap_or_else(|e| panic!("allowed sample {descr:?} ({label:?}) rejected: {e}"));
        }
    }

    #[test]
    fn label_validation_runs_before_padding() {
        let geo = Fat32Geometry::for_volume_size(4 * GIB).expect("valid");
        // A 5-byte label with a bad byte at position 4 must fail
        // — padding the tail should not mask invalidity.
        let err = synthesize(&geo, b"AAAA.", 1).expect_err("dot must be rejected");
        match err {
            BootSectorError::LabelHasInvalidByte { offset, byte } => {
                assert_eq!(offset, 4);
                assert_eq!(byte, b'.');
            }
            other => panic!("expected LabelHasInvalidByte, got {other:?}"),
        }
    }

    // --- is_valid_volume_label_byte exhaustive negative coverage --------

    #[test]
    fn is_valid_volume_label_byte_rejects_all_lowercase() {
        for b in b'a'..=b'z' {
            assert!(
                !is_valid_volume_label_byte(b),
                "lowercase {:?} ({b:#04x}) should be rejected",
                b as char
            );
        }
    }

    #[test]
    fn is_valid_volume_label_byte_rejects_all_control_chars() {
        for b in 0_u8..0x20 {
            assert!(
                !is_valid_volume_label_byte(b),
                "control char {b:#04x} should be rejected"
            );
        }
        assert!(
            !is_valid_volume_label_byte(0x7F),
            "DEL (0x7F) should be rejected"
        );
    }

    #[test]
    fn is_valid_volume_label_byte_rejects_explicit_forbidden_set() {
        let forbidden: &[u8] = b"\"*+,./:;<=>?[\\]|";
        for &b in forbidden {
            assert!(
                !is_valid_volume_label_byte(b),
                "explicit forbidden byte {:?} ({b:#04x}) should be rejected",
                b as char
            );
        }
    }

    #[test]
    fn is_valid_volume_label_byte_accepts_uppercase_and_digits() {
        for b in b'A'..=b'Z' {
            assert!(is_valid_volume_label_byte(b));
        }
        for b in b'0'..=b'9' {
            assert!(is_valid_volume_label_byte(b));
        }
    }

    // --- Geometric invariants over the full size sweep ------------------

    #[test]
    fn end_signature_is_always_55_aa_across_volume_sizes() {
        const STEP: u64 = 17 * MIB;
        let mut size = 34 * MIB;
        while size <= 64 * GIB {
            let geo = Fat32Geometry::for_volume_size(size).expect("size in sweep is valid");
            let s = synthesize(&geo, b"S", 1).expect("synth at sweep size");
            assert_eq!(
                &s[0x1FE..0x200],
                &[0x55, 0xAA],
                "missing 0x55AA at size {size}"
            );
            size += STEP;
        }
    }

    #[test]
    fn total_sec_32_matches_geometry_across_volume_sizes() {
        const STEP: u64 = 17 * MIB;
        let mut size = 34 * MIB;
        while size <= 64 * GIB {
            let geo = Fat32Geometry::for_volume_size(size).expect("size in sweep is valid");
            let expected = u32::try_from(geo.total_sectors()).expect("fits in u32");
            let s = synthesize(&geo, b"S", 1).expect("synth at sweep size");
            assert_eq!(
                u32::from_le_bytes(s[0x20..0x24].try_into().unwrap()),
                expected,
                "BPB_TotSec32 mismatch at size {size}"
            );
            size += STEP;
        }
    }
}
