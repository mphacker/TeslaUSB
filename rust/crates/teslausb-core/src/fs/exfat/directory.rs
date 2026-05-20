//! `exFAT` directory entry encoders and root-directory synthesizer.
//!
//! Phase 2.10 of the B-1 rewrite. Builds the on-disk byte
//! representation of every `exFAT` directory entry kind the
//! synthesizer needs:
//!
//! | Entry type | Hex | Producer |
//! |---|---|---|
//! | Allocation Bitmap | `0x81` | [`encode_allocation_bitmap_entry`] |
//! | `UpCase` Table | `0x82` | [`encode_upcase_table_entry`] |
//! | Volume Label | `0x83` | [`encode_volume_label_entry`] |
//! | File | `0x85` | [`encode_file_entry`] |
//! | Stream Extension | `0xC0` | [`encode_stream_extension_entry`] |
//! | File Name | `0xC1` | [`encode_file_name_entry`] |
//!
//! Plus the two `exFAT` checksum primitives:
//!
//! | Checksum | Spec § | Producer |
//! |---|---|---|
//! | Entry-set `SetChecksum` | §6.3.3 | [`set_checksum`] |
//! | UTF-16 `NameHash` | §7.4 | [`name_hash`] |
//!
//! And two high-level builders:
//!
//! * [`encode_file_entry_set`] — assembles a complete primary +
//!   secondaries entry set for a single file or directory and
//!   stamps both checksums.
//! * [`synthesize_root_directory`] — produces a single
//!   zero-padded cluster of bytes containing the three special
//!   root-directory entries (bitmap, upcase, label) that the
//!   `exFAT` driver expects to find at the cluster pointed to by
//!   `BootSector.FirstClusterOfRootDirectory`.
//!
//! ## Specification anchor
//!
//! Microsoft `exFAT` File System Specification v1.00 (August 27,
//! 2019). Directory entries are §6; the `SetChecksum` and
//! `NameHash` algorithms are §6.3.3 and §7.4. The five
//! primary/secondary entry types live in §7.1 – §7.7.
//!
//! ## What the root directory of an empty volume contains
//!
//! Per §7 the root directory of a valid `exFAT` volume MUST
//! contain (in order):
//!
//! 1. One Allocation Bitmap entry (`0x81`) per FAT (B-1 ships one
//!    FAT, so one `0x81`).
//! 2. One `UpCase` Table entry (`0x82`).
//! 3. Zero or one Volume Label entry (`0x83`); B-1 always emits
//!    one.
//! 4. Zero or more File entry sets (`0x85` + `0xC0` + `0xC1*`).
//!
//! End-of-directory is signalled by the first entry with the
//! `InUse` bit (`EntryType & 0x80`) clear, or by reaching the
//! end of the directory's cluster chain. Since the rest of the
//! root-directory cluster is zero-filled, the first 32-byte slot
//! past the synthesizer's emitted entries naturally ends the
//! directory.

use core::fmt;

use super::geometry::ExfatGeometry;
use super::upcase_table::UpcaseTable;
use crate::fs::geometry::Geometry;

/// Size of a single `exFAT` directory entry in bytes
/// (`exFAT` spec §6.1: "all directory entries are 32 bytes
/// long").
pub const DIRECTORY_ENTRY_SIZE_BYTES: usize = 32;

/// Maximum number of UTF-16 code units in a Volume Label
/// (`exFAT` spec §7.3.2 — the field holds 11 code units).
pub const MAX_VOLUME_LABEL_CODE_UNITS: usize = 11;

/// Maximum number of UTF-16 code units in a file name
/// (`exFAT` spec §7.7.2 — `NameLength` is a `u8` capped at 255).
pub const MAX_FILE_NAME_CODE_UNITS: usize = 255;

/// UTF-16 code units carried by a single File Name secondary
/// entry (`exFAT` spec §7.7.1).
pub const NAME_CODE_UNITS_PER_NAME_ENTRY: usize = 15;

/// `EntryType` byte for Allocation Bitmap (`exFAT` spec §7.1).
pub const ENTRY_TYPE_ALLOCATION_BITMAP: u8 = 0x81;

/// `EntryType` byte for `UpCase` Table (`exFAT` spec §7.2).
pub const ENTRY_TYPE_UPCASE_TABLE: u8 = 0x82;

/// `EntryType` byte for Volume Label (`exFAT` spec §7.3).
pub const ENTRY_TYPE_VOLUME_LABEL: u8 = 0x83;

/// `EntryType` byte for File (`exFAT` spec §7.4).
pub const ENTRY_TYPE_FILE: u8 = 0x85;

/// `EntryType` byte for Stream Extension (`exFAT` spec §7.6).
pub const ENTRY_TYPE_STREAM_EXTENSION: u8 = 0xC0;

/// `EntryType` byte for File Name (`exFAT` spec §7.7).
pub const ENTRY_TYPE_FILE_NAME: u8 = 0xC1;

const _: () = {
    assert!(DIRECTORY_ENTRY_SIZE_BYTES == 32);
    assert!(NAME_CODE_UNITS_PER_NAME_ENTRY == 15);
    assert!(MAX_VOLUME_LABEL_CODE_UNITS == 11);
    assert!(MAX_FILE_NAME_CODE_UNITS == 255);
};

/// File attribute bits (`exFAT` spec §7.4.5).
///
/// This struct mirrors the on-disk u16 bit field one-for-one;
/// the `struct_excessive_bools` lint is intentionally allowed
/// because the spec defines this as five independent flag bits.
#[allow(clippy::struct_excessive_bools)]
#[derive(Default, Clone, Copy, Debug, PartialEq, Eq)]
pub struct FileAttributes {
    /// Bit 0 — file may not be modified.
    pub read_only: bool,
    /// Bit 1 — file is hidden from default listings.
    pub hidden: bool,
    /// Bit 2 — file is owned by the operating system.
    pub system: bool,
    /// Bit 4 — entry describes a directory rather than a file.
    pub directory: bool,
    /// Bit 5 — file has been modified since the last backup.
    pub archive: bool,
}

impl FileAttributes {
    /// Pack the attribute bits into the on-disk u16 layout.
    #[must_use]
    pub const fn to_u16(self) -> u16 {
        let mut bits: u16 = 0;
        if self.read_only {
            bits |= 1 << 0;
        }
        if self.hidden {
            bits |= 1 << 1;
        }
        if self.system {
            bits |= 1 << 2;
        }
        if self.directory {
            bits |= 1 << 4;
        }
        if self.archive {
            bits |= 1 << 5;
        }
        bits
    }
}

