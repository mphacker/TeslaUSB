//! Directory-tree walk: from the root cluster, follow each directory's
//! own cluster chain, decode its entries with
//! [`teslausb_core::fs::exfat::dir_decode::decode_directory_cluster`]
//! (threading the cross-cluster carry), and emit a [`FileRecord`] per
//! live file. Subdirectories are recursed via an explicit work-list
//! with depth / entry / directory caps and a visited-set so a corrupt
//! directory graph can never loop or exhaust memory.
//!
//! The walk **reads only** — it never writes, mounts, or allocates on
//! the volume. Deleted and malformed entries are skipped (not indexed);
//! checksum-failed entries are still emitted with `set_checksum_ok =
//! false` so the stability gate can decide.

use std::collections::HashSet;

use teslausb_core::fs::exfat::dir_decode::{DecodedExfatEntry, decode_directory_cluster};
use teslausb_core::fs::exfat::directory::FileTimestamps;

use crate::error::ScannerError;
use crate::reader::BlockReader;
use crate::volume::Volume;

/// Maximum directory nesting depth before the walk gives up.
const MAX_DEPTH: u32 = 32;
/// Maximum total file records emitted in one walk.
const MAX_FILE_RECORDS: usize = 500_000;
/// Maximum directories visited in one walk.
const MAX_DIRECTORIES: usize = 100_000;

/// One live file discovered by the walk. All fields come straight from
/// the decoded exFAT File entry; no interpretation is applied here.
#[derive(Debug, Clone)]
pub struct FileRecord {
    /// MBR partition slot the file lives in (0-based).
    pub partition_slot: u8,
    /// Full `/`-joined path from the volume root (e.g.
    /// `TeslaCam/SavedClips/2026-06-01_20-10-53/...-front.mp4`).
    pub path: String,
    /// File name (last path component).
    pub name: String,
    /// First cluster of the file's data.
    pub first_cluster: u32,
    /// `DataLength` — total file size in bytes.
    pub data_length: u64,
    /// `ValidDataLength` — bytes actually written (`<= data_length`).
    /// The authoritative "fully written so far" signal: a mid-write
    /// file has `valid_data_length < data_length`.
    pub valid_data_length: u64,
    /// `true` if the extent is contiguous (`NoFatChain` flag set).
    pub no_fat_chain: bool,
    /// On-disk timestamps (create / modify / access).
    pub timestamps: FileTimestamps,
    /// `true` if the recomputed entry-set checksum matched on disk.
    pub set_checksum_ok: bool,
    /// First cluster of the directory that contains this entry — part
    /// of the file's stable identity across scans.
    pub dir_first_cluster: u32,
}

/// A directory pending traversal.
struct PendingDir {
    first_cluster: u32,
    no_fat_chain: bool,
    /// `Some(span)` for a subdirectory (declared cluster count);
    /// `None` for the root (follow the FAT to its end).
    contiguous_span: Option<u64>,
    path_prefix: String,
    depth: u32,
}

/// Walk the whole directory tree of `volume`, returning every live
/// file. `partition_slot` is stamped into each record's identity.
///
/// # Errors
///
/// Propagates reader / cluster / chain errors. A malformed *entry* is
/// skipped, not fatal; only structural read failures abort.
pub fn walk_volume<R: BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
    partition_slot: u8,
) -> Result<Vec<FileRecord>, ScannerError> {
    let root = volume.params().first_root_cluster;
    let mut records: Vec<FileRecord> = Vec::new();
    let mut visited_dirs: HashSet<u32> = HashSet::new();
    let mut dirs_seen: usize = 0;

    let mut stack: Vec<PendingDir> = vec![PendingDir {
        first_cluster: root,
        no_fat_chain: false,
        contiguous_span: None,
        path_prefix: String::new(),
        depth: 0,
    }];

    while let Some(dir) = stack.pop() {
        if !visited_dirs.insert(dir.first_cluster) {
            continue; // directory cycle — already walked this cluster
        }
        dirs_seen += 1;
        if dirs_seen > MAX_DIRECTORIES {
            return Err(ScannerError::LimitExceeded("directory count cap"));
        }

        let span = dir
            .contiguous_span
            .unwrap_or_else(|| u64::from(volume.params().cluster_count).saturating_add(1));
        let clusters = volume.follow_chain(dir.first_cluster, dir.no_fat_chain, span)?;
        let entries = read_directory_entries(volume, &clusters)?;

        for entry in entries {
            let DecodedExfatEntry::File {
                name,
                attributes,
                timestamps,
                first_cluster,
                valid_data_length,
                data_length,
                no_fat_chain,
                set_checksum_ok,
                ..
            } = entry
            else {
                continue; // bitmap/upcase/label/deleted/malformed: not indexed
            };

            let Some(name) = name else {
                continue; // undecodable name — cannot path it
            };

            let child_path = if dir.path_prefix.is_empty() {
                name.clone()
            } else {
                format!("{}/{}", dir.path_prefix, name)
            };

            if attributes.directory {
                if dir.depth + 1 >= MAX_DEPTH {
                    return Err(ScannerError::LimitExceeded("directory depth cap"));
                }
                if volume.params().is_valid_cluster(first_cluster) {
                    let sub_span = data_length.div_ceil(volume.params().bytes_per_cluster());
                    stack.push(PendingDir {
                        first_cluster,
                        no_fat_chain,
                        contiguous_span: Some(sub_span),
                        path_prefix: child_path,
                        depth: dir.depth + 1,
                    });
                }
                continue;
            }

            if records.len() >= MAX_FILE_RECORDS {
                return Err(ScannerError::LimitExceeded("file record cap"));
            }
            records.push(FileRecord {
                partition_slot,
                path: child_path,
                name,
                first_cluster,
                data_length,
                valid_data_length,
                no_fat_chain,
                timestamps,
                set_checksum_ok,
                dir_first_cluster: dir.first_cluster,
            });
        }
    }

    Ok(records)
}

/// Decode every entry of a directory given its cluster list, threading
/// the cross-cluster partial-entry carry and stopping at end-of-dir.
fn read_directory_entries<R: BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
    clusters: &[u32],
) -> Result<Vec<DecodedExfatEntry>, ScannerError> {
    let mut all = Vec::new();
    let mut carry = None;
    for &cluster in clusters {
        let bytes = volume.read_cluster(cluster)?;
        let result =
            decode_directory_cluster(&bytes, carry).map_err(|e| ScannerError::ChainError {
                first: cluster,
                reason: directory_decode_reason(&e),
            })?;
        all.extend(result.entries);
        if result.end_of_directory_seen {
            return Ok(all);
        }
        carry = result.trailing_partial_set;
    }
    Ok(all)
}

/// Map a directory-decode error to a static reason string (the only
/// variant is unaligned input, which cluster-sized reads never hit).
fn directory_decode_reason(
    _e: &teslausb_core::fs::exfat::dir_decode::ExfatDirDecodeError,
) -> &'static str {
    "directory cluster not 32-byte aligned"
}
