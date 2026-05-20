//! FAT32 cluster-chain walker for the write-side pipeline
//! (Phase 3.5b).
//!
//! Phase 3.5a decodes a directory cluster into
//! [`crate::fs::fat32::dir_decode::DecodedDirEntry::File`] values,
//! each of which carries a *first* cluster but not the total
//! cluster count. To compute the file's full extent (or extents,
//! if it's fragmented), the caller needs to walk the cluster
//! chain in the FAT.
//!
//! This module is the bridge: given a slice of FAT bytes and a
//! starting cluster, [`walk_chain`] returns the ordered list of
//! cluster numbers in the chain, and [`chain_to_extents`]
//! collapses that list into one or more `(first_cluster,
//! cluster_count)` runs.
//!
//! ## What the caller hands us
//!
//! In Phase 3.5c, the wiring layer maintains a running buffer of
//! the FAT (the canonical copy that mirrors what Tesla wrote).
//! As Tesla writes more sectors of the FAT, the buffer grows.
//! When a directory cluster's `DecodedDirEntry::File` arrives,
//! the wiring layer hands us the FAT buffer + the file's
//! `first_cluster` and gets back the cluster chain to feed into
//! [`crate::fs::cluster_map::ClusterMap::insert`].
//!
//! ## Per-entry classification
//!
//! Per fatgen103 §4.1 (echoed in this crate's
//! [`crate::fs::fat32::fat_table`] constants), each 32-bit FAT
//! entry uses only the **low 28 bits**:
//!
//! | 28-bit value           | Meaning                          |
//! |------------------------|----------------------------------|
//! | `0x0000000`            | Free cluster                     |
//! | `0x0000002..=0xFFFFFEF`| Next cluster in chain            |
//! | `0xFFFFFF0..=0xFFFFFF6`| Reserved                         |
//! | `0xFFFFFF7`            | Bad cluster                      |
//! | `0xFFFFFF8..=0xFFFFFFF`| End of chain                     |
//!
//! ## Cycle / overflow safety
//!
//! A corrupt FAT could form a cycle (cluster N points back to
//! itself or an earlier cluster) that would loop forever.
//! [`walk_chain`] is bounded by [`MAX_CHAIN_LENGTH`] (default 1
//! million) — beyond that the walker returns
//! [`ChainWalkError::ChainTooLong`]. The bound is generous
//! enough for a 32 GiB volume with 32 KiB clusters (~1 M
//! clusters per file maximum) and tight enough that a runaway
//! cycle is caught quickly.

use crate::fs::cluster_layout::FIRST_DATA_CLUSTER;
use crate::fs::cluster_map::FileExtent;
use crate::fs::fat32::fat_table::{
    BAD_CLUSTER_MARKER, END_OF_CHAIN_MIN, FAT_ENTRY_SIZE_BYTES, FREE_CLUSTER,
};

/// Maximum number of clusters [`walk_chain`] will follow before
/// declaring the chain malformed. See module-level docs.
pub const MAX_CHAIN_LENGTH: usize = 1_048_576;

/// Mask for the meaningful low 28 bits of a FAT32 entry
/// (fatgen103 §4.1).
const FAT32_ENTRY_VALUE_MASK: u32 = 0x0FFF_FFFF;

/// Reserved-range lower bound (inclusive). The fatgen103 §4.1
/// reserved range is `0x0FFFFFF0..=0x0FFFFFF6`; any value at or
/// above this lower bound but below `BAD_CLUSTER_MARKER` is
/// reserved.
const RESERVED_RANGE_MIN: u32 = 0x0FFF_FFF0;