/// `exFAT` timestamp fields for a File entry (`exFAT` spec §7.4.8 –
/// §7.4.13).
#[derive(Default, Clone, Copy, Debug, PartialEq, Eq)]
pub struct FileTimestamps {
    /// Packed local-time creation timestamp.
    pub create_timestamp: u32,
    /// Packed local-time last-modified timestamp.
    pub modify_timestamp: u32,
    /// Packed local-time last-accessed timestamp.
    pub access_timestamp: u32,
    /// Sub-second creation increment in units of 10 ms (0..199).
    pub create_10ms: u8,
    /// Sub-second modification increment in units of 10 ms.
    pub modify_10ms: u8,
    /// Signed UTC offset for the create timestamp (raw byte).
    pub create_utc_offset: u8,
    /// Signed UTC offset for the modify timestamp (raw byte).
    pub modify_utc_offset: u8,
    /// Signed UTC offset for the access timestamp (raw byte).
    pub access_utc_offset: u8,
}

/// Error returned by the directory builders when caller input
/// violates an `exFAT` constraint.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DirectoryError {
    /// Caller passed an empty file name; `exFAT` requires at
    /// least one UTF-16 code unit.
    EmptyName,
    /// File name exceeds the `u8` `NameLength` cap of 255 code
    /// units.
    NameTooLong {
        /// `MAX_FILE_NAME_CODE_UNITS`.
        max_code_units: usize,
        /// Number of UTF-16 code units the caller supplied.
        found: usize,
    },
    /// Volume label exceeds the 11-code-unit cap.
    LabelTooLong {
        /// `MAX_VOLUME_LABEL_CODE_UNITS`.
        max_code_units: usize,
        /// Number of UTF-16 code units the caller supplied.
        found: usize,
    },
    /// `synthesize_root_directory` was called with a cluster
    /// smaller than the bytes needed to hold the three required
    /// special entries (96 bytes).
    RootClusterTooSmall {
        /// Total entry bytes the caller asked us to lay out.
        needed_bytes: usize,
        /// Bytes available in the root cluster.
        cluster_bytes: usize,
    },
}

impl fmt::Display for DirectoryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyName => f.write_str("exFAT name must have at least one code unit"),
            Self::NameTooLong {
                max_code_units,
                found,
            } => write!(
                f,
                "exFAT name length {found} exceeds the spec maximum of {max_code_units}",
            ),
            Self::LabelTooLong {
                max_code_units,
                found,
            } => write!(
                f,
                "exFAT volume label length {found} exceeds the spec maximum of {max_code_units}",
            ),
            Self::RootClusterTooSmall {
                needed_bytes,
                cluster_bytes,
            } => write!(
                f,
                "exFAT root cluster ({cluster_bytes} bytes) is too small for the {needed_bytes} bytes of \
                 mandatory root directory entries",
            ),
        }
    }
}

impl core::error::Error for DirectoryError {}

// ============================================================
// Primitive checksums
// ============================================================

/// Compute the `exFAT` `SetChecksum` (`exFAT` spec §6.3.3) over a
/// sequence of 32-byte directory entries.
///
/// The checksum spans **every** byte of the entry set except
/// bytes 2 and 3 of the primary entry — those are where the
/// checksum itself lands, so they must be zero (or any value)
/// during computation. The algorithm is the same rotate-right-
/// then-add used by the boot and upcase checksums but uses 16-bit
/// arithmetic.
#[must_use]
pub fn set_checksum(entries: &[u8]) -> u16 {
    let mut checksum: u16 = 0;
    for (idx, &byte) in entries.iter().enumerate() {
        if idx == 2 || idx == 3 {
            continue;
        }
        checksum = checksum.rotate_right(1).wrapping_add(u16::from(byte));
    }
    checksum
}

/// Compute the `exFAT` `NameHash` (`exFAT` spec §7.4 /
/// `StreamExtension.NameHash`) over a UTF-16 name folded through
/// the supplied upcase table.
///
/// Spec algorithm:
///
/// 1. For each code unit in the name, look up its uppercase form
///    in the upcase table.
/// 2. Encode each uppercased code unit as two little-endian
///    bytes.
/// 3. Run those bytes through the rotate-right-then-add 16-bit
///    accumulator.
#[must_use]
pub fn name_hash(name: &[u16], upcase: &UpcaseTable) -> u16 {
    let mut hash: u16 = 0;
    for &code_unit in name {
        let upper = upcase.uppercase(code_unit);
        let bytes = upper.to_le_bytes();
        for &byte in &bytes {
            hash = hash.rotate_right(1).wrapping_add(u16::from(byte));
        }
    }
    hash
}

// ============================================================
// Single-entry encoders
// ============================================================

/// Encode an Allocation Bitmap directory entry (type `0x81`,
/// `exFAT` spec §7.1).
#[must_use]
pub fn encode_allocation_bitmap_entry(
    first_cluster: u32,
    size_bytes: u64,
) -> [u8; DIRECTORY_ENTRY_SIZE_BYTES] {
    let mut entry = [0_u8; DIRECTORY_ENTRY_SIZE_BYTES];
    write_byte(&mut entry, 0x00, ENTRY_TYPE_ALLOCATION_BITMAP);
    // 0x01 BitmapFlags: bit 0 = bitmap-index 0 (the only bitmap
    // we ship — this is the "first FAT" bitmap).
    write_byte(&mut entry, 0x01, 0);
    // 0x02..0x14 Reserved — already zero.
    write_u32_le(&mut entry, 0x14, first_cluster);
    write_u64_le(&mut entry, 0x18, size_bytes);
    entry
}

/// Encode an `UpCase` Table directory entry (type `0x82`,
/// `exFAT` spec §7.2).
#[must_use]
pub fn encode_upcase_table_entry(
    table_checksum: u32,
    first_cluster: u32,
    size_bytes: u64,
) -> [u8; DIRECTORY_ENTRY_SIZE_BYTES] {
    let mut entry = [0_u8; DIRECTORY_ENTRY_SIZE_BYTES];
    write_byte(&mut entry, 0x00, ENTRY_TYPE_UPCASE_TABLE);
    // 0x01..0x04 Reserved1 — already zero.
    write_u32_le(&mut entry, 0x04, table_checksum);
    // 0x08..0x14 Reserved2 — already zero.
    write_u32_le(&mut entry, 0x14, first_cluster);
    write_u64_le(&mut entry, 0x18, size_bytes);
    entry
}

/// Encode a Volume Label directory entry (type `0x83`,
/// `exFAT` spec §7.3). `label` is up to
/// [`MAX_VOLUME_LABEL_CODE_UNITS`] UTF-16 code units.
///
/// # Errors
///
/// Returns [`DirectoryError::LabelTooLong`] when `label` has
/// more than [`MAX_VOLUME_LABEL_CODE_UNITS`] code units.
pub fn encode_volume_label_entry(
    label: &[u16],
) -> Result<[u8; DIRECTORY_ENTRY_SIZE_BYTES], DirectoryError> {
    if label.len() > MAX_VOLUME_LABEL_CODE_UNITS {
        return Err(DirectoryError::LabelTooLong {
            max_code_units: MAX_VOLUME_LABEL_CODE_UNITS,
            found: label.len(),
        });
    }
    let mut entry = [0_u8; DIRECTORY_ENTRY_SIZE_BYTES];
    write_byte(&mut entry, 0x00, ENTRY_TYPE_VOLUME_LABEL);
    // CharacterCount fits because length is bounded by
    // MAX_VOLUME_LABEL_CODE_UNITS (11) above.
    let char_count = u8::try_from(label.len()).unwrap_or(0);
    write_byte(&mut entry, 0x01, char_count);
    for (i, &cu) in label.iter().enumerate() {
        let off = 0x02 + (i * 2);
        write_u16_le(&mut entry, off, cu);
    }
    // Trailing label code units and 0x18..0x20 Reserved already
    // zero.
    Ok(entry)
}

