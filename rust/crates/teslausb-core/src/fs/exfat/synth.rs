//! `exFAT` read dispatcher (Phase 2.11).
//!
//! [`ExfatSynth`] is the `exFAT` parallel of
//! [`crate::fs::fat32::synth::Fat32Synth`]. Given a byte offset
//! and a caller-supplied `&mut [u8]` to fill,
//! [`ExfatSynth::read`] dispatches each contiguous chunk of the
//! request to the appropriate region synthesizer:
//!
//! | [`RegionKind`]              | Source                                          |
//! |-----------------------------|-------------------------------------------------|
//! | `ExfatMainBootRegion`       | the pre-computed 6144-byte main boot region     |
//! | `ExfatBackupBootRegion`     | the same 6144 bytes (spec §3.2 normative)       |
//! | `FatTable { .. }`           | on-the-fly FAT sector synthesis (special chains)|
//! | `Data`                      | cluster-routed (bitmap / upcase / root / zero)  |
//! | `Reserved`                  | zero-fill (excess tail of an oversized volume)  |
//!
//! Region lookup is delegated to
//! [`ExfatGeometry::region_at`] (Phase 2.8). The dispatcher loops,
//! asks the geometry which region holds the current cursor,
//! copies as many bytes as fit in that region, advances, and
//! repeats until the request is drained.
//!
//! ## Cluster layout
//!
//! [`ExfatSynth::new`] picks the following deterministic layout:
//!
//! | Cluster index            | Owner                |
//! |--------------------------|----------------------|
//! | 2                        | Root directory       |
//! | 3..3+B                   | Allocation Bitmap    |
//! | 3+B..3+B+U               | `UpCase` Table         |
//! | 3+B+U..end               | Free (zero-filled)   |
//!
//! …where `B = bitmap.size_clusters(geometry)` and
//! `U = ceil(131_072 / bytes_per_cluster)`.
//!
//! ## What this module does NOT do
//!
//! * It does not allocate per-read. Construction reserves the
//!   12-sector boot region, the `AllocationBitmap` `Vec<u8>`, the
//!   `UpcaseTable` `Vec<u8>` (`131_072` bytes), and one
//!   `bytes_per_cluster`-sized root directory `Vec<u8>`.
//!   [`ExfatSynth::read`] takes a caller-supplied buffer and
//!   never allocates.
//! * It does not yet synthesize user files — every cluster outside
//!   the root / bitmap / upcase ranges reads back as zeros. The
//!   lazy file loader is Phase 2.13.
//! * It does not implement write. Writes will be gated at the
//!   transmission layer (Phase 3) and the dispatcher will grow a
//!   parallel `write` method then.

use core::fmt;

use super::allocation_bitmap::{AllocationBitmap, AllocationBitmapError};
use super::boot_sector::{self, BOOT_REGION_SIZE_BYTES, BootSectorError};
use super::directory::{self, DirectoryError, RootDirectoryParams};
use super::geometry::{ExfatGeometry, FIRST_CLUSTER_NUMBER};
use super::layout::ExfatLayout;
use super::upcase_table::{UPCASE_TABLE_SIZE_BYTES, UpcaseTable};
use crate::fs::data_cluster_source::DataClusterSource;
use crate::fs::geometry::{Geometry, Region, RegionKind, SECTOR_SIZE_BYTES};

/// Materialised `exFAT` synthesizer: pre-computed boot region,
/// built allocation bitmap, upcase table, and root directory
/// cluster, ready to serve byte-range reads via [`Self::read`].
#[derive(Debug)]
pub struct ExfatSynth {
    geometry: ExfatGeometry,
    boot_region: [u8; BOOT_REGION_SIZE_BYTES],
    bitmap: AllocationBitmap,
    upcase: UpcaseTable,
    root_directory: Vec<u8>,
    /// First cluster of the allocation bitmap stream.
    bitmap_first_cluster: u32,
    /// Number of clusters occupied by the bitmap stream.
    bitmap_cluster_count: u32,
    /// First cluster of the upcase table stream.
    upcase_first_cluster: u32,
    /// Number of clusters occupied by the upcase table stream.
    upcase_cluster_count: u32,
    /// Optional source for cluster-heap bytes outside the
    /// root/bitmap/upcase ranges. Installed via
    /// [`Self::with_layout`]; unset by default so the data
    /// region zero-fills as Phase 2.11 documented.
    data_source: Option<Box<dyn DataClusterSource + Send + Sync>>,
}

