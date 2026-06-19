//! exFAT volume reader: cluster addressing, FAT-chain following, and
//! cluster reads on top of a [`BlockReader`] and parsed [`ExfatParams`].
//!
//! Every traversal that can be driven by torn or adversarial on-disk
//! data uses checked arithmetic, validates cluster numbers against
//! `2..=cluster_count+1`, detects FAT cycles, and bounds chain length
//! by a hard cap so a corrupt FAT can never loop or OOM the reader.
#![allow(clippy::doc_markdown)] // exFAT field names are not Rust paths

use std::collections::HashSet;

use crate::boot::ExfatParams;
use crate::error::ScannerError;
use crate::reader::BlockReader;

/// exFAT FAT sentinel: end of cluster chain.
const FAT_END_OF_CHAIN: u32 = 0xFFFF_FFFF;
/// exFAT FAT sentinel: bad cluster.
const FAT_BAD_CLUSTER: u32 = 0xFFFF_FFF7;
/// Hard cap on clusters followed for a single chain, independent of the
/// declared volume size — a final backstop against a pathological FAT.
const HARD_MAX_CHAIN_CLUSTERS: u64 = 8_000_000;

/// One decoded FAT entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FatEntry {
    /// Points to the next cluster in the chain (already range-checked).
    Next(u32),
    /// End-of-chain marker.
    EndOfChain,
    /// Free / bad / reserved / out-of-range — invalid inside a chain.
    Invalid,
}

/// A readable exFAT volume: a [`BlockReader`] plus parsed geometry.
#[derive(Debug)]
pub struct Volume<'r, R: BlockReader + ?Sized> {
    reader: &'r R,
    params: ExfatParams,
}

impl<'r, R: BlockReader + ?Sized> Volume<'r, R> {
    /// Wrap `reader` with already-parsed `params`.
    #[must_use]
    pub fn new(reader: &'r R, params: ExfatParams) -> Self {
        Self { reader, params }
    }

    /// The volume geometry.
    #[must_use]
    pub fn params(&self) -> &ExfatParams {
        &self.params
    }

    /// Read the full contents of `cluster` (`bytes_per_cluster` bytes).
    ///
    /// # Errors
    ///
    /// [`ScannerError::InvalidCluster`] if out of range / overflow, or
    /// [`ScannerError::Reader`] if the read fails.
    pub fn read_cluster(&self, cluster: u32) -> Result<Vec<u8>, ScannerError> {
        let offset = self.params.cluster_byte_offset(cluster)?;
        let len = usize::try_from(self.params.bytes_per_cluster()).map_err(|_| {
            ScannerError::InvalidCluster {
                cluster,
                reason: "cluster size exceeds usize",
            }
        })?;
        Ok(self.reader.read_vec_at(offset, len)?)
    }

    /// Read `len` bytes of file data starting at byte `start_in_file`,
    /// following the cluster chain `clusters` (in order). Used for
    /// bounded MP4 header/tail reads and SEI extraction — never slurps
    /// more than `len` bytes.
    ///
    /// # Errors
    ///
    /// Propagates read / cluster errors; returns fewer bytes only if
    /// the chain is shorter than the requested range (caller decides
    /// whether that is "torn").
    pub fn read_file_range(
        &self,
        clusters: &[u32],
        start_in_file: u64,
        len: usize,
    ) -> Result<Vec<u8>, ScannerError> {
        let bpc = self.params.bytes_per_cluster();
        let mut out = Vec::with_capacity(len);
        let mut pos = start_in_file;
        let end = start_in_file.saturating_add(len as u64);
        while pos < end {
            let cluster_index = pos / bpc;
            let within = pos % bpc;
            let Ok(idx) = usize::try_from(cluster_index) else {
                break;
            };
            let Some(&cluster) = clusters.get(idx) else {
                break;
            };
            let base = self.params.cluster_byte_offset(cluster)?;
            let take = (bpc - within).min(end - pos);
            let take_usize = usize::try_from(take).map_err(|_| ScannerError::InvalidCluster {
                cluster,
                reason: "read length exceeds usize",
            })?;
            let chunk = self.reader.read_vec_at(base + within, take_usize)?;
            out.extend_from_slice(&chunk);
            pos = pos.saturating_add(take);
        }
        Ok(out)
    }

