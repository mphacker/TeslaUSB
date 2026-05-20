//! FAT32 directory entry decoder.
//!
//! Phase 3.5a of the B-1 rewrite. The companion encoder in
//! [`crate::fs::fat32::directory`] turns
//! `(name, first_cluster, file_size, attributes, timestamps)`
//! into the 32-byte on-disk entries (SFN + LFN). This module
//! does the reverse: given a cluster's worth of directory bytes
//! (as written by Tesla into the synth volume), it walks the
//! entries in order, reassembles LFN chains, and returns a
//! stream of [`DecodedDirEntry`] values.
//!
//! Phase 3.5c will compose this decoder with the
//! [`crate::fs::cluster_map`] mutator inside `SynthBackend::write`:
//! every directory-cluster write is decoded, every
//! [`DecodedDirEntry::File`] produces a `(first_cluster,
//! file_path)` pair that gets joined with the FAT-table chain
//! (Phase 3.5b) into one or more [`crate::fs::cluster_map::FileExtent`]
//! inserts.
//!
//! ## Decoder contract
//!
//! * **Input** — a byte slice whose length is a multiple of
//!   [`crate::fs::fat32::directory::DIR_ENTRY_SIZE_BYTES`] (32). Typically
//!   one cluster (`bytes_per_cluster` bytes) but the decoder
//!   accepts any 32-byte-aligned length so the caller can hand
//!   it sub-cluster ranges if it only saw a partial write.
//! * **Output** — a [`Vec<DecodedDirEntry>`] in directory order
//!   *plus* the residual LFN chain (if any) — see
//!   [`DecodeResult::trailing_lfn`].
//! * **Errors** — *almost never*. The decoder is **lenient by
//!   design**: garbage bytes are reported as
//!   [`DecodedDirEntry::Malformed`] rather than aborting the
//!   walk, because Tesla may write half a cluster at a time and
//!   we must keep going. The only hard error is a non-32-byte-
//!   aligned input length.
//!
//! ## LFN reassembly state machine
//!
//! 1. Maintain an in-progress LFN buffer that accumulates
//!    `(ordinal, 13 UCS-2 chars, checksum)` triples in the
//!    order they appear on disk.
//! 2. The first LFN entry of a chain has `Ord & LAST_LONG_ENTRY`
//!    set (`0x40`) and carries the **highest** ordinal. Subsequent
//!    entries (no `LAST_LONG_ENTRY` bit) decrement the ordinal by
//!    one until ordinal 1 — the entry immediately before the SFN.
//! 3. When an SFN entry follows, the accumulated LFN chain is
//!    decoded into a string (concatenation in **reverse on-disk
//!    order**: ordinal 1 chars first, then ordinal 2, etc.) and
//!    truncated at the first `0x0000` terminator. The result
//!    plus the SFN entry produces a [`DecodedDirEntry::File`].
//! 4. If a chain is interrupted (deleted entry, missing ordinal,
//!    checksum mismatch with the SFN's `ShortName::checksum`),
//!    the entry is degraded to [`DecodedDirEntry::ShortNameOnly`]
//!    and the LFN buffer is cleared.
//! 5. If the cluster ends with LFN entries but no SFN yet, those
//!    entries are returned in [`DecodeResult::trailing_lfn`] so
//!    the caller can pre-pend them when it processes the next
//!    cluster of the directory chain.

use core::convert::TryFrom;

use super::directory::{
    ATTR_DIRECTORY, ATTR_LONG_NAME, ATTR_VOLUME_ID, DIR_ENTRY_DELETED, DIR_ENTRY_END_OF_DIRECTORY,
    DIR_ENTRY_ESCAPED_E5, DIR_ENTRY_SIZE_BYTES, LAST_LONG_ENTRY, LFN_CHARS_PER_ENTRY,
    LFN_MAX_ENTRIES, ShortName, VOLUME_LABEL_NAME_LEN,
};

