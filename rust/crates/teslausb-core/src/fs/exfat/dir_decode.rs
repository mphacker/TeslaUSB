//! exFAT directory entry decoder.
//!
//! Phase 3.5d of the B-1 rewrite. The companion encoders in
//! [`crate::fs::exfat::directory`] turn `(name, attributes,
//! timestamps, first_cluster, data_length, no_fat_chain)` into
//! the 32-byte on-disk entries: a File primary (`0x85`) followed
//! by a Stream Extension secondary (`0xC0`) and one or more File
//! Name secondaries (`0xC1`). This module does the inverse:
//! given a cluster's worth of directory bytes (as written by
//! Tesla into the synth volume), it walks the entries in order,
//! reassembles entry sets, validates the `SetChecksum` (spec
//! §6.3.3), reconstructs the UTF-16 file name, and returns a
//! stream of [`DecodedExfatEntry`] values.
//!
//! Phase 3.5e will compose this decoder with the
//! [`crate::fs::cluster_map`] mutator inside the exFAT write
//! state machine: every directory-cluster write is decoded,
//! every [`DecodedExfatEntry::File`] produces a
//! `(first_cluster, file_path, data_length, no_fat_chain)`
//! tuple that gets converted into one or more
//! [`crate::fs::cluster_map::FileExtent`] inserts. For
//! `no_fat_chain == true` the extent is just
//! `[first_cluster, first_cluster + ceil(data_length /
//! bytes_per_cluster))`; for `no_fat_chain == false` the
//! companion chain walker visits the FAT to enumerate the
//! actual cluster sequence.
//!
//! ## Decoder contract
//!
//! * **Input** — a byte slice whose length is a multiple of
//!   [`crate::fs::exfat::directory::DIRECTORY_ENTRY_SIZE_BYTES`]
//!   (32). Typically one cluster, but the decoder accepts any
//!   32-byte-aligned length so callers can hand it sub-cluster
//!   ranges when they only saw a partial write.
//! * **Output** — an [`ExfatDecodeResult`] holding the decoded
//!   entries in directory order, plus an optional
//!   [`PartialEntrySet`] carry for an entry-set whose primary
//!   landed at the tail of the buffer but whose secondaries
//!   spilled into the next cluster.
//! * **Errors** — *almost never*. The decoder is **lenient by
//!   design**: garbage bytes are reported as
//!   [`DecodedExfatEntry::Malformed`] rather than aborting the
//!   walk, because Tesla may write half a cluster at a time and
//!   we must keep going. The only hard error is a
//!   non-32-byte-aligned input length.
//!
//! ## Entry-set assembly state machine
//!
//! 1. If `initial_carry` is `Some`, resume in "consuming
//!    secondaries" state with that primary's claimed
//!    `SecondaryCount`.
//! 2. For each 32-byte entry:
//!    * `EntryType == 0x00` → end of directory; stop the walk.
//!    * Inside a File entry set: validate that the next entry's
//!      type is the expected secondary (`0xC0` then `0xC1*`,
//!      or the deleted equivalents `0x40` then `0x41*`). On
//!      mismatch, the in-flight set is reported as
//!      [`DecodedExfatEntry::Malformed`] and decoding continues
//!      with the offending byte re-classified as a fresh
//!      entry.
//!    * Outside a File entry set, classify by `EntryType`:
//!      * `0x81` Allocation Bitmap (single, no secondaries)
//!      * `0x82` `UpCase` Table (single)
//!      * `0x83` Volume Label (single)
//!      * `0x85` File primary → enter the set-consuming state
//!      * `0x05` deleted File primary (`InUse` bit clear) →
//!        enter the deleted-set-consuming state
//!      * Everything else → [`DecodedExfatEntry::Malformed`]
//! 3. When `1 + SecondaryCount` entries have been consumed:
//!    a) Validate the `SetChecksum` by recomputing it over the
//!       collected bytes with the primary's `[2..4]` zeroed,
//!       and comparing against the primary's claimed value;
//!       record the result in
//!       [`DecodedExfatEntry::File::set_checksum_ok`].
//!    b) Concatenate the File Name chunks (15 UTF-16 LE code
//!       units per `0xC1` entry), truncate at the Stream
//!       Extension's `NameLength`, and decode to UTF-8. On
//!       decode failure, leave the name as `None`.
//!    c) Emit a [`DecodedExfatEntry::File`] (or `DeletedFile`).
//! 4. If the buffer ends mid-set, the partial state is returned
//!    in [`ExfatDecodeResult::trailing_partial_set`] so the
//!    caller can resume with the next cluster.

use core::convert::TryFrom;

use super::directory::{
    DIRECTORY_ENTRY_SIZE_BYTES, ENTRY_TYPE_ALLOCATION_BITMAP, ENTRY_TYPE_FILE,
    ENTRY_TYPE_FILE_NAME, ENTRY_TYPE_STREAM_EXTENSION, ENTRY_TYPE_UPCASE_TABLE,
    ENTRY_TYPE_VOLUME_LABEL, FileAttributes, FileTimestamps, MAX_FILE_NAME_CODE_UNITS,
    MAX_VOLUME_LABEL_CODE_UNITS, NAME_CODE_UNITS_PER_NAME_ENTRY, set_checksum,
};

// =====================================================================
// Constants
// =====================================================================

/// Bit in `EntryType` that marks an entry as in-use; clearing
/// this bit (subtracting `0x80`) is the spec-mandated way to
/// "delete" an entry without touching the rest of its 32 bytes
/// (exFAT spec §6.2).
pub const ENTRY_TYPE_IN_USE_BIT: u8 = 0x80;

/// Sentinel `EntryType` byte that terminates a directory (spec
/// §6.1: "an `EntryType` of `0x00` represents an end-of-directory
/// marker, and all subsequent entries are unused").
pub const ENTRY_TYPE_END_OF_DIRECTORY: u8 = 0x00;

/// Deleted-File primary `EntryType` (`0x85 & !0x80`).
pub const ENTRY_TYPE_FILE_DELETED: u8 = 0x05;
/// Deleted Stream Extension secondary `EntryType` (`0xC0 & !0x80`).
pub const ENTRY_TYPE_STREAM_EXTENSION_DELETED: u8 = 0x40;
/// Deleted File Name secondary `EntryType` (`0xC1 & !0x80`).
pub const ENTRY_TYPE_FILE_NAME_DELETED: u8 = 0x41;