    /// Read a bounded window from a file chain without resolving the entire file.
    ///
    /// `readable_size` is the authoritative readable ceiling (`min(VDL, DataLength)`).
    /// The resolved cluster working set is bounded to the clusters touched by
    /// `[start_in_file, start_in_file + len)`.
    ///
    /// # Errors
    ///
    /// Returns the same structural errors as [`Volume::follow_chain`] /
    /// [`Volume::read_file_range`] when the on-disk chain is malformed.
    pub fn read_file_window(
        &self,
        first_cluster: u32,
        no_fat_chain: bool,
        readable_size: u64,
        start_in_file: u64,
        len: usize,
    ) -> Result<Vec<u8>, ScannerError> {
        if len == 0 || start_in_file >= readable_size {
            return Ok(Vec::new());
        }
        if !self.params.is_valid_cluster(first_cluster) {
            return Err(ScannerError::InvalidCluster {
                cluster: first_cluster,
                reason: "chain start out of range",
            });
        }

        let bpc = self.params.bytes_per_cluster().max(1);
        let end = start_in_file
            .saturating_add(len as u64)
            .min(readable_size);
        let window_len = end.saturating_sub(start_in_file);
        if window_len == 0 {
            return Ok(Vec::new());
        }

        let readable_clusters = readable_size.div_ceil(bpc);
        let start_cluster_index = start_in_file / bpc;
        let end_cluster_index = (end - 1) / bpc;
        let clusters_needed = end_cluster_index
            .saturating_sub(start_cluster_index)
            .saturating_add(1);

        if end_cluster_index >= readable_clusters {
            return Err(ScannerError::ChainError {
                first: first_cluster,
                reason: "requested range exceeds readable-size cluster span",
            });
        }

        let clusters = if no_fat_chain {
            self.contiguous_chain_window(first_cluster, start_cluster_index, clusters_needed)?
        } else {
            self.fat_chain_window(
                first_cluster,
                start_cluster_index,
                clusters_needed,
                readable_clusters,
            )?
        };

        let start_within_window = start_in_file % bpc;
        self.read_file_range(
            &clusters,
            start_within_window,
            usize::try_from(window_len).map_err(|_| ScannerError::ChainError {
                first: first_cluster,
                reason: "window length exceeds usize",
            })?,
        )
    }

    /// Decode the FAT entry for `cluster`.
    fn fat_entry(&self, cluster: u32) -> Result<FatEntry, ScannerError> {
        let offset = self.params.fat_entry_byte_offset(cluster)?;
        let raw = self.reader.read_vec_at(offset, 4)?;
        let arr: [u8; 4] = raw
            .as_slice()
            .try_into()
            .map_err(|_| ScannerError::ChainError {
                first: cluster,
                reason: "short FAT entry",
            })?;
        let value = u32::from_le_bytes(arr);
        Ok(if value == FAT_END_OF_CHAIN {
            FatEntry::EndOfChain
        } else if value == FAT_BAD_CLUSTER || !self.params.is_valid_cluster(value) {
            FatEntry::Invalid
        } else {
            FatEntry::Next(value)
        })
    }