/// Offset of `FstClusHI` within an SFN entry.
const SFN_OFFSET_FST_CLUS_HI: usize = 0x14;
/// Offset of `FstClusLO` within an SFN entry.
const SFN_OFFSET_FST_CLUS_LO: usize = 0x1A;
/// Offset of `FileSize` within an SFN entry.
const SFN_OFFSET_FILE_SIZE: usize = 0x1C;
/// Offset of `Attr` within any entry.
const ENTRY_OFFSET_ATTR: usize = 0x0B;
/// Offset of `Ord` within an LFN entry (= byte 0).
const LFN_OFFSET_ORD: usize = 0x00;
/// Offset of `Chksum` within an LFN entry.
const LFN_OFFSET_CHKSUM: usize = 0x0D;
/// Offsets and lengths of the three LFN name fields (in bytes).
/// Each pair is a UCS-2 code unit (little-endian); the count is
/// the number of pairs the field can hold.
const LFN_NAME1_OFFSET: usize = 0x01;
const LFN_NAME1_PAIRS: usize = 5;
const LFN_NAME2_OFFSET: usize = 0x0E;
const LFN_NAME2_PAIRS: usize = 6;
const LFN_NAME3_OFFSET: usize = 0x1C;
const LFN_NAME3_PAIRS: usize = 2;
/// UCS-2 null terminator marking the end of the LFN name proper
/// (subsequent slots are 0xFFFF guard padding).
const LFN_TERMINATOR: u16 = 0x0000;

/// One decoded directory entry, post LFN-reassembly.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DecodedDirEntry {
    /// A live file or subdirectory entry. `long_name` is `Some`
    /// when the LFN chain reassembled successfully; `None`
    /// otherwise (the SFN was orphaned or the chain was
    /// corrupted, see [`DecodedDirEntry::ShortNameOnly`] for the
    /// latter explicit path).
    File {
        /// Long name (UTF-8) if reassembled; otherwise `None`.
        long_name: Option<String>,
        /// 11-byte 8.3 short name as stored on disk.
        short_name: ShortName,
        /// File attributes bitfield (raw `Attr` byte).
        attributes: u8,
        /// First cluster number (`FstClusHI << 16 | FstClusLO`).
        /// Zero for an empty file.
        first_cluster: u32,
        /// File size in bytes (zero for directories).
        file_size: u32,
        /// Byte offset within the input buffer of the SFN entry.
        /// Used by the caller to log the precise on-disk location.
        sfn_offset: usize,
    },
    /// A live SFN entry whose preceding LFN chain was invalid or
    /// absent. Distinguished from `File { long_name: None, .. }`
    /// because the cause is different (chain corruption vs.
    /// SFN-only naming) and tests want to assert one vs. the
    /// other.
    ShortNameOnly {
        /// 11-byte 8.3 short name as stored on disk.
        short_name: ShortName,
        /// File attributes bitfield (raw `Attr` byte).
        attributes: u8,
        /// First cluster number.
        first_cluster: u32,
        /// File size in bytes.
        file_size: u32,
        /// Byte offset within the input buffer of the SFN entry.
        sfn_offset: usize,
    },
    /// The volume-label entry (FAT32 root directory only).
    VolumeLabel {
        /// 11-byte label as stored on disk (space-padded).
        label: [u8; VOLUME_LABEL_NAME_LEN],
        /// Byte offset within the input buffer.
        offset: usize,
    },
    /// A deleted entry (`Name[0] == 0xE5`). Tracked so the
    /// caller can wire up Phase 3.5c's `cluster_map.remove_file`
    /// (the deleted entry's `first_cluster` identifies the
    /// extent to free).
    Deleted {
        /// 11-byte name field as stored on disk. Byte 0 is the
        /// `0xE5` deletion marker; bytes 1..11 still hold the
        /// original characters and can be used for diagnostic
        /// matching. Note that a `0x05` escaped-leading-E5 was
        /// substituted on write, so a deleted entry that
        /// originally started with `0xE5` no longer has the
        /// `0x05` substitution (it is re-encoded to `0xE5`).
        raw_name: [u8; VOLUME_LABEL_NAME_LEN],
        /// First cluster number — the caller can free its chain.
        first_cluster: u32,
        /// File size in bytes.
        file_size: u32,
        /// Byte offset within the input buffer.
        offset: usize,
    },
    /// An entry that does not conform to any of the above
    /// classifications (e.g. an LFN with a zero `Ord` byte, an
    /// SFN with a leading-space `Name[0]`). The caller may log
    /// or ignore. Not an error: the decoder keeps going.
    Malformed {
        /// Copy of the 32 bytes for diagnostic logging.
        bytes: [u8; DIR_ENTRY_SIZE_BYTES],
        /// Byte offset within the input buffer.
        offset: usize,
        /// Human-readable reason the entry was rejected.
        reason: &'static str,
    },
}