/// Errors returned by [`ExfatSynth::new`] and [`ExfatSynth::read`].
#[derive(Debug, PartialEq, Eq)]
pub enum ExfatSynthError {
    /// The boot region synthesizer rejected the geometry.
    BootRegion(BootSectorError),
    /// The allocation bitmap rejected one of the marking calls
    /// the constructor makes (would only happen if the geometry
    /// reports impossibly few clusters to hold the metadata).
    Bitmap(AllocationBitmapError),
    /// The directory synthesizer rejected the root cluster size
    /// or volume label.
    Directory(DirectoryError),
    /// The geometry reports a cluster heap too small to hold the
    /// mandatory bitmap + upcase + root directory clusters.
    ClusterHeapTooSmall {
        /// Total clusters the constructor needs.
        needed_clusters: u32,
        /// Clusters the geometry actually provides.
        available_clusters: u32,
    },
    /// `offset` is at or beyond the geometry's volume size.
    OffsetBeyondVolume {
        /// The caller's offset.
        offset: u64,
        /// The geometry's volume size.
        volume_size: u64,
    },
    /// `offset + length` exceeds the geometry's volume size.
    LengthExceedsVolume {
        /// The caller's offset.
        offset: u64,
        /// The caller's buffer length in bytes.
        length: u64,
        /// The geometry's volume size.
        volume_size: u64,
    },
    /// The geometry returned a [`RegionKind`] that is not part of
    /// the `exFAT` on-disk layout (for example, a FAT32 boot
    /// sector). A correctly-constructed [`ExfatGeometry`] never
    /// produces such regions; this variant exists as
    /// defense-in-depth.
    UnsupportedRegion {
        /// The offending region kind.
        kind: RegionKind,
    },
    /// [`ExfatSynth::with_layout`] received a layout planned
    /// against a different `bytes_per_cluster` than this synth's
    /// geometry. The layout and synth must come from the same
    /// geometry.
    LayoutMismatch {
        /// The synth's `bytes_per_cluster`.
        synth_bytes_per_cluster: u32,
        /// The layout's `bytes_per_cluster`.
        layout_bytes_per_cluster: u32,
    },
}

impl fmt::Display for ExfatSynthError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BootRegion(err) => write!(f, "exFAT boot region synthesis failed: {err}"),
            Self::Bitmap(err) => write!(f, "exFAT allocation bitmap construction failed: {err}"),
            Self::Directory(err) => write!(f, "exFAT root directory synthesis failed: {err}"),
            Self::ClusterHeapTooSmall {
                needed_clusters,
                available_clusters,
            } => write!(
                f,
                "exFAT cluster heap holds {available_clusters} clusters but {needed_clusters} are \
                 needed for the bitmap, upcase table, and root directory",
            ),
            Self::OffsetBeyondVolume {
                offset,
                volume_size,
            } => write!(
                f,
                "read offset {offset} is at or beyond the volume size {volume_size}",
            ),
            Self::LengthExceedsVolume {
                offset,
                length,
                volume_size,
            } => write!(
                f,
                "read of {length} bytes at offset {offset} extends past the volume size {volume_size}",
            ),
            Self::UnsupportedRegion { kind } => {
                write!(f, "exFAT synth received an unsupported region kind: {kind}")
            }
            Self::LayoutMismatch {
                synth_bytes_per_cluster,
                layout_bytes_per_cluster,
            } => write!(
                f,
                "exFAT layout was planned for {layout_bytes_per_cluster} bytes per cluster but \
                 this synth's geometry uses {synth_bytes_per_cluster}",
            ),
        }
    }
}

impl core::error::Error for ExfatSynthError {
    fn source(&self) -> Option<&(dyn core::error::Error + 'static)> {
        match self {
            Self::BootRegion(err) => Some(err),
            Self::Bitmap(err) => Some(err),
            Self::Directory(err) => Some(err),
            Self::ClusterHeapTooSmall { .. }
            | Self::OffsetBeyondVolume { .. }
            | Self::LengthExceedsVolume { .. }
            | Self::UnsupportedRegion { .. }
            | Self::LayoutMismatch { .. } => None,
        }
    }
}

