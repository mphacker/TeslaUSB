//! [`PartitionedDiskBackend`] — composes child filesystem backends
//! behind a single synthesized MBR-partitioned disk.
//!
//! ADR-0023 replaces the two separate USB LUNs (each a whole-disk
//! filesystem on its own `/dev/nbdN`) with one mass-storage LUN that
//! exposes an MBR partition table holding two exFAT partitions, so the
//! vehicle reads `LockChime.wav`, `LightShow/`, and `Boombox/` from the
//! partitions of the *one* device it uses for dashcam — the v1-proven
//! topology that the hardware test on 2026-06-01 confirmed is required.
//!
//! This backend is the routing half of that design. It owns:
//!
//! * the rendered 512-byte MBR (sector 0), produced from a
//!   [`DiskLayout`] in `teslausb-core`, and
//! * an ordered list of child backends — one per partition — each
//!   serving the bytes *inside* its partition starting from child
//!   offset 0.
//!
//! Every NBD byte offset is routed: sector 0 and the inter-partition
//! alignment gaps are served synthetically (MBR bytes / zeros); offsets
//! that fall inside a partition are translated to a child-relative
//! offset and delegated. A single request that straddles a region
//! boundary is split at the boundary and each sub-range routed
//! independently, so the all-or-nothing [`BlockBackend`] contract holds
//! regardless of how the host slices its reads.
//!
//! ## Why composition (no FS-synth changes)
//!
//! The child type is generic (`B: BlockBackend`) and is, in
//! production, the existing `ReloadableBackend` wrapping a
//! `SynthBackend`. The partition router is pure composition over the
//! [`BlockBackend`] seam: the FAT/exFAT synthesis layer is untouched,
//! and per-partition SIGHUP live-reload keeps working because each
//! child is still an independent `ReloadableBackend`.
//!
//! ## Write routing
//!
//! Writes inside a partition are delegated to the child. Writes that
//! land in the MBR or an alignment gap are accepted and ignored: Tesla
//! never repartitions the disk, and silently absorbing a stray
//! metadata write (e.g. a host probing sector 0) is safer for the
//! "USB must always accept writes" invariant than surfacing an NBD
//! error. Such writes are logged at debug for observability.

use teslausb_core::backend::{BackendError, BackendResult, BlockBackend, WriteFlags, check_bounds};
use teslausb_core::fs::mbr::{DiskLayout, MBR_SIZE_BYTES};
use tracing::debug;

/// Errors constructing a [`PartitionedDiskBackend`].
///
/// Routing/runtime errors surface as [`BackendError`]; these are
/// startup-time wiring mistakes that must abort daemon boot.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum PartitionedDiskError {
    /// The number of child backends did not match the number of
    /// planned partitions.
    #[error("partition/child count mismatch: {partitions} partitions but {children} children")]
    ChildCountMismatch {
        /// Partitions in the layout.
        partitions: usize,
        /// Child backends supplied.
        children: usize,
    },

    /// A child backend's reported size did not equal the partition it
    /// was paired with. The partition table and the filesystem inside
    /// it would disagree on the volume length, so this is rejected at
    /// construction rather than producing a subtly corrupt disk.
    #[error(
        "partition {index} size mismatch: partition is {partition_bytes} bytes \
         but child backend reports {child_bytes} bytes"
    )]
    SizeMismatch {
        /// Index of the offending partition.
        index: usize,
        /// Partition length in bytes from the layout.
        partition_bytes: u64,
        /// Size the child backend reported.
        child_bytes: u64,
    },
}

/// One partition's placement plus the backend that serves it.
struct PartitionSlot<B> {
    /// Absolute byte offset of the partition's first byte on the disk.
    start: u64,
    /// Length of the partition in bytes.
    len: u64,
    /// Backend serving the partition's interior (child offset 0..len).
    backend: B,
}

impl<B> PartitionSlot<B> {
    /// One past the partition's last byte on the disk.
    fn end(&self) -> u64 {
        self.start + self.len
    }
}

/// Where a given disk offset falls.
enum Region {
    /// Inside partition `index`; the child should be read/written at
    /// `child_offset`, and `avail` bytes remain before the partition
    /// ends.
    Partition {
        index: usize,
        child_offset: u64,
        avail: u64,
    },
    /// Outside every partition (sector 0 or an alignment gap); `avail`
    /// bytes remain before the next partition (or end of disk).
    Synthetic { avail: u64 },
}

