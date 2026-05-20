//! FAT32 read dispatcher (Phase 2.6).
//!
//! Given a byte offset and a caller-supplied `&mut [u8]` to fill,
//! [`Fat32Synth::read`] dispatches each contiguous chunk of the
//! request to the appropriate region synthesizer:
//!
//! | [`RegionKind`]              | Source                                            |
//! |-----------------------------|---------------------------------------------------|
//! | `Fat32BootSector`           | the pre-computed 512-byte boot sector             |
//! | `Fat32BackupBootSector`     | the same 512 bytes (fatgen103 §3.4 normative)     |
//! | `Fat32FsInfo`               | the pre-computed 512-byte `FsInfo` sector         |
//! | `Reserved`                  | zero-fill (the reserved gap between boot regions) |
//! | `FatTable { .. }`           | [`FatTable::synthesize_sector`] (both mirrors)    |
//! | `Data`                      | zero-fill (Phase 2.6 has no directory layout yet) |
//!
//! Region lookup is delegated to [`Fat32Geometry::region_at`]
//! (Phase 2.1). The dispatcher itself is region-agnostic in
//! structure: it loops, asks the geometry which region holds the
//! current cursor, copies as many bytes as fit in that region,
//! advances, and repeats until the request is drained.
//!
//! ## Why the data region zero-fills in Phase 2.6
//!
//! The `DirTreeBackend` trait introduced in Phase 2.4 lets the
//! synthesizer build a valid FAT chain table from a list of
//! cluster numbers, but it does not yet describe which 32-byte
//! directory entries live in which cluster. Phase 2.7 (FAT32
//! integration test) and the lazy-load module (Phase 2.13) close
//! that loop. For Phase 2.6 the data region is allocated in the
//! FAT (so the chain count is honest) but byte-reads of any
//! cluster come back as 512-byte zero runs — the dispatcher does
//! not yet know which directory entries or file contents to lay
//! out where. This matches the plan-stated scope:
//! "`fs::fat32::synth::read(offset, len)` dispatcher; tests cover
//! region boundaries, partial-region reads, beyond-EOF."
//!
//! ## What this module does NOT do
//!
//! * It does not allocate. Construction reserves three 512-byte
//!   arrays (boot, `FsInfo`, backup boot) plus the `FatTable`'s
//!   `Vec<u32>` (which Phase 2.4 already owns). [`Fat32Synth::read`]
//!   takes a caller-supplied buffer and never allocates.
//! * It does not synthesize directory entries — see above.
//! * It does not implement write. The NBD daemon will gate writes
//!   at the transmission layer (Phase 3) and the dispatcher will
//!   grow a parallel `write` method then.

use core::fmt;

use crate::fs::cluster_layout::FIRST_DATA_CLUSTER;
use crate::fs::data_cluster_source::DataClusterSource;
use crate::fs::fat32::boot_sector::{self, BOOT_SECTOR_SIZE_BYTES, BootSectorError};
use crate::fs::fat32::fat_table::{DirTreeBackend, FAT_SECTOR_SIZE_BYTES, FatTable, FatTableError};
use crate::fs::fat32::fsinfo::{self, FSINFO_SECTOR_SIZE_BYTES, FsInfoError};
use crate::fs::fat32::geometry::Fat32Geometry;
use crate::fs::geometry::{Geometry, Region, RegionKind, SECTOR_SIZE_BYTES};

/// Materialised FAT32 synthesizer: pre-computed boot + `FsInfo`
/// sectors and a built FAT table, ready to serve byte-range
/// reads via [`Self::read`].
///
/// Construction is the only fallible operation; subsequent
/// `read` calls only fail if the caller's `(offset, len)` is
/// out-of-bounds (or if the underlying [`FatTable::synthesize_sector`]
/// ever returns an error, which the dispatcher's own bounds checks
/// make unreachable).
///
/// ## Data region
///
/// By default the data region zero-fills — Phase 2.6's
/// original behaviour, preserved for backwards compatibility
/// with the 461 Phase-2 tests. Wire in a
/// [`DataClusterSource`] via [`Self::with_data_source`] to
/// serve real cluster bytes (Phase 2.17 introduces
/// [`crate::fs::fat32::layout::Fat32Layout`] as the canonical
/// source for directory clusters).
#[derive(Debug)]
pub struct Fat32Synth {
    geometry: Fat32Geometry,
    boot_sector: [u8; BOOT_SECTOR_SIZE_BYTES],
    fsinfo_sector: [u8; FSINFO_SECTOR_SIZE_BYTES],
    fat_table: FatTable,
    data_source: Option<Box<dyn DataClusterSource + Send + Sync>>,
    first_data_byte: u64,
    bytes_per_cluster: u32,
}