/// Offset of `SecondaryCount` within a File primary (spec §7.4.2).
const FILE_OFFSET_SECONDARY_COUNT: usize = 0x01;
/// Offset of `SetChecksum` within a File primary (spec §7.4.3).
const FILE_OFFSET_SET_CHECKSUM: usize = 0x02;
/// Offset of `FileAttributes` (u16 LE) within a File primary
/// (spec §7.4.4).
const FILE_OFFSET_ATTRIBUTES: usize = 0x04;
/// Offset of `CreateTimestamp` within a File primary
/// (spec §7.4.7).
const FILE_OFFSET_CREATE_TIMESTAMP: usize = 0x08;
/// Offset of `LastModifiedTimestamp` (spec §7.4.8).
const FILE_OFFSET_MODIFY_TIMESTAMP: usize = 0x0C;
/// Offset of `LastAccessedTimestamp` (spec §7.4.9).
const FILE_OFFSET_ACCESS_TIMESTAMP: usize = 0x10;
/// Offset of `Create10msIncrement` (spec §7.4.10).
const FILE_OFFSET_CREATE_10MS: usize = 0x14;
/// Offset of `LastModified10msIncrement` (spec §7.4.11).
const FILE_OFFSET_MODIFY_10MS: usize = 0x15;
/// Offset of `CreateUtcOffset` (spec §7.4.13).
const FILE_OFFSET_CREATE_UTC_OFFSET: usize = 0x16;
/// Offset of `LastModifiedUtcOffset` (spec §7.4.14).
const FILE_OFFSET_MODIFY_UTC_OFFSET: usize = 0x17;
/// Offset of `LastAccessedUtcOffset` (spec §7.4.15).
const FILE_OFFSET_ACCESS_UTC_OFFSET: usize = 0x18;

/// Offset of `GeneralSecondaryFlags` within a Stream Extension
/// secondary (spec §7.6.3).
const STREAM_OFFSET_SECONDARY_FLAGS: usize = 0x01;
/// Offset of `NameLength` within a Stream Extension secondary
/// (spec §7.6.4).
const STREAM_OFFSET_NAME_LENGTH: usize = 0x03;
/// Offset of `NameHash` within a Stream Extension secondary
/// (spec §7.6.5).
const STREAM_OFFSET_NAME_HASH: usize = 0x04;
/// Offset of `ValidDataLength` (u64 LE) within a Stream
/// Extension secondary (spec §7.6.6).
const STREAM_OFFSET_VALID_DATA_LENGTH: usize = 0x08;
/// Offset of `FirstCluster` within a Stream Extension secondary
/// (spec §7.6.7).
const STREAM_OFFSET_FIRST_CLUSTER: usize = 0x14;
/// Offset of `DataLength` (u64 LE) within a Stream Extension
/// secondary (spec §7.6.8).
const STREAM_OFFSET_DATA_LENGTH: usize = 0x18;

/// Offset of `FileName` UTF-16 chunk within a File Name
/// secondary (spec §7.7.2).
const FILE_NAME_OFFSET: usize = 0x02;

/// Offset of `FirstCluster` within an Allocation Bitmap primary
/// (spec §7.1.5).
const BITMAP_OFFSET_FIRST_CLUSTER: usize = 0x14;
/// Offset of `DataLength` (u64 LE) within an Allocation Bitmap
/// primary (spec §7.1.6).
const BITMAP_OFFSET_DATA_LENGTH: usize = 0x18;
/// Offset of `BitmapFlags` within an Allocation Bitmap primary
/// (spec §7.1.2).
const BITMAP_OFFSET_FLAGS: usize = 0x01;

/// Offset of `TableChecksum` (u32 LE) within an `UpCase` Table
/// primary (spec §7.2.3).
const UPCASE_OFFSET_TABLE_CHECKSUM: usize = 0x04;
/// Offset of `FirstCluster` within an `UpCase` Table primary
/// (spec §7.2.4).
const UPCASE_OFFSET_FIRST_CLUSTER: usize = 0x14;
/// Offset of `DataLength` (u64 LE) within an `UpCase` Table
/// primary (spec §7.2.5).
const UPCASE_OFFSET_DATA_LENGTH: usize = 0x18;

/// Offset of `CharacterCount` within a Volume Label primary
/// (spec §7.3.2).
const VOLUME_LABEL_OFFSET_CHAR_COUNT: usize = 0x01;
/// Offset of `VolumeLabel` UTF-16 chunk within a Volume Label
/// primary (spec §7.3.3).
const VOLUME_LABEL_OFFSET_LABEL: usize = 0x02;

/// Bit 1 of `GeneralSecondaryFlags` — `NoFatChain` (spec §7.6.3).
const STREAM_FLAG_NO_FAT_CHAIN: u8 = 1 << 1;

/// Maximum entries a single File entry set can contain
/// (`1 + 1 + ceil(255/15) = 19`, spec §6.2.1).
const MAX_ENTRIES_PER_FILE_SET: usize = 19;

const _: () = {
    assert!(ENTRY_TYPE_FILE_DELETED == ENTRY_TYPE_FILE & !ENTRY_TYPE_IN_USE_BIT);
    assert!(
        ENTRY_TYPE_STREAM_EXTENSION_DELETED == ENTRY_TYPE_STREAM_EXTENSION & !ENTRY_TYPE_IN_USE_BIT
    );
    assert!(ENTRY_TYPE_FILE_NAME_DELETED == ENTRY_TYPE_FILE_NAME & !ENTRY_TYPE_IN_USE_BIT);
};

// =====================================================================
// Types
// =====================================================================

