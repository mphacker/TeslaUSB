//! Master Boot Record (MBR) partition-table synthesis.
//!
//! ADR-0023 collapses the two separate USB LUNs into a single
//! mass-storage LUN that exposes an MBR partition table with two
//! exFAT partitions, so the vehicle reads `LockChime.wav`,
//! `LightShow/`, and `Boombox/` from the partitions of the *one*
//! device it uses for dashcam (the v1-proven topology). This module
//! owns the pure-compute half of that: planning where each partition
//! lands on the synthesized disk and rendering the 512-byte sector 0.
//!
//! It lives in `teslausb-core::fs` because it is filesystem-adjacent
//! pure logic with no I/O — the same charter layer as the FAT/exFAT
//! geometry planners. The `teslafat` `PartitionedDiskBackend` consumes
//! a [`DiskLayout`] to both serve sector 0 and route every other byte
//! offset into the correct child filesystem backend.
//!
//! ## What is and is not modelled
//!
//! Only the classic 512-byte MBR with up to four *primary* partition
//! entries is modelled — no extended/logical partitions, no GPT.
//! Two exFAT partitions is the whole requirement (ADR-0023), and the
//! 2 TiB ceiling of the 32-bit LBA fields is far above the Pi's SD
//! card. The CHS fields in each entry are written as the
//! "address with LBA instead" sentinel (`0xFE/0xFF/0xFF`): every
//! consumer in this system (the Linux partition scanner and Tesla's
//! firmware) addresses partitions by LBA, and emitting real CHS
//! triples for multi-hundred-GiB partitions is both impossible (they
//! exceed the CHS range) and pointless.
//!
//! No `unsafe` and no panicking indexing — every byte is written via
//! `copy_from_slice` into a fixed-size array (workspace lint
//! `unsafe_code = "deny"`).

use crate::fs::geometry::SECTOR_SIZE_BYTES;

/// Size of the MBR (sector 0) in bytes.
pub const MBR_SIZE_BYTES: usize = SECTOR_SIZE_BYTES as usize;

/// Maximum primary partitions a classic MBR can describe.
pub const MAX_PRIMARY_PARTITIONS: usize = 4;

/// Partition type byte shared by NTFS / exFAT / IFS (`0x07`). Tesla
/// and Linux both mount the exFAT volume found inside such a
/// partition; there is no exFAT-specific MBR type byte.
pub const PARTITION_TYPE_EXFAT: u8 = 0x07;

/// Sectors per MiB at the universal 512-byte logical sector size.
pub const SECTORS_PER_MIB: u32 = (1024 * 1024) / SECTOR_SIZE_BYTES;

/// Default partition alignment: 1 MiB, the modern convention `fdisk`
/// and `parted` use. Aligning partition starts to 1 MiB keeps the
/// exFAT regions on erase-block-friendly boundaries and leaves the
/// first MiB as the reserved area that holds sector 0.
pub const DEFAULT_ALIGNMENT_SECTORS: u32 = SECTORS_PER_MIB;

/// Two-byte boot signature terminating every valid MBR.
const BOOT_SIGNATURE: [u8; 2] = [0x55, 0xAA];

/// Offset of the 4-byte disk signature within the MBR.
const DISK_SIGNATURE_OFFSET: usize = 440;

/// Offset of the first 16-byte partition entry within the MBR.
const PARTITION_TABLE_OFFSET: usize = 446;

/// Size of a single partition entry in bytes.
const PARTITION_ENTRY_SIZE: usize = 16;

/// CHS triple meaning "this entry is addressed by LBA"; written for
/// both the start and end CHS of every partition (see module docs).
const CHS_LBA_SENTINEL: [u8; 3] = [0xFE, 0xFF, 0xFF];

/// Errors from planning or rendering an MBR.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum MbrError {
    /// More partitions were requested than a primary MBR can hold.
    #[error(
        "too many partitions: {count} requested, MBR supports at most {MAX_PRIMARY_PARTITIONS}"
    )]
    TooManyPartitions {
        /// Number of partitions the caller asked for.
        count: usize,
    },

    /// An MBR disk needs at least one partition to be useful.
    #[error("no partitions requested; an MBR disk needs at least one")]
    NoPartitions,

    /// A requested partition had a zero sector count.
    #[error("partition {index} has a zero sector count")]
    ZeroSizedPartition {
        /// Index of the offending partition in the request list.
        index: usize,
    },

    /// The cumulative layout exceeded the 32-bit LBA addressing
    /// limit of the MBR (`2^32` sectors, i.e. 2 TiB at 512-byte
    /// sectors).
    #[error("disk layout overflows the 32-bit MBR LBA space at partition {index}")]
    GeometryOverflow {
        /// Index of the partition whose placement overflowed.
        index: usize,
    },
}

