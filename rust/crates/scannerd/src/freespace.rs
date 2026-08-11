//! exFAT free-space via allocation-bitmap popcount. Pure over `BlockReader`;
//! host-testable. Fails closed on any structural anomaly — never guesses.

use teslausb_core::fs::exfat::dir_decode::DecodedExfatEntry;

use crate::error::ScannerError;
use crate::reader::BlockReader;
use crate::volume::Volume;
use crate::walk::read_directory_entries;

/// Hard ceiling on the allocation-bitmap size we will buffer while measuring
/// free space. The `TeslaCam` volume is 128 GiB; even at exFAT's smallest legal
/// 512-byte cluster that is a 32 MiB bitmap (`ceil(128 GiB / 512 / 8)`), and
/// real Tesla formatting (128 KiB clusters) yields a 128 KiB bitmap. A
/// `cluster_count` implying a larger bitmap cannot describe this device's
/// volumes, so we reject it fail-closed rather than allocate hundreds of MiB
/// (or OOM-abort) on a corrupt boot sector.
const MAX_BITMAP_BYTES: u64 = 64 * 1024 * 1024;

/// Exact free-space facts for one exFAT volume, derived from the allocation
/// bitmap. `stable` is `false` when repeated reads of a live (car-writing)
/// bitmap disagreed — the volume mutated mid-measurement, so the returned
/// counts are a best-effort snapshot rather than a quiescent truth.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VolumeStats {
    /// Total data clusters in the cluster heap.
    pub cluster_count: u32,
    /// Bytes per cluster (allocation unit).
    pub bytes_per_cluster: u64,
    /// Allocated clusters (popcount of the allocation bitmap).
    pub used_clusters: u32,
    /// Free clusters (`cluster_count - used_clusters`).
    pub free_clusters: u32,
    /// Total addressable bytes (`cluster_count * bytes_per_cluster`).
    pub total_bytes: u64,
    /// Allocated bytes (`used_clusters * bytes_per_cluster`).
    pub used_bytes: u64,
    /// Free bytes (`total_bytes - used_bytes`).
    pub free_bytes: u64,
    /// `true` only when repeated bitmap reads agreed (volume was quiescent).
    pub stable: bool,
}

/// Compute [`VolumeStats`] for `volume` by locating its allocation bitmap in
/// the root directory and counting allocated clusters.
///
/// # Errors
/// [`ScannerError::FreeSpace`] on a missing/duplicate bitmap, a `data_length`
/// smaller than the volume requires, an impossible popcount (used > count), or
/// a short bitmap chain; propagates reader/cluster/chain errors otherwise.
pub fn volume_free_space<R: BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
) -> Result<VolumeStats, ScannerError> {
    // Compute and bound the bitmap size FIRST, before walking any on-image
    // chains: a corrupt boot sector claiming a huge cluster_count must fail
    // closed here, before we allocate the root-directory or bitmap chains it
    // implies (follow_chain would otherwise grow toward its 8M-cluster cap).
    let cluster_count = volume.params().cluster_count;
    let bytes_per_cluster = volume.params().bytes_per_cluster();
    let needed_bytes = (u64::from(cluster_count).saturating_add(7)) / 8;
    if needed_bytes > MAX_BITMAP_BYTES {
        return Err(ScannerError::FreeSpace(
            "allocation bitmap exceeds supported maximum size",
        ));
    }

    let root = volume.params().first_root_cluster;
    let span = u64::from(volume.params().cluster_count).saturating_add(1);
    let clusters = volume.follow_chain(root, false, span)?;
    let entries = read_directory_entries(volume, &clusters)?;

    let mut bitmap: Option<(u32, u64)> = None;
    for entry in entries {
        let DecodedExfatEntry::AllocationBitmap {
            bitmap_index,
            first_cluster,
            data_length,
            ..
        } = entry
        else {
            continue;
        };
        if bitmap_index != 0 {
            continue;
        }
        if bitmap.is_some() {
            return Err(ScannerError::FreeSpace("duplicate allocation bitmap"));
        }
        bitmap = Some((first_cluster, data_length));
    }
    let Some((bitmap_first_cluster, bitmap_data_length)) = bitmap else {
        return Err(ScannerError::FreeSpace("allocation bitmap not found"));
    };

    if bitmap_data_length < needed_bytes {
        return Err(ScannerError::FreeSpace(
            "bitmap data_length too small for cluster_count",
        ));
    }

    let clusters_needed_u64 = needed_bytes.div_ceil(bytes_per_cluster.max(1));
    let clusters_needed = usize::try_from(clusters_needed_u64)
        .map_err(|_| ScannerError::FreeSpace("bitmap cluster count overflow"))?;
    let bitmap_chain = volume.follow_chain_bounded(bitmap_first_cluster, clusters_needed)?;
    if bitmap_chain.len() < clusters_needed {
        return Err(ScannerError::FreeSpace(
            "bitmap chain shorter than required",
        ));
    }

    let (used_clusters, stable) = stable_bitmap_popcount(
        volume,
        &bitmap_chain,
        cluster_count,
        needed_bytes,
        bytes_per_cluster,
        clusters_needed,
    )?;

    let free_clusters = cluster_count
        .checked_sub(used_clusters)
        .ok_or(ScannerError::FreeSpace("popcount exceeds cluster_count"))?;
    let total_bytes = u64::from(cluster_count)
        .checked_mul(bytes_per_cluster)
        .ok_or(ScannerError::FreeSpace("total_bytes overflow"))?;
    let used_bytes = u64::from(used_clusters)
        .checked_mul(bytes_per_cluster)
        .ok_or(ScannerError::FreeSpace("used_bytes overflow"))?;
    let free_bytes = total_bytes
        .checked_sub(used_bytes)
        .ok_or(ScannerError::FreeSpace("free_bytes underflow"))?;

    Ok(VolumeStats {
        cluster_count,
        bytes_per_cluster,
        used_clusters,
        free_clusters,
        total_bytes,
        used_bytes,
        free_bytes,
        stable,
    })
}