/// A [`BlockBackend`] that presents a single MBR-partitioned disk over
/// `N` child filesystem backends.
pub struct PartitionedDiskBackend<B> {
    /// Rendered sector 0.
    mbr: [u8; MBR_SIZE_BYTES],
    /// Partitions in table order (ascending, non-overlapping).
    slots: Vec<PartitionSlot<B>>,
    /// Total disk size in bytes (constant for the NBD handshake).
    total: u64,
}

impl<B: BlockBackend> PartitionedDiskBackend<B> {
    /// Compose `children` into a partitioned disk described by
    /// `layout`. The `children` must be in the same order as
    /// `layout.partitions`, and each child's [`BlockBackend::size`]
    /// must equal its partition's length.
    ///
    /// # Errors
    ///
    /// * [`PartitionedDiskError::ChildCountMismatch`] if the counts
    ///   differ.
    /// * [`PartitionedDiskError::SizeMismatch`] if any child's size
    ///   does not equal its partition length.
    pub fn new(layout: &DiskLayout, children: Vec<B>) -> Result<Self, PartitionedDiskError> {
        if layout.partitions.len() != children.len() {
            return Err(PartitionedDiskError::ChildCountMismatch {
                partitions: layout.partitions.len(),
                children: children.len(),
            });
        }

        let mut slots = Vec::with_capacity(children.len());
        for (index, (planned, backend)) in layout.partitions.iter().zip(children).enumerate() {
            let partition_bytes = planned.len_bytes();
            let child_bytes = backend.size();
            if child_bytes != partition_bytes {
                return Err(PartitionedDiskError::SizeMismatch {
                    index,
                    partition_bytes,
                    child_bytes,
                });
            }
            slots.push(PartitionSlot {
                start: planned.start_byte(),
                len: partition_bytes,
                backend,
            });
        }

        Ok(Self {
            mbr: layout.render_mbr(),
            slots,
            total: layout.total_size_bytes(),
        })
    }

    /// Classify the disk offset `at` (which must be `< self.total`).
    fn locate(&self, at: u64) -> Region {
        for (index, slot) in self.slots.iter().enumerate() {
            if at >= slot.start && at < slot.end() {
                return Region::Partition {
                    index,
                    child_offset: at - slot.start,
                    avail: slot.end() - at,
                };
            }
        }
        // Not in any partition: a synthetic region. It extends until
        // the next partition that starts after `at`, or end of disk.
        let next_boundary = self
            .slots
            .iter()
            .map(|s| s.start)
            .filter(|&start| start > at)
            .min()
            .unwrap_or(self.total);
        Region::Synthetic {
            avail: next_boundary - at,
        }
    }

    /// Fill `buf` for a synthetic region beginning at disk offset
    /// `at`: bytes within sector 0 come from the rendered MBR, every
    /// other synthetic byte is zero.
    fn fill_synthetic(&self, at: u64, buf: &mut [u8]) {
        for (i, out) in buf.iter_mut().enumerate() {
            // `at + i` is bounded by `self.total`, far below usize::MAX
            // on the 64-bit Pi; the fallback keeps it total.
            let pos = at.saturating_add(i as u64);
            *out = usize::try_from(pos)
                .ok()
                .filter(|&p| p < MBR_SIZE_BYTES)
                .and_then(|p| self.mbr.get(p).copied())
                .unwrap_or(0);
        }
    }
}

/// Narrow a `u64` byte count to the `usize` chunk we can act on this
/// iteration, saturating instead of panicking on a 32-bit host.
fn clamp_to_usize(value: u64) -> usize {
    usize::try_from(value).unwrap_or(usize::MAX)
}

impl<B: BlockBackend> BlockBackend for PartitionedDiskBackend<B> {
    fn size(&self) -> u64 {
        self.total
    }

    #[allow(clippy::indexing_slicing)] // every split is clamped to `remaining`
    async fn read(&self, offset: u64, buf: &mut [u8]) -> BackendResult<()> {
        check_bounds(offset, buf.len(), self.total)?;
        let mut filled = 0usize;
        while filled < buf.len() {
            let cur = offset + filled as u64;
            let remaining = buf.len() - filled;
            match self.locate(cur) {
                Region::Partition {
                    index,
                    child_offset,
                    avail,
                } => {
                    let take = remaining.min(clamp_to_usize(avail));
                    let slot = self.slots.get(index).ok_or(BackendError::InvalidArgument(
                        "partition index out of range",
                    ))?;
                    slot.backend
                        .read(child_offset, &mut buf[filled..filled + take])
                        .await?;
                    filled += take;
                }
                Region::Synthetic { avail } => {
                    let take = remaining.min(clamp_to_usize(avail));
                    self.fill_synthetic(cur, &mut buf[filled..filled + take]);
                    filled += take;
                }
            }
        }
        Ok(())
    }