/// One decoded exFAT directory entry, post entry-set reassembly.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DecodedExfatEntry {
    /// Allocation Bitmap (`0x81`) primary entry. Single-entry
    /// set — no secondaries. spec §7.1.
    AllocationBitmap {
        /// Bitmap index. `0` for the first (and, in B-1's
        /// one-FAT volumes, only) bitmap.
        bitmap_index: u8,
        /// First cluster of the bitmap stream.
        first_cluster: u32,
        /// Bitmap stream length in bytes.
        data_length: u64,
        /// Byte offset within the input buffer.
        offset: usize,
    },
    /// `UpCase` Table (`0x82`) primary entry. spec §7.2.
    UpcaseTable {
        /// First cluster of the upcase stream.
        first_cluster: u32,
        /// Upcase stream length in bytes.
        data_length: u64,
        /// `TableChecksum` value as stored on disk.
        table_checksum: u32,
        /// Byte offset within the input buffer.
        offset: usize,
    },
    /// Volume Label (`0x83`) primary entry. spec §7.3.
    VolumeLabel {
        /// UTF-16 code units of the label (1..=11).
        label_utf16: Vec<u16>,
        /// Decoded UTF-8 label, or `None` if the UTF-16 was
        /// not well-formed.
        label_utf8: Option<String>,
        /// Byte offset within the input buffer.
        offset: usize,
    },
    /// A complete File (`0x85`) entry set, post checksum +
    /// name reassembly.
    File {
        /// UTF-8 file name, or `None` if the UTF-16 chunks did
        /// not decode cleanly. Even when `None` the rest of
        /// the entry fields are still useful for the cluster
        /// map (the file's data extent is known regardless of
        /// the name).
        name: Option<String>,
        /// `NameLength` as carried by the Stream Extension
        /// (number of UTF-16 code units in the original name).
        name_length: u8,
        /// `NameHash` value as stored on disk. Caller can
        /// independently recompute it from `name_utf16` and
        /// the volume's upcase table to verify integrity, but
        /// this decoder treats a mismatch as caller's problem
        /// (the spec doesn't say what to do).
        name_hash: u16,
        /// UTF-16 code units exactly as concatenated from the
        /// File Name secondaries and truncated to `name_length`.
        name_utf16: Vec<u16>,
        /// Attributes for the File entry.
        attributes: FileAttributes,
        /// Timestamps for the File entry.
        timestamps: FileTimestamps,
        /// First cluster of the file's data.
        first_cluster: u32,
        /// `ValidDataLength` (`<= data_length`).
        valid_data_length: u64,
        /// `DataLength` (total file size in bytes).
        data_length: u64,
        /// `true` if the Stream Extension's `NoFatChain` flag
        /// was set — extent is contiguous.
        no_fat_chain: bool,
        /// `true` if the recomputed `SetChecksum` matched the
        /// value stored in the primary's `[2..4]`. A mismatch
        /// does NOT prevent the entry from being emitted; the
        /// caller decides whether to trust it.
        set_checksum_ok: bool,
        /// `SetChecksum` value as stored on disk.
        set_checksum: u16,
        /// `SecondaryCount` as stored on disk (excludes the
        /// primary itself; should be `1 + ceil(name_length /
        /// 15)`).
        secondary_count: u8,
        /// Byte offset within the input buffer of the primary
        /// entry.
        primary_offset: usize,
    },
    /// A deleted File entry set (`0x05` primary, `0x40`/`0x41`
    /// secondaries). The data still exists on disk; the
    /// caller (Phase 3.5e) routes this through
    /// `cluster_map.remove_file` and `dir_tree.discard`.
    DeletedFile {
        /// UTF-8 file name if the deleted secondaries' chunks
        /// still decode cleanly. exFAT does NOT scribble over
        /// the name bytes on delete (unlike FAT32's leading
        /// `0xE5`), so this is usually still recoverable.
        name: Option<String>,
        /// `NameLength` from the deleted Stream Extension.
        name_length: u8,
        /// UTF-16 code units from the deleted name entries.
        name_utf16: Vec<u16>,
        /// First cluster of the file's data (caller frees its
        /// extent).
        first_cluster: u32,
        /// `ValidDataLength` from the deleted Stream Extension.
        valid_data_length: u64,
        /// `DataLength` from the deleted Stream Extension.
        data_length: u64,
        /// `true` if the deleted Stream Extension had its
        /// `NoFatChain` flag set.
        no_fat_chain: bool,
        /// `SecondaryCount` as carried by the deleted primary.
        secondary_count: u8,
        /// Byte offset within the input buffer of the primary
        /// entry.
        primary_offset: usize,
    },
    /// An entry that did not conform to any of the above
    /// classifications (unknown `EntryType`, primary followed
    /// by wrong secondary kind, secondary count exceeds the
    /// spec maximum, etc.). Not an error: the decoder keeps
    /// going.
    Malformed {
        /// Copy of the offending 32 bytes for diagnostic
        /// logging.
        bytes: [u8; DIRECTORY_ENTRY_SIZE_BYTES],
        /// Byte offset within the input buffer.
        offset: usize,
        /// Human-readable reason the entry was rejected.
        reason: &'static str,
    },
}

/// In-flight state for an entry set whose primary landed at
/// the tail of one cluster and whose secondaries are expected
/// in the next cluster (or were merely incomplete when the
/// caller handed us a partial write).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PartialEntrySet {
    /// Primary entry bytes (`0x85` or `0x05`).
    pub primary: [u8; DIRECTORY_ENTRY_SIZE_BYTES],
    /// Byte offset within the original buffer of the primary
    /// (purely informational once the buffer is gone).
    pub primary_offset: usize,
    /// Secondary entry bytes collected so far, in on-disk
    /// order.
    pub secondaries_seen: Vec<[u8; DIRECTORY_ENTRY_SIZE_BYTES]>,
    /// `SecondaryCount` claimed by the primary (excludes the
    /// primary itself).
    pub expected_secondary_count: u8,
    /// `true` if the primary's `EntryType` had the `InUse` bit
    /// clear (deleted set).
    pub is_deleted: bool,
}

/// Result of [`decode_directory_cluster`].
#[derive(Debug, Default, PartialEq, Eq)]
pub struct ExfatDecodeResult {
    /// Decoded entries in directory order.
    pub entries: Vec<DecodedExfatEntry>,
    /// In-flight entry-set whose secondaries were not all
    /// present in this buffer. The caller (Phase 3.5e)
    /// carries this forward into the next directory cluster
    /// of the chain.
    pub trailing_partial_set: Option<PartialEntrySet>,
    /// Set to `true` if the decoder hit
    /// [`ENTRY_TYPE_END_OF_DIRECTORY`] before consuming the
    /// whole buffer. Subsequent bytes are not decoded.
    pub end_of_directory_seen: bool,
}