/// Materialise the masked allocation bitmap once (bounded by `MAX_BITMAP_BYTES`,
/// via a fallible reservation that fails closed instead of OOM-aborting), then
/// re-read it a second time and compare byte-for-byte. If the two passes agree
/// the volume was quiescent and the popcount is reported `stable`; if they
/// differ the live volume mutated under us, so a third count-only pass supplies
/// the freshest number reported `stable = false`.
fn stable_bitmap_popcount<R: BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
    bitmap_chain: &[u32],
    cluster_count: u32,
    needed_bytes: u64,
    bytes_per_cluster: u64,
    clusters_needed: usize,
) -> Result<(u32, bool), ScannerError> {
    let needed_usize = usize::try_from(needed_bytes)
        .map_err(|_| ScannerError::FreeSpace("bitmap size overflow"))?;
    let mut a_bytes: Vec<u8> = Vec::new();
    a_bytes
        .try_reserve_exact(needed_usize)
        .map_err(|_| ScannerError::FreeSpace("bitmap allocation failed"))?;
    let a_used = scan_bitmap(
        volume,
        bitmap_chain,
        cluster_count,
        needed_bytes,
        bytes_per_cluster,
        clusters_needed,
        |_, masked| a_bytes.push(masked),
    )?;

    let mut matches_a = true;
    let b_used = scan_bitmap(
        volume,
        bitmap_chain,
        cluster_count,
        needed_bytes,
        bytes_per_cluster,
        clusters_needed,
        |idx, masked| {
            if usize::try_from(idx)
                .ok()
                .and_then(|i| a_bytes.get(i))
                .copied()
                != Some(masked)
            {
                matches_a = false;
            }
        },
    )?;

    if matches_a && a_used == b_used {
        return Ok((a_used, true));
    }

    let c_used = scan_bitmap(
        volume,
        bitmap_chain,
        cluster_count,
        needed_bytes,
        bytes_per_cluster,
        clusters_needed,
        |_, _| {},
    )?;
    Ok((c_used, false))
}