/// A single partition the caller wants placed on the disk.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PartitionRequest {
    /// Length of the partition in 512-byte sectors. Must equal the
    /// size (in sectors) of the child filesystem backend that will
    /// serve this partition's bytes, so the partition table and the
    /// filesystem inside it agree on the volume length.
    pub sector_count: u32,
    /// MBR partition type byte (e.g. [`PARTITION_TYPE_EXFAT`]).
    pub partition_type: u8,
}

/// A partition after placement: where it starts and how long it is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PlannedPartition {
    /// First sector (LBA) of the partition on the synthesized disk.
    pub start_lba: u32,
    /// Length of the partition in 512-byte sectors.
    pub sector_count: u32,
    /// MBR partition type byte.
    pub partition_type: u8,
}

impl PlannedPartition {
    /// Byte offset of the partition's first byte on the disk.
    #[must_use]
    pub const fn start_byte(self) -> u64 {
        self.start_lba as u64 * SECTOR_SIZE_BYTES as u64
    }

    /// Length of the partition in bytes.
    #[must_use]
    pub const fn len_bytes(self) -> u64 {
        self.sector_count as u64 * SECTOR_SIZE_BYTES as u64
    }

    /// Byte offset one past the partition's last byte on the disk.
    #[must_use]
    pub const fn end_byte(self) -> u64 {
        self.start_byte() + self.len_bytes()
    }
}

/// A planned single-disk MBR layout: the disk signature plus the
/// placement of every partition. Produced by [`DiskLayout::plan`];
/// consumed to [`render`](DiskLayout::render_mbr) sector 0 and to
/// route byte offsets in `teslafat`'s `PartitionedDiskBackend`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiskLayout {
    /// 32-bit disk signature written at offset 440 of sector 0.
    pub disk_signature: u32,
    /// Partitions in table order.
    pub partitions: Vec<PlannedPartition>,
}

impl DiskLayout {
    /// Plan a disk that places `requests` back-to-back, each aligned
    /// up to `alignment_sectors`, after a leading reserved gap (one
    /// alignment unit) that holds sector 0.
    ///
    /// The partition sizes are taken verbatim from each request; only
    /// the start LBAs are computed. The first partition starts at
    /// `alignment_sectors` (LBA 2048 with the 1 MiB default), matching
    /// what `fdisk` produces.
    ///
    /// # Errors
    ///
    /// * [`MbrError::NoPartitions`] if `requests` is empty.
    /// * [`MbrError::TooManyPartitions`] if more than
    ///   [`MAX_PRIMARY_PARTITIONS`] are requested.
    /// * [`MbrError::ZeroSizedPartition`] if any request has a zero
    ///   sector count.
    /// * [`MbrError::GeometryOverflow`] if placing a partition would
    ///   push a start or end LBA past `u32::MAX`.
    pub fn plan(
        disk_signature: u32,
        requests: &[PartitionRequest],
        alignment_sectors: u32,
    ) -> Result<Self, MbrError> {
        if requests.is_empty() {
            return Err(MbrError::NoPartitions);
        }
        if requests.len() > MAX_PRIMARY_PARTITIONS {
            return Err(MbrError::TooManyPartitions {
                count: requests.len(),
            });
        }

        let align = alignment_sectors.max(1);
        let mut partitions = Vec::with_capacity(requests.len());
        // First partition begins one alignment unit in, reserving the
        // leading region for sector 0 (and matching fdisk's LBA 2048).
        let mut cursor: u64 = u64::from(align);

        for (index, req) in requests.iter().enumerate() {
            if req.sector_count == 0 {
                return Err(MbrError::ZeroSizedPartition { index });
            }

            let start_lba = align_up_u64(cursor, u64::from(align));
            let end = start_lba + u64::from(req.sector_count);
            // start_lba and the one-past-the-end sector must both be
            // representable; the kernel stores both start and length
            // as u32 and addresses up to start+count.
            if start_lba > u64::from(u32::MAX) || end > u64::from(u32::MAX) {
                return Err(MbrError::GeometryOverflow { index });
            }

            // Casts are guarded by the bound check above.
            #[allow(clippy::cast_possible_truncation)]
            partitions.push(PlannedPartition {
                start_lba: start_lba as u32,
                sector_count: req.sector_count,
                partition_type: req.partition_type,
            });
            cursor = end;
        }

        Ok(Self {
            disk_signature,
            partitions,
        })
    }