impl ExfatSynth {
    /// Build an `ExfatSynth` from a geometry, a 32-bit volume
    /// serial, and an optional UTF-16 volume label (up to 11
    /// code units).
    ///
    /// The constructor:
    ///
    /// 1. Synthesizes the 12-sector main boot region.
    /// 2. Builds the upcase table (always the uncompressed
    ///    ASCII-fold + identity table from Phase 2.9).
    /// 3. Picks the cluster layout: root @ cluster 2, bitmap
    ///    starting at cluster 3, upcase starting after bitmap.
    /// 4. Builds the allocation bitmap and marks the root +
    ///    bitmap + upcase clusters as allocated.
    /// 5. Synthesizes the root directory cluster.
    ///
    /// # Errors
    ///
    /// * [`ExfatSynthError::BootRegion`] if boot synthesis fails.
    /// * [`ExfatSynthError::Bitmap`] if a mark call fails (would
    ///   indicate a geometry mismatch).
    /// * [`ExfatSynthError::Directory`] if the root cluster is
    ///   too small or the label is too long.
    /// * [`ExfatSynthError::ClusterHeapTooSmall`] if the
    ///   geometry's cluster heap is too small to hold the
    ///   bitmap, upcase table, and root directory.
    pub fn new(
        geometry: ExfatGeometry,
        volume_serial: u32,
        volume_label_utf16: &[u16],
    ) -> Result<Self, ExfatSynthError> {
        let boot_region = boot_sector::synthesize(&geometry, volume_serial)
            .map_err(ExfatSynthError::BootRegion)?;
        let upcase = UpcaseTable::ascii_identity();
        let bytes_per_cluster = u64::from(geometry.bytes_per_cluster());

        let mut bitmap = AllocationBitmap::new_empty(&geometry);
        let bitmap_cluster_count = bitmap.size_clusters(&geometry);
        let upcase_size_bytes = u64::try_from(UPCASE_TABLE_SIZE_BYTES).unwrap_or(u64::MAX);
        let upcase_cluster_count =
            u32::try_from(upcase_size_bytes.div_ceil(bytes_per_cluster.max(1))).unwrap_or(u32::MAX);

        let root_cluster = geometry.first_root_directory_cluster();
        let bitmap_first_cluster =
            root_cluster
                .checked_add(1)
                .ok_or(ExfatSynthError::ClusterHeapTooSmall {
                    needed_clusters: u32::MAX,
                    available_clusters: geometry.cluster_count(),
                })?;
        let upcase_first_cluster = bitmap_first_cluster
            .checked_add(bitmap_cluster_count)
            .ok_or(ExfatSynthError::ClusterHeapTooSmall {
                needed_clusters: u32::MAX,
                available_clusters: geometry.cluster_count(),
            })?;
        let last_metadata_cluster = upcase_first_cluster
            .checked_add(upcase_cluster_count)
            .ok_or(ExfatSynthError::ClusterHeapTooSmall {
                needed_clusters: u32::MAX,
                available_clusters: geometry.cluster_count(),
            })?
            .saturating_sub(1);
        let cluster_end_exclusive = FIRST_CLUSTER_NUMBER.saturating_add(geometry.cluster_count());
        if last_metadata_cluster >= cluster_end_exclusive {
            let needed_clusters = last_metadata_cluster
                .saturating_sub(FIRST_CLUSTER_NUMBER)
                .saturating_add(1);
            return Err(ExfatSynthError::ClusterHeapTooSmall {
                needed_clusters,
                available_clusters: geometry.cluster_count(),
            });
        }

        bitmap
            .mark_allocated(root_cluster)
            .map_err(ExfatSynthError::Bitmap)?;
        bitmap
            .mark_range_allocated(bitmap_first_cluster, bitmap_cluster_count)
            .map_err(ExfatSynthError::Bitmap)?;
        bitmap
            .mark_range_allocated(upcase_first_cluster, upcase_cluster_count)
            .map_err(ExfatSynthError::Bitmap)?;

        let root_directory = directory::synthesize_root_directory(
            &geometry,
            &RootDirectoryParams {
                bitmap_first_cluster,
                bitmap_size_bytes: bitmap.size_bytes(),
                upcase_first_cluster,
                upcase_size_bytes,
                upcase_checksum: upcase.checksum(),
                volume_label_utf16,
            },
        )
        .map_err(ExfatSynthError::Directory)?;

        Ok(Self {
            geometry,
            boot_region,
            bitmap,
            upcase,
            root_directory,
            bitmap_first_cluster,
            bitmap_cluster_count,
            upcase_first_cluster,
            upcase_cluster_count,
            data_source: None,
        })
    }

    /// Install a planned [`ExfatLayout`] into this synth: marks
    /// every layout extent in the allocation bitmap, replaces
    /// the default 3-entry root cluster with the layout's
    /// fully-rendered root cluster (which still begins with the
    /// mandatory bitmap + upcase + label entries), and wires
    /// the layout as the data source for subdirectory cluster
    /// reads.
    ///
    /// File data clusters fall through to zero-fill at this
    /// layer; the Phase-2.19 `DirTreeMaterializer` wraps the
    /// layout to serve real file bytes.
    ///
    /// # Errors
    ///
    /// * [`ExfatSynthError::LayoutMismatch`] if `layout`'s
    ///   `bytes_per_cluster` doesn't match this synth's
    ///   geometry — indicates the layout was planned against a
    ///   different volume.
    /// * [`ExfatSynthError::Bitmap`] if marking any layout
    ///   extent in the allocation bitmap fails (would only
    ///   happen if the layout allocator and the bitmap
    ///   disagree about cluster bounds, which is a
    ///   construction bug).
    pub fn with_layout(mut self, layout: ExfatLayout) -> Result<Self, ExfatSynthError> {
        let expected_bytes_per_cluster = self.geometry.bytes_per_cluster();
        if layout.bytes_per_cluster() != expected_bytes_per_cluster {
            return Err(ExfatSynthError::LayoutMismatch {
                synth_bytes_per_cluster: expected_bytes_per_cluster,
                layout_bytes_per_cluster: layout.bytes_per_cluster(),
            });
        }
        let expected_root_len = expected_bytes_per_cluster as usize;
        if layout.root_directory_bytes().len() != expected_root_len {
            return Err(ExfatSynthError::LayoutMismatch {
                synth_bytes_per_cluster: expected_bytes_per_cluster,
                layout_bytes_per_cluster: layout.bytes_per_cluster(),
            });
        }
        for extent in layout.allocated_extents() {
            if extent.is_empty() {
                continue;
            }
            self.bitmap
                .mark_range_allocated(extent.first_cluster, extent.cluster_count)
                .map_err(ExfatSynthError::Bitmap)?;
        }
        self.root_directory = layout.root_directory_bytes().to_vec();
        self.data_source = Some(Box::new(layout));
        Ok(self)
    }