    #[allow(clippy::indexing_slicing)] // every split is clamped to `remaining`
    async fn write(&self, offset: u64, buf: &[u8], flags: WriteFlags) -> BackendResult<()> {
        check_bounds(offset, buf.len(), self.total)?;
        let mut written = 0usize;
        while written < buf.len() {
            let cur = offset + written as u64;
            let remaining = buf.len() - written;
            match self.locate(cur) {
                Region::Partition {
                    index,
                    child_offset,
                    avail,
                } => {
                    let take = remaining.min(clamp_to_usize(avail));
                    let slot = self.slots.get(index).ok_or(BackendError::InvalidArgument(
                        "partition index out of range",
                    ))?;
                    slot.backend
                        .write(child_offset, &buf[written..written + take], flags)
                        .await?;
                    written += take;
                }
                Region::Synthetic { avail } => {
                    let take = remaining.min(clamp_to_usize(avail));
                    // Tesla never repartitions; absorb stray MBR/gap
                    // writes rather than fail the host write.
                    debug!(
                        offset = cur,
                        len = take,
                        "ignoring write to MBR/partition-gap region"
                    );
                    written += take;
                }
            }
        }
        Ok(())
    }

    async fn flush(&self) -> BackendResult<()> {
        for slot in &self.slots {
            slot.backend.flush().await?;
        }
        Ok(())
    }
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::indexing_slicing,
    clippy::cast_possible_truncation
)]
mod tests {
    use super::*;
    use teslausb_core::backend::mock::{MockBackend, MockOp, NullBackend};
    use teslausb_core::fs::mbr::{
        DEFAULT_ALIGNMENT_SECTORS, PARTITION_TYPE_EXFAT, PartitionRequest,
    };

    const SECTOR: u64 = 512;

    fn req(sectors: u32) -> PartitionRequest {
        PartitionRequest {
            sector_count: sectors,
            partition_type: PARTITION_TYPE_EXFAT,
        }
    }

    /// Two small partitions (in sectors) → layout + `NullBackend`
    /// children sized to match.
    fn two_part_disk(
        p1_sectors: u32,
        p2_sectors: u32,
    ) -> (DiskLayout, PartitionedDiskBackend<NullBackend>) {
        let layout = DiskLayout::plan(
            0xABCD_1234,
            &[req(p1_sectors), req(p2_sectors)],
            DEFAULT_ALIGNMENT_SECTORS,
        )
        .unwrap();
        let children = vec![
            NullBackend::new(clamp_to_usize(u64::from(p1_sectors) * SECTOR)),
            NullBackend::new(clamp_to_usize(u64::from(p2_sectors) * SECTOR)),
        ];
        let disk = PartitionedDiskBackend::new(&layout, children).unwrap();
        (layout, disk)
    }

    #[test]
    fn rejects_child_count_mismatch() {
        let layout =
            DiskLayout::plan(0, &[req(2048), req(2048)], DEFAULT_ALIGNMENT_SECTORS).unwrap();
        let result = PartitionedDiskBackend::new(&layout, vec![NullBackend::new(2048 * 512)]);
        assert_eq!(
            result.err(),
            Some(PartitionedDiskError::ChildCountMismatch {
                partitions: 2,
                children: 1
            })
        );
    }

    #[test]
    fn rejects_size_mismatch() {
        let layout = DiskLayout::plan(0, &[req(2048)], DEFAULT_ALIGNMENT_SECTORS).unwrap();
        // Child is one sector too small.
        let result = PartitionedDiskBackend::new(&layout, vec![NullBackend::new(2047 * 512)]);
        assert!(matches!(
            result.err(),
            Some(PartitionedDiskError::SizeMismatch { index: 0, .. })
        ));
    }

    #[test]
    fn size_is_total_disk() {
        let (layout, disk) = two_part_disk(2048, 4096);
        assert_eq!(disk.size(), layout.total_size_bytes());
        // 2048 (gap) + 2048 (p1) + 4096 (p2) sectors.
        assert_eq!(disk.size(), (2048 + 2048 + 4096) * SECTOR);
    }

    #[tokio::test]
    async fn sector_zero_reads_back_the_mbr() {
        let (layout, disk) = two_part_disk(2048, 2048);
        let mut buf = vec![0u8; 512];
        disk.read(0, &mut buf).await.unwrap();
        assert_eq!(buf, layout.render_mbr());
        // Boot signature present.
        assert_eq!(&buf[510..512], &[0x55, 0xAA]);
    }