/// Errors returned by [`walk_chain`].
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ChainWalkError {
    /// `start_cluster` is below `FIRST_DATA_CLUSTER` (the
    /// reserved entries 0 and 1 don't own data).
    #[error("start_cluster {0} is reserved (must be >= 2)")]
    ReservedStartCluster(u32),
    /// The FAT entry for some cluster in the chain is past the
    /// end of the supplied FAT buffer.
    #[error("cluster {cluster} at FAT byte {byte_offset} is past buffer length {buffer_len}")]
    OutOfBoundsCluster {
        /// The cluster whose entry was sought.
        cluster: u32,
        /// The byte offset within the FAT where its entry would
        /// have been.
        byte_offset: u64,
        /// The length of the supplied FAT buffer in bytes.
        buffer_len: usize,
    },
    /// The walker followed a chain longer than [`MAX_CHAIN_LENGTH`]
    /// without finding an end-of-chain marker.
    #[error("chain longer than {max} clusters from start {start_cluster}")]
    ChainTooLong {
        /// The chain's starting cluster.
        start_cluster: u32,
        /// The bound that was exceeded.
        max: usize,
    },
    /// A cluster in the chain was encountered twice — the chain
    /// contains a cycle. Returned with the first repeated
    /// cluster.
    #[error("cycle detected: cluster {repeated} re-entered the chain")]
    Cycle {
        /// The cluster that was visited twice.
        repeated: u32,
    },
    /// A cluster in the chain pointed to a free entry
    /// (`0x00000000`). A live file should never reference a
    /// free cluster.
    #[error("cluster {cluster} pointed to a free entry")]
    PointsToFree {
        /// The cluster whose entry was free.
        cluster: u32,
    },
    /// A cluster in the chain pointed to a bad-cluster marker
    /// or a reserved-range value.
    #[error("cluster {cluster} pointed to invalid value {value:#010x} (bad cluster or reserved)")]
    PointsToInvalid {
        /// The cluster whose entry was bad/reserved.
        cluster: u32,
        /// The raw 28-bit value.
        value: u32,
    },
}

/// Walk the cluster chain starting at `start_cluster`, reading
/// FAT entries from `fat_bytes`.
///
/// `fat_bytes` is the byte image of one FAT copy starting at FAT
/// byte 0 (which is the entry for cluster 0). Phase 3.5c's
/// wiring layer keeps a running canonical FAT mirror; for tests,
/// the caller can construct an arbitrary `Vec<u8>` by encoding
/// `u32`s in little-endian.
///
/// Returns the chain as `Vec<u32>` of cluster numbers, in order
/// — `start_cluster` first, then each follower, ending at the
/// cluster whose entry is the EOC marker.
///
/// # Errors
///
/// See [`ChainWalkError`].
pub fn walk_chain(fat_bytes: &[u8], start_cluster: u32) -> Result<Vec<u32>, ChainWalkError> {
    if start_cluster < FIRST_DATA_CLUSTER {
        return Err(ChainWalkError::ReservedStartCluster(start_cluster));
    }
    let mut chain = Vec::new();
    let mut seen = std::collections::HashSet::new();
    let mut current = start_cluster;
    loop {
        if chain.len() >= MAX_CHAIN_LENGTH {
            return Err(ChainWalkError::ChainTooLong {
                start_cluster,
                max: MAX_CHAIN_LENGTH,
            });
        }
        if !seen.insert(current) {
            return Err(ChainWalkError::Cycle { repeated: current });
        }
        chain.push(current);

        let entry = read_fat_entry(fat_bytes, current)?;
        if (END_OF_CHAIN_MIN..=FAT32_ENTRY_VALUE_MASK).contains(&entry) {
            // EOC marker (anywhere in 0x0FFFFFF8..=0x0FFFFFFF) —
            // chain ends with the current cluster included.
            return Ok(chain);
        }
        if entry == FREE_CLUSTER {
            return Err(ChainWalkError::PointsToFree { cluster: current });
        }
        if entry == BAD_CLUSTER_MARKER || (RESERVED_RANGE_MIN..BAD_CLUSTER_MARKER).contains(&entry)
        {
            return Err(ChainWalkError::PointsToInvalid {
                cluster: current,
                value: entry,
            });
        }
        if entry < FIRST_DATA_CLUSTER {
            return Err(ChainWalkError::PointsToInvalid {
                cluster: current,
                value: entry,
            });
        }
        current = entry;
    }
}