    /// The geometry this synthesizer was built for.
    #[must_use]
    pub fn geometry(&self) -> &ExfatGeometry {
        &self.geometry
    }

    /// First cluster of the allocation bitmap stream.
    #[must_use]
    pub fn bitmap_first_cluster(&self) -> u32 {
        self.bitmap_first_cluster
    }

    /// First cluster of the upcase table stream.
    #[must_use]
    pub fn upcase_first_cluster(&self) -> u32 {
        self.upcase_first_cluster
    }

    /// Fill `out` with the bytes that live at `offset` in the
    /// synthesized volume.
    ///
    /// An empty `out` is a no-op and always returns `Ok(())`.
    ///
    /// # Errors
    ///
    /// * [`ExfatSynthError::OffsetBeyondVolume`] if `offset` is
    ///   at or beyond the volume size.
    /// * [`ExfatSynthError::LengthExceedsVolume`] if
    ///   `offset + out.len()` exceeds the volume size.
    /// * [`ExfatSynthError::UnsupportedRegion`] if the geometry
    ///   yields a region kind the dispatcher doesn't recognise
    ///   (defense-in-depth — never happens with the shipped
    ///   `ExfatGeometry`).
    pub fn read(&self, offset: u64, out: &mut [u8]) -> Result<(), ExfatSynthError> {
        if out.is_empty() {
            return Ok(());
        }
        let volume_size = self.geometry.volume_size_bytes();
        if offset >= volume_size {
            return Err(ExfatSynthError::OffsetBeyondVolume {
                offset,
                volume_size,
            });
        }
        let len_u64 = u64::try_from(out.len()).unwrap_or(u64::MAX);
        let end_offset =
            offset
                .checked_add(len_u64)
                .ok_or(ExfatSynthError::LengthExceedsVolume {
                    offset,
                    length: len_u64,
                    volume_size,
                })?;
        if end_offset > volume_size {
            return Err(ExfatSynthError::LengthExceedsVolume {
                offset,
                length: len_u64,
                volume_size,
            });
        }

        let mut cursor = offset;
        let mut remaining: &mut [u8] = out;
        while !remaining.is_empty() {
            let region =
                self.geometry
                    .region_at(cursor)
                    .ok_or(ExfatSynthError::OffsetBeyondVolume {
                        offset: cursor,
                        volume_size,
                    })?;
            let region_remaining_u64 = region.end().saturating_sub(cursor);
            let region_remaining_usize =
                usize::try_from(region_remaining_u64).unwrap_or(usize::MAX);
            let take = region_remaining_usize.min(remaining.len());
            let (chunk, rest) = remaining.split_at_mut(take);
            self.read_region(region, cursor, chunk)?;
            cursor = cursor.saturating_add(u64::try_from(take).unwrap_or(u64::MAX));
            remaining = rest;
        }
        Ok(())
    }

    fn read_region(
        &self,
        region: Region,
        offset: u64,
        out: &mut [u8],
    ) -> Result<(), ExfatSynthError> {
        let byte_in_region = offset.saturating_sub(region.start);
        match region.kind {
            RegionKind::ExfatMainBootRegion | RegionKind::ExfatBackupBootRegion => {
                self.read_boot_region(byte_in_region, out);
            }
            RegionKind::FatTable { .. } => {
                self.read_fat_region(byte_in_region, out);
            }
            RegionKind::Data => {
                self.read_data_region(byte_in_region, out);
            }
            RegionKind::Reserved => {
                out.fill(0);
            }
            RegionKind::Fat32BootSector
            | RegionKind::Fat32BackupBootSector
            | RegionKind::Fat32FsInfo => {
                return Err(ExfatSynthError::UnsupportedRegion { kind: region.kind });
            }
        }
        Ok(())
    }

    fn read_boot_region(&self, byte_in_region: u64, out: &mut [u8]) {
        // Caller has clamped to region bytes, so byte_in_region
        // + out.len() <= BOOT_REGION_SIZE_BYTES.
        let start = usize::try_from(byte_in_region).unwrap_or(BOOT_REGION_SIZE_BYTES);
        let end = start.saturating_add(out.len()).min(BOOT_REGION_SIZE_BYTES);
        #[allow(clippy::indexing_slicing)] // bounds clamped above
        out.copy_from_slice(&self.boot_region[start..end]);
    }

    fn read_fat_region(&self, byte_in_region: u64, out: &mut [u8]) {
        for (i, slot) in out.iter_mut().enumerate() {
            let byte_offset = byte_in_region.saturating_add(u64::try_from(i).unwrap_or(u64::MAX));
            let entry_index_u64 = byte_offset / u64::from(FAT_ENTRY_SIZE_BYTES);
            let entry_byte =
                usize::try_from(byte_offset % u64::from(FAT_ENTRY_SIZE_BYTES)).unwrap_or(0);
            let cluster = u32::try_from(entry_index_u64).unwrap_or(u32::MAX);
            let value = self.fat_entry_value(cluster);
            let bytes = value.to_le_bytes();
            #[allow(clippy::indexing_slicing)] // entry_byte ∈ 0..4 by modulo
            {
                *slot = bytes[entry_byte];
            }
        }
    }