/// Result of [`decode_directory_cluster`].
#[derive(Debug, Default, PartialEq, Eq)]
pub struct DecodeResult {
    /// Decoded entries in directory order.
    pub entries: Vec<DecodedDirEntry>,
    /// LFN entries seen at the end of the buffer without a
    /// terminating SFN. The caller (Phase 3.5c) carries this
    /// forward into the next directory cluster of the chain.
    pub trailing_lfn: Vec<LfnEntry>,
    /// Set to `true` if the decoder hit
    /// [`DIR_ENTRY_END_OF_DIRECTORY`] before consuming the whole
    /// buffer. Subsequent bytes are not decoded.
    pub end_of_directory_seen: bool,
}

/// Errors that abort the decoder before any entries are returned.
/// Per-entry decode failures are reported as
/// [`DecodedDirEntry::Malformed`] in [`DecodeResult::entries`].
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum DirDecodeError {
    /// Input length is not a multiple of
    /// [`DIR_ENTRY_SIZE_BYTES`] (32).
    #[error("input length {length} is not a multiple of {DIR_ENTRY_SIZE_BYTES} bytes")]
    UnalignedInput {
        /// The offending length in bytes.
        length: usize,
    },
}

/// Raw LFN entry, post-decode but pre-reassembly. Held in
/// [`DecodeResult::trailing_lfn`] so the caller can stitch
/// across cluster boundaries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LfnEntry {
    /// Ordinal byte with the `LAST_LONG_ENTRY` bit masked off.
    /// `1..=20` (per fatgen103 §7).
    pub ordinal: u8,
    /// `true` if the `LAST_LONG_ENTRY` (0x40) bit was set on the
    /// raw `Ord` byte; marks the start (highest ordinal) of a
    /// chain.
    pub is_last_in_chain: bool,
    /// The SFN checksum value the chain claims its target SFN
    /// will carry. Used during reassembly to gate the chain
    /// against the trailing SFN.
    pub checksum: u8,
    /// The 13 UCS-2 code units carried by this entry.
    pub chars: [u16; LFN_CHARS_PER_ENTRY],
    /// Byte offset within the input buffer of this entry.
    pub source_offset: usize,
}

/// Walk a slice of FAT32 directory bytes and decode each
/// 32-byte entry.
///
/// `initial_lfn_carry` lets the caller resume an LFN chain
/// that started in a previous cluster — pass an empty `Vec` on
/// the first call. Most callers will pass back
/// `DecodeResult::trailing_lfn` on the subsequent call.
///
/// # Errors
///
/// * [`DirDecodeError::UnalignedInput`] if `bytes.len()` is
///   not a multiple of [`DIR_ENTRY_SIZE_BYTES`].
pub fn decode_directory_cluster(
    bytes: &[u8],
    initial_lfn_carry: Vec<LfnEntry>,
) -> Result<DecodeResult, DirDecodeError> {
    if bytes.len() % DIR_ENTRY_SIZE_BYTES != 0 {
        return Err(DirDecodeError::UnalignedInput {
            length: bytes.len(),
        });
    }

    let mut result = DecodeResult::default();
    let mut lfn_carry = initial_lfn_carry;
    let mut offset = 0usize;

    while offset + DIR_ENTRY_SIZE_BYTES <= bytes.len() {
        let Some(entry_slice) = bytes.get(offset..offset + DIR_ENTRY_SIZE_BYTES) else {
            break;
        };
        let Ok(entry_bytes) = <&[u8; DIR_ENTRY_SIZE_BYTES]>::try_from(entry_slice) else {
            offset += DIR_ENTRY_SIZE_BYTES;
            continue;
        };
        let first_byte = entry_bytes[0];
        let attr = entry_bytes[ENTRY_OFFSET_ATTR];

        if first_byte == DIR_ENTRY_END_OF_DIRECTORY {
            result.end_of_directory_seen = true;
            break;
        }

        if first_byte == DIR_ENTRY_DELETED {
            // Deleted entry — emit and clear any in-flight LFN
            // chain (a deleted SFN invalidates the preceding LFNs
            // even if they're live; Tesla MUST mark each
            // separately, but we defend anyway).
            lfn_carry.clear();
            result
                .entries
                .push(decode_deleted_entry(entry_bytes, offset));
            offset += DIR_ENTRY_SIZE_BYTES;
            continue;
        }

        if attr == ATTR_LONG_NAME {
            if let Some(lfn) = decode_lfn_entry(entry_bytes, offset) {
                lfn_carry.push(lfn);
            } else {
                lfn_carry.clear();
                result
                    .entries
                    .push(malformed(entry_bytes, offset, "invalid LFN entry"));
            }
            offset += DIR_ENTRY_SIZE_BYTES;
            continue;
        }

        // Not LFN — must be either a volume label (FAT32 root)
        // or an SFN (live file/directory). Volume label bit is
        // mutually exclusive with LFN-only attribute combo.
        if attr & ATTR_VOLUME_ID != 0 && attr & ATTR_DIRECTORY == 0 {
            // Volume label entry. Tesla writes this at byte 0 of
            // the root cluster (per fatgen103 §6.1).
            lfn_carry.clear();
            result
                .entries
                .push(decode_volume_label(entry_bytes, offset));
            offset += DIR_ENTRY_SIZE_BYTES;
            continue;
        }

        // SFN entry. Possibly with a leading LFN chain.
        match decode_sfn_entry(entry_bytes, &lfn_carry, offset) {
            Ok(decoded) => result.entries.push(decoded),
            Err(reason) => result.entries.push(malformed(entry_bytes, offset, reason)),
        }
        lfn_carry.clear();
        offset += DIR_ENTRY_SIZE_BYTES;
    }

    result.trailing_lfn = lfn_carry;
    Ok(result)
}