/// Errors that abort the decoder before any entries are
/// returned. Per-entry decode failures are reported as
/// [`DecodedExfatEntry::Malformed`] in
/// [`ExfatDecodeResult::entries`].
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ExfatDirDecodeError {
    /// Input length is not a multiple of
    /// [`DIRECTORY_ENTRY_SIZE_BYTES`] (32).
    #[error("input length {length} is not a multiple of {DIRECTORY_ENTRY_SIZE_BYTES} bytes")]
    UnalignedInput {
        /// The offending length in bytes.
        length: usize,
    },
}

// =====================================================================
// Main entry point
// =====================================================================

/// Walk a slice of exFAT directory bytes and decode each
/// 32-byte entry.
///
/// `initial_carry` lets the caller resume an entry set that
/// started in a previous cluster — pass `None` on the first
/// call. Most callers will pass back the previous result's
/// [`ExfatDecodeResult::trailing_partial_set`] on the next call.
///
/// # Errors
///
/// * [`ExfatDirDecodeError::UnalignedInput`] if `bytes.len()`
///   is not a multiple of [`DIRECTORY_ENTRY_SIZE_BYTES`].
pub fn decode_directory_cluster(
    bytes: &[u8],
    initial_carry: Option<PartialEntrySet>,
) -> Result<ExfatDecodeResult, ExfatDirDecodeError> {
    if bytes.len() % DIRECTORY_ENTRY_SIZE_BYTES != 0 {
        return Err(ExfatDirDecodeError::UnalignedInput {
            length: bytes.len(),
        });
    }

    let mut result = ExfatDecodeResult::default();
    let mut carry = initial_carry;
    let mut offset = 0usize;

    while offset + DIRECTORY_ENTRY_SIZE_BYTES <= bytes.len() {
        let Some(entry_slice) = bytes.get(offset..offset + DIRECTORY_ENTRY_SIZE_BYTES) else {
            break;
        };
        let Ok(entry_bytes) = <&[u8; DIRECTORY_ENTRY_SIZE_BYTES]>::try_from(entry_slice) else {
            offset += DIRECTORY_ENTRY_SIZE_BYTES;
            continue;
        };

        if carry.is_some() {
            if !consume_in_flight(&mut carry, &mut result, entry_bytes, &mut offset) {
                // AbortAndReclassify: do NOT advance offset.
                continue;
            }
            continue;
        }

        if entry_bytes[0] == ENTRY_TYPE_END_OF_DIRECTORY {
            result.end_of_directory_seen = true;
            break;
        }

        classify_first_entry(entry_bytes, offset, &mut result, &mut carry);
        offset += DIRECTORY_ENTRY_SIZE_BYTES;
    }

    result.trailing_partial_set = carry;
    Ok(result)
}

/// Try to slot `entry_bytes` into the current in-flight entry
/// set. Returns `true` if the caller should advance the offset
/// (entry consumed or set finalized); returns `false` if the
/// entry should be re-classified from scratch (abort + reclassify
/// path).
fn consume_in_flight(
    carry: &mut Option<PartialEntrySet>,
    result: &mut ExfatDecodeResult,
    entry_bytes: &[u8; DIRECTORY_ENTRY_SIZE_BYTES],
    offset: &mut usize,
) -> bool {
    let outcome = {
        let Some(in_flight) = carry.as_mut() else {
            unreachable!("consume_in_flight called with carry = None")
        };
        consume_secondary(in_flight, entry_bytes)
    };
    match outcome {
        ConsumeOutcome::Continue => {
            *offset += DIRECTORY_ENTRY_SIZE_BYTES;
            let finished_now = carry.as_ref().is_some_and(|s| {
                s.secondaries_seen.len() == usize::from(s.expected_secondary_count)
            });
            if finished_now {
                if let Some(set) = carry.take() {
                    result.entries.push(finalize_entry_set(&set));
                }
            }
            true
        }
        ConsumeOutcome::AbortAndReclassify { reason } => {
            if let Some(aborted) = carry.take() {
                result.entries.push(DecodedExfatEntry::Malformed {
                    bytes: aborted.primary,
                    offset: aborted.primary_offset,
                    reason,
                });
                for sec in aborted.secondaries_seen {
                    result.entries.push(DecodedExfatEntry::Malformed {
                        bytes: sec,
                        offset: 0,
                        reason: "secondary of malformed entry set",
                    });
                }
            }
            false
        }
    }
}

fn classify_first_entry(
    entry_bytes: &[u8; DIRECTORY_ENTRY_SIZE_BYTES],
    offset: usize,
    result: &mut ExfatDecodeResult,
    carry: &mut Option<PartialEntrySet>,
) {
    let entry_type = entry_bytes[0];
    match entry_type {
        ENTRY_TYPE_ALLOCATION_BITMAP => {
            result
                .entries
                .push(decode_allocation_bitmap(entry_bytes, offset));
        }
        ENTRY_TYPE_UPCASE_TABLE => {
            result
                .entries
                .push(decode_upcase_table(entry_bytes, offset));
        }
        ENTRY_TYPE_VOLUME_LABEL => {
            result
                .entries
                .push(decode_volume_label(entry_bytes, offset));
        }
        ENTRY_TYPE_FILE | ENTRY_TYPE_FILE_DELETED => {
            let secondary_count = entry_bytes[FILE_OFFSET_SECONDARY_COUNT];
            // Spec §6.2.1: SecondaryCount in [1, 18].
            if secondary_count == 0 || usize::from(secondary_count) >= MAX_ENTRIES_PER_FILE_SET {
                result.entries.push(DecodedExfatEntry::Malformed {
                    bytes: *entry_bytes,
                    offset,
                    reason: "File primary SecondaryCount out of range",
                });
            } else {
                *carry = Some(PartialEntrySet {
                    primary: *entry_bytes,
                    primary_offset: offset,
                    secondaries_seen: Vec::with_capacity(usize::from(secondary_count)),
                    expected_secondary_count: secondary_count,
                    is_deleted: entry_type == ENTRY_TYPE_FILE_DELETED,
                });
            }
        }
        _ => {
            result.entries.push(DecodedExfatEntry::Malformed {
                bytes: *entry_bytes,
                offset,
                reason: "unknown EntryType",
            });
        }
    }
}