    fn fat_entry_value(&self, cluster: u32) -> u32 {
        const MEDIA_DESCRIPTOR_ENTRY: u32 = 0xFFFF_FFF8;
        const END_OF_CHAIN: u32 = 0xFFFF_FFFF;
        const FREE: u32 = 0;
        let root_cluster = self.geometry.first_root_directory_cluster();
        if cluster == 0 {
            return MEDIA_DESCRIPTOR_ENTRY;
        }
        if cluster == 1 {
            return END_OF_CHAIN;
        }
        if cluster == root_cluster {
            // Root directory occupies exactly one cluster.
            return END_OF_CHAIN;
        }
        if let Some(next) = chain_next(
            cluster,
            self.bitmap_first_cluster,
            self.bitmap_cluster_count,
        ) {
            return next;
        }
        if let Some(next) = chain_next(
            cluster,
            self.upcase_first_cluster,
            self.upcase_cluster_count,
        ) {
            return next;
        }
        FREE
    }

    fn read_data_region(&self, byte_in_region: u64, out: &mut [u8]) {
        let bytes_per_cluster = u64::from(self.geometry.bytes_per_cluster());
        let mut remaining_out = out;
        let mut cursor = byte_in_region;
        while !remaining_out.is_empty() {
            let cluster_index_in_heap = cursor / bytes_per_cluster.max(1);
            let byte_in_cluster_u64 = cursor % bytes_per_cluster.max(1);
            let cluster = FIRST_CLUSTER_NUMBER
                .saturating_add(u32::try_from(cluster_index_in_heap).unwrap_or(u32::MAX));
            let bytes_until_cluster_end = bytes_per_cluster.saturating_sub(byte_in_cluster_u64);
            let chunk_len = usize::try_from(bytes_until_cluster_end)
                .unwrap_or(usize::MAX)
                .min(remaining_out.len());
            let (chunk, rest) = remaining_out.split_at_mut(chunk_len);
            self.read_data_cluster_chunk(cluster, byte_in_cluster_u64, chunk);
            cursor = cursor.saturating_add(u64::try_from(chunk_len).unwrap_or(u64::MAX));
            remaining_out = rest;
        }
    }

    fn read_data_cluster_chunk(&self, cluster: u32, byte_in_cluster: u64, out: &mut [u8]) {
        let root_cluster = self.geometry.first_root_directory_cluster();
        if cluster == root_cluster {
            copy_from_stream(&self.root_directory, byte_in_cluster, out);
            return;
        }
        if let Some(local_offset) = stream_offset(
            cluster,
            byte_in_cluster,
            self.bitmap_first_cluster,
            self.bitmap_cluster_count,
            u64::from(self.geometry.bytes_per_cluster()),
        ) {
            copy_from_stream(self.bitmap.bytes(), local_offset, out);
            return;
        }
        if let Some(local_offset) = stream_offset(
            cluster,
            byte_in_cluster,
            self.upcase_first_cluster,
            self.upcase_cluster_count,
            u64::from(self.geometry.bytes_per_cluster()),
        ) {
            copy_from_stream(self.upcase.bytes(), local_offset, out);
            return;
        }
        if let Some(source) = self.data_source.as_ref() {
            let byte_in_cluster_usize = usize::try_from(byte_in_cluster).unwrap_or(usize::MAX);
            source.read_cluster_bytes(cluster, byte_in_cluster_usize, out);
            return;
        }
        out.fill(0);
    }
}

/// Size in bytes of a single 32-bit `exFAT` FAT entry.
const FAT_ENTRY_SIZE_BYTES: u32 = 4;

const _: () = {
    // Sector size must be a multiple of FAT entry size so the
    // dispatcher can iterate per-byte safely.
    assert!(SECTOR_SIZE_BYTES % FAT_ENTRY_SIZE_BYTES == 0);
};

/// Helper: if `cluster` belongs to the contiguous stream
/// `[first, first + count)`, return the FAT chain pointer for
/// it (next cluster, or end-of-chain for the last cluster).
fn chain_next(cluster: u32, first: u32, count: u32) -> Option<u32> {
    if count == 0 {
        return None;
    }
    let end_exclusive = first.checked_add(count)?;
    if cluster >= first && cluster < end_exclusive {
        let is_last = cluster + 1 == end_exclusive;
        Some(if is_last { 0xFFFF_FFFF } else { cluster + 1 })
    } else {
        None
    }
}