/// Encode a primary File directory entry (type `0x85`,
/// `exFAT` spec §7.4).
///
/// `secondary_count` MUST equal the total number of secondary
/// entries that will follow this primary (1 Stream Extension +
/// N File Name = `1 + ceil(name_len / 15)`).
///
/// `entry_set_checksum` MUST be the §6.3.3 `SetChecksum` computed
/// over the WHOLE entry set after the secondary entries are
/// constructed (bytes 2 and 3 of *this* entry are excluded by the
/// algorithm).
#[must_use]
pub fn encode_file_entry(
    attributes: FileAttributes,
    timestamps: &FileTimestamps,
    secondary_count: u8,
    entry_set_checksum: u16,
) -> [u8; DIRECTORY_ENTRY_SIZE_BYTES] {
    let mut entry = [0_u8; DIRECTORY_ENTRY_SIZE_BYTES];
    write_byte(&mut entry, 0x00, ENTRY_TYPE_FILE);
    write_byte(&mut entry, 0x01, secondary_count);
    write_u16_le(&mut entry, 0x02, entry_set_checksum);
    write_u16_le(&mut entry, 0x04, attributes.to_u16());
    // 0x06..0x08 Reserved1 — already zero.
    write_u32_le(&mut entry, 0x08, timestamps.create_timestamp);
    write_u32_le(&mut entry, 0x0C, timestamps.modify_timestamp);
    write_u32_le(&mut entry, 0x10, timestamps.access_timestamp);
    write_byte(&mut entry, 0x14, timestamps.create_10ms);
    write_byte(&mut entry, 0x15, timestamps.modify_10ms);
    write_byte(&mut entry, 0x16, timestamps.create_utc_offset);
    write_byte(&mut entry, 0x17, timestamps.modify_utc_offset);
    write_byte(&mut entry, 0x18, timestamps.access_utc_offset);
    // 0x19..0x20 Reserved2 — already zero.
    entry
}

/// Stream Extension secondary flags (`exFAT` spec §7.6.3).
///
/// `AllocationPossible` (bit 0) is set whenever a file has any
/// data; `NoFatChain` (bit 1) is set when the file's data is
/// laid out contiguously and therefore needs no chain in the
/// FAT — every file the synthesizer produces is contiguous.
#[must_use]
pub const fn stream_secondary_flags(allocation_possible: bool, no_fat_chain: bool) -> u8 {
    let mut bits: u8 = 0;
    if allocation_possible {
        bits |= 1 << 0;
    }
    if no_fat_chain {
        bits |= 1 << 1;
    }
    bits
}

/// Encode a Stream Extension secondary entry (type `0xC0`,
/// `exFAT` spec §7.6).
///
/// `name_length` is the number of UTF-16 code units in the
/// caller's UTF-16 name (NOT bytes). `name_hash_value` MUST be
/// the §7.4 `NameHash` of the same UTF-16 name folded through
/// the volume's upcase table.
#[must_use]
pub fn encode_stream_extension_entry(
    secondary_flags: u8,
    name_length: u8,
    name_hash_value: u16,
    valid_data_length: u64,
    first_cluster: u32,
    data_length: u64,
) -> [u8; DIRECTORY_ENTRY_SIZE_BYTES] {
    let mut entry = [0_u8; DIRECTORY_ENTRY_SIZE_BYTES];
    write_byte(&mut entry, 0x00, ENTRY_TYPE_STREAM_EXTENSION);
    write_byte(&mut entry, 0x01, secondary_flags);
    // 0x02 Reserved1 — already zero.
    write_byte(&mut entry, 0x03, name_length);
    write_u16_le(&mut entry, 0x04, name_hash_value);
    // 0x06..0x08 Reserved2 — already zero.
    write_u64_le(&mut entry, 0x08, valid_data_length);
    // 0x10..0x14 Reserved3 — already zero.
    write_u32_le(&mut entry, 0x14, first_cluster);
    write_u64_le(&mut entry, 0x18, data_length);
    entry
}

/// Encode one File Name secondary entry (type `0xC1`,
/// `exFAT` spec §7.7) carrying up to
/// [`NAME_CODE_UNITS_PER_NAME_ENTRY`] = 15 UTF-16 code units.
/// Shorter chunks are padded with zero code units per spec.
#[must_use]
pub fn encode_file_name_entry(name_chunk: &[u16]) -> [u8; DIRECTORY_ENTRY_SIZE_BYTES] {
    debug_assert!(
        name_chunk.len() <= NAME_CODE_UNITS_PER_NAME_ENTRY,
        "name chunk exceeds 15 code units",
    );
    let mut entry = [0_u8; DIRECTORY_ENTRY_SIZE_BYTES];
    write_byte(&mut entry, 0x00, ENTRY_TYPE_FILE_NAME);
    // 0x01 GeneralSecondaryFlags = 0 for File Name entries
    // (spec §7.7.3 — "all bits reserved").
    write_byte(&mut entry, 0x01, 0);
    let limit = name_chunk.len().min(NAME_CODE_UNITS_PER_NAME_ENTRY);
    for (i, &cu) in name_chunk.iter().enumerate().take(limit) {
        let off = 0x02 + (i * 2);
        write_u16_le(&mut entry, off, cu);
    }
    entry
}

// ============================================================
// High-level entry-set builder
// ============================================================

/// Parameters for a single regular file or directory entry set.
#[derive(Clone, Debug)]
pub struct FileEntrySetParams<'a> {
    /// UTF-16 code units of the file name (1..=255).
    pub name: &'a [u16],
    /// Attributes for the File entry (RO / hidden / system /
    /// directory / archive).
    pub attributes: FileAttributes,
    /// Timestamps for the File entry.
    pub timestamps: FileTimestamps,
    /// First cluster of the file's data (or directory's cluster
    /// chain). 0 for empty files per `exFAT` spec §7.6.7.
    pub first_cluster: u32,
    /// Number of bytes of file data already valid
    /// (`ValidDataLength`, `exFAT` spec §7.6.5).
    pub valid_data_length: u64,
    /// Total bytes of file data (`DataLength`, `exFAT` spec
    /// §7.6.8). Must be `>= valid_data_length`.
    pub data_length: u64,
    /// `true` if the file's data is contiguous (no FAT chain
    /// needed). Every file the B-1 synthesizer ships is
    /// contiguous because we lay each file out in a single
    /// extent of consecutive clusters.
    pub no_fat_chain: bool,
}