    /// Follow the cluster chain starting at `first`.
    ///
    /// * `no_fat_chain == true`: the extent is `contiguous_span`
    ///   contiguous clusters (the exFAT `NoFatChain` flag); the FAT is
    ///   NOT read.
    /// * `no_fat_chain == false`: walk the FAT to its end-of-chain,
    ///   bounded by the cluster count and the hard cap, rejecting
    ///   cycles, bad/free clusters, and out-of-range pointers.
    ///
    /// `contiguous_span` is the declared cluster count
    /// (`ceil(data_length / bytes_per_cluster)`); for FAT-chained files
    /// it is ignored (the FAT is authoritative) but the caller can
    /// compare the returned length against it.
    ///
    /// # Errors
    ///
    /// [`ScannerError::InvalidCluster`] / [`ScannerError::ChainError`]
    /// on any range, cycle, or sentinel violation.
    pub fn follow_chain(
        &self,
        first: u32,
        no_fat_chain: bool,
        contiguous_span: u64,
    ) -> Result<Vec<u32>, ScannerError> {
        if !self.params.is_valid_cluster(first) {
            return Err(ScannerError::InvalidCluster {
                cluster: first,
                reason: "chain start out of range",
            });
        }

        let cap = u64::from(self.params.cluster_count)
            .saturating_add(1)
            .min(HARD_MAX_CHAIN_CLUSTERS);

        if no_fat_chain {
            return self.contiguous_chain(first, contiguous_span.min(cap));
        }

        let mut chain = Vec::new();
        let mut visited: HashSet<u32> = HashSet::new();
        let mut cur = first;
        loop {
            if chain.len() as u64 >= cap {
                return Err(ScannerError::ChainError {
                    first,
                    reason: "chain exceeds cluster-count cap (unterminated/cyclic)",
                });
            }
            if !visited.insert(cur) {
                return Err(ScannerError::ChainError {
                    first,
                    reason: "FAT cycle detected",
                });
            }
            chain.push(cur);
            match self.fat_entry(cur)? {
                FatEntry::EndOfChain => break,
                FatEntry::Next(next) => cur = next,
                FatEntry::Invalid => {
                    return Err(ScannerError::ChainError {
                        first,
                        reason: "free/bad/out-of-range cluster mid-chain",
                    });
                }
            }
        }
        Ok(chain)
    }

    /// Produce `span` contiguous clusters starting at `first`,
    /// validating each is in range.
    fn contiguous_chain(&self, first: u32, span: u64) -> Result<Vec<u32>, ScannerError> {
        let span_usize = usize::try_from(span).map_err(|_| ScannerError::ChainError {
            first,
            reason: "contiguous span exceeds usize",
        })?;
        let mut chain = Vec::with_capacity(span_usize.min(1024));
        for i in 0..span {
            let i32_off = u32::try_from(i).map_err(|_| ScannerError::ChainError {
                first,
                reason: "contiguous span exceeds u32",
            })?;
            let cluster = first.checked_add(i32_off).ok_or(ScannerError::ChainError {
                first,
                reason: "contiguous cluster overflow",
            })?;
            if !self.params.is_valid_cluster(cluster) {
                return Err(ScannerError::ChainError {
                    first,
                    reason: "contiguous extent runs past cluster heap",
                });
            }
            chain.push(cluster);
        }
        Ok(chain)
    }

    fn contiguous_chain_window(
        &self,
        first: u32,
        start_cluster_index: u64,
        clusters_needed: u64,
    ) -> Result<Vec<u32>, ScannerError> {
        let window_start = first
            .checked_add(u32::try_from(start_cluster_index).map_err(|_| ScannerError::ChainError {
                first,
                reason: "window start exceeds u32",
            })?)
            .ok_or(ScannerError::ChainError {
                first,
                reason: "contiguous window start overflow",
            })?;
        self.contiguous_chain(window_start, clusters_needed)
    }

    fn fat_chain_window(
        &self,
        first: u32,
        start_cluster_index: u64,
        clusters_needed: u64,
        readable_clusters: u64,
    ) -> Result<Vec<u32>, ScannerError> {
        if start_cluster_index.saturating_add(clusters_needed) > readable_clusters {
            return Err(ScannerError::ChainError {
                first,
                reason: "requested range exceeds readable chain span",
            });
        }

        let cap = readable_clusters
            .min(u64::from(self.params.cluster_count).saturating_add(1))
            .min(HARD_MAX_CHAIN_CLUSTERS);
        let mut visited: HashSet<u32> = HashSet::new();
        let mut cur = first;
        let mut index = 0_u64;
        let mut out = Vec::with_capacity(usize::try_from(clusters_needed).unwrap_or(0).min(1024));
        let target_end = start_cluster_index.saturating_add(clusters_needed);

        while index < target_end {
            if index >= cap {
                return Err(ScannerError::ChainError {
                    first,
                    reason: "chain exceeds cluster-count cap (unterminated/cyclic)",
                });
            }
            if !visited.insert(cur) {
                return Err(ScannerError::ChainError {
                    first,
                    reason: "FAT cycle detected",
                });
            }
            if index >= start_cluster_index {
                out.push(cur);
            }
            index = index.saturating_add(1);
            if index >= target_end {
                break;
            }
            cur = match self.fat_entry(cur)? {
                FatEntry::EndOfChain => {
                    return Err(ScannerError::ChainError {
                        first,
                        reason: "chain ended before requested window",
                    });
                }
                FatEntry::Next(next) => next,
                FatEntry::Invalid => {
                    return Err(ScannerError::ChainError {
                        first,
                        reason: "free/bad/out-of-range cluster mid-chain",
                    });
                }
            };
        }

        Ok(out)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::indexing_slicing, clippy::panic)]