/// Errors returned by [`Fat32Synth::new`] and [`Fat32Synth::read`].
#[derive(Debug, PartialEq, Eq)]
pub enum Fat32SynthError {
    /// The boot sector synthesizer rejected the caller's
    /// arguments (bad volume label, oversized `BPB_TotSec32`).
    /// Surfaced verbatim so the caller can match on the inner
    /// variant.
    BootSector(BootSectorError),
    /// The `FsInfo` synthesizer rejected the caller's hints
    /// (free-count exceeds data clusters, next-free hint out of
    /// range). Surfaced verbatim.
    FsInfo(FsInfoError),
    /// The FAT-table builder rejected the caller's backend
    /// (chain references reserved/out-of-range/bad-marker cluster,
    /// double allocation, empty chain). Surfaced verbatim.
    FatTable(FatTableError),
    /// `offset` is at or beyond the geometry's volume size.
    OffsetBeyondVolume {
        /// The caller's offset.
        offset: u64,
        /// The geometry's volume size.
        volume_size: u64,
    },
    /// `offset + length` exceeds the geometry's volume size
    /// (the request reads past the end of the volume).
    LengthExceedsVolume {
        /// The caller's offset.
        offset: u64,
        /// The caller's buffer length in bytes.
        length: u64,
        /// The geometry's volume size.
        volume_size: u64,
    },
    /// The geometry returned a [`RegionKind`] that is not part of
    /// the FAT32 on-disk layout (for example, an exFAT boot region).
    /// A correctly-constructed [`Fat32Geometry`] never produces such
    /// regions; this variant exists as defense-in-depth for callers
    /// who hand-build a [`crate::fs::geometry::Region`] slice or
    /// extend [`RegionKind`] in the future.
    UnsupportedRegion {
        /// The offending region kind.
        kind: RegionKind,
    },
}

impl fmt::Display for Fat32SynthError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BootSector(err) => write!(f, "boot-sector synthesis failed: {err}"),
            Self::FsInfo(err) => write!(f, "FsInfo synthesis failed: {err}"),
            Self::FatTable(err) => write!(f, "FAT-table synthesis failed: {err}"),
            Self::OffsetBeyondVolume {
                offset,
                volume_size,
            } => write!(
                f,
                "read offset {offset} is at or beyond the volume size {volume_size}"
            ),
            Self::LengthExceedsVolume {
                offset,
                length,
                volume_size,
            } => write!(
                f,
                "read of {length} bytes at offset {offset} extends past the volume size {volume_size}"
            ),
            Self::UnsupportedRegion { kind } => {
                write!(f, "FAT32 synth received an unsupported region kind: {kind}")
            }
        }
    }
}

impl std::error::Error for Fat32SynthError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::BootSector(err) => Some(err),
            Self::FsInfo(err) => Some(err),
            Self::FatTable(err) => Some(err),
            Self::OffsetBeyondVolume { .. }
            | Self::LengthExceedsVolume { .. }
            | Self::UnsupportedRegion { .. } => None,
        }
    }
}

impl Fat32Synth {
    /// Build a `Fat32Synth` from a geometry, a volume label, a
    /// volume serial, optional `FsInfo` hints, and a directory-tree
    /// backend.
    ///
    /// The three sub-synthesizers are invoked in deterministic
    /// order:
    ///
    /// 1. [`super::fat_table::FatTable::build`] consumes `backend`.
    /// 2. [`super::boot_sector::synthesize`] consumes `geometry`, `label`,
    ///    `volume_serial`.
    /// 3. [`super::fsinfo::synthesize`] consumes `geometry`, `free_count`,
    ///    `next_free_hint`.
    ///
    /// Any one of them can return an error; the dispatcher
    /// converts each into the matching [`Fat32SynthError`] variant
    /// (with the original error preserved as `source()`).
    ///
    /// The backup boot sector at sector 6 is a byte-for-byte copy
    /// of the boot sector (fatgen103 §3.4 normative); the
    /// dispatcher serves the same bytes for both regions, so no
    /// separate buffer is materialised.
    ///
    /// # Errors
    ///
    /// * [`Fat32SynthError::FatTable`] if `backend` describes an
    ///   invalid chain layout.
    /// * [`Fat32SynthError::BootSector`] if `label` is invalid or
    ///   the geometry's sector count exceeds `u32::MAX`.
    /// * [`Fat32SynthError::FsInfo`] if the optional hints are
    ///   inconsistent with the geometry's cluster bounds.
    pub fn new(
        geometry: Fat32Geometry,
        label: &[u8],
        volume_serial: u32,
        free_count: Option<u32>,
        next_free_hint: Option<u32>,
        backend: &dyn DirTreeBackend,
    ) -> Result<Self, Fat32SynthError> {
        let fat_table = FatTable::build(&geometry, backend).map_err(Fat32SynthError::FatTable)?;
        let boot_sector = boot_sector::synthesize(&geometry, label, volume_serial)
            .map_err(Fat32SynthError::BootSector)?;
        let fsinfo_sector = fsinfo::synthesize(&geometry, free_count, next_free_hint)
            .map_err(Fat32SynthError::FsInfo)?;
        let first_data_byte = geometry
            .first_data_sector()
            .saturating_mul(u64::from(SECTOR_SIZE_BYTES));
        let bytes_per_cluster = geometry.bytes_per_cluster();
        Ok(Self {
            geometry,
            boot_sector,
            fsinfo_sector,
            fat_table,
            data_source: None,
            first_data_byte,
            bytes_per_cluster,
        })
    }