/// Build a complete entry set (one File primary + one Stream
/// Extension + N File Name secondaries) for a single file or
/// directory, with both the `SetChecksum` and `NameHash` computed
/// and stamped into the right fields.
///
/// Returns `(1 + secondary_count) * 32` bytes.
///
/// # Errors
///
/// * [`DirectoryError::EmptyName`] if `params.name` is empty.
/// * [`DirectoryError::NameTooLong`] if `params.name` exceeds
///   [`MAX_FILE_NAME_CODE_UNITS`].
pub fn encode_file_entry_set(
    params: &FileEntrySetParams<'_>,
    upcase: &UpcaseTable,
) -> Result<Vec<u8>, DirectoryError> {
    if params.name.is_empty() {
        return Err(DirectoryError::EmptyName);
    }
    if params.name.len() > MAX_FILE_NAME_CODE_UNITS {
        return Err(DirectoryError::NameTooLong {
            max_code_units: MAX_FILE_NAME_CODE_UNITS,
            found: params.name.len(),
        });
    }
    let name_length = u8::try_from(params.name.len()).map_err(|_| DirectoryError::NameTooLong {
        max_code_units: MAX_FILE_NAME_CODE_UNITS,
        found: params.name.len(),
    })?;

    // Number of File Name entries = ceil(name_length / 15).
    let name_entry_count = params.name.len().div_ceil(NAME_CODE_UNITS_PER_NAME_ENTRY);
    // secondary_count = 1 stream-ext + N name entries; bounded
    // by ceil(255/15) + 1 = 18, so always fits in u8.
    let secondary_count = u8::try_from(name_entry_count + 1).unwrap_or(u8::MAX);

    let hash = name_hash(params.name, upcase);
    let flags = stream_secondary_flags(true, params.no_fat_chain);

    let stream = encode_stream_extension_entry(
        flags,
        name_length,
        hash,
        params.valid_data_length,
        params.first_cluster,
        params.data_length,
    );

    let mut entries =
        Vec::with_capacity((1 + usize::from(secondary_count)) * DIRECTORY_ENTRY_SIZE_BYTES);

    // Lay out the primary first with a placeholder checksum of
    // 0; rewrite bytes 2..4 after computing the SetChecksum.
    let primary_placeholder =
        encode_file_entry(params.attributes, &params.timestamps, secondary_count, 0);
    entries.extend_from_slice(&primary_placeholder);
    entries.extend_from_slice(&stream);

    for chunk in params.name.chunks(NAME_CODE_UNITS_PER_NAME_ENTRY) {
        let name_entry = encode_file_name_entry(chunk);
        entries.extend_from_slice(&name_entry);
    }

    let checksum = set_checksum(&entries);
    // Bytes 2..4 of the entry set is the primary's SetChecksum.
    let checksum_bytes = checksum.to_le_bytes();
    #[allow(clippy::indexing_slicing)] // bytes 2..4 always exist: entries has at least 32 bytes
    {
        entries[2] = checksum_bytes[0];
        entries[3] = checksum_bytes[1];
    }
    Ok(entries)
}

// ============================================================
// Root directory synthesizer
// ============================================================

/// Inputs needed to synthesize the volume's root directory.
#[derive(Clone, Debug)]
pub struct RootDirectoryParams<'a> {
    /// First cluster of the allocation bitmap stream.
    pub bitmap_first_cluster: u32,
    /// Bitmap stream size in bytes (matches
    /// `AllocationBitmap::size_bytes()`).
    pub bitmap_size_bytes: u64,
    /// First cluster of the upcase table stream.
    pub upcase_first_cluster: u32,
    /// `Upcase` table size in bytes. Always equals
    /// [`crate::fs::exfat::upcase_table::UPCASE_TABLE_SIZE_BYTES`]
    /// (`256` for the ASCII-fold table; see that module for why
    /// the table is intentionally kept small).
    pub upcase_size_bytes: u64,
    /// Cached upcase checksum (`UpcaseTable::checksum`).
    pub upcase_checksum: u32,
    /// UTF-16 volume label (0..=11 code units).
    pub volume_label_utf16: &'a [u16],
}

/// Build the synthesized root directory as a single
/// zero-padded cluster of bytes ready for the Phase 2.11
/// dispatcher.
///
/// Layout (per `exFAT` spec §7):
///
/// | Offset | Size | Entry |
/// |---|---|---|
/// | 0x00 | 32 | Allocation Bitmap (0x81) |
/// | 0x20 | 32 | `UpCase` Table (0x82) |
/// | 0x40 | 32 | Volume Label (0x83) |
/// | 0x60.. | rest | Zero-filled (terminates the directory) |
///
/// # Errors
///
/// * [`DirectoryError::LabelTooLong`] if
///   `params.volume_label_utf16` exceeds
///   [`MAX_VOLUME_LABEL_CODE_UNITS`].
/// * [`DirectoryError::RootClusterTooSmall`] if the geometry's
///   bytes-per-cluster is smaller than `3 *
///   DIRECTORY_ENTRY_SIZE_BYTES` (96 bytes).
pub fn synthesize_root_directory(
    geometry: &ExfatGeometry,
    params: &RootDirectoryParams<'_>,
) -> Result<Vec<u8>, DirectoryError> {
    let cluster_bytes = geometry.bytes_per_cluster() as usize;
    let needed_bytes = 3 * DIRECTORY_ENTRY_SIZE_BYTES;
    if cluster_bytes < needed_bytes {
        return Err(DirectoryError::RootClusterTooSmall {
            needed_bytes,
            cluster_bytes,
        });
    }

    let bitmap =
        encode_allocation_bitmap_entry(params.bitmap_first_cluster, params.bitmap_size_bytes);
    let upcase = encode_upcase_table_entry(
        params.upcase_checksum,
        params.upcase_first_cluster,
        params.upcase_size_bytes,
    );
    let label = encode_volume_label_entry(params.volume_label_utf16)?;

    let mut buf = vec![0_u8; cluster_bytes];
    #[allow(clippy::indexing_slicing)]
    // cluster_bytes >= 96 verified above; entries are fixed 32 bytes
    {
        buf[0x00..0x20].copy_from_slice(&bitmap);
        buf[0x20..0x40].copy_from_slice(&upcase);
        buf[0x40..0x60].copy_from_slice(&label);
    }
    Ok(buf)
}

// ============================================================
// Private write helpers (compile-time-constant offsets)
// ============================================================

/// All `write_*_le` helpers index at compile-time-constant
/// offsets into a 32-byte `[u8; 32]` buffer or a `Vec<u8>`
/// whose length is guaranteed to be a multiple of 32. Indexing
/// cannot panic.
#[allow(clippy::indexing_slicing)]
fn write_byte(buf: &mut [u8], off: usize, value: u8) {
    buf[off] = value;
}

#[allow(clippy::indexing_slicing)]
fn write_u16_le(buf: &mut [u8], off: usize, value: u16) {
    let bytes = value.to_le_bytes();
    buf[off] = bytes[0];
    buf[off + 1] = bytes[1];
}