/// Read one FAT entry as a 28-bit value (top 4 bits masked off).
///
/// # Errors
///
/// * [`ChainWalkError::OutOfBoundsCluster`] if the entry would
///   be past the end of `fat_bytes`.
fn read_fat_entry(fat_bytes: &[u8], cluster: u32) -> Result<u32, ChainWalkError> {
    let byte_offset = u64::from(cluster) * u64::from(FAT_ENTRY_SIZE_BYTES);
    let start = usize::try_from(byte_offset).map_err(|_| ChainWalkError::OutOfBoundsCluster {
        cluster,
        byte_offset,
        buffer_len: fat_bytes.len(),
    })?;
    let end = start + FAT_ENTRY_SIZE_BYTES as usize;
    let slice = fat_bytes
        .get(start..end)
        .ok_or(ChainWalkError::OutOfBoundsCluster {
            cluster,
            byte_offset,
            buffer_len: fat_bytes.len(),
        })?;
    let arr: [u8; 4] = slice
        .try_into()
        .map_err(|_| ChainWalkError::OutOfBoundsCluster {
            cluster,
            byte_offset,
            buffer_len: fat_bytes.len(),
        })?;
    let raw = u32::from_le_bytes(arr);
    Ok(raw & FAT32_ENTRY_VALUE_MASK)
}