fn decode_deleted_entry(bytes: &[u8; DIR_ENTRY_SIZE_BYTES], offset: usize) -> DecodedDirEntry {
    let mut raw_name = [0u8; VOLUME_LABEL_NAME_LEN];
    raw_name.copy_from_slice(&bytes[..VOLUME_LABEL_NAME_LEN]);
    if raw_name[0] == DIR_ENTRY_ESCAPED_E5 {
        raw_name[0] = DIR_ENTRY_DELETED;
    }
    DecodedDirEntry::Deleted {
        raw_name,
        first_cluster: extract_first_cluster(bytes),
        file_size: extract_file_size(bytes),
        offset,
    }
}

fn decode_volume_label(bytes: &[u8; DIR_ENTRY_SIZE_BYTES], offset: usize) -> DecodedDirEntry {
    let mut label = [0u8; VOLUME_LABEL_NAME_LEN];
    label.copy_from_slice(&bytes[..VOLUME_LABEL_NAME_LEN]);
    DecodedDirEntry::VolumeLabel { label, offset }
}

fn decode_lfn_entry(bytes: &[u8; DIR_ENTRY_SIZE_BYTES], offset: usize) -> Option<LfnEntry> {
    let ord_raw = bytes[LFN_OFFSET_ORD];
    let is_last_in_chain = ord_raw & LAST_LONG_ENTRY != 0;
    let ordinal = ord_raw & !LAST_LONG_ENTRY;
    if ordinal == 0 || ordinal as usize > LFN_MAX_ENTRIES {
        return None;
    }
    let checksum = bytes[LFN_OFFSET_CHKSUM];
    let mut chars = [0u16; LFN_CHARS_PER_ENTRY];
    copy_lfn_pairs(bytes, LFN_NAME1_OFFSET, LFN_NAME1_PAIRS, &mut chars, 0);
    copy_lfn_pairs(
        bytes,
        LFN_NAME2_OFFSET,
        LFN_NAME2_PAIRS,
        &mut chars,
        LFN_NAME1_PAIRS,
    );
    copy_lfn_pairs(
        bytes,
        LFN_NAME3_OFFSET,
        LFN_NAME3_PAIRS,
        &mut chars,
        LFN_NAME1_PAIRS + LFN_NAME2_PAIRS,
    );
    Some(LfnEntry {
        ordinal,
        is_last_in_chain,
        checksum,
        chars,
        source_offset: offset,
    })
}