    /// Total size of the synthesized disk in bytes: from sector 0
    /// through the last byte of the final partition.
    #[must_use]
    pub fn total_size_bytes(&self) -> u64 {
        self.partitions.last().map_or(0, |p| p.end_byte())
    }

    /// Render sector 0 (the 512-byte MBR) for this layout.
    ///
    /// All offsets are compile-time constants into a fixed
    /// `[u8; 512]`, so the slicing can never panic; the
    /// `indexing_slicing` allow matches the convention used by the
    /// FAT/exFAT boot-sector synthesizers in this crate.
    #[must_use]
    #[allow(clippy::indexing_slicing)]
    pub fn render_mbr(&self) -> [u8; MBR_SIZE_BYTES] {
        let mut mbr = [0u8; MBR_SIZE_BYTES];

        mbr[DISK_SIGNATURE_OFFSET..DISK_SIGNATURE_OFFSET + 4]
            .copy_from_slice(&self.disk_signature.to_le_bytes());

        for (index, part) in self.partitions.iter().enumerate() {
            let base = PARTITION_TABLE_OFFSET + index * PARTITION_ENTRY_SIZE;
            let entry = &mut mbr[base..base + PARTITION_ENTRY_SIZE];
            // byte 0: boot flag — non-bootable; Tesla boots nothing.
            entry[0] = 0x00;
            // bytes 1..4: start CHS — LBA sentinel.
            entry[1..4].copy_from_slice(&CHS_LBA_SENTINEL);
            // byte 4: partition type.
            entry[4] = part.partition_type;
            // bytes 5..8: end CHS — LBA sentinel.
            entry[5..8].copy_from_slice(&CHS_LBA_SENTINEL);
            // bytes 8..12: start LBA (u32 LE).
            entry[8..12].copy_from_slice(&part.start_lba.to_le_bytes());
            // bytes 12..16: sector count (u32 LE).
            entry[12..16].copy_from_slice(&part.sector_count.to_le_bytes());
        }

        let sig = MBR_SIZE_BYTES - 2;
        mbr[sig..].copy_from_slice(&BOOT_SIGNATURE);
        mbr
    }
}

/// Round `value` up to the next multiple of `align` (`align >= 1`).
fn align_up_u64(value: u64, align: u64) -> u64 {
    if align <= 1 {
        return value;
    }
    let rem = value % align;
    if rem == 0 {
        value
    } else {
        value + (align - rem)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::indexing_slicing)]
mod tests {
    use super::*;

    fn req(sector_count: u32) -> PartitionRequest {
        PartitionRequest {
            sector_count,
            partition_type: PARTITION_TYPE_EXFAT,
        }
    }