/// Internal: result of trying to slot a candidate secondary
/// entry into an in-flight set.
enum ConsumeOutcome {
    /// The entry was consumed (it had the right secondary
    /// `EntryType`). Caller advances offset by 32 bytes.
    Continue,
    /// The entry was rejected because its `EntryType` did not
    /// match the expected secondary kind. The set is aborted
    /// (emitted as Malformed) and the offending entry is
    /// re-classified from scratch. Caller does NOT advance the
    /// offset.
    AbortAndReclassify { reason: &'static str },
}

fn consume_secondary(
    in_flight: &mut PartialEntrySet,
    entry_bytes: &[u8; DIRECTORY_ENTRY_SIZE_BYTES],
) -> ConsumeOutcome {
    let entry_type = entry_bytes[0];
    let position = in_flight.secondaries_seen.len();
    let expected_type = expected_secondary_type(position, in_flight.is_deleted);
    if entry_type == expected_type {
        in_flight.secondaries_seen.push(*entry_bytes);
        ConsumeOutcome::Continue
    } else {
        ConsumeOutcome::AbortAndReclassify {
            reason: "unexpected secondary EntryType",
        }
    }
}

fn expected_secondary_type(position: usize, is_deleted: bool) -> u8 {
    // Position 0 of secondaries is the Stream Extension; the
    // rest are File Name entries.
    if position == 0 {
        if is_deleted {
            ENTRY_TYPE_STREAM_EXTENSION_DELETED
        } else {
            ENTRY_TYPE_STREAM_EXTENSION
        }
    } else if is_deleted {
        ENTRY_TYPE_FILE_NAME_DELETED
    } else {
        ENTRY_TYPE_FILE_NAME
    }
}

// =====================================================================
// Single-entry decoders
// =====================================================================

fn decode_allocation_bitmap(
    bytes: &[u8; DIRECTORY_ENTRY_SIZE_BYTES],
    offset: usize,
) -> DecodedExfatEntry {
    DecodedExfatEntry::AllocationBitmap {
        bitmap_index: bytes[BITMAP_OFFSET_FLAGS] & 0x01,
        first_cluster: read_u32_le(bytes, BITMAP_OFFSET_FIRST_CLUSTER),
        data_length: read_u64_le(bytes, BITMAP_OFFSET_DATA_LENGTH),
        offset,
    }
}

fn decode_upcase_table(
    bytes: &[u8; DIRECTORY_ENTRY_SIZE_BYTES],
    offset: usize,
) -> DecodedExfatEntry {
    DecodedExfatEntry::UpcaseTable {
        first_cluster: read_u32_le(bytes, UPCASE_OFFSET_FIRST_CLUSTER),
        data_length: read_u64_le(bytes, UPCASE_OFFSET_DATA_LENGTH),
        table_checksum: read_u32_le(bytes, UPCASE_OFFSET_TABLE_CHECKSUM),
        offset,
    }
}

fn decode_volume_label(
    bytes: &[u8; DIRECTORY_ENTRY_SIZE_BYTES],
    offset: usize,
) -> DecodedExfatEntry {
    let char_count = bytes[VOLUME_LABEL_OFFSET_CHAR_COUNT];
    let limit = usize::from(char_count).min(MAX_VOLUME_LABEL_CODE_UNITS);
    let mut label_utf16 = Vec::with_capacity(limit);
    for i in 0..limit {
        label_utf16.push(read_u16_le(bytes, VOLUME_LABEL_OFFSET_LABEL + i * 2));
    }
    let label_utf8 = String::from_utf16(&label_utf16).ok();
    DecodedExfatEntry::VolumeLabel {
        label_utf16,
        label_utf8,
        offset,
    }
}

// =====================================================================
// Entry-set finalization
// =====================================================================

fn finalize_entry_set(set: &PartialEntrySet) -> DecodedExfatEntry {
    // Index 0 of secondaries is the Stream Extension; the rest
    // are File Name entries.
    let Some(stream) = set.secondaries_seen.first() else {
        return DecodedExfatEntry::Malformed {
            bytes: set.primary,
            offset: set.primary_offset,
            reason: "File entry set has no Stream Extension",
        };
    };

    let secondary_flags = stream[STREAM_OFFSET_SECONDARY_FLAGS];
    let name_length = stream[STREAM_OFFSET_NAME_LENGTH];
    let name_hash = read_u16_le(stream, STREAM_OFFSET_NAME_HASH);
    let valid_data_length = read_u64_le(stream, STREAM_OFFSET_VALID_DATA_LENGTH);
    let first_cluster = read_u32_le(stream, STREAM_OFFSET_FIRST_CLUSTER);
    let data_length = read_u64_le(stream, STREAM_OFFSET_DATA_LENGTH);
    let no_fat_chain = secondary_flags & STREAM_FLAG_NO_FAT_CHAIN != 0;

    let name_utf16 = collect_name_utf16(&set.secondaries_seen, name_length);
    let name = decode_name(&name_utf16);

    if set.is_deleted {
        return DecodedExfatEntry::DeletedFile {
            name,
            name_length,
            name_utf16,
            first_cluster,
            valid_data_length,
            data_length,
            no_fat_chain,
            secondary_count: set.expected_secondary_count,
            primary_offset: set.primary_offset,
        };
    }

    let attributes = FileAttributes::from_u16(read_u16_le(&set.primary, FILE_OFFSET_ATTRIBUTES));
    let timestamps = FileTimestamps {
        create_timestamp: read_u32_le(&set.primary, FILE_OFFSET_CREATE_TIMESTAMP),
        modify_timestamp: read_u32_le(&set.primary, FILE_OFFSET_MODIFY_TIMESTAMP),
        access_timestamp: read_u32_le(&set.primary, FILE_OFFSET_ACCESS_TIMESTAMP),
        create_10ms: set.primary[FILE_OFFSET_CREATE_10MS],
        modify_10ms: set.primary[FILE_OFFSET_MODIFY_10MS],
        create_utc_offset: set.primary[FILE_OFFSET_CREATE_UTC_OFFSET],
        modify_utc_offset: set.primary[FILE_OFFSET_MODIFY_UTC_OFFSET],
        access_utc_offset: set.primary[FILE_OFFSET_ACCESS_UTC_OFFSET],
    };

    let claimed_checksum = read_u16_le(&set.primary, FILE_OFFSET_SET_CHECKSUM);
    let set_checksum_ok = verify_set_checksum(set);

    DecodedExfatEntry::File {
        name,
        name_length,
        name_hash,
        name_utf16,
        attributes,
        timestamps,
        first_cluster,
        valid_data_length,
        data_length,
        no_fat_chain,
        set_checksum_ok,
        set_checksum: claimed_checksum,
        secondary_count: set.expected_secondary_count,
        primary_offset: set.primary_offset,
    }
}

fn collect_name_utf16(
    secondaries: &[[u8; DIRECTORY_ENTRY_SIZE_BYTES]],
    name_length: u8,
) -> Vec<u16> {
    let total_needed = usize::from(name_length).min(MAX_FILE_NAME_CODE_UNITS);
    let mut out = Vec::with_capacity(total_needed);
    // secondaries[0] is the Stream Extension; name entries
    // start at index 1.
    for entry in secondaries.iter().skip(1) {
        for slot in 0..NAME_CODE_UNITS_PER_NAME_ENTRY {
            if out.len() == total_needed {
                return out;
            }
            out.push(read_u16_le(entry, FILE_NAME_OFFSET + slot * 2));
        }
    }
    out
}

fn decode_name(units: &[u16]) -> Option<String> {
    if units.is_empty() {
        return None;
    }
    String::from_utf16(units).ok()
}

fn verify_set_checksum(set: &PartialEntrySet) -> bool {
    // The SetChecksum is computed over all entries in the set
    // with the primary's bytes [2..4] (the checksum field
    // itself) set to 0 (spec §6.3.3).
    let mut buf = Vec::with_capacity((1 + set.secondaries_seen.len()) * DIRECTORY_ENTRY_SIZE_BYTES);
    let mut primary_zeroed = set.primary;
    primary_zeroed[FILE_OFFSET_SET_CHECKSUM] = 0;
    primary_zeroed[FILE_OFFSET_SET_CHECKSUM + 1] = 0;
    buf.extend_from_slice(&primary_zeroed);
    for sec in &set.secondaries_seen {
        buf.extend_from_slice(sec);
    }
    let computed = set_checksum(&buf);
    let claimed = read_u16_le(&set.primary, FILE_OFFSET_SET_CHECKSUM);
    computed == claimed
}

// =====================================================================
// Little-endian helpers
// =====================================================================

fn read_u16_le(bytes: &[u8], offset: usize) -> u16 {
    let lo = u16::from(bytes.get(offset).copied().unwrap_or(0));
    let hi = u16::from(bytes.get(offset + 1).copied().unwrap_or(0));
    lo | (hi << 8)
}

fn read_u32_le(bytes: &[u8], offset: usize) -> u32 {
    let mut buf = [0u8; 4];
    for (i, slot) in buf.iter_mut().enumerate() {
        *slot = bytes.get(offset + i).copied().unwrap_or(0);
    }
    u32::from_le_bytes(buf)
}

fn read_u64_le(bytes: &[u8], offset: usize) -> u64 {
    let mut buf = [0u8; 8];
    for (i, slot) in buf.iter_mut().enumerate() {
        *slot = bytes.get(offset + i).copied().unwrap_or(0);
    }
    u64::from_le_bytes(buf)
}

#[cfg(test)]
#[allow(
    clippy::cognitive_complexity,
    clippy::cast_possible_truncation,
    clippy::cast_sign_loss,
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::panic,
    clippy::unwrap_used
)]
mod tests {
    use super::*;
    use crate::fs::exfat::directory::{
        FileAttributes as FA, FileEntrySetParams, FileTimestamps as FT,
        encode_allocation_bitmap_entry, encode_file_entry_set, encode_upcase_table_entry,
        encode_volume_label_entry,
    };
    use crate::fs::exfat::upcase_table::UpcaseTable;