fn copy_lfn_pairs(
    src: &[u8; DIR_ENTRY_SIZE_BYTES],
    src_offset: usize,
    pair_count: usize,
    dest: &mut [u16; LFN_CHARS_PER_ENTRY],
    dest_offset: usize,
) {
    for i in 0..pair_count {
        let lo_index = src_offset + i * 2;
        let hi_index = lo_index + 1;
        let lo = src.get(lo_index).copied().unwrap_or(0);
        let hi = src.get(hi_index).copied().unwrap_or(0);
        let unit = u16::from(lo) | (u16::from(hi) << 8);
        if let Some(slot) = dest.get_mut(dest_offset + i) {
            *slot = unit;
        }
    }
}

fn decode_sfn_entry(
    bytes: &[u8; DIR_ENTRY_SIZE_BYTES],
    lfn_carry: &[LfnEntry],
    sfn_offset: usize,
) -> Result<DecodedDirEntry, &'static str> {
    let mut name_bytes = [0u8; VOLUME_LABEL_NAME_LEN];
    name_bytes.copy_from_slice(&bytes[..VOLUME_LABEL_NAME_LEN]);
    if name_bytes[0] == DIR_ENTRY_ESCAPED_E5 {
        name_bytes[0] = DIR_ENTRY_DELETED;
    }
    let short_name = ShortName::from_bytes(&name_bytes).map_err(|_| "invalid SFN bytes")?;
    let attributes = bytes[ENTRY_OFFSET_ATTR];
    let first_cluster = extract_first_cluster(bytes);
    let file_size = extract_file_size(bytes);

    let long_name = reassemble_lfn_chain(lfn_carry, &short_name);

    if long_name.is_none() && !lfn_carry.is_empty() {
        return Ok(DecodedDirEntry::ShortNameOnly {
            short_name,
            attributes,
            first_cluster,
            file_size,
            sfn_offset,
        });
    }

    Ok(DecodedDirEntry::File {
        long_name,
        short_name,
        attributes,
        first_cluster,
        file_size,
        sfn_offset,
    })
}

fn reassemble_lfn_chain(lfn_carry: &[LfnEntry], short_name: &ShortName) -> Option<String> {
    if lfn_carry.is_empty() {
        return None;
    }
    let expected_checksum = short_name.checksum();
    // The first entry must have LAST_LONG_ENTRY set and the
    // ordinal must equal the chain length.
    let first = lfn_carry.first()?;
    if !first.is_last_in_chain {
        return None;
    }
    let expected_count = first.ordinal as usize;
    if lfn_carry.len() != expected_count {
        return None;
    }
    // Verify each entry: descending ordinals, all same checksum.
    for (idx, entry) in lfn_carry.iter().enumerate() {
        let expected_ord = u8::try_from(expected_count - idx).ok()?;
        if entry.ordinal != expected_ord {
            return None;
        }
        if entry.checksum != expected_checksum {
            return None;
        }
        let is_first = idx == 0;
        if entry.is_last_in_chain != is_first {
            return None;
        }
    }
    // Concatenate in REVERSE on-disk order: ordinal 1 chars
    // first, then ordinal 2, ..., then ordinal N.
    let mut all_chars: Vec<u16> = Vec::with_capacity(expected_count * LFN_CHARS_PER_ENTRY);
    for entry in lfn_carry.iter().rev() {
        all_chars.extend_from_slice(&entry.chars);
    }
    // Truncate at first 0x0000 terminator.
    let end = all_chars
        .iter()
        .position(|&c| c == LFN_TERMINATOR)
        .unwrap_or(all_chars.len());
    let trimmed = all_chars.get(..end).unwrap_or(&[]);
    String::from_utf16(trimmed).ok()
}

fn extract_first_cluster(bytes: &[u8; DIR_ENTRY_SIZE_BYTES]) -> u32 {
    let hi = read_u16_le(bytes, SFN_OFFSET_FST_CLUS_HI);
    let lo = read_u16_le(bytes, SFN_OFFSET_FST_CLUS_LO);
    (u32::from(hi) << 16) | u32::from(lo)
}

fn extract_file_size(bytes: &[u8; DIR_ENTRY_SIZE_BYTES]) -> u32 {
    let b0 = bytes.get(SFN_OFFSET_FILE_SIZE).copied().unwrap_or(0);
    let b1 = bytes.get(SFN_OFFSET_FILE_SIZE + 1).copied().unwrap_or(0);
    let b2 = bytes.get(SFN_OFFSET_FILE_SIZE + 2).copied().unwrap_or(0);
    let b3 = bytes.get(SFN_OFFSET_FILE_SIZE + 3).copied().unwrap_or(0);
    u32::from_le_bytes([b0, b1, b2, b3])
}