/// Read the first `needed_bytes` of the allocation bitmap along `bitmap_chain`,
/// applying the final-byte mask for the `cluster_count % 8` tail bits, invoking
/// `on_byte(index, masked)` for each masked byte and returning the total
/// popcount (allocated clusters). Fails closed on a short read, a chain shorter
/// than required, or a popcount exceeding `cluster_count`.
fn scan_bitmap<R: BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
    bitmap_chain: &[u32],
    cluster_count: u32,
    needed_bytes: u64,
    bytes_per_cluster: u64,
    clusters_needed: usize,
    mut on_byte: impl FnMut(u64, u8),
) -> Result<u32, ScannerError> {
    let mut remaining = needed_bytes;
    let mut used_clusters: u64 = 0;
    let mut bytes_seen: u64 = 0;
    let tail_bits = cluster_count % 8;

    for &cluster in bitmap_chain.iter().take(clusters_needed) {
        if remaining == 0 {
            break;
        }
        let bytes = volume.read_cluster(cluster)?;
        let consume_u64 = remaining.min(bytes_per_cluster);
        let consume = usize::try_from(consume_u64)
            .map_err(|_| ScannerError::FreeSpace("bitmap read length overflow"))?;
        if bytes.len() < consume {
            return Err(ScannerError::FreeSpace("bitmap cluster short read"));
        }
        for byte in bytes.iter().take(consume) {
            let is_last_needed = bytes_seen.checked_add(1).is_some_and(|n| n == needed_bytes);
            let masked = if is_last_needed && tail_bits != 0 {
                *byte & ((1_u8 << tail_bits) - 1)
            } else {
                *byte
            };
            on_byte(bytes_seen, masked);
            used_clusters = used_clusters
                .checked_add(u64::from(masked.count_ones()))
                .ok_or(ScannerError::FreeSpace("popcount overflow"))?;
            if used_clusters > u64::from(cluster_count) {
                return Err(ScannerError::FreeSpace("popcount exceeds cluster_count"));
            }
            bytes_seen = bytes_seen
                .checked_add(1)
                .ok_or(ScannerError::FreeSpace("bitmap byte counter overflow"))?;
        }
        remaining = remaining.saturating_sub(consume_u64);
    }

    if remaining != 0 {
        return Err(ScannerError::FreeSpace(
            "bitmap chain shorter than required",
        ));
    }

    u32::try_from(used_clusters).map_err(|_| ScannerError::FreeSpace("used cluster count overflow"))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::indexing_slicing, clippy::panic)]
mod tests {
    use super::*;
    use crate::boot::ExfatParams;
    use crate::reader::{BlockReader, ReaderError, SliceReader};
    use std::collections::VecDeque;
    use std::sync::Mutex;

    const BPS: usize = 512;

    fn tiny_params(cluster_count: u32) -> ExfatParams {
        ExfatParams {
            partition_offset_sectors: 0,
            volume_length_sectors: 128,
            fat_offset_sectors: 1,
            fat_length_sectors: 1,
            cluster_heap_offset_sectors: 2,
            cluster_count,
            first_root_cluster: 2,
            volume_serial: 0,
            bytes_per_sector_shift: 9,
            sectors_per_cluster_shift: 0,
            number_of_fats: 1,
        }
    }

    fn cluster_offset(cluster: u32) -> usize {
        (2 + (cluster as usize - 2)) * BPS
    }

    fn build_image(
        cluster_count: u32,
        fat_entries: &[(u32, u32)],
        root_entries: &[([u8; 32], usize)],
        bitmap_clusters: &[(u32, &[u8])],
    ) -> (Vec<u8>, ExfatParams) {
        let params = tiny_params(cluster_count);
        let mut img = vec![0_u8; 128 * BPS];
        for &(cluster, value) in fat_entries {
            let off = BPS + (cluster as usize) * 4;
            img[off..off + 4].copy_from_slice(&value.to_le_bytes());
        }
        let root_off = cluster_offset(2);
        for &(entry, slot) in root_entries {
            let off = root_off + slot * 32;
            img[off..off + 32].copy_from_slice(&entry);
        }
        for &(cluster, bytes) in bitmap_clusters {
            let off = cluster_offset(cluster);
            img[off..off + bytes.len()].copy_from_slice(bytes);
        }
        (img, params)
    }

    fn bitmap_entry(bitmap_index: u8, first_cluster: u32, data_length: u64) -> [u8; 32] {
        let mut entry = [0_u8; 32];
        entry[0] = 0x81;
        entry[1] = bitmap_index;
        entry[0x14..0x18].copy_from_slice(&first_cluster.to_le_bytes());
        entry[0x18..0x20].copy_from_slice(&data_length.to_le_bytes());
        entry
    }

    #[test]
    fn happy_path_counts_allocated_clusters() {
        let entry = bitmap_entry(0, 3, 2);
        let (img, params) = build_image(
            16,
            &[(2, 0xFFFF_FFFF), (3, 0xFFFF_FFFF)],
            &[(entry, 0)],
            &[(3, &[0b0000_0101, 0b0000_0001])],
        );
        let reader = SliceReader::new(img);
        let volume = Volume::new(&reader, params);
        let stats = volume_free_space(&volume).unwrap();
        assert_eq!(stats.used_clusters, 3);
        assert_eq!(stats.free_clusters, 13);
        assert!(stats.stable);
        assert_eq!(stats.total_bytes, 16 * 512);
    }