    fn upcase() -> UpcaseTable {
        UpcaseTable::ascii_identity()
    }

    fn timestamps() -> FT {
        FT {
            create_timestamp: 0x4A21_0000,
            modify_timestamp: 0x4A21_0001,
            access_timestamp: 0x4A21_0002,
            create_10ms: 50,
            modify_10ms: 25,
            create_utc_offset: 0x80,
            modify_utc_offset: 0x80,
            access_utc_offset: 0x80,
        }
    }

    fn utf16(s: &str) -> Vec<u16> {
        s.encode_utf16().collect()
    }

    fn pad_to_cluster(bytes: &mut Vec<u8>, cluster_size: usize) {
        while bytes.len() < cluster_size {
            bytes.push(0);
        }
    }

    fn build_file_set(name: &str, first_cluster: u32, data_length: u64) -> Vec<u8> {
        let n = utf16(name);
        let params = FileEntrySetParams {
            name: &n,
            attributes: FA::default(),
            timestamps: timestamps(),
            first_cluster,
            valid_data_length: data_length,
            data_length,
            no_fat_chain: true,
        };
        encode_file_entry_set(&params, &upcase()).expect("encode")
    }

    #[test]
    fn rejects_unaligned_input() {
        let result = decode_directory_cluster(&[0u8; 33], None);
        assert!(matches!(
            result,
            Err(ExfatDirDecodeError::UnalignedInput { length: 33 })
        ));
    }

    #[test]
    fn empty_buffer_returns_empty_result() {
        let r = decode_directory_cluster(&[], None).expect("decode");
        assert!(r.entries.is_empty());
        assert!(r.trailing_partial_set.is_none());
        assert!(!r.end_of_directory_seen);
    }

    #[test]
    fn all_zeros_signals_end_of_directory() {
        let r = decode_directory_cluster(&[0u8; 32], None).expect("decode");
        assert!(r.entries.is_empty());
        assert!(r.end_of_directory_seen);
    }