    /// Install a [`DataClusterSource`] to back the data region.
    ///
    /// Without a source, reads of any data-region byte return
    /// zeros (Phase 2.6 behaviour). With a source installed,
    /// the dispatcher routes each touched data cluster through
    /// [`DataClusterSource::read_cluster_bytes`] — typically
    /// supplied by [`crate::fs::fat32::layout::Fat32Layout`]
    /// (Phase 2.17) for directory clusters or by
    /// `teslafat::DirTreeMaterializer` (Phase 2.19) for full
    /// directory + file content.
    ///
    /// Builder-style on purpose: existing call sites that
    /// don't care about the data region keep passing the
    /// six-argument [`Self::new`] form unchanged.
    #[must_use]
    pub fn with_data_source(mut self, source: Box<dyn DataClusterSource + Send + Sync>) -> Self {
        self.data_source = Some(source);
        self
    }

    /// The geometry this synthesizer was built for.
    ///
    /// Useful when the caller (the NBD daemon) needs to advertise
    /// the export size or check region boundaries without holding
    /// its own copy of the geometry.
    #[must_use]
    pub fn geometry(&self) -> &Fat32Geometry {
        &self.geometry
    }

    /// Fill `out` with the bytes that live at `offset` in the
    /// synthesized volume.
    ///
    /// The request is split across the regions it touches; each
    /// chunk is routed to the appropriate sub-synthesizer (see
    /// the module-level doc for the region-to-source table).
    ///
    /// An empty `out` is a no-op and always returns `Ok(())`.
    ///
    /// # Errors
    ///
    /// * [`Fat32SynthError::OffsetBeyondVolume`] if `offset` is
    ///   at or beyond the volume size.
    /// * [`Fat32SynthError::LengthExceedsVolume`] if
    ///   `offset + out.len()` exceeds the volume size (or wraps
    ///   `u64` arithmetic, which the bounds check forces).
    /// * [`Fat32SynthError::FatTable`] if the FAT-sector
    ///   synthesizer ever returns an error (the dispatcher's own
    ///   bounds checks make this unreachable, but the error is
    ///   surfaced rather than swallowed).
    pub fn read(&self, offset: u64, out: &mut [u8]) -> Result<(), Fat32SynthError> {
        if out.is_empty() {
            return Ok(());
        }
        let volume_size = self.geometry.volume_size_bytes();
        if offset >= volume_size {
            return Err(Fat32SynthError::OffsetBeyondVolume {
                offset,
                volume_size,
            });
        }
        let len_u64 = u64::try_from(out.len()).unwrap_or(u64::MAX);
        let end_offset =
            offset
                .checked_add(len_u64)
                .ok_or(Fat32SynthError::LengthExceedsVolume {
                    offset,
                    length: len_u64,
                    volume_size,
                })?;
        if end_offset > volume_size {
            return Err(Fat32SynthError::LengthExceedsVolume {
                offset,
                length: len_u64,
                volume_size,
            });
        }

        let mut cursor = offset;
        let mut remaining: &mut [u8] = out;
        while !remaining.is_empty() {
            // SAFETY-of-`Option::unwrap`: we've already validated
            // `cursor < volume_size` at loop entry; the region map
            // tiles the volume gap-free per the `Geometry` trait
            // invariant, so `region_at(cursor)` returns `Some`
            // whenever `cursor < volume_size`. We still produce a
            // typed error instead of unwrapping, to keep the
            // unreachable path observable and lint-clean.
            let region =
                self.geometry
                    .region_at(cursor)
                    .ok_or(Fat32SynthError::OffsetBeyondVolume {
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
    ) -> Result<(), Fat32SynthError> {
        // PRECONDITION: byte_in_region + out.len() <= region.len.
        // Enforced by `read` which clamps each iteration's `take`
        // to `region.end() - cursor`. The
        // `#[allow(clippy::indexing_slicing)]` annotation below
        // documents that the bounds are caller-validated.
        let byte_in_region =
            usize::try_from(offset.saturating_sub(region.start)).unwrap_or(usize::MAX);
        match region.kind {
            RegionKind::Fat32BootSector | RegionKind::Fat32BackupBootSector => {
                copy_within_sector(&self.boot_sector, byte_in_region, out);
            }
            RegionKind::Fat32FsInfo => {
                copy_within_sector(&self.fsinfo_sector, byte_in_region, out);
            }
            RegionKind::Reserved => {
                out.fill(0);
            }
            RegionKind::Data => {
                self.read_data_region(offset, out);
            }
            RegionKind::FatTable { .. } => {
                self.read_fat_table(byte_in_region, out)?;
            }
            RegionKind::ExfatMainBootRegion | RegionKind::ExfatBackupBootRegion => {
                return Err(Fat32SynthError::UnsupportedRegion { kind: region.kind });
            }
        }
        Ok(())
    }

    fn read_fat_table(
        &self,
        mut byte_in_region: usize,
        mut out: &mut [u8],
    ) -> Result<(), Fat32SynthError> {
        while !out.is_empty() {
            let sector_in_fat_usize = byte_in_region / FAT_SECTOR_SIZE_BYTES;
            let sector_byte_offset = byte_in_region % FAT_SECTOR_SIZE_BYTES;
            let sector_in_fat =
                u32::try_from(sector_in_fat_usize).unwrap_or(self.fat_table.fat_size_sectors());
            let sector = self
                .fat_table
                .synthesize_sector(sector_in_fat)
                .map_err(Fat32SynthError::FatTable)?;
            let chunk_len = (FAT_SECTOR_SIZE_BYTES - sector_byte_offset).min(out.len());
            copy_within_sector_into_chunk(&sector, sector_byte_offset, chunk_len, out);
            byte_in_region = byte_in_region.saturating_add(chunk_len);
            let (_, rest) = out.split_at_mut(chunk_len);
            out = rest;
        }
        Ok(())
    }

    /// Data region split into per-cluster reads.
    ///
    /// `offset` is the absolute byte offset into the volume
    /// (already validated by [`Self::read`] to lie within the
    /// data region). The dispatcher hands out each contiguous
    /// chunk to [`DataClusterSource::read_cluster_bytes`], or
    /// zero-fills when no source is installed.
    fn read_data_region(&self, offset: u64, out: &mut [u8]) {
        let Some(ref source) = self.data_source else {
            out.fill(0);
            return;
        };
        if out.is_empty() || self.bytes_per_cluster == 0 {
            out.fill(0);
            return;
        }
        let bytes_per_cluster_u64 = u64::from(self.bytes_per_cluster);
        let bytes_per_cluster_usize = self.bytes_per_cluster as usize;
        let mut cursor = offset;
        let mut remaining = out;
        while !remaining.is_empty() {
            let byte_in_data = cursor.saturating_sub(self.first_data_byte);
            let cluster_index = byte_in_data / bytes_per_cluster_u64;
            let byte_in_cluster_u64 = byte_in_data % bytes_per_cluster_u64;
            let byte_in_cluster = usize::try_from(byte_in_cluster_u64).unwrap_or(usize::MAX);
            let cluster_number =
                u32::try_from(cluster_index.saturating_add(u64::from(FIRST_DATA_CLUSTER)))
                    .unwrap_or(u32::MAX);
            let chunk_len = bytes_per_cluster_usize
                .saturating_sub(byte_in_cluster)
                .min(remaining.len());
            let (chunk, rest) = remaining.split_at_mut(chunk_len);
            source.read_cluster_bytes(cluster_number, byte_in_cluster, chunk);
            cursor = cursor.saturating_add(chunk_len as u64);
            remaining = rest;
        }
    }
}

/// Copy `out.len()` bytes starting at `byte_in_sector` from
/// `sector` into `out`.
///
/// Caller guarantees `byte_in_sector + out.len() <= sector.len()`.
#[allow(clippy::indexing_slicing)]
fn copy_within_sector(sector: &[u8], byte_in_sector: usize, out: &mut [u8]) {
    let end = byte_in_sector + out.len();
    out.copy_from_slice(&sector[byte_in_sector..end]);
}

/// Copy `chunk_len` bytes from `sector[sector_byte_offset..]`
/// into the head of `out`. Caller guarantees the slice indices
/// are in bounds for both buffers.
#[allow(clippy::indexing_slicing)]
fn copy_within_sector_into_chunk(
    sector: &[u8],
    sector_byte_offset: usize,
    chunk_len: usize,
    out: &mut [u8],
) {
    let src_end = sector_byte_offset + chunk_len;
    out[..chunk_len].copy_from_slice(&sector[sector_byte_offset..src_end]);
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
    use crate::fs::fat32::boot_sector::ROOT_DIRECTORY_CLUSTER;
    use crate::fs::fat32::fat_table::InMemoryDirTree;
    use crate::fs::fat32::geometry::{BACKUP_BOOT_SECTOR_INDEX, FSINFO_SECTOR_INDEX};

    const SECTOR: u64 = 512;
    const SECTOR_USIZE: usize = 512;
    const FOUR_GIB: u64 = 4 * 1024 * 1024 * 1024;
    /// 34 MiB — smallest valid FAT32 (Phase 2.1 geometry test pin).
    const SMALL: u64 = 34 * 1024 * 1024;

    fn synth_4gib() -> Fat32Synth {
        let geo = Fat32Geometry::for_volume_size(FOUR_GIB).expect("4 GiB geometry");
        let backend = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
        Fat32Synth::new(geo, b"TESTLABEL  ", 0x1234_5678, None, None, &backend).expect("synth ok")
    }

    fn synth_small() -> Fat32Synth {
        let geo = Fat32Geometry::for_volume_size(SMALL).expect("34 MiB geometry");
        let backend = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
        Fat32Synth::new(geo, b"SMALL      ", 0x0BAD_BEEF, None, None, &backend).expect("synth ok")
    }

    // ── Construction ─────────────────────────────────────────────────

    #[test]
    fn construction_returns_geometry() {
        let s = synth_4gib();
        assert_eq!(s.geometry().volume_size_bytes(), FOUR_GIB);
    }

    #[test]
    fn construction_propagates_boot_sector_error() {
        // A volume label with a lowercase byte is rejected by
        // boot_sector::synthesize per fatgen103 §6.1.
        let geo = Fat32Geometry::for_volume_size(FOUR_GIB).unwrap();
        let backend = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
        let err = Fat32Synth::new(geo, b"lowercase  ", 0, None, None, &backend)
            .expect_err("lowercase label");
        assert!(matches!(err, Fat32SynthError::BootSector(_)));
    }

    #[test]
    fn construction_propagates_fsinfo_error() {
        let geo = Fat32Geometry::for_volume_size(FOUR_GIB).unwrap();
        let backend = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
        // next_free_hint = 1 is below ROOT_DIRECTORY_CLUSTER = 2.
        let err = Fat32Synth::new(geo, b"TEST       ", 0, None, Some(1), &backend)
            .expect_err("bad next_free");
        assert!(matches!(err, Fat32SynthError::FsInfo(_)));
    }

    #[test]
    fn construction_propagates_fat_table_error() {
        let geo = Fat32Geometry::for_volume_size(FOUR_GIB).unwrap();
        // Chain containing cluster 1 — reserved.
        let backend = InMemoryDirTree::from_chains(vec![vec![1]]);
        let err = Fat32Synth::new(geo, b"TEST       ", 0, None, None, &backend)
            .expect_err("reserved cluster");
        assert!(matches!(err, Fat32SynthError::FatTable(_)));
    }

    #[test]
    fn error_display_includes_inner() {
        let geo = Fat32Geometry::for_volume_size(FOUR_GIB).unwrap();
        let backend = InMemoryDirTree::from_chains(vec![vec![1]]);
        let err = Fat32Synth::new(geo, b"TEST       ", 0, None, None, &backend).unwrap_err();
        let msg = format!("{err}");
        assert!(msg.contains("FAT-table"), "got: {msg}");
        assert!(msg.contains("reserved cluster 1"), "got: {msg}");
    }

    #[test]
    fn error_source_chain_walks_to_inner_error() {
        let geo = Fat32Geometry::for_volume_size(FOUR_GIB).unwrap();
        let backend = InMemoryDirTree::from_chains(vec![vec![1]]);
        let err = Fat32Synth::new(geo, b"TEST       ", 0, None, None, &backend).unwrap_err();
        let src = std::error::Error::source(&err);
        assert!(src.is_some(), "FatTable variant should expose source");
    }

    // ── Empty / boundary requests ─────────────────────────────────────

    #[test]
    fn empty_read_is_noop() {
        let s = synth_4gib();
        let mut buf: [u8; 0] = [];
        s.read(0, &mut buf).expect("empty read ok");
    }

    #[test]
    fn empty_read_past_eof_is_still_noop() {
        let s = synth_4gib();
        let mut buf: [u8; 0] = [];
        // Per spec, an empty read is a no-op regardless of offset.
        s.read(FOUR_GIB + 1000, &mut buf)
            .expect("empty read past EOF ok");
    }

    #[test]
    fn offset_at_volume_end_is_rejected() {
        let s = synth_4gib();
        let mut buf = [0u8; 1];
        let err = s.read(FOUR_GIB, &mut buf).expect_err("offset at EOF");
        assert!(matches!(
            err,
            Fat32SynthError::OffsetBeyondVolume {
                offset: FOUR_GIB,
                ..
            }
        ));
    }

    #[test]
    fn offset_past_volume_end_is_rejected() {
        let s = synth_4gib();
        let mut buf = [0u8; 1];
        let err = s.read(FOUR_GIB + 1, &mut buf).expect_err("offset past EOF");
        assert!(matches!(err, Fat32SynthError::OffsetBeyondVolume { .. }));
    }

    #[test]
    fn read_extending_past_volume_end_is_rejected() {
        let s = synth_4gib();
        // Last sector starts at FOUR_GIB - 512; reading 513 bytes
        // there extends 1 byte past the volume end.
        let err = s
            .read(FOUR_GIB - 512, &mut [0u8; 513])
            .expect_err("read past EOF");
        assert!(matches!(err, Fat32SynthError::LengthExceedsVolume { .. }));
    }

    #[test]
    fn read_exactly_to_volume_end_succeeds() {
        let s = synth_4gib();
        // Last sector: FOUR_GIB - 512 .. FOUR_GIB.
        let mut buf = vec![0xAAu8; 512];
        s.read(FOUR_GIB - 512, &mut buf).expect("last sector ok");
        // Data region is zero-filled in Phase 2.6 — the last
        // sector is well inside the data region for 4 GiB.
        assert!(buf.iter().all(|&b| b == 0));
    }

    // ── Boot sector reads ────────────────────────────────────────────

    #[test]
    fn full_boot_sector_matches_boot_sector_synthesize() {
        let geo = Fat32Geometry::for_volume_size(FOUR_GIB).expect("geo");
        let expected = boot_sector::synthesize(&geo, b"TESTLABEL  ", 0x1234_5678).expect("boot");
        let s = synth_4gib();
        let mut buf = [0u8; BOOT_SECTOR_SIZE_BYTES];
        s.read(0, &mut buf).expect("boot read ok");
        assert_eq!(buf, expected);
    }

    #[test]
    fn boot_sector_partial_head_matches_first_n_bytes() {
        let geo = Fat32Geometry::for_volume_size(FOUR_GIB).unwrap();
        let expected = boot_sector::synthesize(&geo, b"TESTLABEL  ", 0x1234_5678).unwrap();
        let s = synth_4gib();
        let mut buf = [0u8; 100];
        s.read(0, &mut buf).unwrap();
        assert_eq!(&buf[..], &expected[..100]);
    }

    #[test]
    fn boot_sector_partial_tail_matches_last_n_bytes() {
        let geo = Fat32Geometry::for_volume_size(FOUR_GIB).unwrap();
        let expected = boot_sector::synthesize(&geo, b"TESTLABEL  ", 0x1234_5678).unwrap();
        let s = synth_4gib();
        // Read bytes 256..512 of the boot sector.
        let mut buf = [0u8; 256];
        s.read(256, &mut buf).unwrap();
        assert_eq!(&buf[..], &expected[256..512]);
    }

    #[test]
    fn boot_sector_single_byte_read_matches_signature_low_byte() {
        let s = synth_4gib();
        let mut buf = [0u8; 1];
        // Offset 0x1FE is the start of the 0x55 0xAA end-signature.
        s.read(0x1FE, &mut buf).unwrap();
        assert_eq!(buf[0], 0x55);
    }

    // ── FsInfo reads ─────────────────────────────────────────────────

    #[test]
    fn fsinfo_sector_matches_fsinfo_synthesize() {
        let geo = Fat32Geometry::for_volume_size(FOUR_GIB).unwrap();
        let expected = fsinfo::synthesize(&geo, None, None).unwrap();
        let s = synth_4gib();
        let mut buf = [0u8; FSINFO_SECTOR_SIZE_BYTES];
        s.read(u64::from(FSINFO_SECTOR_INDEX) * SECTOR, &mut buf)
            .unwrap();
        assert_eq!(buf, expected);
    }

    #[test]
    fn read_spanning_boot_into_fsinfo_concatenates_correctly() {
        let geo = Fat32Geometry::for_volume_size(FOUR_GIB).unwrap();
        let boot = boot_sector::synthesize(&geo, b"TESTLABEL  ", 0x1234_5678).unwrap();
        let fsi = fsinfo::synthesize(&geo, None, None).unwrap();
        let s = synth_4gib();
        // Read bytes 500..600 — first 12 from boot, next 88 from FsInfo.
        let mut buf = [0u8; 100];
        s.read(500, &mut buf).unwrap();
        assert_eq!(&buf[..12], &boot[500..512], "boot tail");
        assert_eq!(&buf[12..], &fsi[..88], "fsinfo head");
    }

    // ── Reserved gap ─────────────────────────────────────────────────

    #[test]
    fn reserved_gap_between_fsinfo_and_backup_is_zero() {
        let s = synth_4gib();
        // Sectors 2..6 are reserved zero-fill.
        let mut buf = vec![0xAAu8; 4 * SECTOR_USIZE];
        s.read(2 * SECTOR, &mut buf).unwrap();
        assert!(buf.iter().all(|&b| b == 0), "reserved gap is not zero");
    }

    // ── Backup boot sector ───────────────────────────────────────────

    #[test]
    fn backup_boot_sector_equals_boot_sector() {
        let s = synth_4gib();
        let mut boot = [0u8; BOOT_SECTOR_SIZE_BYTES];
        let mut backup = [0u8; BOOT_SECTOR_SIZE_BYTES];
        s.read(0, &mut boot).unwrap();
        s.read(u64::from(BACKUP_BOOT_SECTOR_INDEX) * SECTOR, &mut backup)
            .unwrap();
        assert_eq!(boot, backup);
    }

    // ── FAT table reads ──────────────────────────────────────────────

    fn fat1_start_bytes(_s: &Fat32Synth) -> u64 {
        // FAT 1 starts at sector RESERVED_SECTORS = 32.
        32 * SECTOR
    }

    #[test]
    fn fat1_first_sector_matches_fat_table_synthesize() {
        let s = synth_4gib();
        let expected = s.fat_table.synthesize_sector(0).unwrap();
        let mut buf = [0u8; FAT_SECTOR_SIZE_BYTES];
        s.read(fat1_start_bytes(&s), &mut buf).unwrap();
        assert_eq!(buf, expected);
    }

    #[test]
    fn fat1_cross_sector_read_concatenates_two_sectors() {
        let s = synth_4gib();
        let sector0 = s.fat_table.synthesize_sector(0).unwrap();
        let sector1 = s.fat_table.synthesize_sector(1).unwrap();
        // Read 256 bytes of sector 0 tail + 256 bytes of sector 1 head.
        let mut buf = [0u8; 512];
        s.read(fat1_start_bytes(&s) + 256, &mut buf).unwrap();
        assert_eq!(&buf[..256], &sector0[256..512], "sector 0 tail");
        assert_eq!(&buf[256..], &sector1[..256], "sector 1 head");
    }

    #[test]
    fn fat2_mirrors_fat1_byte_for_byte() {
        let s = synth_4gib();
        let fat1_offset = fat1_start_bytes(&s);
        let fat_size_bytes = u64::from(s.fat_table.fat_size_sectors()) * SECTOR;
        let fat2_offset = fat1_offset + fat_size_bytes;

        let mut buf1 = vec![0u8; 4 * FAT_SECTOR_SIZE_BYTES];
        let mut buf2 = vec![0u8; 4 * FAT_SECTOR_SIZE_BYTES];
        s.read(fat1_offset, &mut buf1).unwrap();
        s.read(fat2_offset, &mut buf2).unwrap();
        assert_eq!(buf1, buf2, "FAT2 must mirror FAT1");
    }

    #[test]
    fn fat1_to_fat2_boundary_read_spans_two_regions() {
        let s = synth_4gib();
        let fat1_offset = fat1_start_bytes(&s);
        let fat_size_bytes = u64::from(s.fat_table.fat_size_sectors()) * SECTOR;
        let fat2_offset = fat1_offset + fat_size_bytes;
        // Read the last 256 bytes of FAT1 + first 256 bytes of FAT2.
        let last_sector_fat1 = s.fat_table.fat_size_sectors() - 1;
        let last_sector_bytes = s.fat_table.synthesize_sector(last_sector_fat1).unwrap();
        let first_sector_fat2 = s.fat_table.synthesize_sector(0).unwrap();

        let mut buf = [0u8; 512];
        s.read(fat2_offset - 256, &mut buf).unwrap();
        assert_eq!(&buf[..256], &last_sector_bytes[256..], "FAT1 last tail");
        assert_eq!(&buf[256..], &first_sector_fat2[..256], "FAT2 first head");
    }

    #[test]
    fn fat_entries_2_through_n_match_chain_for_root_directory() {
        // ROOT_DIRECTORY_CLUSTER (= 2) is the only chain; FAT[2]
        // should be END_OF_CHAIN_MARKER.
        use crate::fs::fat32::fat_table::END_OF_CHAIN_MARKER;
        let s = synth_4gib();
        let mut buf = [0u8; 4];
        s.read(fat1_start_bytes(&s) + 8, &mut buf).unwrap(); // FAT[2] is at byte offset 8
        let v = u32::from_le_bytes(buf);
        assert_eq!(v, END_OF_CHAIN_MARKER);
    }

    // ── Data region ──────────────────────────────────────────────────

    #[test]
    fn data_region_reads_as_zero_in_phase_2_6() {
        let s = synth_4gib();
        let data_start = s.geometry().first_data_sector() * SECTOR;
        let mut buf = vec![0xCDu8; 4096];
        s.read(data_start, &mut buf).unwrap();
        assert!(buf.iter().all(|&b| b == 0));
    }

    #[test]
    fn data_region_last_sector_reads_as_zero() {
        let s = synth_4gib();
        let mut buf = [0xEEu8; 512];
        s.read(FOUR_GIB - 512, &mut buf).unwrap();
        assert!(buf.iter().all(|&b| b == 0));
    }

    // ── Multi-region traversal ───────────────────────────────────────

    #[test]
    fn full_volume_read_traverses_every_region_without_corruption() {
        // Smallest valid geometry → smallest volume to read in one
        // call; 34 MiB stays well under any reasonable test budget.
        let s = synth_small();
        let size_usize = usize::try_from(SMALL).expect("34 MiB fits");
        let mut buf = vec![0u8; size_usize];
        s.read(0, &mut buf).expect("full-volume read ok");

        // Spot-check region boundaries by comparing against
        // sub-synthesizer outputs:
        let geo = Fat32Geometry::for_volume_size(SMALL).unwrap();
        let expected_boot = boot_sector::synthesize(&geo, b"SMALL      ", 0x0BAD_BEEF).unwrap();
        let expected_fsi = fsinfo::synthesize(&geo, None, None).unwrap();
        assert_eq!(&buf[..512], &expected_boot[..], "boot sector");
        assert_eq!(&buf[512..1024], &expected_fsi[..], "FsInfo sector");
        // Reserved gap (sectors 2..6) is zero.
        assert!(
            buf[2 * 512..6 * 512].iter().all(|&b| b == 0),
            "reserved 2..6 zero"
        );
        // Backup boot at sector 6.
        assert_eq!(
            &buf[6 * 512..7 * 512],
            &expected_boot[..],
            "backup boot sector"
        );
        // Reserved 7..32 is zero.
        assert!(
            buf[7 * 512..32 * 512].iter().all(|&b| b == 0),
            "reserved 7..32 zero"
        );
    }

    #[test]
    fn cross_region_read_from_backup_boot_into_reserved_into_fat() {
        let s = synth_4gib();
        let geo = Fat32Geometry::for_volume_size(FOUR_GIB).unwrap();
        let expected_boot = boot_sector::synthesize(&geo, b"TESTLABEL  ", 0x1234_5678).unwrap();
        let expected_fat0 = s.fat_table.synthesize_sector(0).unwrap();

        // Span: last 256 bytes of backup boot (sector 6) +
        // 25 reserved sectors (7..32) + first 256 bytes of FAT1.
        let backup_start = u64::from(BACKUP_BOOT_SECTOR_INDEX) * SECTOR;
        let read_start = backup_start + 256;
        let total_len = 256 + 25 * 512 + 256;
        let mut buf = vec![0xAFu8; total_len];
        s.read(read_start, &mut buf).unwrap();

        // First 256 bytes = backup boot tail.
        assert_eq!(&buf[..256], &expected_boot[256..512], "backup tail");
        // Next 25 sectors = reserved zero.
        assert!(
            buf[256..256 + 25 * 512].iter().all(|&b| b == 0),
            "reserved zone"
        );
        // Last 256 = FAT1 sector 0 head.
        assert_eq!(
            &buf[256 + 25 * 512..],
            &expected_fat0[..256],
            "FAT1 sector 0 head"
        );
    }

    #[test]
    fn cursor_advances_correctly_across_unequal_region_lengths() {
        // Read every region from offset 0 by issuing one byte at
        // a time for the first 16 bytes (covers boot start), then
        // a 1024-byte read spanning into FsInfo. Verify the first
        // 1040 bytes match the concatenation of the synthesizer
        // outputs.
        let geo = Fat32Geometry::for_volume_size(FOUR_GIB).unwrap();
        let boot = boot_sector::synthesize(&geo, b"TESTLABEL  ", 0x1234_5678).unwrap();
        let fsi = fsinfo::synthesize(&geo, None, None).unwrap();

        let s = synth_4gib();
        for (i, expected) in boot.iter().enumerate().take(16) {
            let mut buf = [0u8; 1];
            s.read(u64::try_from(i).unwrap(), &mut buf).unwrap();
            assert_eq!(buf[0], *expected, "byte {i}");
        }

        let mut buf = [0u8; 1008];
        s.read(16, &mut buf).unwrap();
        assert_eq!(&buf[..512 - 16], &boot[16..512], "boot tail");
        assert_eq!(&buf[512 - 16..], &fsi[..512], "fsinfo full");
    }
}