    #[tokio::test]
    async fn reserved_gap_after_mbr_reads_zeros() {
        let (_layout, disk) = two_part_disk(2048, 2048);
        // Offset 512 up to partition 1 start (2048*512) is the
        // reserved gap and must be zeros.
        let mut buf = vec![0xFFu8; 4096];
        disk.read(512, &mut buf).await.unwrap();
        assert!(buf.iter().all(|&b| b == 0), "reserved gap must be zero");
    }

    #[tokio::test]
    async fn partition_writes_route_to_the_right_child_and_read_back() {
        let (layout, disk) = two_part_disk(2048, 4096);
        let p1_start = layout.partitions[0].start_byte();
        let p2_start = layout.partitions[1].start_byte();

        let pattern1 = vec![0xA1u8; 1024];
        let pattern2 = vec![0xB2u8; 2048];
        disk.write(p1_start, &pattern1, WriteFlags::default())
            .await
            .unwrap();
        disk.write(p2_start, &pattern2, WriteFlags::default())
            .await
            .unwrap();

        let mut got1 = vec![0u8; 1024];
        let mut got2 = vec![0u8; 2048];
        disk.read(p1_start, &mut got1).await.unwrap();
        disk.read(p2_start, &mut got2).await.unwrap();
        assert_eq!(got1, pattern1, "partition 1 payload");
        assert_eq!(got2, pattern2, "partition 2 payload");

        // Partition 1's bytes must NOT have leaked into partition 2.
        let mut p2_head = vec![0xFFu8; 16];
        disk.read(p2_start, &mut p2_head).await.unwrap();
        assert!(p2_head.iter().all(|&b| b == 0xB2));
    }

    #[tokio::test]
    async fn child_offset_is_partition_relative() {
        // A write near the end of partition 1 must hit the child at a
        // partition-relative offset, not the absolute disk offset.
        let (layout, disk) = two_part_disk(2048, 2048);
        let p1 = &layout.partitions[0];
        let near_end = p1.start_byte() + p1.len_bytes() - 512;
        let payload = vec![0x7Eu8; 512];
        disk.write(near_end, &payload, WriteFlags::default())
            .await
            .unwrap();

        let mut got = vec![0u8; 512];
        disk.read(near_end, &mut got).await.unwrap();
        assert_eq!(got, payload);
    }

    #[tokio::test]
    async fn read_straddling_gap_and_partition_boundary_is_split() {
        // Read the last 512 bytes of the reserved gap plus the first
        // 512 bytes of partition 1: the gap half is zeros, the
        // partition half is whatever we wrote.
        let (layout, disk) = two_part_disk(2048, 2048);
        let p1_start = layout.partitions[0].start_byte();
        let marker = vec![0x5Au8; 512];
        disk.write(p1_start, &marker, WriteFlags::default())
            .await
            .unwrap();

        let mut buf = vec![0xFFu8; 1024];
        disk.read(p1_start - 512, &mut buf).await.unwrap();
        assert!(buf[..512].iter().all(|&b| b == 0), "gap half zero");
        assert!(buf[512..].iter().all(|&b| b == 0x5A), "partition half");
    }

    #[tokio::test]
    async fn writes_to_mbr_region_are_ignored_not_errored() {
        let (layout, disk) = two_part_disk(2048, 2048);
        // Writing to sector 0 must succeed (accepted-and-ignored) and
        // must not change what reads back.
        let junk = vec![0x99u8; 512];
        disk.write(0, &junk, WriteFlags::default()).await.unwrap();
        let mut buf = vec![0u8; 512];
        disk.read(0, &mut buf).await.unwrap();
        assert_eq!(buf, layout.render_mbr(), "MBR unchanged by ignored write");
    }

    #[tokio::test]
    async fn out_of_bounds_read_is_rejected() {
        let (_layout, disk) = two_part_disk(2048, 2048);
        let mut buf = vec![0u8; 512];
        let err = disk.read(disk.size(), &mut buf).await.unwrap_err();
        assert!(matches!(err, BackendError::OutOfBounds { .. }));
    }

    #[tokio::test]
    async fn flush_fans_out_to_every_child() {
        let layout =
            DiskLayout::plan(0, &[req(2048), req(2048)], DEFAULT_ALIGNMENT_SECTORS).unwrap();
        let children = vec![MockBackend::new(2048 * 512), MockBackend::new(2048 * 512)];
        let disk = PartitionedDiskBackend::new(&layout, children).unwrap();
        disk.flush().await.unwrap();
        for slot in &disk.slots {
            assert!(
                slot.backend.ops().contains(&MockOp::Flush),
                "each child must receive flush"
            );
        }
    }
}