/// Collapse a cluster chain into one or more
/// [`FileExtent`] runs.
///
/// The returned vector lists extents in chain order, each
/// extent being a maximal contiguous run. For a perfectly
/// contiguous file the result has length 1; for fragmented
/// files the count rises.
///
/// `file_path` and `bytes_per_cluster` describe the file the
/// chain belongs to. `bytes_per_cluster` is needed to compute
/// the second-and-later extents'
/// [`FileExtent::first_byte_in_file`].
///
/// Returns an empty vector for an empty chain (Phase 3.5c never
/// calls this with an empty chain — `walk_chain` always returns
/// at least one cluster — but the empty-input case is well-
/// defined for defensive callers).
#[must_use]
pub fn chain_to_extents(
    chain: &[u32],
    file_path: std::path::PathBuf,
    bytes_per_cluster: u32,
) -> Vec<FileExtent> {
    let mut extents = Vec::new();
    let Some(&first) = chain.first() else {
        return extents;
    };
    let mut current_first = first;
    let mut current_count: u32 = 1;
    let mut first_byte_in_file: u64 = 0;

    for &next in chain.iter().skip(1) {
        if next == current_first.saturating_add(current_count) {
            current_count = current_count.saturating_add(1);
            continue;
        }
        // Break: emit the current run.
        extents.push(FileExtent {
            first_cluster: current_first,
            cluster_count: current_count,
            first_byte_in_file,
            file_path: file_path.clone(),
        });
        first_byte_in_file = first_byte_in_file
            .saturating_add(u64::from(current_count).saturating_mul(u64::from(bytes_per_cluster)));
        current_first = next;
        current_count = 1;
    }
    // Tail run.
    extents.push(FileExtent {
        first_cluster: current_first,
        cluster_count: current_count,
        first_byte_in_file,
        file_path,
    });
    extents
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
    use std::path::PathBuf;

    /// Build a FAT buffer big enough for `cluster_count` clusters
    /// (entries `0..cluster_count`), with reserved entries 0 and 1
    /// pre-filled per fatgen103 §4.1 and all data clusters free.
    fn empty_fat(cluster_count: u32) -> Vec<u8> {
        let mut buf = vec![0u8; (cluster_count as usize) * 4];
        write_entry(&mut buf, 0, 0x0FFF_FFF8);
        write_entry(&mut buf, 1, 0x0FFF_FFFF);
        buf
    }

    fn write_entry(buf: &mut [u8], cluster: u32, value: u32) {
        let start = (cluster as usize) * 4;
        let bytes = value.to_le_bytes();
        buf[start..start + 4].copy_from_slice(&bytes);
    }

    #[test]
    fn walk_single_cluster_chain() {
        let mut fat = empty_fat(10);
        // Cluster 2 EOC.
        write_entry(&mut fat, 2, 0x0FFF_FFFF);
        let chain = walk_chain(&fat, 2).expect("ok");
        assert_eq!(chain, vec![2]);
    }

    #[test]
    fn walk_two_cluster_contiguous_chain() {
        let mut fat = empty_fat(10);
        write_entry(&mut fat, 2, 3); // 2 -> 3
        write_entry(&mut fat, 3, 0x0FFF_FFFF); // 3 EOC
        let chain = walk_chain(&fat, 2).expect("ok");
        assert_eq!(chain, vec![2, 3]);
    }

    #[test]
    fn walk_multi_cluster_contiguous_chain() {
        let mut fat = empty_fat(20);
        write_entry(&mut fat, 5, 6);
        write_entry(&mut fat, 6, 7);
        write_entry(&mut fat, 7, 8);
        write_entry(&mut fat, 8, 0x0FFF_FFFF);
        let chain = walk_chain(&fat, 5).expect("ok");
        assert_eq!(chain, vec![5, 6, 7, 8]);
    }

    #[test]
    fn walk_fragmented_chain_2_4_6() {
        let mut fat = empty_fat(20);
        write_entry(&mut fat, 2, 4);
        write_entry(&mut fat, 4, 6);
        write_entry(&mut fat, 6, 0x0FFF_FFF8); // also valid EOC
        let chain = walk_chain(&fat, 2).expect("ok");
        assert_eq!(chain, vec![2, 4, 6]);
    }

    #[test]
    fn walk_chain_with_eoc_anywhere_in_eoc_range() {
        // Per fatgen103 §4.1 ANY value in 0x0FFFFFF8..=0x0FFFFFFF
        // is a valid EOC marker.
        for eoc in [0x0FFF_FFF8u32, 0x0FFF_FFFCu32, 0x0FFF_FFFFu32] {
            let mut fat = empty_fat(5);
            write_entry(&mut fat, 2, eoc);
            let chain = walk_chain(&fat, 2).expect("ok");
            assert_eq!(chain, vec![2]);
        }
    }

    #[test]
    fn reserved_start_cluster_zero_or_one_is_rejected() {
        let fat = empty_fat(5);
        assert_eq!(
            walk_chain(&fat, 0).expect_err("err"),
            ChainWalkError::ReservedStartCluster(0)
        );
        assert_eq!(
            walk_chain(&fat, 1).expect_err("err"),
            ChainWalkError::ReservedStartCluster(1)
        );
    }

    #[test]
    fn out_of_bounds_cluster_is_rejected() {
        let fat = empty_fat(5); // entries 0..4 valid
        let err = walk_chain(&fat, 100).expect_err("err");
        assert!(matches!(err, ChainWalkError::OutOfBoundsCluster { .. }));
    }

    #[test]
    fn cycle_back_to_start_is_detected() {
        let mut fat = empty_fat(10);
        write_entry(&mut fat, 2, 3);
        write_entry(&mut fat, 3, 2); // back to start
        let err = walk_chain(&fat, 2).expect_err("err");
        assert_eq!(err, ChainWalkError::Cycle { repeated: 2 });
    }

    #[test]
    fn cycle_within_chain_is_detected() {
        let mut fat = empty_fat(20);
        write_entry(&mut fat, 5, 6);
        write_entry(&mut fat, 6, 7);
        write_entry(&mut fat, 7, 6); // 7 -> 6 cycle
        let err = walk_chain(&fat, 5).expect_err("err");
        assert_eq!(err, ChainWalkError::Cycle { repeated: 6 });
    }

    #[test]
    fn chain_pointing_to_free_cluster_is_rejected() {
        let mut fat = empty_fat(10);
        write_entry(&mut fat, 2, 3);
        // Cluster 3 is implicitly free (0x00000000) — empty_fat
        // didn't touch it.
        let err = walk_chain(&fat, 2).expect_err("err");
        assert_eq!(err, ChainWalkError::PointsToFree { cluster: 3 });
    }

    #[test]
    fn chain_pointing_to_bad_cluster_marker_is_rejected() {
        let mut fat = empty_fat(10);
        write_entry(&mut fat, 2, BAD_CLUSTER_MARKER);
        let err = walk_chain(&fat, 2).expect_err("err");
        match err {
            ChainWalkError::PointsToInvalid { cluster, value } => {
                assert_eq!(cluster, 2);
                assert_eq!(value, BAD_CLUSTER_MARKER);
            }
            other => panic!("expected PointsToInvalid, got {other:?}"),
        }
    }

    #[test]
    fn chain_pointing_to_reserved_range_is_rejected() {
        let mut fat = empty_fat(10);
        write_entry(&mut fat, 2, 0x0FFF_FFF3); // in reserved range
        let err = walk_chain(&fat, 2).expect_err("err");
        match err {
            ChainWalkError::PointsToInvalid { cluster, value } => {
                assert_eq!(cluster, 2);
                assert_eq!(value, 0x0FFF_FFF3);
            }
            other => panic!("expected PointsToInvalid, got {other:?}"),
        }
    }

    #[test]
    fn upper_4_bits_of_entry_are_ignored() {
        // Per fatgen103 §4.1, the top 4 bits of a FAT32 entry
        // are reserved on read. An entry of 0xF000_0003 is the
        // same as 0x0000_0003 (cluster 3).
        let mut fat = empty_fat(10);
        write_entry(&mut fat, 2, 0xF000_0003);
        write_entry(&mut fat, 3, 0x0FFF_FFFF);
        let chain = walk_chain(&fat, 2).expect("ok");
        assert_eq!(chain, vec![2, 3]);
    }

    #[test]
    fn chain_to_extents_single_cluster() {
        let extents = chain_to_extents(&[5], PathBuf::from("x.bin"), 4096);
        assert_eq!(extents.len(), 1);
        assert_eq!(extents[0].first_cluster, 5);
        assert_eq!(extents[0].cluster_count, 1);
        assert_eq!(extents[0].first_byte_in_file, 0);
    }

    #[test]
    fn chain_to_extents_contiguous_run() {
        let extents = chain_to_extents(&[5, 6, 7, 8], PathBuf::from("x.bin"), 4096);
        assert_eq!(extents.len(), 1);
        assert_eq!(extents[0].first_cluster, 5);
        assert_eq!(extents[0].cluster_count, 4);
        assert_eq!(extents[0].first_byte_in_file, 0);
    }

    #[test]
    fn chain_to_extents_fragmented_2_4_6() {
        let extents = chain_to_extents(&[2, 4, 6], PathBuf::from("x.bin"), 4096);
        assert_eq!(extents.len(), 3);
        assert_eq!(extents[0].first_cluster, 2);
        assert_eq!(extents[0].cluster_count, 1);
        assert_eq!(extents[0].first_byte_in_file, 0);
        assert_eq!(extents[1].first_cluster, 4);
        assert_eq!(extents[1].cluster_count, 1);
        assert_eq!(extents[1].first_byte_in_file, 4096);
        assert_eq!(extents[2].first_cluster, 6);
        assert_eq!(extents[2].cluster_count, 1);
        assert_eq!(extents[2].first_byte_in_file, 8192);
    }

    #[test]
    fn chain_to_extents_mixed_5_6_7_then_jump_to_10_11() {
        let extents = chain_to_extents(&[5, 6, 7, 10, 11], PathBuf::from("x.bin"), 4096);
        assert_eq!(extents.len(), 2);
        assert_eq!(extents[0].first_cluster, 5);
        assert_eq!(extents[0].cluster_count, 3);
        assert_eq!(extents[0].first_byte_in_file, 0);
        assert_eq!(extents[1].first_cluster, 10);
        assert_eq!(extents[1].cluster_count, 2);
        assert_eq!(extents[1].first_byte_in_file, 3 * 4096);
    }

    #[test]
    fn chain_to_extents_empty_chain_returns_empty() {
        let extents = chain_to_extents(&[], PathBuf::from("x.bin"), 4096);
        assert!(extents.is_empty());
    }
}