#[allow(clippy::indexing_slicing)]
fn write_u32_le(buf: &mut [u8], off: usize, value: u32) {
    let bytes = value.to_le_bytes();
    buf[off] = bytes[0];
    buf[off + 1] = bytes[1];
    buf[off + 2] = bytes[2];
    buf[off + 3] = bytes[3];
}

#[allow(clippy::indexing_slicing)]
fn write_u64_le(buf: &mut [u8], off: usize, value: u64) {
    let bytes = value.to_le_bytes();
    buf[off..off + 8].copy_from_slice(&bytes);
}

// ============================================================
// Tests
// ============================================================

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

    fn geometry_64mib() -> ExfatGeometry {
        ExfatGeometry::for_volume_size(64 * 1024 * 1024).unwrap()
    }

    fn label_for(s: &str) -> Vec<u16> {
        s.encode_utf16().collect()
    }

    // ---------- Constants ----------

    #[test]
    fn directory_entry_size_is_32_bytes() {
        assert_eq!(DIRECTORY_ENTRY_SIZE_BYTES, 32);
    }

    #[test]
    fn entry_type_constants_match_spec() {
        assert_eq!(ENTRY_TYPE_ALLOCATION_BITMAP, 0x81);
        assert_eq!(ENTRY_TYPE_UPCASE_TABLE, 0x82);
        assert_eq!(ENTRY_TYPE_VOLUME_LABEL, 0x83);
        assert_eq!(ENTRY_TYPE_FILE, 0x85);
        assert_eq!(ENTRY_TYPE_STREAM_EXTENSION, 0xC0);
        assert_eq!(ENTRY_TYPE_FILE_NAME, 0xC1);
    }

    // ---------- FileAttributes packing ----------

    #[test]
    fn file_attributes_default_is_zero() {
        assert_eq!(FileAttributes::default().to_u16(), 0);
    }

    #[test]
    fn file_attributes_read_only_bit() {
        let a = FileAttributes {
            read_only: true,
            ..FileAttributes::default()
        };
        assert_eq!(a.to_u16(), 0x0001);
    }

    #[test]
    fn file_attributes_directory_bit() {
        let a = FileAttributes {
            directory: true,
            ..FileAttributes::default()
        };
        assert_eq!(a.to_u16(), 0x0010);
    }

    #[test]
    fn file_attributes_archive_bit() {
        let a = FileAttributes {
            archive: true,
            ..FileAttributes::default()
        };
        assert_eq!(a.to_u16(), 0x0020);
    }

    #[test]
    fn file_attributes_all_set() {
        let a = FileAttributes {
            read_only: true,
            hidden: true,
            system: true,
            directory: true,
            archive: true,
        };
        // bits 0, 1, 2, 4, 5 = 0b0011_0111 = 0x37
        assert_eq!(a.to_u16(), 0x0037);
    }

    // ---------- Allocation Bitmap entry (0x81) ----------

    #[test]
    fn allocation_bitmap_entry_type_byte_is_0x81() {
        let entry = encode_allocation_bitmap_entry(3, 8192);
        assert_eq!(entry[0], 0x81);
    }

    #[test]
    fn allocation_bitmap_entry_first_cluster_at_offset_0x14() {
        let entry = encode_allocation_bitmap_entry(0xDEAD_BEEF, 0);
        assert_eq!(&entry[0x14..0x18], &[0xEF, 0xBE, 0xAD, 0xDE]);
    }

    #[test]
    fn allocation_bitmap_entry_size_at_offset_0x18() {
        let entry = encode_allocation_bitmap_entry(0, 0x0123_4567_89AB_CDEF);
        assert_eq!(
            &entry[0x18..0x20],
            &[0xEF, 0xCD, 0xAB, 0x89, 0x67, 0x45, 0x23, 0x01]
        );
    }

    #[test]
    fn allocation_bitmap_entry_reserved_bytes_are_zero() {
        let entry = encode_allocation_bitmap_entry(3, 8192);
        // BitmapFlags + Reserved bytes (0x01..0x14).
        for &b in &entry[0x01..0x14] {
            assert_eq!(b, 0);
        }
    }

    #[test]
    fn allocation_bitmap_entry_is_exactly_32_bytes() {
        let entry = encode_allocation_bitmap_entry(3, 8192);
        assert_eq!(entry.len(), DIRECTORY_ENTRY_SIZE_BYTES);
    }

    // ---------- UpCase Table entry (0x82) ----------

    #[test]
    fn upcase_table_entry_type_byte_is_0x82() {
        let entry = encode_upcase_table_entry(0x1234_5678, 5, 131_072);
        assert_eq!(entry[0], 0x82);
    }

    #[test]
    fn upcase_table_entry_checksum_at_offset_0x04() {
        let entry = encode_upcase_table_entry(0xCAFE_BABE, 0, 0);
        assert_eq!(&entry[0x04..0x08], &[0xBE, 0xBA, 0xFE, 0xCA]);
    }

    #[test]
    fn upcase_table_entry_first_cluster_at_offset_0x14() {
        let entry = encode_upcase_table_entry(0, 0x0000_00AA, 131_072);
        assert_eq!(&entry[0x14..0x18], &[0xAA, 0x00, 0x00, 0x00]);
    }

    #[test]
    fn upcase_table_entry_size_at_offset_0x18() {
        let entry = encode_upcase_table_entry(0, 0, 131_072);
        assert_eq!(
            &entry[0x18..0x20],
            &[0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00]
        );
    }

    #[test]
    fn upcase_table_entry_reserved2_is_zero() {
        let entry = encode_upcase_table_entry(0xFFFF_FFFF, 0xFFFF_FFFF, u64::MAX);
        for &b in &entry[0x08..0x14] {
            assert_eq!(b, 0);
        }
    }

    // ---------- Volume Label entry (0x83) ----------

    #[test]
    fn volume_label_entry_type_byte_is_0x83() {
        let entry = encode_volume_label_entry(&label_for("TESLA")).unwrap();
        assert_eq!(entry[0], 0x83);
    }

    #[test]
    fn volume_label_entry_character_count() {
        let entry = encode_volume_label_entry(&label_for("TESLA")).unwrap();
        assert_eq!(entry[1], 5);
    }

    #[test]
    fn volume_label_entry_label_bytes_are_utf16_le() {
        let entry = encode_volume_label_entry(&label_for("AB")).unwrap();
        assert_eq!(&entry[0x02..0x06], &[b'A', 0, b'B', 0]);
    }

    #[test]
    fn volume_label_entry_unused_bytes_are_zero() {
        let entry = encode_volume_label_entry(&label_for("AB")).unwrap();
        // After "AB" (4 bytes) the rest of the 22-byte label
        // field through 0x18 must be zero.
        for &b in &entry[0x06..0x18] {
            assert_eq!(b, 0);
        }
    }

    #[test]
    fn volume_label_empty_is_allowed() {
        let entry = encode_volume_label_entry(&[]).unwrap();
        assert_eq!(entry[0], 0x83);
        assert_eq!(entry[1], 0);
    }

    #[test]
    fn volume_label_at_maximum_length_succeeds() {
        let entry = encode_volume_label_entry(&label_for("HELLOWORLD!")).unwrap();
        assert_eq!(entry[1], 11);
    }

    #[test]
    fn volume_label_too_long_returns_error() {
        let too_long: Vec<u16> = (0..12).collect();
        let err = encode_volume_label_entry(&too_long).unwrap_err();
        assert!(matches!(
            err,
            DirectoryError::LabelTooLong {
                max_code_units: 11,
                found: 12,
            }
        ));
    }

    // ---------- File entry (0x85) ----------

    fn ts_sample() -> FileTimestamps {
        FileTimestamps {
            create_timestamp: 0x1111_2222,
            modify_timestamp: 0x3333_4444,
            access_timestamp: 0x5555_6666,
            create_10ms: 10,
            modify_10ms: 20,
            create_utc_offset: 0xA0,
            modify_utc_offset: 0xA0,
            access_utc_offset: 0xA0,
        }
    }

    #[test]
    fn file_entry_type_byte_is_0x85() {
        let entry = encode_file_entry(FileAttributes::default(), &ts_sample(), 2, 0xABCD);
        assert_eq!(entry[0], 0x85);
    }

    #[test]
    fn file_entry_secondary_count_at_offset_1() {
        let entry = encode_file_entry(FileAttributes::default(), &ts_sample(), 7, 0);
        assert_eq!(entry[1], 7);
    }

    #[test]
    fn file_entry_set_checksum_at_offset_2() {
        let entry = encode_file_entry(FileAttributes::default(), &ts_sample(), 2, 0xCAFE);
        assert_eq!(&entry[2..4], &[0xFE, 0xCA]);
    }

    #[test]
    fn file_entry_attributes_at_offset_4() {
        let attrs = FileAttributes {
            archive: true,
            ..FileAttributes::default()
        };
        let entry = encode_file_entry(attrs, &ts_sample(), 2, 0);
        assert_eq!(&entry[4..6], &[0x20, 0x00]);
    }

    #[test]
    fn file_entry_timestamps_at_correct_offsets() {
        let ts = ts_sample();
        let entry = encode_file_entry(FileAttributes::default(), &ts, 2, 0);
        assert_eq!(&entry[0x08..0x0C], &ts.create_timestamp.to_le_bytes());
        assert_eq!(&entry[0x0C..0x10], &ts.modify_timestamp.to_le_bytes());
        assert_eq!(&entry[0x10..0x14], &ts.access_timestamp.to_le_bytes());
        assert_eq!(entry[0x14], ts.create_10ms);
        assert_eq!(entry[0x15], ts.modify_10ms);
        assert_eq!(entry[0x16], ts.create_utc_offset);
        assert_eq!(entry[0x17], ts.modify_utc_offset);
        assert_eq!(entry[0x18], ts.access_utc_offset);
    }

    // ---------- Stream Extension (0xC0) ----------

    #[test]
    fn stream_secondary_flags_packing() {
        assert_eq!(stream_secondary_flags(false, false), 0);
        assert_eq!(stream_secondary_flags(true, false), 0x01);
        assert_eq!(stream_secondary_flags(false, true), 0x02);
        assert_eq!(stream_secondary_flags(true, true), 0x03);
    }

    #[test]
    fn stream_entry_type_byte_is_0xc0() {
        let entry = encode_stream_extension_entry(0x03, 5, 0xABCD, 100, 7, 200);
        assert_eq!(entry[0], 0xC0);
    }

    #[test]
    fn stream_entry_flags_name_length_and_hash_at_correct_offsets() {
        let entry = encode_stream_extension_entry(0x03, 8, 0xBEEF, 0, 0, 0);
        assert_eq!(entry[0x01], 0x03);
        assert_eq!(entry[0x03], 8);
        assert_eq!(&entry[0x04..0x06], &[0xEF, 0xBE]);
    }

    #[test]
    fn stream_entry_lengths_and_first_cluster_at_correct_offsets() {
        let entry = encode_stream_extension_entry(
            0x01,
            1,
            0,
            0x0011_2233_4455_6677,
            0xCAFE_BABE,
            0x0011_2233_4455_6677,
        );
        assert_eq!(
            &entry[0x08..0x10],
            &[0x77, 0x66, 0x55, 0x44, 0x33, 0x22, 0x11, 0x00]
        );
        assert_eq!(&entry[0x14..0x18], &[0xBE, 0xBA, 0xFE, 0xCA]);
        assert_eq!(
            &entry[0x18..0x20],
            &[0x77, 0x66, 0x55, 0x44, 0x33, 0x22, 0x11, 0x00]
        );
    }

    // ---------- File Name entry (0xC1) ----------

    #[test]
    fn file_name_entry_type_byte_is_0xc1() {
        let entry = encode_file_name_entry(&label_for("HELLO"));
        assert_eq!(entry[0], 0xC1);
    }

    #[test]
    fn file_name_entry_holds_utf16_le() {
        let entry = encode_file_name_entry(&label_for("AB"));
        assert_eq!(entry[0x02], b'A');
        assert_eq!(entry[0x03], 0);
        assert_eq!(entry[0x04], b'B');
        assert_eq!(entry[0x05], 0);
    }

    #[test]
    fn file_name_entry_trailing_bytes_are_zero() {
        let entry = encode_file_name_entry(&label_for("AB"));
        // After "AB" (4 bytes) through end of name field (0x20).
        for &b in &entry[0x06..0x20] {
            assert_eq!(b, 0);
        }
    }

    #[test]
    fn file_name_entry_full_15_code_units() {
        let cu_15 = label_for("HelloWorld!2026");
        assert_eq!(cu_15.len(), 15);
        let entry = encode_file_name_entry(&cu_15);
        for (i, &cu) in cu_15.iter().enumerate() {
            let off = 0x02 + (i * 2);
            assert_eq!(u16::from_le_bytes([entry[off], entry[off + 1]]), cu);
        }
    }

    // ---------- NameHash ----------

    #[test]
    fn name_hash_of_empty_name_is_zero() {
        let upcase = UpcaseTable::ascii_identity();
        assert_eq!(name_hash(&[], &upcase), 0);
    }

    #[test]
    fn name_hash_folds_case_via_upcase_table() {
        let upcase = UpcaseTable::ascii_identity();
        let lower = label_for("hello");
        let upper = label_for("HELLO");
        assert_eq!(name_hash(&lower, &upcase), name_hash(&upper, &upcase));
    }

    #[test]
    fn name_hash_is_sensitive_to_a_single_byte_change() {
        let upcase = UpcaseTable::ascii_identity();
        let a = label_for("HELLO");
        let b = label_for("HELLP");
        assert_ne!(name_hash(&a, &upcase), name_hash(&b, &upcase));
    }

    #[test]
    fn name_hash_matches_independent_reference() {
        let upcase = UpcaseTable::ascii_identity();
        let name = label_for("hello.MP4");
        let folded: Vec<u16> = name.iter().map(|&c| upcase.uppercase(c)).collect();
        let mut reference: u16 = 0;
        for cu in folded {
            for byte in cu.to_le_bytes() {
                let rotated = if reference & 1 != 0 {
                    (reference >> 1) | 0x8000
                } else {
                    reference >> 1
                };
                reference = rotated.wrapping_add(u16::from(byte));
            }
        }
        assert_eq!(name_hash(&name, &upcase), reference);
    }

    // ---------- SetChecksum ----------

    #[test]
    fn set_checksum_of_empty_input_is_zero() {
        assert_eq!(set_checksum(&[]), 0);
    }

    #[test]
    fn set_checksum_skips_offsets_2_and_3() {
        // 32-byte buffer with non-zero data only at offsets 2/3
        // produces the same checksum as the all-zero buffer.
        let mut buf = [0_u8; 32];
        buf[2] = 0xDE;
        buf[3] = 0xAD;
        assert_eq!(set_checksum(&buf), 0);
    }

    #[test]
    fn set_checksum_changes_with_any_other_byte() {
        let mut buf = [0_u8; 96];
        let baseline = set_checksum(&buf);
        buf[4] = 0xFF;
        assert_ne!(set_checksum(&buf), baseline);
    }

    #[test]
    fn set_checksum_matches_independent_reference() {
        let buf: Vec<u8> = (0..96_u8).collect();
        let mut reference: u16 = 0;
        for (i, &b) in buf.iter().enumerate() {
            if i == 2 || i == 3 {
                continue;
            }
            let rotated = if reference & 1 != 0 {
                (reference >> 1) | 0x8000
            } else {
                reference >> 1
            };
            reference = rotated.wrapping_add(u16::from(b));
        }
        assert_eq!(set_checksum(&buf), reference);
    }

    // ---------- encode_file_entry_set ----------

    fn sample_params(name: &[u16]) -> FileEntrySetParams<'_> {
        FileEntrySetParams {
            name,
            attributes: FileAttributes {
                archive: true,
                ..FileAttributes::default()
            },
            timestamps: ts_sample(),
            first_cluster: 5,
            valid_data_length: 1024,
            data_length: 1024,
            no_fat_chain: true,
        }
    }

    #[test]
    fn entry_set_short_name_has_three_entries() {
        let upcase = UpcaseTable::ascii_identity();
        let name = label_for("HELLO.MP4");
        let bytes = encode_file_entry_set(&sample_params(&name), &upcase).unwrap();
        // primary + stream + 1 name (9 cu fits in one slot).
        assert_eq!(bytes.len(), 3 * DIRECTORY_ENTRY_SIZE_BYTES);
        assert_eq!(bytes[0], 0x85);
        assert_eq!(bytes[0x20], 0xC0);
        assert_eq!(bytes[0x40], 0xC1);
    }

    #[test]
    fn entry_set_secondary_count_at_offset_1_matches_spec() {
        let upcase = UpcaseTable::ascii_identity();
        let name = label_for("HELLO.MP4");
        let bytes = encode_file_entry_set(&sample_params(&name), &upcase).unwrap();
        // 1 stream-ext + 1 file-name = 2 secondaries.
        assert_eq!(bytes[1], 2);
    }

    #[test]
    fn entry_set_long_name_spans_multiple_name_entries() {
        let upcase = UpcaseTable::ascii_identity();
        // 31 code units → ceil(31/15) = 3 name entries.
        let name = label_for("2026-05-19_22-15-03-FRONT.MP4__");
        assert_eq!(name.len(), 31);
        let bytes = encode_file_entry_set(&sample_params(&name), &upcase).unwrap();
        // primary + stream + 3 name entries = 5 entries.
        assert_eq!(bytes.len(), 5 * DIRECTORY_ENTRY_SIZE_BYTES);
        // Secondary count = 4.
        assert_eq!(bytes[1], 4);
        // Last 3 entries are 0xC1.
        assert_eq!(bytes[0x40], 0xC1);
        assert_eq!(bytes[0x60], 0xC1);
        assert_eq!(bytes[0x80], 0xC1);
    }

    #[test]
    fn entry_set_name_hash_stamped_into_stream_entry() {
        let upcase = UpcaseTable::ascii_identity();
        let name = label_for("HELLO.MP4");
        let bytes = encode_file_entry_set(&sample_params(&name), &upcase).unwrap();
        let expected = name_hash(&name, &upcase);
        let stamped = u16::from_le_bytes([bytes[0x20 + 0x04], bytes[0x20 + 0x05]]);
        assert_eq!(stamped, expected);
    }

    #[test]
    fn entry_set_checksum_verifies() {
        let upcase = UpcaseTable::ascii_identity();
        let name = label_for("HELLO.MP4");
        let bytes = encode_file_entry_set(&sample_params(&name), &upcase).unwrap();
        // After stamping, recomputing the SetChecksum over the
        // whole set (with offsets 2/3 skipped) must equal the
        // value stored at offsets 2/3.
        let stored = u16::from_le_bytes([bytes[2], bytes[3]]);
        let recomputed = set_checksum(&bytes);
        assert_eq!(stored, recomputed);
    }

    #[test]
    fn entry_set_empty_name_returns_error() {
        let upcase = UpcaseTable::ascii_identity();
        let err = encode_file_entry_set(&sample_params(&[]), &upcase).unwrap_err();
        assert!(matches!(err, DirectoryError::EmptyName));
    }

    #[test]
    fn entry_set_overlong_name_returns_error() {
        let upcase = UpcaseTable::ascii_identity();
        let too_long: Vec<u16> = (0..256).map(|i| u16::from(b'A') + (i % 26)).collect();
        let err = encode_file_entry_set(&sample_params(&too_long), &upcase).unwrap_err();
        assert!(matches!(
            err,
            DirectoryError::NameTooLong {
                max_code_units: MAX_FILE_NAME_CODE_UNITS,
                found: 256,
            }
        ));
    }

    #[test]
    fn entry_set_at_maximum_name_length_succeeds() {
        let upcase = UpcaseTable::ascii_identity();
        let name: Vec<u16> = (0..255).map(|i| u16::from(b'A') + (i % 26)).collect();
        let bytes = encode_file_entry_set(&sample_params(&name), &upcase).unwrap();
        // 255 / 15 = 17 name entries.
        assert_eq!(bytes.len(), (1 + 1 + 17) * DIRECTORY_ENTRY_SIZE_BYTES);
        assert_eq!(bytes[1], 18); // secondary_count = 1 + 17
    }

    #[test]
    fn entry_set_no_fat_chain_flag_propagates() {
        let upcase = UpcaseTable::ascii_identity();
        let name = label_for("X");
        let mut params = sample_params(&name);
        params.no_fat_chain = false;
        let bytes = encode_file_entry_set(&params, &upcase).unwrap();
        // Stream Extension secondary flags at byte 0x21
        // (= 0x20 + 0x01): AllocationPossible only.
        assert_eq!(bytes[0x21], 0x01);

        params.no_fat_chain = true;
        let bytes2 = encode_file_entry_set(&params, &upcase).unwrap();
        // AllocationPossible | NoFatChain.
        assert_eq!(bytes2[0x21], 0x03);
    }

    // ---------- Root directory synthesizer ----------

    #[test]
    fn root_directory_size_equals_one_cluster() {
        let g = geometry_64mib();
        let cluster_bytes = g.bytes_per_cluster() as usize;
        let params = RootDirectoryParams {
            bitmap_first_cluster: 2,
            bitmap_size_bytes: 8192,
            upcase_first_cluster: 4,
            upcase_size_bytes: 131_072,
            upcase_checksum: 0xDEAD_BEEF,
            volume_label_utf16: &label_for("TESLACAM"),
        };
        let buf = synthesize_root_directory(&g, &params).unwrap();
        assert_eq!(buf.len(), cluster_bytes);
    }

    #[test]
    fn root_directory_first_three_entries_are_special() {
        let g = geometry_64mib();
        let params = RootDirectoryParams {
            bitmap_first_cluster: 2,
            bitmap_size_bytes: 8192,
            upcase_first_cluster: 4,
            upcase_size_bytes: 131_072,
            upcase_checksum: 0xDEAD_BEEF,
            volume_label_utf16: &label_for("TESLACAM"),
        };
        let buf = synthesize_root_directory(&g, &params).unwrap();
        assert_eq!(buf[0x00], 0x81);
        assert_eq!(buf[0x20], 0x82);
        assert_eq!(buf[0x40], 0x83);
    }

    #[test]
    fn root_directory_fourth_entry_is_end_marker_zero() {
        let g = geometry_64mib();
        let params = RootDirectoryParams {
            bitmap_first_cluster: 2,
            bitmap_size_bytes: 8192,
            upcase_first_cluster: 4,
            upcase_size_bytes: 131_072,
            upcase_checksum: 0xDEAD_BEEF,
            volume_label_utf16: &label_for("TESLACAM"),
        };
        let buf = synthesize_root_directory(&g, &params).unwrap();
        // First byte of the 4th 32-byte slot must be zero
        // (= end-of-directory per spec §7.1.1.2).
        assert_eq!(buf[0x60], 0);
    }

    #[test]
    fn root_directory_bitmap_cluster_serialized_correctly() {
        let g = geometry_64mib();
        let params = RootDirectoryParams {
            bitmap_first_cluster: 0x1234_5678,
            bitmap_size_bytes: 0xABCD,
            upcase_first_cluster: 0,
            upcase_size_bytes: 0,
            upcase_checksum: 0,
            volume_label_utf16: &[],
        };
        let buf = synthesize_root_directory(&g, &params).unwrap();
        assert_eq!(&buf[0x14..0x18], &[0x78, 0x56, 0x34, 0x12]);
        assert_eq!(&buf[0x18..0x20], &0xABCD_u64.to_le_bytes());
    }

    #[test]
    fn root_directory_upcase_fields_serialized_correctly() {
        let g = geometry_64mib();
        let params = RootDirectoryParams {
            bitmap_first_cluster: 0,
            bitmap_size_bytes: 0,
            upcase_first_cluster: 0x000A_BCDE,
            upcase_size_bytes: 131_072,
            upcase_checksum: 0x1122_3344,
            volume_label_utf16: &[],
        };
        let buf = synthesize_root_directory(&g, &params).unwrap();
        // UpCase entry checksum at 0x20 + 0x04.
        assert_eq!(&buf[0x24..0x28], &[0x44, 0x33, 0x22, 0x11]);
        // UpCase entry first cluster at 0x20 + 0x14.
        assert_eq!(&buf[0x34..0x38], &[0xDE, 0xBC, 0x0A, 0x00]);
        // UpCase entry data length at 0x20 + 0x18.
        assert_eq!(&buf[0x38..0x40], &131_072_u64.to_le_bytes());
    }

    #[test]
    fn root_directory_label_charcount_serialized_correctly() {
        let g = geometry_64mib();
        let params = RootDirectoryParams {
            bitmap_first_cluster: 2,
            bitmap_size_bytes: 8192,
            upcase_first_cluster: 4,
            upcase_size_bytes: 131_072,
            upcase_checksum: 0xDEAD_BEEF,
            volume_label_utf16: &label_for("TESLA"),
        };
        let buf = synthesize_root_directory(&g, &params).unwrap();
        // Label CharacterCount at 0x40 + 0x01.
        assert_eq!(buf[0x41], 5);
        // First label code unit at 0x40 + 0x02 = 'T'.
        assert_eq!(buf[0x42], b'T');
        assert_eq!(buf[0x43], 0);
    }

    #[test]
    fn root_directory_label_too_long_propagates_error() {
        let g = geometry_64mib();
        let too_long: Vec<u16> = (0..12).collect();
        let params = RootDirectoryParams {
            bitmap_first_cluster: 2,
            bitmap_size_bytes: 8192,
            upcase_first_cluster: 4,
            upcase_size_bytes: 131_072,
            upcase_checksum: 0xDEAD_BEEF,
            volume_label_utf16: &too_long,
        };
        let err = synthesize_root_directory(&g, &params).unwrap_err();
        assert!(matches!(err, DirectoryError::LabelTooLong { .. }));
    }

    #[test]
    fn root_directory_buffer_tail_is_zero_filled() {
        let g = geometry_64mib();
        let cluster_bytes = g.bytes_per_cluster() as usize;
        let params = RootDirectoryParams {
            bitmap_first_cluster: 2,
            bitmap_size_bytes: 8192,
            upcase_first_cluster: 4,
            upcase_size_bytes: 131_072,
            upcase_checksum: 0xDEAD_BEEF,
            volume_label_utf16: &label_for("TESLA"),
        };
        let buf = synthesize_root_directory(&g, &params).unwrap();
        // Bytes from 0x60 through end of cluster all zero.
        for &b in &buf[0x60..cluster_bytes] {
            assert_eq!(b, 0);
        }
    }

    // ---------- Display for DirectoryError ----------

    #[test]
    fn directory_error_display_strings_are_informative() {
        let e = DirectoryError::EmptyName;
        assert!(format!("{e}").contains("least one"));
        let e = DirectoryError::NameTooLong {
            max_code_units: 255,
            found: 300,
        };
        assert!(format!("{e}").contains("300"));
        let e = DirectoryError::LabelTooLong {
            max_code_units: 11,
            found: 50,
        };
        assert!(format!("{e}").contains("50"));
        let e = DirectoryError::RootClusterTooSmall {
            needed_bytes: 96,
            cluster_bytes: 64,
        };
        assert!(format!("{e}").contains("64"));
    }
}