    #[test]
    fn decodes_allocation_bitmap_primary() {
        let entry = encode_allocation_bitmap_entry(2, 4096);
        let r = decode_directory_cluster(&entry, None).expect("decode");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedExfatEntry::AllocationBitmap {
                bitmap_index,
                first_cluster,
                data_length,
                ..
            } => {
                assert_eq!(*bitmap_index, 0);
                assert_eq!(*first_cluster, 2);
                assert_eq!(*data_length, 4096);
            }
            other => panic!("expected AllocationBitmap, got {other:?}"),
        }
    }

    #[test]
    fn decodes_upcase_table_primary() {
        let tbl = upcase();
        let entry = encode_upcase_table_entry(tbl.checksum(), 3, tbl.size_bytes() as u64);
        let r = decode_directory_cluster(&entry, None).expect("decode");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedExfatEntry::UpcaseTable {
                first_cluster,
                data_length,
                table_checksum,
                ..
            } => {
                assert_eq!(*first_cluster, 3);
                assert_eq!(*data_length, tbl.size_bytes() as u64);
                assert_eq!(*table_checksum, tbl.checksum());
            }
            other => panic!("expected UpcaseTable, got {other:?}"),
        }
    }

    #[test]
    fn decodes_volume_label_primary() {
        let label_utf16 = utf16("TESLA");
        let entry = encode_volume_label_entry(&label_utf16).expect("label");
        let r = decode_directory_cluster(&entry, None).expect("decode");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedExfatEntry::VolumeLabel {
                label_utf16: decoded_units,
                label_utf8,
                ..
            } => {
                assert_eq!(decoded_units, &label_utf16);
                assert_eq!(label_utf8.as_deref(), Some("TESLA"));
            }
            other => panic!("expected VolumeLabel, got {other:?}"),
        }
    }

    #[test]
    fn decodes_file_entry_set_round_trip() {
        let bytes = build_file_set("hello.bin", 7, 4096);
        let r = decode_directory_cluster(&bytes, None).expect("decode");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedExfatEntry::File {
                name,
                first_cluster,
                data_length,
                valid_data_length,
                no_fat_chain,
                set_checksum_ok,
                primary_offset,
                ..
            } => {
                assert_eq!(name.as_deref(), Some("hello.bin"));
                assert_eq!(*first_cluster, 7);
                assert_eq!(*data_length, 4096);
                assert_eq!(*valid_data_length, 4096);
                assert!(*no_fat_chain);
                assert!(*set_checksum_ok, "round-trip checksum must verify");
                assert_eq!(*primary_offset, 0);
            }
            other => panic!("expected File, got {other:?}"),
        }
    }

    #[test]
    fn decodes_multi_name_file_entry_set() {
        // 30 code units → 2 File Name secondaries needed.
        let long_name = "abcdefghijklmnopqrstuvwxyz1234"; // 30 ASCII chars
        let bytes = build_file_set(long_name, 100, 65536);
        let r = decode_directory_cluster(&bytes, None).expect("decode");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedExfatEntry::File {
                name,
                name_length,
                secondary_count,
                set_checksum_ok,
                ..
            } => {
                assert_eq!(name.as_deref(), Some(long_name));
                assert_eq!(*name_length, 30);
                // 2 name entries + 1 stream = 3 secondaries.
                assert_eq!(*secondary_count, 3);
                assert!(*set_checksum_ok);
            }
            other => panic!("expected File, got {other:?}"),
        }
    }

    #[test]
    fn entry_set_crossing_buffer_boundary_carries() {
        let bytes = build_file_set("split.bin", 50, 1024);
        // Split after the primary; secondaries land in the
        // next "cluster" the caller will hand us.
        let first_half = &bytes[..DIRECTORY_ENTRY_SIZE_BYTES];
        let second_half = &bytes[DIRECTORY_ENTRY_SIZE_BYTES..];
        let r1 = decode_directory_cluster(first_half, None).expect("decode");
        assert_eq!(r1.entries.len(), 0);
        let carry = r1.trailing_partial_set.expect("carry");
        assert_eq!(carry.expected_secondary_count, 2);
        let r2 = decode_directory_cluster(second_half, Some(carry)).expect("decode");
        assert_eq!(r2.entries.len(), 1);
        assert!(r2.trailing_partial_set.is_none());
        match &r2.entries[0] {
            DecodedExfatEntry::File {
                name,
                set_checksum_ok,
                ..
            } => {
                assert_eq!(name.as_deref(), Some("split.bin"));
                assert!(*set_checksum_ok);
            }
            other => panic!("expected File, got {other:?}"),
        }
    }

    #[test]
    fn deleted_file_entry_set_decoded() {
        let mut bytes = build_file_set("gone.bin", 9, 512);
        // Clear the InUse bit on the primary + secondaries.
        bytes[0] &= !ENTRY_TYPE_IN_USE_BIT;
        bytes[DIRECTORY_ENTRY_SIZE_BYTES] &= !ENTRY_TYPE_IN_USE_BIT;
        bytes[DIRECTORY_ENTRY_SIZE_BYTES * 2] &= !ENTRY_TYPE_IN_USE_BIT;
        let r = decode_directory_cluster(&bytes, None).expect("decode");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedExfatEntry::DeletedFile {
                name,
                first_cluster,
                data_length,
                no_fat_chain,
                ..
            } => {
                // Deleted-name reassembly: name still readable.
                assert_eq!(name.as_deref(), Some("gone.bin"));
                assert_eq!(*first_cluster, 9);
                assert_eq!(*data_length, 512);
                assert!(*no_fat_chain);
            }
            other => panic!("expected DeletedFile, got {other:?}"),
        }
    }

    #[test]
    fn checksum_mismatch_is_reported_but_entry_still_emitted() {
        let mut bytes = build_file_set("flip.bin", 11, 200);
        // Corrupt the SetChecksum field directly.
        bytes[FILE_OFFSET_SET_CHECKSUM] ^= 0xFF;
        let r = decode_directory_cluster(&bytes, None).expect("decode");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedExfatEntry::File {
                set_checksum_ok,
                name,
                first_cluster,
                ..
            } => {
                assert!(!set_checksum_ok);
                assert_eq!(name.as_deref(), Some("flip.bin"));
                assert_eq!(*first_cluster, 11);
            }
            other => panic!("expected File, got {other:?}"),
        }
    }

    #[test]
    fn unknown_entry_type_reported_as_malformed() {
        let mut entry = [0u8; DIRECTORY_ENTRY_SIZE_BYTES];
        entry[0] = 0xAB; // unknown
        let r = decode_directory_cluster(&entry, None).expect("decode");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedExfatEntry::Malformed { reason, .. } => {
                assert!(reason.contains("unknown"));
            }
            other => panic!("expected Malformed, got {other:?}"),
        }
    }

    #[test]
    fn end_of_directory_stops_walk_mid_buffer() {
        let mut bytes = build_file_set("good.bin", 7, 100);
        // Append a sentinel that ends the directory.
        let mut buf = vec![0u8; DIRECTORY_ENTRY_SIZE_BYTES * 8];
        buf[..bytes.len()].copy_from_slice(&bytes);
        // EOD byte already 0 at position bytes.len().
        // Append a *would-be* file primary AFTER the EOD that
        // should NOT be decoded.
        let trailing = build_file_set("never.bin", 8, 100);
        let trailing_offset = bytes.len() + DIRECTORY_ENTRY_SIZE_BYTES;
        if trailing_offset + trailing.len() <= buf.len() {
            buf[trailing_offset..trailing_offset + trailing.len()].copy_from_slice(&trailing);
        }
        // Round to 32-byte multiple just in case.
        pad_to_cluster(&mut bytes, buf.len());

        let r = decode_directory_cluster(&buf, None).expect("decode");
        assert!(r.end_of_directory_seen);
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedExfatEntry::File { name, .. } => {
                assert_eq!(name.as_deref(), Some("good.bin"));
            }
            other => panic!("expected File, got {other:?}"),
        }
    }

    #[test]
    fn file_primary_with_zero_secondary_count_is_malformed() {
        let mut entry = [0u8; DIRECTORY_ENTRY_SIZE_BYTES];
        entry[0] = ENTRY_TYPE_FILE;
        entry[FILE_OFFSET_SECONDARY_COUNT] = 0;
        let r = decode_directory_cluster(&entry, None).expect("decode");
        assert_eq!(r.entries.len(), 1);
        assert!(matches!(r.entries[0], DecodedExfatEntry::Malformed { .. }));
    }

    #[test]
    fn file_primary_with_excessive_secondary_count_is_malformed() {
        let mut entry = [0u8; DIRECTORY_ENTRY_SIZE_BYTES];
        entry[0] = ENTRY_TYPE_FILE;
        entry[FILE_OFFSET_SECONDARY_COUNT] = 200;
        let r = decode_directory_cluster(&entry, None).expect("decode");
        assert_eq!(r.entries.len(), 1);
        assert!(matches!(r.entries[0], DecodedExfatEntry::Malformed { .. }));
    }

    #[test]
    fn mismatched_secondary_kind_aborts_set_and_reclassifies() {
        // Build a valid file primary, then immediately emit a
        // VolumeLabel where we expected a StreamExtension.
        let mut bytes = build_file_set("partial.bin", 7, 100);
        // Truncate to just the primary, then append a
        // VolumeLabel-typed entry (0x83) where the Stream
        // Extension (0xC0) is expected.
        let primary = bytes[..DIRECTORY_ENTRY_SIZE_BYTES].to_vec();
        let mut stray = [0u8; DIRECTORY_ENTRY_SIZE_BYTES];
        stray[0] = ENTRY_TYPE_VOLUME_LABEL;
        bytes.clear();
        bytes.extend_from_slice(&primary);
        bytes.extend_from_slice(&stray);

        let r = decode_directory_cluster(&bytes, None).expect("decode");
        // Expect: Malformed(primary) + VolumeLabel
        // (re-classified).
        assert_eq!(r.entries.len(), 2);
        assert!(matches!(r.entries[0], DecodedExfatEntry::Malformed { .. }));
        assert!(matches!(
            r.entries[1],
            DecodedExfatEntry::VolumeLabel { .. }
        ));
    }

    #[test]
    fn two_files_in_one_buffer_decoded_independently() {
        let mut buf = Vec::new();
        buf.extend_from_slice(&build_file_set("first.bin", 5, 100));
        buf.extend_from_slice(&build_file_set("second.bin", 6, 200));
        let r = decode_directory_cluster(&buf, None).expect("decode");
        assert_eq!(r.entries.len(), 2);
        match (&r.entries[0], &r.entries[1]) {
            (
                DecodedExfatEntry::File { name: n1, .. },
                DecodedExfatEntry::File { name: n2, .. },
            ) => {
                assert_eq!(n1.as_deref(), Some("first.bin"));
                assert_eq!(n2.as_deref(), Some("second.bin"));
            }
            other => panic!("expected two Files, got {other:?}"),
        }
    }

    #[test]
    fn root_directory_synthesized_decodes_to_three_entries() {
        // Build the canonical 3-entry root header (bitmap +
        // upcase + label) using the encoder primitives, then
        // round-trip through the decoder.
        let tbl = upcase();
        let mut buf = Vec::new();
        buf.extend_from_slice(&encode_allocation_bitmap_entry(2, 4096));
        buf.extend_from_slice(&encode_upcase_table_entry(
            tbl.checksum(),
            3,
            tbl.size_bytes() as u64,
        ));
        let label_utf16 = utf16("CYBERTRUCK");
        buf.extend_from_slice(&encode_volume_label_entry(&label_utf16).expect("label"));

        let r = decode_directory_cluster(&buf, None).expect("decode");
        assert_eq!(r.entries.len(), 3);
        assert!(matches!(
            r.entries[0],
            DecodedExfatEntry::AllocationBitmap { .. }
        ));
        assert!(matches!(
            r.entries[1],
            DecodedExfatEntry::UpcaseTable { .. }
        ));
        assert!(matches!(
            r.entries[2],
            DecodedExfatEntry::VolumeLabel { .. }
        ));
    }

    #[test]
    fn boundary_split_3_chunks_reassembles() {
        // Build a long-name file that spans 3 entries
        // (primary + stream + 2 name entries = 4 entries).
        let long = "abcdefghijklmnopqrstuvwxyz0123"; // 30 chars → 2 name entries
        let bytes = build_file_set(long, 50, 100);
        assert_eq!(bytes.len(), DIRECTORY_ENTRY_SIZE_BYTES * 4);
        // Split into 3 chunks: 1 entry, 2 entries, 1 entry.
        let c1 = &bytes[..DIRECTORY_ENTRY_SIZE_BYTES];
        let c2 = &bytes[DIRECTORY_ENTRY_SIZE_BYTES..DIRECTORY_ENTRY_SIZE_BYTES * 3];
        let c3 = &bytes[DIRECTORY_ENTRY_SIZE_BYTES * 3..];
        let r1 = decode_directory_cluster(c1, None).expect("c1");
        let carry1 = r1.trailing_partial_set.expect("after primary, set carries");
        let r2 = decode_directory_cluster(c2, Some(carry1)).expect("c2");
        let carry2 = r2.trailing_partial_set.expect("set still incomplete");
        let r3 = decode_directory_cluster(c3, Some(carry2)).expect("c3");
        assert_eq!(r3.entries.len(), 1);
        match &r3.entries[0] {
            DecodedExfatEntry::File { name, .. } => {
                assert_eq!(name.as_deref(), Some(long));
            }
            other => panic!("expected File, got {other:?}"),
        }
    }
}