/// Helper: if `cluster` is in the contiguous range
/// `[first, first + count)`, return the byte offset into the
/// stream that corresponds to `(cluster, byte_in_cluster)`.
fn stream_offset(
    cluster: u32,
    byte_in_cluster: u64,
    first: u32,
    count: u32,
    bytes_per_cluster: u64,
) -> Option<u64> {
    if count == 0 || cluster < first {
        return None;
    }
    let end_exclusive = first.checked_add(count)?;
    if cluster >= end_exclusive {
        return None;
    }
    let cluster_offset = u64::from(cluster - first);
    Some(
        cluster_offset
            .saturating_mul(bytes_per_cluster)
            .saturating_add(byte_in_cluster),
    )
}

/// Helper: copy `out.len()` bytes from `stream[start..]` into
/// `out`, zero-padding past the end of the stream.
fn copy_from_stream(stream: &[u8], start: u64, out: &mut [u8]) {
    let start_usize = usize::try_from(start).unwrap_or(usize::MAX);
    if start_usize >= stream.len() {
        out.fill(0);
        return;
    }
    let available = stream.len() - start_usize;
    let copy_len = available.min(out.len());
    #[allow(clippy::indexing_slicing)] // bounds verified above
    {
        out[..copy_len].copy_from_slice(&stream[start_usize..start_usize + copy_len]);
        if copy_len < out.len() {
            out[copy_len..].fill(0);
        }
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
    use super::*;
    use crate::fs::exfat::directory::{
        DIRECTORY_ENTRY_SIZE_BYTES, ENTRY_TYPE_ALLOCATION_BITMAP, ENTRY_TYPE_UPCASE_TABLE,
        ENTRY_TYPE_VOLUME_LABEL,
    };

    const SIXTY_FOUR_MIB: u64 = 64 * 1024 * 1024;
    const FOUR_GIB: u64 = 4 * 1024 * 1024 * 1024;

    fn synth_64mib() -> ExfatSynth {
        let geo = ExfatGeometry::for_volume_size(SIXTY_FOUR_MIB).expect("64 MiB geometry");
        let label: Vec<u16> = "TESLACAM".encode_utf16().collect();
        ExfatSynth::new(geo, 0x1234_5678, &label).expect("synth ok")
    }

    fn synth_4gib() -> ExfatSynth {
        let geo = ExfatGeometry::for_volume_size(FOUR_GIB).expect("4 GiB geometry");
        let label: Vec<u16> = "TESLACAM".encode_utf16().collect();
        ExfatSynth::new(geo, 0x0000_0001, &label).expect("synth ok")
    }

    // ---------- Construction ----------

    #[test]
    fn construction_returns_geometry() {
        let s = synth_64mib();
        assert_eq!(s.geometry().volume_size_bytes(), SIXTY_FOUR_MIB);
    }

    #[test]
    fn construction_assigns_root_at_cluster_2() {
        let s = synth_64mib();
        assert_eq!(s.geometry().first_root_directory_cluster(), 2);
    }

    #[test]
    fn construction_assigns_bitmap_at_cluster_3() {
        let s = synth_64mib();
        assert_eq!(s.bitmap_first_cluster(), 3);
    }

    #[test]
    fn construction_assigns_upcase_after_bitmap() {
        let s = synth_64mib();
        assert!(s.upcase_first_cluster() > s.bitmap_first_cluster());
    }

    #[test]
    fn construction_rejects_label_too_long() {
        let geo = ExfatGeometry::for_volume_size(SIXTY_FOUR_MIB).unwrap();
        let too_long: Vec<u16> = (0_u16..12).collect();
        let err = ExfatSynth::new(geo, 0, &too_long).unwrap_err();
        assert!(matches!(err, ExfatSynthError::Directory(_)));
    }

    // ---------- Read bounds checks ----------

    #[test]
    fn empty_read_is_ok() {
        let s = synth_64mib();
        let mut buf = [];
        s.read(0, &mut buf).unwrap();
    }

    #[test]
    fn read_at_offset_equal_to_volume_size_errors() {
        let s = synth_64mib();
        let mut buf = [0_u8; 1];
        let err = s.read(SIXTY_FOUR_MIB, &mut buf).unwrap_err();
        assert!(matches!(err, ExfatSynthError::OffsetBeyondVolume { .. }));
    }

    #[test]
    fn read_extending_past_volume_size_errors() {
        let s = synth_64mib();
        let mut buf = [0_u8; 2];
        let err = s.read(SIXTY_FOUR_MIB - 1, &mut buf).unwrap_err();
        assert!(matches!(err, ExfatSynthError::LengthExceedsVolume { .. }));
    }

    // ---------- Boot region dispatch ----------

    #[test]
    fn read_at_offset_zero_returns_jump_boot() {
        let s = synth_64mib();
        let mut buf = [0_u8; 3];
        s.read(0, &mut buf).unwrap();
        assert_eq!(buf, [0xEB, 0x76, 0x90]);
    }

    #[test]
    fn read_at_filesystem_name_returns_exfat() {
        let s = synth_64mib();
        let mut buf = [0_u8; 8];
        s.read(3, &mut buf).unwrap();
        assert_eq!(&buf, b"EXFAT   ");
    }

    #[test]
    fn read_at_boot_signature_returns_55aa() {
        let s = synth_64mib();
        let mut buf = [0_u8; 2];
        s.read(0x1FE, &mut buf).unwrap();
        assert_eq!(buf, [0x55, 0xAA]);
    }

    #[test]
    fn backup_boot_region_serves_same_bytes_as_main() {
        let s = synth_64mib();
        let mut main = [0_u8; 64];
        let mut backup = [0_u8; 64];
        s.read(0, &mut main).unwrap();
        s.read(12 * 512, &mut backup).unwrap();
        assert_eq!(main, backup);
    }

    // ---------- FAT region dispatch ----------

    fn fat_offset(s: &ExfatSynth) -> u64 {
        // FAT begins at sector 24 = 2 boot regions of 12 sectors.
        u64::from(s.geometry().fat_offset_sectors()) * 512
    }

    fn read_fat_entry(s: &ExfatSynth, cluster: u32) -> u32 {
        let off = fat_offset(s) + u64::from(cluster) * 4;
        let mut buf = [0_u8; 4];
        s.read(off, &mut buf).unwrap();
        u32::from_le_bytes(buf)
    }

    #[test]
    fn fat_entry_zero_is_media_descriptor() {
        let s = synth_64mib();
        assert_eq!(read_fat_entry(&s, 0), 0xFFFF_FFF8);
    }

    #[test]
    fn fat_entry_one_is_end_of_chain() {
        let s = synth_64mib();
        assert_eq!(read_fat_entry(&s, 1), 0xFFFF_FFFF);
    }

    #[test]
    fn fat_entry_for_root_is_end_of_chain() {
        let s = synth_64mib();
        assert_eq!(read_fat_entry(&s, 2), 0xFFFF_FFFF);
    }

    #[test]
    fn fat_entry_for_bitmap_first_cluster_chains_or_terminates() {
        let s = synth_64mib();
        let first = s.bitmap_first_cluster();
        let val = read_fat_entry(&s, first);
        // Either next cluster or end-of-chain depending on size.
        assert!(val == 0xFFFF_FFFF || val == first + 1);
    }

    #[test]
    fn fat_entry_for_free_cluster_is_zero() {
        let s = synth_64mib();
        let free = s.upcase_first_cluster() + 100; // way past upcase stream
        assert!(free < FIRST_CLUSTER_NUMBER + s.geometry().cluster_count());
        assert_eq!(read_fat_entry(&s, free), 0);
    }

    #[test]
    fn upcase_chain_terminates_at_last_cluster() {
        let s = synth_4gib();
        let last_upcase = s.upcase_first_cluster() + s.upcase_cluster_count - 1;
        assert_eq!(read_fat_entry(&s, last_upcase), 0xFFFF_FFFF);
    }

    // ---------- Data region: root directory ----------

    fn data_offset(s: &ExfatSynth) -> u64 {
        u64::from(s.geometry().cluster_heap_offset_sectors()) * 512
    }

    fn cluster_byte_offset(s: &ExfatSynth, cluster: u32) -> u64 {
        let cluster_index = u64::from(cluster - FIRST_CLUSTER_NUMBER);
        data_offset(s) + cluster_index * u64::from(s.geometry().bytes_per_cluster())
    }

    #[test]
    fn root_cluster_starts_with_allocation_bitmap_entry() {
        let s = synth_64mib();
        let off = cluster_byte_offset(&s, 2);
        let mut buf = [0_u8; DIRECTORY_ENTRY_SIZE_BYTES];
        s.read(off, &mut buf).unwrap();
        assert_eq!(buf[0], ENTRY_TYPE_ALLOCATION_BITMAP);
    }

    #[test]
    fn root_cluster_second_entry_is_upcase_table() {
        let s = synth_64mib();
        let off = cluster_byte_offset(&s, 2) + 32;
        let mut buf = [0_u8; DIRECTORY_ENTRY_SIZE_BYTES];
        s.read(off, &mut buf).unwrap();
        assert_eq!(buf[0], ENTRY_TYPE_UPCASE_TABLE);
    }

    #[test]
    fn root_cluster_third_entry_is_volume_label() {
        let s = synth_64mib();
        let off = cluster_byte_offset(&s, 2) + 64;
        let mut buf = [0_u8; DIRECTORY_ENTRY_SIZE_BYTES];
        s.read(off, &mut buf).unwrap();
        assert_eq!(buf[0], ENTRY_TYPE_VOLUME_LABEL);
        assert_eq!(buf[1], 8); // "TESLACAM" = 8 code units.
    }

    #[test]
    fn root_cluster_fourth_slot_is_zero_eod_marker() {
        let s = synth_64mib();
        let off = cluster_byte_offset(&s, 2) + 96;
        let mut buf = [0_u8; 1];
        s.read(off, &mut buf).unwrap();
        assert_eq!(buf[0], 0);
    }

    // ---------- Data region: bitmap ----------

    #[test]
    fn bitmap_cluster_first_byte_reflects_marked_bits() {
        let s = synth_64mib();
        let off = cluster_byte_offset(&s, s.bitmap_first_cluster());
        let mut buf = [0_u8; 1];
        s.read(off, &mut buf).unwrap();
        // Cluster 2 (root) → bit 0; cluster 3 (bitmap) → bit 1;
        // cluster 4..end-of-upcase → more bits. So byte 0 is
        // non-zero.
        assert_ne!(buf[0], 0);
        // Specifically bit 0 (cluster 2 = root) must be set.
        assert_eq!(buf[0] & 0x01, 0x01);
    }

    // ---------- Data region: upcase ----------

    #[test]
    fn upcase_cluster_first_two_bytes_are_code_unit_zero() {
        let s = synth_64mib();
        let off = cluster_byte_offset(&s, s.upcase_first_cluster());
        let mut buf = [0_u8; 2];
        s.read(off, &mut buf).unwrap();
        // entry[0] = u16::to_le_bytes(0) = [0, 0]: code unit 0
        // maps to itself.
        assert_eq!(buf, [0, 0]);
    }

    #[test]
    fn upcase_lowercase_a_maps_to_uppercase_a() {
        let s = synth_64mib();
        // Code unit 0x0061 ('a') → entry at upcase byte offset
        // 0x0061 * 2 = 0xC2.
        let off = cluster_byte_offset(&s, s.upcase_first_cluster()) + 0xC2;
        let mut buf = [0_u8; 2];
        s.read(off, &mut buf).unwrap();
        assert_eq!(u16::from_le_bytes(buf), 0x0041);
    }

    // ---------- Data region: free clusters ----------

    #[test]
    fn free_cluster_reads_back_as_zeros() {
        let s = synth_4gib();
        let free_cluster = s.upcase_first_cluster() + s.upcase_cluster_count + 5;
        assert!(free_cluster < FIRST_CLUSTER_NUMBER + s.geometry().cluster_count());
        let off = cluster_byte_offset(&s, free_cluster);
        let mut buf = [0_u8; 64];
        s.read(off, &mut buf).unwrap();
        assert!(buf.iter().all(|&b| b == 0));
    }

    // ---------- Cross-region reads ----------

    #[test]
    fn read_spanning_main_and_backup_boot_works() {
        let s = synth_64mib();
        // Start 4 bytes before backup boot region begins (i.e.,
        // last 4 bytes of main + first 4 bytes of backup), read
        // 8 bytes.
        let backup_start = 12_u64 * 512;
        let mut buf = [0_u8; 8];
        s.read(backup_start - 4, &mut buf).unwrap();
        // First 4 bytes are the tail of main boot region,
        // last 4 bytes are the head of backup region — and
        // those head bytes are byte-for-byte the head of main.
        let mut head_of_main = [0_u8; 4];
        s.read(0, &mut head_of_main).unwrap();
        assert_eq!(&buf[4..8], &head_of_main);
    }

    // ---------- Determinism ----------

    #[test]
    fn two_reads_at_same_offset_return_identical_bytes() {
        let s = synth_64mib();
        let mut a = vec![0_u8; 4096];
        let mut b = vec![0_u8; 4096];
        let off = cluster_byte_offset(&s, 2);
        s.read(off, &mut a).unwrap();
        s.read(off, &mut b).unwrap();
        assert_eq!(a, b);
    }

    // ---------- chain_next / stream_offset units ----------

    #[test]
    fn chain_next_inside_range_returns_successor() {
        assert_eq!(chain_next(5, 5, 3), Some(6));
        assert_eq!(chain_next(6, 5, 3), Some(7));
    }

    #[test]
    fn chain_next_at_last_returns_end_of_chain() {
        assert_eq!(chain_next(7, 5, 3), Some(0xFFFF_FFFF));
    }

    #[test]
    fn chain_next_outside_range_returns_none() {
        assert_eq!(chain_next(4, 5, 3), None);
        assert_eq!(chain_next(8, 5, 3), None);
        assert_eq!(chain_next(5, 5, 0), None);
    }

    #[test]
    fn stream_offset_computes_byte_address() {
        assert_eq!(stream_offset(5, 0, 5, 3, 4096), Some(0));
        assert_eq!(stream_offset(5, 100, 5, 3, 4096), Some(100));
        assert_eq!(stream_offset(6, 0, 5, 3, 4096), Some(4096));
        assert_eq!(stream_offset(7, 50, 5, 3, 4096), Some(8192 + 50));
    }

    #[test]
    fn stream_offset_outside_returns_none() {
        assert_eq!(stream_offset(4, 0, 5, 3, 4096), None);
        assert_eq!(stream_offset(8, 0, 5, 3, 4096), None);
    }

    // ---------- Display for ExfatSynthError ----------

    #[test]
    fn error_display_strings_are_informative() {
        let e = ExfatSynthError::OffsetBeyondVolume {
            offset: 1000,
            volume_size: 500,
        };
        assert!(format!("{e}").contains("1000"));
        let e = ExfatSynthError::LengthExceedsVolume {
            offset: 0,
            length: 1000,
            volume_size: 500,
        };
        assert!(format!("{e}").contains("500"));
        let e = ExfatSynthError::ClusterHeapTooSmall {
            needed_clusters: 100,
            available_clusters: 50,
        };
        let s = format!("{e}");
        assert!(s.contains("100") && s.contains("50"));
    }
}