mod tests {
    use super::*;
    use crate::reader::SliceReader;

    /// Build a tiny exFAT volume image (no MBR) with a controllable
    /// FAT, returning the image bytes and params. Layout (sectors,
    /// 512B each, partition_offset 0):
    ///   fat_offset = 1, fat_length = 1, cluster_heap_offset = 2,
    ///   cluster_count = 16, bytes_per_sector_shift = 9,
    ///   sectors_per_cluster_shift = 0 (512B clusters).
    fn tiny_volume(fat: &[(u32, u32)]) -> (Vec<u8>, ExfatParams) {
        let params = ExfatParams {
            partition_offset_sectors: 0,
            volume_length_sectors: 64,
            fat_offset_sectors: 1,
            fat_length_sectors: 1,
            cluster_heap_offset_sectors: 2,
            cluster_count: 16,
            first_root_cluster: 2,
            volume_serial: 0,
            bytes_per_sector_shift: 9,
            sectors_per_cluster_shift: 0,
            number_of_fats: 1,
        };
        // Image: enough for heap (cluster index up to ~18).
        let mut img = vec![0_u8; 64 * 512];
        // Write FAT entries at fat_offset (sector 1 => byte 512).
        for &(cluster, value) in fat {
            let off = 512 + (cluster as usize) * 4;
            img[off..off + 4].copy_from_slice(&value.to_le_bytes());
        }
        (img, params)
    }

    #[test]
    fn follows_simple_fat_chain() {
        // 2 -> 3 -> 5 -> EOC
        let (img, params) = tiny_volume(&[(2, 3), (3, 5), (5, FAT_END_OF_CHAIN)]);
        let reader = SliceReader::new(img);
        let vol = Volume::new(&reader, params);
        let chain = vol.follow_chain(2, false, 3).unwrap();
        assert_eq!(chain, vec![2, 3, 5]);
    }

    #[test]
    fn rejects_fat_cycle() {
        // 2 -> 3 -> 2 (cycle)
        let (img, params) = tiny_volume(&[(2, 3), (3, 2)]);
        let reader = SliceReader::new(img);
        let vol = Volume::new(&reader, params);
        let err = vol.follow_chain(2, false, 8).unwrap_err();
        assert!(matches!(err, ScannerError::ChainError { reason, .. } if reason.contains("cycle")));
    }

    #[test]
    fn rejects_out_of_range_next() {
        // 2 -> 999 (out of range)
        let (img, params) = tiny_volume(&[(2, 999)]);
        let reader = SliceReader::new(img);
        let vol = Volume::new(&reader, params);
        assert!(vol.follow_chain(2, false, 8).is_err());
    }

    #[test]
    fn contiguous_no_fat_chain() {
        let (img, params) = tiny_volume(&[]);
        let reader = SliceReader::new(img);
        let vol = Volume::new(&reader, params);
        let chain = vol.follow_chain(4, true, 3).unwrap();
        assert_eq!(chain, vec![4, 5, 6]);
    }

    #[test]
    fn contiguous_runs_past_heap_is_rejected() {
        let (img, params) = tiny_volume(&[]);
        let reader = SliceReader::new(img);
        let vol = Volume::new(&reader, params);
        // max valid cluster = cluster_count + 1 = 17; span from 16 of 5
        // would reach 20 -> rejected.
        assert!(vol.follow_chain(16, true, 5).is_err());
    }
}