    #[test]
    fn final_byte_mask_ignores_padding_bits() {
        let entry = bitmap_entry(0, 3, 2);
        let (img, params) = build_image(
            10,
            &[(2, 0xFFFF_FFFF), (3, 0xFFFF_FFFF)],
            &[(entry, 0)],
            &[(3, &[0, 0xFF])],
        );
        let reader = SliceReader::new(img);
        let volume = Volume::new(&reader, params);
        let stats = volume_free_space(&volume).unwrap();
        assert_eq!(stats.used_clusters, 2);
        assert_eq!(stats.free_clusters, 8);
    }

    #[test]
    fn fragmented_bitmap_chain_is_followed() {
        let entry = bitmap_entry(0, 5, 513);
        let mut first = vec![0_u8; 512];
        first[0] = 0b0000_0001;
        let second = vec![0b0000_0001];
        let (img, params) = build_image(
            4097,
            &[(2, 0xFFFF_FFFF), (5, 7), (7, 0xFFFF_FFFF)],
            &[(entry, 0)],
            &[(5, &first), (7, &second)],
        );
        let reader = SliceReader::new(img);
        let volume = Volume::new(&reader, params);
        let stats = volume_free_space(&volume).unwrap();
        assert_eq!(stats.used_clusters, 2);
        assert_eq!(stats.free_clusters, 4095);
    }

    #[test]
    fn missing_bitmap_is_error() {
        let (img, params) = build_image(16, &[(2, 0xFFFF_FFFF)], &[], &[]);
        let reader = SliceReader::new(img);
        let volume = Volume::new(&reader, params);
        let err = volume_free_space(&volume).unwrap_err();
        assert!(matches!(
            err,
            ScannerError::FreeSpace("allocation bitmap not found")
        ));
    }

    #[test]
    fn duplicate_bitmap_is_error() {
        let e0 = bitmap_entry(0, 3, 2);
        let e1 = bitmap_entry(0, 4, 2);
        let (img, params) = build_image(
            16,
            &[(2, 0xFFFF_FFFF), (3, 0xFFFF_FFFF), (4, 0xFFFF_FFFF)],
            &[(e0, 0), (e1, 1)],
            &[(3, &[0, 0]), (4, &[0, 0])],
        );
        let reader = SliceReader::new(img);
        let volume = Volume::new(&reader, params);
        let err = volume_free_space(&volume).unwrap_err();
        assert!(matches!(
            err,
            ScannerError::FreeSpace("duplicate allocation bitmap")
        ));
    }

    #[test]
    fn data_length_too_small_is_error() {
        let entry = bitmap_entry(0, 3, 1);
        let (img, params) = build_image(
            16,
            &[(2, 0xFFFF_FFFF), (3, 0xFFFF_FFFF)],
            &[(entry, 0)],
            &[(3, &[0, 0])],
        );
        let reader = SliceReader::new(img);
        let volume = Volume::new(&reader, params);
        let err = volume_free_space(&volume).unwrap_err();
        assert!(matches!(
            err,
            ScannerError::FreeSpace("bitmap data_length too small for cluster_count")
        ));
    }

    #[test]
    fn short_bitmap_chain_is_error() {
        let entry = bitmap_entry(0, 3, 513);
        let (img, params) = build_image(
            4097,
            &[(2, 0xFFFF_FFFF), (3, 0xFFFF_FFFF)],
            &[(entry, 0)],
            &[(3, &[0_u8])],
        );
        let reader = SliceReader::new(img);
        let volume = Volume::new(&reader, params);
        let err = volume_free_space(&volume).unwrap_err();
        assert!(matches!(
            err,
            ScannerError::FreeSpace("bitmap chain shorter than required")
        ));
    }

    #[test]
    fn live_mutation_marks_stats_unstable() {
        let entry = bitmap_entry(0, 3, 1);
        let (img, params) = build_image(
            8,
            &[(2, 0xFFFF_FFFF), (3, 0xFFFF_FFFF)],
            &[(entry, 0)],
            &[(3, &[0])],
        );
        let root_reads = vec![vec![0b0000_0001], vec![0b0000_0011], vec![0b0000_0101]];
        let reader = FlakyBitmapReader::new(img, 3, root_reads);
        let volume = Volume::new(&reader, params);
        let stats = volume_free_space(&volume).unwrap();
        assert!(!stats.stable);
        assert_eq!(stats.used_clusters, 2);
    }