fn read_u16_le(bytes: &[u8; DIR_ENTRY_SIZE_BYTES], offset: usize) -> u16 {
    let lo = bytes.get(offset).copied().unwrap_or(0);
    let hi = bytes.get(offset + 1).copied().unwrap_or(0);
    u16::from_le_bytes([lo, hi])
}

fn malformed(
    bytes: &[u8; DIR_ENTRY_SIZE_BYTES],
    offset: usize,
    reason: &'static str,
) -> DecodedDirEntry {
    DecodedDirEntry::Malformed {
        bytes: *bytes,
        offset,
        reason,
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
    use super::super::directory::{
        FileAttributes, Timestamps, synthesize_lfn_sequence, synthesize_sfn_entry,
        synthesize_volume_label_entry,
    };
    use super::*;

    fn epoch_ts() -> Timestamps {
        Timestamps::epoch()
    }

    fn make_file_entry_bytes(
        long_name: &str,
        sfn_str: &str,
        first_cluster: u32,
        file_size: u32,
    ) -> Vec<u8> {
        let sfn = ShortName::from_padded_str(sfn_str).expect("sfn");
        let lfn_entries = synthesize_lfn_sequence(long_name, sfn.checksum()).expect("lfn");
        let sfn_entry = synthesize_sfn_entry(
            &sfn,
            FileAttributes::archive(),
            first_cluster,
            file_size,
            &epoch_ts(),
        );
        let mut bytes = Vec::with_capacity((lfn_entries.len() + 1) * DIR_ENTRY_SIZE_BYTES);
        for e in &lfn_entries {
            bytes.extend_from_slice(e);
        }
        bytes.extend_from_slice(&sfn_entry);
        bytes
    }

    fn pad_to_cluster(bytes: &mut Vec<u8>, cluster_size: usize) {
        bytes.resize(cluster_size, 0u8);
    }

    #[test]
    fn empty_input_decodes_to_empty_result() {
        let r = decode_directory_cluster(&[], vec![]).expect("ok");
        assert!(r.entries.is_empty());
        assert!(r.trailing_lfn.is_empty());
        assert!(!r.end_of_directory_seen);
    }

    #[test]
    fn unaligned_input_returns_error() {
        let err = decode_directory_cluster(&[0u8; 33], vec![]).expect_err("err");
        assert_eq!(err, DirDecodeError::UnalignedInput { length: 33 });
    }

    #[test]
    fn end_of_directory_marker_stops_walk() {
        let bytes = vec![0u8; 64]; // two empty entries
        let r = decode_directory_cluster(&bytes, vec![]).expect("ok");
        assert!(r.end_of_directory_seen);
        assert!(r.entries.is_empty());
    }

    #[test]
    fn decode_sfn_only_file_no_lfn() {
        let sfn = ShortName::from_padded_str("HELLO.TXT").expect("sfn");
        let sfn_entry = synthesize_sfn_entry(&sfn, FileAttributes::archive(), 100, 42, &epoch_ts());
        let mut bytes = sfn_entry.to_vec();
        pad_to_cluster(&mut bytes, 64);
        let r = decode_directory_cluster(&bytes, vec![]).expect("ok");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedDirEntry::File {
                long_name,
                short_name,
                first_cluster,
                file_size,
                attributes,
                sfn_offset,
            } => {
                assert!(long_name.is_none(), "no LFN was synthesized");
                assert_eq!(short_name.as_bytes(), sfn.as_bytes());
                assert_eq!(*first_cluster, 100);
                assert_eq!(*file_size, 42);
                assert_eq!(*attributes, FileAttributes::archive().raw());
                assert_eq!(*sfn_offset, 0);
            }
            other => panic!("expected File, got {other:?}"),
        }
        assert!(r.end_of_directory_seen);
    }

    #[test]
    fn decode_lfn_plus_sfn_short_filename() {
        let bytes_one = make_file_entry_bytes("hello.txt", "HELLO.TXT", 200, 100);
        let mut bytes = bytes_one;
        pad_to_cluster(&mut bytes, 128);
        let r = decode_directory_cluster(&bytes, vec![]).expect("ok");
        // 1 LFN + 1 SFN = 2 entries on disk, but the decoder
        // collapses them to ONE File entry.
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedDirEntry::File {
                long_name,
                first_cluster,
                ..
            } => {
                assert_eq!(long_name.as_deref(), Some("hello.txt"));
                assert_eq!(*first_cluster, 200);
            }
            other => panic!("expected File, got {other:?}"),
        }
    }

    #[test]
    fn decode_lfn_plus_sfn_long_filename_spans_multiple_lfn_entries() {
        let long_name = "2026-05-20_14-32-15-front.mp4";
        let bytes_one = make_file_entry_bytes(long_name, "20260~01.MP4", 300, 1_048_576);
        let mut bytes = bytes_one;
        pad_to_cluster(&mut bytes, 256);
        let r = decode_directory_cluster(&bytes, vec![]).expect("ok");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedDirEntry::File {
                long_name: lname,
                first_cluster,
                file_size,
                ..
            } => {
                assert_eq!(lname.as_deref(), Some(long_name));
                assert_eq!(*first_cluster, 300);
                assert_eq!(*file_size, 1_048_576);
            }
            other => panic!("expected File, got {other:?}"),
        }
    }

    #[test]
    fn decode_deleted_entry_extracts_first_cluster() {
        let sfn = ShortName::from_padded_str("VICTIM.TXT").expect("sfn");
        let sfn_entry =
            synthesize_sfn_entry(&sfn, FileAttributes::archive(), 500, 1234, &epoch_ts());
        let mut bytes = sfn_entry.to_vec();
        bytes[0] = DIR_ENTRY_DELETED;
        pad_to_cluster(&mut bytes, 64);
        let r = decode_directory_cluster(&bytes, vec![]).expect("ok");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedDirEntry::Deleted {
                first_cluster,
                file_size,
                raw_name,
                ..
            } => {
                assert_eq!(*first_cluster, 500);
                assert_eq!(*file_size, 1234);
                assert_eq!(raw_name[0], DIR_ENTRY_DELETED);
                assert_eq!(&raw_name[1..3], b"IC");
            }
            other => panic!("expected Deleted, got {other:?}"),
        }
    }

    #[test]
    fn decode_volume_label_entry_extracts_label() {
        let label = *b"TESLACAM   ";
        let entry = synthesize_volume_label_entry(&label, &epoch_ts());
        let mut bytes = entry.to_vec();
        pad_to_cluster(&mut bytes, 64);
        let r = decode_directory_cluster(&bytes, vec![]).expect("ok");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedDirEntry::VolumeLabel { label, offset } => {
                assert_eq!(label, b"TESLACAM   ");
                assert_eq!(*offset, 0);
            }
            other => panic!("expected VolumeLabel, got {other:?}"),
        }
    }

    #[test]
    fn lfn_with_bad_checksum_degrades_to_short_name_only() {
        let sfn = ShortName::from_padded_str("TARGET.TXT").expect("sfn");
        let wrong_checksum = sfn.checksum().wrapping_add(1);
        let lfn_entries = synthesize_lfn_sequence("target.txt", wrong_checksum).expect("lfn");
        let sfn_entry = synthesize_sfn_entry(&sfn, FileAttributes::archive(), 9, 11, &epoch_ts());
        let mut bytes = Vec::new();
        for e in &lfn_entries {
            bytes.extend_from_slice(e);
        }
        bytes.extend_from_slice(&sfn_entry);
        pad_to_cluster(&mut bytes, 128);
        let r = decode_directory_cluster(&bytes, vec![]).expect("ok");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedDirEntry::ShortNameOnly { first_cluster, .. } => {
                assert_eq!(*first_cluster, 9);
            }
            other => panic!("expected ShortNameOnly, got {other:?}"),
        }
    }

    #[test]
    fn malformed_lfn_with_zero_ordinal_is_reported_as_malformed() {
        let mut bytes = vec![0u8; DIR_ENTRY_SIZE_BYTES * 2];
        bytes[0] = LAST_LONG_ENTRY;
        bytes[ENTRY_OFFSET_ATTR] = ATTR_LONG_NAME;
        let r = decode_directory_cluster(&bytes, vec![]).expect("ok");
        assert_eq!(r.entries.len(), 1);
        match &r.entries[0] {
            DecodedDirEntry::Malformed { reason, .. } => {
                assert_eq!(*reason, "invalid LFN entry");
            }
            other => panic!("expected Malformed, got {other:?}"),
        }
        assert!(r.end_of_directory_seen);
    }

    #[test]
    fn trailing_lfn_without_sfn_is_returned_in_carry() {
        let sfn = ShortName::from_padded_str("HELLO.TXT").expect("sfn");
        let lfn_entries = synthesize_lfn_sequence("hello.txt", sfn.checksum()).expect("lfn");
        let mut bytes = Vec::new();
        for e in &lfn_entries {
            bytes.extend_from_slice(e);
        }
        bytes.resize(bytes.len() + DIR_ENTRY_SIZE_BYTES, 0u8);
        let r = decode_directory_cluster(&bytes, vec![]).expect("ok");
        assert!(r.entries.is_empty());
        assert_eq!(r.trailing_lfn.len(), 1);
        assert!(r.trailing_lfn[0].is_last_in_chain);
    }

    #[test]
    fn lfn_carry_from_previous_cluster_completes_on_sfn() {
        let sfn = ShortName::from_padded_str("HELLO.TXT").expect("sfn");
        let lfn_entries = synthesize_lfn_sequence("hello.txt", sfn.checksum()).expect("lfn");
        let mut lfn_only = Vec::new();
        for e in &lfn_entries {
            lfn_only.extend_from_slice(e);
        }
        let r1 = decode_directory_cluster(&lfn_only, vec![]).expect("ok");
        assert!(r1.entries.is_empty());
        assert_eq!(r1.trailing_lfn.len(), 1);

        let sfn_entry = synthesize_sfn_entry(&sfn, FileAttributes::archive(), 77, 42, &epoch_ts());
        let r2 = decode_directory_cluster(&sfn_entry, r1.trailing_lfn).expect("ok");
        assert_eq!(r2.entries.len(), 1);
        match &r2.entries[0] {
            DecodedDirEntry::File { long_name, .. } => {
                assert_eq!(long_name.as_deref(), Some("hello.txt"));
            }
            other => panic!("expected File, got {other:?}"),
        }
    }

    #[test]
    fn deleted_entry_clears_pending_lfn_chain() {
        let sfn1 = ShortName::from_padded_str("VICTIM.TXT").expect("sfn");
        let lfn1_entries = synthesize_lfn_sequence("victim.txt", sfn1.checksum()).expect("lfn");
        let mut bytes = Vec::new();
        for e in &lfn1_entries {
            bytes.extend_from_slice(e);
        }
        let sfn1_entry =
            synthesize_sfn_entry(&sfn1, FileAttributes::archive(), 10, 100, &epoch_ts());
        let mut deleted_sfn1 = sfn1_entry;
        deleted_sfn1[0] = DIR_ENTRY_DELETED;
        bytes.extend_from_slice(&deleted_sfn1);

        let sfn2 = ShortName::from_padded_str("LIVE.TXT").expect("sfn2");
        let lfn2_entries = synthesize_lfn_sequence("live.txt", sfn2.checksum()).expect("lfn2");
        for e in &lfn2_entries {
            bytes.extend_from_slice(e);
        }
        let sfn2_entry =
            synthesize_sfn_entry(&sfn2, FileAttributes::archive(), 20, 200, &epoch_ts());
        bytes.extend_from_slice(&sfn2_entry);

        let r = decode_directory_cluster(&bytes, vec![]).expect("ok");
        let kinds: Vec<&'static str> = r
            .entries
            .iter()
            .map(|e| match e {
                DecodedDirEntry::File { .. } => "file",
                DecodedDirEntry::ShortNameOnly { .. } => "sfn_only",
                DecodedDirEntry::Deleted { .. } => "deleted",
                DecodedDirEntry::VolumeLabel { .. } => "vol",
                DecodedDirEntry::Malformed { .. } => "malformed",
            })
            .collect();
        assert_eq!(kinds, vec!["deleted", "file"]);
        if let DecodedDirEntry::File { long_name, .. } = &r.entries[1] {
            assert_eq!(long_name.as_deref(), Some("live.txt"));
        } else {
            panic!("entry 1 not File");
        }
    }
}