    fn le_u32(bytes: &[u8]) -> u32 {
        u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]])
    }

    #[test]
    fn rejects_empty_request_list() {
        assert_eq!(
            DiskLayout::plan(0, &[], DEFAULT_ALIGNMENT_SECTORS),
            Err(MbrError::NoPartitions)
        );
    }

    #[test]
    fn rejects_more_than_four_partitions() {
        let five = vec![req(2048); 5];
        assert_eq!(
            DiskLayout::plan(0, &five, DEFAULT_ALIGNMENT_SECTORS),
            Err(MbrError::TooManyPartitions { count: 5 })
        );
    }

    #[test]
    fn rejects_zero_sized_partition() {
        let reqs = [req(2048), req(0)];
        assert_eq!(
            DiskLayout::plan(0, &reqs, DEFAULT_ALIGNMENT_SECTORS),
            Err(MbrError::ZeroSizedPartition { index: 1 })
        );
    }

    #[test]
    fn first_partition_starts_at_one_mib() {
        let layout = DiskLayout::plan(0, &[req(4096)], DEFAULT_ALIGNMENT_SECTORS).unwrap();
        assert_eq!(layout.partitions[0].start_lba, SECTORS_PER_MIB);
        assert_eq!(layout.partitions[0].start_lba, 2048);
    }

    #[test]
    fn second_partition_is_aligned_after_the_first() {
        // part1 = 5000 sectors → ends at 2048+5000 = 7048, which is
        // not 2048-aligned, so part2 must round up to 8192.
        let layout =
            DiskLayout::plan(0, &[req(5000), req(4096)], DEFAULT_ALIGNMENT_SECTORS).unwrap();
        assert_eq!(layout.partitions[0].start_lba, 2048);
        assert_eq!(layout.partitions[1].start_lba, 8192);
        // No overlap: part1 end (7048) < part2 start (8192).
        assert!(layout.partitions[0].end_byte() <= layout.partitions[1].start_byte());
    }

    #[test]
    fn total_size_reaches_end_of_last_partition() {
        let layout =
            DiskLayout::plan(0, &[req(2048), req(4096)], DEFAULT_ALIGNMENT_SECTORS).unwrap();
        // part2 starts at 4096, length 4096 → ends at sector 8192.
        assert_eq!(layout.partitions[1].start_lba, 4096);
        assert_eq!(
            layout.total_size_bytes(),
            8192 * u64::from(SECTOR_SIZE_BYTES)
        );
    }

    #[test]
    fn detects_lba_overflow() {
        // A partition whose end would exceed u32::MAX sectors.
        let huge = req(u32::MAX);
        let err = DiskLayout::plan(0, &[huge], DEFAULT_ALIGNMENT_SECTORS).unwrap_err();
        assert_eq!(err, MbrError::GeometryOverflow { index: 0 });
    }

    #[test]
    fn rendered_mbr_has_boot_signature() {
        let layout =
            DiskLayout::plan(0xDEAD_BEEF, &[req(2048)], DEFAULT_ALIGNMENT_SECTORS).unwrap();
        let mbr = layout.render_mbr();
        assert_eq!(mbr.len(), 512);
        assert_eq!(mbr[510], 0x55);
        assert_eq!(mbr[511], 0xAA);
    }

    #[test]
    fn rendered_mbr_carries_disk_signature() {
        let layout =
            DiskLayout::plan(0x1234_5678, &[req(2048)], DEFAULT_ALIGNMENT_SECTORS).unwrap();
        let mbr = layout.render_mbr();
        assert_eq!(le_u32(&mbr[440..444]), 0x1234_5678);
    }

    #[test]
    fn rendered_partition_entries_are_byte_exact() {
        let layout = DiskLayout::plan(
            0,
            &[req(0x0010_0000), req(0x0002_0000)],
            DEFAULT_ALIGNMENT_SECTORS,
        )
        .unwrap();
        let mbr = layout.render_mbr();

        // Entry 0 at offset 446.
        let e0 = &mbr[446..462];
        assert_eq!(e0[0], 0x00, "non-bootable");
        assert_eq!(&e0[1..4], &CHS_LBA_SENTINEL, "start CHS sentinel");
        assert_eq!(e0[4], PARTITION_TYPE_EXFAT, "type byte");
        assert_eq!(&e0[5..8], &CHS_LBA_SENTINEL, "end CHS sentinel");
        assert_eq!(le_u32(&e0[8..12]), 2048, "start LBA");
        assert_eq!(le_u32(&e0[12..16]), 0x0010_0000, "sector count");

        // Entry 1 at offset 462. part1 spans 2048..(2048+0x100000)=
        // 1050624, which is already 2048-aligned (2048*513), so part2
        // starts there with no realignment gap.
        let e1 = &mbr[462..478];
        assert_eq!(e1[4], PARTITION_TYPE_EXFAT);
        assert_eq!(le_u32(&e1[8..12]), 1_050_624, "part2 start LBA");
        assert_eq!(le_u32(&e1[12..16]), 0x0002_0000, "part2 sector count");

        // Entries 2 and 3 are absent → all zero.
        assert!(
            mbr[478..510].iter().all(|&b| b == 0),
            "unused entries zeroed"
        );
    }

    #[test]
    fn realistic_256gib_plus_32gib_layout_fits() {
        // 256 GiB and 32 GiB at 512-byte sectors.
        let cam = 256u64 * 1024 * 1024 * 1024 / u64::from(SECTOR_SIZE_BYTES);
        let media = 32u64 * 1024 * 1024 * 1024 / u64::from(SECTOR_SIZE_BYTES);
        #[allow(clippy::cast_possible_truncation)]
        let layout = DiskLayout::plan(
            0,
            &[req(cam as u32), req(media as u32)],
            DEFAULT_ALIGNMENT_SECTORS,
        )
        .unwrap();
        assert_eq!(layout.partitions.len(), 2);
        // Both GiB sizes are 1 MiB-multiples, so no realignment gap
        // beyond the leading 1 MiB.
        assert_eq!(layout.partitions[0].start_lba, 2048);
        assert_eq!(u64::from(layout.partitions[1].start_lba), 2048 + cam);
        assert_eq!(layout.total_size_bytes(), (2048 + cam + media) * 512);
    }

    #[test]
    fn align_up_is_correct() {
        assert_eq!(align_up_u64(0, 2048), 0);
        assert_eq!(align_up_u64(1, 2048), 2048);
        assert_eq!(align_up_u64(2048, 2048), 2048);
        assert_eq!(align_up_u64(2049, 2048), 4096);
        assert_eq!(align_up_u64(7048, 2048), 8192);
        assert_eq!(align_up_u64(100, 1), 100);
    }
}