    #[test]
    fn same_popcount_different_content_marks_unstable() {
        let entry = bitmap_entry(0, 3, 1);
        let (img, params) = build_image(
            8,
            &[(2, 0xFFFF_FFFF), (3, 0xFFFF_FFFF)],
            &[(entry, 0)],
            &[(3, &[0])],
        );
        let root_reads = vec![vec![0b0000_0011], vec![0b0000_0101], vec![0b0000_0101]];
        let reader = FlakyBitmapReader::new(img, 3, root_reads);
        let volume = Volume::new(&reader, params);
        let stats = volume_free_space(&volume).unwrap();
        assert!(!stats.stable);
        assert_eq!(stats.used_clusters, 2);
    }

    #[test]
    fn two_reads_same_bytes_marks_stable() {
        let entry = bitmap_entry(0, 3, 1);
        let (img, params) = build_image(
            8,
            &[(2, 0xFFFF_FFFF), (3, 0xFFFF_FFFF)],
            &[(entry, 0)],
            &[(3, &[0])],
        );
        let root_reads = vec![vec![0b0000_0101], vec![0b0000_0101]];
        let reader = FlakyBitmapReader::new(img, 3, root_reads);
        let volume = Volume::new(&reader, params);
        let stats = volume_free_space(&volume).unwrap();
        assert!(stats.stable);
        assert_eq!(stats.used_clusters, 2);
    }

    #[test]
    fn same_popcount_reordered_bytes_marks_unstable() {
        let entry = bitmap_entry(0, 3, 2);
        let (img, params) = build_image(
            16,
            &[(2, 0xFFFF_FFFF), (3, 0xFFFF_FFFF)],
            &[(entry, 0)],
            &[(3, &[0, 0])],
        );
        let root_reads = vec![
            vec![0b0000_0011, 0b0000_0101],
            vec![0b0000_0101, 0b0000_0011],
            vec![0b0000_0101, 0b0000_0011],
        ];
        let reader = FlakyBitmapReader::new(img, 3, root_reads);
        let volume = Volume::new(&reader, params);
        let stats = volume_free_space(&volume).unwrap();
        assert!(!stats.stable);
        assert_eq!(stats.used_clusters, 4);
    }

    #[test]
    fn oversized_bitmap_is_rejected_without_allocating() {
        // A corrupt boot sector claiming a huge cluster_count implies a bitmap
        // (~71 MiB here) far larger than any real volume needs. It must fail
        // closed at the size guard, never attempting the allocation or read.
        let entry = bitmap_entry(0, 3, 100_000_000);
        let (img, params) = build_image(
            600_000_000,
            &[(2, 0xFFFF_FFFF), (3, 0xFFFF_FFFF)],
            &[(entry, 0)],
            &[(3, &[0, 0])],
        );
        let reader = SliceReader::new(img);
        let volume = Volume::new(&reader, params);
        let err = volume_free_space(&volume).unwrap_err();
        assert!(matches!(
            err,
            ScannerError::FreeSpace("allocation bitmap exceeds supported maximum size")
        ));
    }

    struct FlakyBitmapReader {
        image: Vec<u8>,
        cluster: u32,
        reads: Mutex<VecDeque<Vec<u8>>>,
    }

    impl FlakyBitmapReader {
        fn new(image: Vec<u8>, cluster: u32, reads: Vec<Vec<u8>>) -> Self {
            Self {
                image,
                cluster,
                reads: Mutex::new(reads.into()),
            }
        }
    }

    impl BlockReader for FlakyBitmapReader {
        fn size_bytes(&self) -> u64 {
            u64::try_from(self.image.len()).unwrap_or(u64::MAX)
        }

        fn read_exact_at(&self, offset: u64, buf: &mut [u8]) -> Result<(), ReaderError> {
            let start = usize::try_from(offset).map_err(|_| ReaderError::OutOfRange {
                offset,
                len: buf.len(),
                size: self.size_bytes(),
            })?;
            let end = start
                .checked_add(buf.len())
                .ok_or(ReaderError::OutOfRange {
                    offset,
                    len: buf.len(),
                    size: self.size_bytes(),
                })?;
            if end > self.image.len() {
                return Err(ReaderError::OutOfRange {
                    offset,
                    len: buf.len(),
                    size: self.size_bytes(),
                });
            }
            buf.copy_from_slice(&self.image[start..end]);
            let target = cluster_offset(self.cluster);
            if start == target && buf.len() == BPS {
                let mut reads = self.reads.lock().map_err(|_| ReaderError::Io {
                    offset,
                    len: buf.len(),
                    source_msg: "bitmap read queue poisoned".to_owned(),
                })?;
                if let Some(next) = reads.pop_front() {
                    let n = next.len().min(buf.len());
                    if n > 0 {
                        buf[..n].copy_from_slice(&next[..n]);
                    }
                }
            }
            Ok(())
        }
    }
}
