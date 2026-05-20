//! `exFAT` allocation bitmap synthesizer.
//!
//! Phase 2.9 of the B-1 rewrite. This module turns a set of
//! allocated cluster ranges into the on-disk **allocation bitmap**
//! that lives inside the cluster heap and records, for every
//! cluster in `2..(2 + ClusterCount)`, whether that cluster is
//! free or in use.
//!
//! ## Specification anchor
//!
//! Microsoft `exFAT` File System Specification v1.00 (August 27,
//! 2019). §7.1 Allocation Bitmap Directory Entry,
//! §7.1.5 Allocation Bitmap (the data stream the entry points to).
//!
//! ## Bit packing
//!
//! Each cluster contributes one bit; bytes are little-endian
//! (the spec calls this "little-endian bit order"):
//!
//! ```text
//! cluster 2  ─→ byte 0, bit 0 (mask 0x01)
//! cluster 3  ─→ byte 0, bit 1 (mask 0x02)
//! cluster 4  ─→ byte 0, bit 2 (mask 0x04)
//! …
//! cluster 9  ─→ byte 0, bit 7 (mask 0x80)
//! cluster 10 ─→ byte 1, bit 0 (mask 0x01)
//! …
//! ```
//!
//! Bit value `0` means the cluster is **free**; `1` means
//! **allocated**.
//!
//! Bitmap size in bytes equals `(ClusterCount + 7) / 8`. Trailing
//! bits in the last byte (those beyond `2 + ClusterCount - 1`) are
//! reserved by the spec and must be written as zero.
//!
//! ## Why a stateful builder?
//!
//! The bitmap is built up by the directory-tree backend (Phase
//! 2.10) marking each cluster it allocates. A stateful builder
//! matches that incremental usage and keeps the API surface
//! discoverable (`new_empty` + `mark_allocated` + `bytes`).
//! It also lets the Phase 2.11 dispatcher serve sector-sized
//! reads directly out of the owned [`Vec<u8>`] without re-running
//! the build.

use core::fmt;
use core::ops::Range;

use crate::fs::exfat::geometry::{ExfatGeometry, FIRST_CLUSTER_NUMBER};
use crate::fs::geometry::{Geometry, SECTOR_SIZE_BYTES};

/// Number of bits each `u8` of the bitmap encodes.
pub const BITS_PER_BYTE: u32 = 8;

/// Errors returned by [`AllocationBitmap`] mutators / queries.
#[derive(Debug, PartialEq, Eq)]
pub enum AllocationBitmapError {
    /// Caller asked about a cluster outside the valid range
    /// `[FIRST_CLUSTER_NUMBER, FIRST_CLUSTER_NUMBER + cluster_count)`.
    ClusterOutOfRange {
        /// The offending cluster number.
        cluster: u32,
        /// First valid cluster number ([`FIRST_CLUSTER_NUMBER`]).
        first_valid: u32,
        /// One past the last valid cluster number.
        end_valid: u32,
    },
    /// A range was supplied whose end overflows `u32` arithmetic.
    /// Almost never reachable in practice — the geometry caps the
    /// cluster count well below `u32::MAX` — but the variant exists
    /// so the mutator's signature is total.
    RangeOverflow {
        /// First cluster the caller tried to mark.
        first_cluster: u32,
        /// Number of clusters the caller tried to mark.
        count: u32,
    },
}

impl fmt::Display for AllocationBitmapError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ClusterOutOfRange {
                cluster,
                first_valid,
                end_valid,
            } => write!(
                f,
                "cluster {cluster} is outside the valid range [{first_valid}, {end_valid})"
            ),
            Self::RangeOverflow {
                first_cluster,
                count,
            } => write!(
                f,
                "cluster range starting at {first_cluster} for {count} entries overflows u32"
            ),
        }
    }
}

impl core::error::Error for AllocationBitmapError {}

/// `exFAT` allocation bitmap — one bit per cluster in the cluster
/// heap.
///
/// Construct with [`AllocationBitmap::new_empty`] (all clusters
/// free); mark allocations with [`AllocationBitmap::mark_allocated`]
/// or [`AllocationBitmap::mark_range_allocated`]; serve to the
/// Phase 2.11 dispatcher via [`AllocationBitmap::bytes`].
#[derive(Debug, Clone)]
pub struct AllocationBitmap {
    bytes: Vec<u8>,
    cluster_count: u32,
}

impl AllocationBitmap {
    /// Build an all-free bitmap sized for `geometry`'s cluster
    /// count.
    ///
    /// Allocates `(cluster_count + 7) / 8` bytes, zero-filled.
    #[must_use]
    pub fn new_empty(geometry: &ExfatGeometry) -> Self {
        let cluster_count = geometry.cluster_count();
        let size_bytes = Self::size_bytes_for(cluster_count);
        let bytes = vec![0_u8; size_bytes];
        Self {
            bytes,
            cluster_count,
        }
    }

    /// Mark a single cluster as allocated (set its bit to `1`).
    ///
    /// # Errors
    ///
    /// * [`AllocationBitmapError::ClusterOutOfRange`] if `cluster`
    ///   is outside `[FIRST_CLUSTER_NUMBER, FIRST_CLUSTER_NUMBER + cluster_count)`.
    pub fn mark_allocated(&mut self, cluster: u32) -> Result<(), AllocationBitmapError> {
        let (byte_index, bit_mask) = self.locate(cluster)?;
        self.set_bit(byte_index, bit_mask);
        Ok(())
    }

    /// Mark `count` consecutive clusters starting at `first_cluster`
    /// as allocated. Equivalent to calling
    /// [`Self::mark_allocated`] in a loop but does the range check
    /// once and walks bytes contiguously.
    ///
    /// `count == 0` is a no-op and returns `Ok(())` even if
    /// `first_cluster` itself would be out of range — the empty
    /// range is vacuously in-range.
    ///
    /// # Errors
    ///
    /// * [`AllocationBitmapError::RangeOverflow`] if
    ///   `first_cluster + count` overflows `u32`.
    /// * [`AllocationBitmapError::ClusterOutOfRange`] if any
    ///   cluster in the requested range is outside the valid
    ///   cluster range.
    pub fn mark_range_allocated(
        &mut self,
        first_cluster: u32,
        count: u32,
    ) -> Result<(), AllocationBitmapError> {
        if count == 0 {
            return Ok(());
        }
        let end = first_cluster
            .checked_add(count)
            .ok_or(AllocationBitmapError::RangeOverflow {
                first_cluster,
                count,
            })?;
        // Validate both endpoints first so a partial mark cannot
        // leak through if the range straddles the upper bound.
        self.validate_cluster(first_cluster)?;
        self.validate_cluster(end - 1)?;
        for cluster in first_cluster..end {
            let (byte_index, bit_mask) =
                Self::locate_for(cluster, self.cluster_count, self.bytes.len())?;
            self.set_bit(byte_index, bit_mask);
        }
        Ok(())
    }

    /// Query a single cluster's allocation state.
    ///
    /// # Errors
    ///
    /// * [`AllocationBitmapError::ClusterOutOfRange`] if `cluster`
    ///   is outside the valid range.
    pub fn is_allocated(&self, cluster: u32) -> Result<bool, AllocationBitmapError> {
        let (byte_index, bit_mask) = self.locate(cluster)?;
        Ok(self.get_bit(byte_index, bit_mask))
    }

    /// Borrow the underlying byte buffer.
    ///
    /// Length equals [`Self::size_bytes`]. Suitable for serving
    /// directly to the Phase 2.11 read dispatcher.
    #[must_use]
    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }

    /// Size of the bitmap in bytes — `(cluster_count + 7) / 8`.
    #[must_use]
    pub fn size_bytes(&self) -> u64 {
        self.bytes.len() as u64
    }

    /// Size of the bitmap rounded up to whole clusters of
    /// `geometry`.
    ///
    /// Cluster heap allocations are quantised to whole clusters,
    /// so the bitmap's storage footprint within the heap is this
    /// value, even though only [`Self::size_bytes`] bytes are
    /// meaningful.
    #[must_use]
    pub fn size_clusters(&self, geometry: &ExfatGeometry) -> u32 {
        let bytes_per_cluster = u64::from(geometry.bytes_per_cluster());
        // size_bytes is at most (u32::MAX + 7)/8 ≈ 5e8, which fits
        // in u32 with room to spare.
        let raw = self.size_bytes().div_ceil(bytes_per_cluster);
        u32::try_from(raw).unwrap_or(u32::MAX)
    }

    /// Cluster count this bitmap was sized for.
    #[must_use]
    pub fn cluster_count(&self) -> u32 {
        self.cluster_count
    }

    /// Compute the byte length required for a bitmap covering
    /// `cluster_count` clusters.
    #[must_use]
    pub fn size_bytes_for(cluster_count: u32) -> usize {
        // (cluster_count + 7) / 8, ceiling division.
        let bits = u64::from(cluster_count);
        let bytes = bits.div_ceil(u64::from(BITS_PER_BYTE));
        usize::try_from(bytes).unwrap_or(usize::MAX)
    }

    fn validate_cluster(&self, cluster: u32) -> Result<(), AllocationBitmapError> {
        let first = FIRST_CLUSTER_NUMBER;
        let end = first.saturating_add(self.cluster_count);
        if cluster < first || cluster >= end {
            return Err(AllocationBitmapError::ClusterOutOfRange {
                cluster,
                first_valid: first,
                end_valid: end,
            });
        }
        Ok(())
    }

    fn locate(&self, cluster: u32) -> Result<(usize, u8), AllocationBitmapError> {
        Self::locate_for(cluster, self.cluster_count, self.bytes.len())
    }

    fn locate_for(
        cluster: u32,
        cluster_count: u32,
        buf_len: usize,
    ) -> Result<(usize, u8), AllocationBitmapError> {
        let first = FIRST_CLUSTER_NUMBER;
        let end = first.saturating_add(cluster_count);
        if cluster < first || cluster >= end {
            return Err(AllocationBitmapError::ClusterOutOfRange {
                cluster,
                first_valid: first,
                end_valid: end,
            });
        }
        let bit_index = cluster - first;
        let byte_index = (bit_index / BITS_PER_BYTE) as usize;
        let bit_in_byte = (bit_index % BITS_PER_BYTE) as u8;
        // Defense-in-depth: every successful validate_cluster keeps
        // `byte_index` strictly less than the buffer length, but the
        // check makes the index access self-evidently in-range.
        if byte_index >= buf_len {
            return Err(AllocationBitmapError::ClusterOutOfRange {
                cluster,
                first_valid: first,
                end_valid: end,
            });
        }
        Ok((byte_index, 1_u8 << bit_in_byte))
    }

    #[allow(clippy::indexing_slicing)] // bounds checked by locate()
    fn set_bit(&mut self, byte_index: usize, bit_mask: u8) {
        self.bytes[byte_index] |= bit_mask;
    }

    #[allow(clippy::indexing_slicing)] // bounds checked by locate()
    fn get_bit(&self, byte_index: usize, bit_mask: u8) -> bool {
        (self.bytes[byte_index] & bit_mask) != 0
    }
}

/// Convenience: convert an inclusive cluster range into an
/// `mark_range_allocated` call.
///
/// Returns the same error as the underlying mutator. Provided
/// because directory-tree code (Phase 2.10) thinks in
/// `Range<u32>`s.
///
/// # Errors
///
/// See [`AllocationBitmap::mark_range_allocated`].
pub fn mark_range(
    bitmap: &mut AllocationBitmap,
    range: Range<u32>,
) -> Result<(), AllocationBitmapError> {
    if range.start >= range.end {
        return Ok(());
    }
    let count = range.end - range.start;
    bitmap.mark_range_allocated(range.start, count)
}

/// Compile-time invariant: `BITS_PER_BYTE` matches the spec.
const _: () = {
    assert!(BITS_PER_BYTE == 8);
    // A FAT sector and our bitmap byte ordering both speak
    // little-endian; the bit-mask `1 << n` matches that.
    assert!(SECTOR_SIZE_BYTES == 512);
};

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

    const MIB: u64 = 1024 * 1024;
    const GIB: u64 = 1024 * 1024 * 1024;

    fn geom_4gib() -> ExfatGeometry {
        ExfatGeometry::for_volume_size(4 * GIB).expect("valid 4 GiB geometry")
    }
    fn geom_64mib() -> ExfatGeometry {
        ExfatGeometry::for_volume_size(64 * MIB).expect("valid 64 MiB geometry")
    }

    // ---------- Sizing ----------

    #[test]
    fn size_bytes_for_one_cluster_is_one_byte() {
        assert_eq!(AllocationBitmap::size_bytes_for(1), 1);
    }

    #[test]
    fn size_bytes_for_eight_clusters_is_one_byte() {
        assert_eq!(AllocationBitmap::size_bytes_for(8), 1);
    }

    #[test]
    fn size_bytes_for_nine_clusters_is_two_bytes() {
        assert_eq!(AllocationBitmap::size_bytes_for(9), 2);
    }

    #[test]
    fn size_bytes_for_4gib_geometry_is_ceil_cluster_count_over_8() {
        let g = geom_4gib();
        let bitmap = AllocationBitmap::new_empty(&g);
        let expected = usize::try_from((u64::from(g.cluster_count()) + 7) / 8).unwrap();
        assert_eq!(bitmap.bytes().len(), expected);
        assert_eq!(usize::try_from(bitmap.size_bytes()).unwrap(), expected);
    }

    #[test]
    fn empty_bitmap_is_zero_filled() {
        let g = geom_4gib();
        let bitmap = AllocationBitmap::new_empty(&g);
        assert!(bitmap.bytes().iter().all(|&b| b == 0));
    }

    #[test]
    fn size_clusters_rounds_up() {
        let g = geom_4gib(); // 32 KiB clusters
        let bitmap = AllocationBitmap::new_empty(&g);
        let bytes = bitmap.size_bytes();
        let bytes_per_cluster = u64::from(g.bytes_per_cluster());
        let expected = u32::try_from(bytes.div_ceil(bytes_per_cluster)).unwrap();
        assert_eq!(bitmap.size_clusters(&g), expected);
    }

    // ---------- Bit-mapping invariants ----------

    #[test]
    fn cluster_2_maps_to_byte_0_bit_0() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        bitmap.mark_allocated(2).unwrap();
        assert_eq!(bitmap.bytes()[0], 0b0000_0001);
    }

    #[test]
    fn cluster_3_maps_to_byte_0_bit_1() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        bitmap.mark_allocated(3).unwrap();
        assert_eq!(bitmap.bytes()[0], 0b0000_0010);
    }

    #[test]
    fn cluster_9_maps_to_byte_0_bit_7() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        bitmap.mark_allocated(9).unwrap();
        assert_eq!(bitmap.bytes()[0], 0b1000_0000);
    }

    #[test]
    fn cluster_10_maps_to_byte_1_bit_0() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        bitmap.mark_allocated(10).unwrap();
        assert_eq!(bitmap.bytes()[0], 0);
        assert_eq!(bitmap.bytes()[1], 0b0000_0001);
    }

    #[test]
    fn multiple_marks_or_together_in_same_byte() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        bitmap.mark_allocated(2).unwrap();
        bitmap.mark_allocated(4).unwrap();
        bitmap.mark_allocated(6).unwrap();
        // bits 0, 2, 4 set (clusters 2, 4, 6 → bit indices 0, 2, 4).
        assert_eq!(bitmap.bytes()[0], 0b0001_0101);
    }

    // ---------- Range marking ----------

    #[test]
    fn mark_range_across_byte_boundary_sets_correct_bits() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        bitmap.mark_range_allocated(2, 12).unwrap();
        assert_eq!(bitmap.bytes()[0], 0xFF);
        assert_eq!(bitmap.bytes()[1], 0b0000_1111);
        assert_eq!(bitmap.bytes()[2], 0);
    }

    #[test]
    fn mark_range_with_count_zero_is_noop() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        bitmap.mark_range_allocated(2, 0).unwrap();
        assert!(bitmap.bytes().iter().all(|&b| b == 0));
    }

    #[test]
    fn mark_range_via_range_helper_matches_method() {
        let g = geom_64mib();
        let mut a = AllocationBitmap::new_empty(&g);
        let mut b = AllocationBitmap::new_empty(&g);
        a.mark_range_allocated(5, 7).unwrap();
        mark_range(&mut b, 5..12).unwrap();
        assert_eq!(a.bytes(), b.bytes());
    }

    #[test]
    fn mark_full_volume_sets_every_meaningful_bit() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        bitmap
            .mark_range_allocated(FIRST_CLUSTER_NUMBER, g.cluster_count())
            .unwrap();
        // Every byte except possibly the trailing partial byte
        // must be 0xFF.
        let bits_total = g.cluster_count();
        let trailing_bits = bits_total % 8;
        let full_bytes = (bits_total / 8) as usize;
        for &b in &bitmap.bytes()[..full_bytes] {
            assert_eq!(b, 0xFF);
        }
        if trailing_bits != 0 {
            let mask = (1_u8 << trailing_bits) - 1;
            assert_eq!(bitmap.bytes()[full_bytes], mask);
        }
    }

    // ---------- Error variants ----------

    #[test]
    fn cluster_zero_is_out_of_range() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        let err = bitmap.mark_allocated(0).unwrap_err();
        assert!(matches!(
            err,
            AllocationBitmapError::ClusterOutOfRange { .. }
        ));
    }

    #[test]
    fn cluster_one_is_out_of_range() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        let err = bitmap.mark_allocated(1).unwrap_err();
        assert!(matches!(
            err,
            AllocationBitmapError::ClusterOutOfRange { .. }
        ));
    }

    #[test]
    fn cluster_past_end_is_out_of_range() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        let past = FIRST_CLUSTER_NUMBER + g.cluster_count();
        let err = bitmap.mark_allocated(past).unwrap_err();
        assert!(matches!(
            err,
            AllocationBitmapError::ClusterOutOfRange { .. }
        ));
    }

    #[test]
    fn range_overflowing_u32_is_rejected_before_any_bit_is_set() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        let err = bitmap.mark_range_allocated(u32::MAX, 2).unwrap_err();
        assert!(matches!(err, AllocationBitmapError::RangeOverflow { .. }));
        assert!(bitmap.bytes().iter().all(|&b| b == 0));
    }

    #[test]
    fn range_straddling_the_end_is_rejected_without_partial_marks() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        let last = FIRST_CLUSTER_NUMBER + g.cluster_count() - 1;
        // start at the last valid cluster, ask for 5 — only 1 is in-range.
        let err = bitmap.mark_range_allocated(last, 5).unwrap_err();
        assert!(matches!(
            err,
            AllocationBitmapError::ClusterOutOfRange { .. }
        ));
        assert!(
            bitmap.bytes().iter().all(|&b| b == 0),
            "no bit must have been set when validation failed"
        );
    }

    // ---------- is_allocated round-trip ----------

    #[test]
    fn is_allocated_round_trip() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        assert!(!bitmap.is_allocated(2).unwrap());
        bitmap.mark_allocated(2).unwrap();
        assert!(bitmap.is_allocated(2).unwrap());
        assert!(!bitmap.is_allocated(3).unwrap());
    }

    #[test]
    fn is_allocated_out_of_range_errors() {
        let g = geom_64mib();
        let bitmap = AllocationBitmap::new_empty(&g);
        assert!(matches!(
            bitmap.is_allocated(0).unwrap_err(),
            AllocationBitmapError::ClusterOutOfRange { .. }
        ));
    }

    // ---------- Trailing bits are never spuriously set ----------

    #[test]
    fn trailing_bits_in_last_byte_remain_zero_after_marking_only_in_range_clusters() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        let last = FIRST_CLUSTER_NUMBER + g.cluster_count() - 1;
        bitmap.mark_allocated(last).unwrap();
        let bits_total = g.cluster_count();
        let trailing_bits = bits_total % 8;
        if trailing_bits == 0 {
            // No trailing bits to verify in this geometry.
            return;
        }
        let last_byte = bitmap.bytes().last().copied().unwrap();
        let reserved_mask = !((1_u8 << trailing_bits) - 1);
        assert_eq!(
            last_byte & reserved_mask,
            0,
            "reserved trailing bits must remain zero"
        );
    }

    // ---------- Determinism ----------

    #[test]
    fn identical_marks_produce_identical_buffers() {
        let g = geom_64mib();
        let mut a = AllocationBitmap::new_empty(&g);
        let mut b = AllocationBitmap::new_empty(&g);
        for c in [2, 5, 9, 17, 32, 64, 100] {
            a.mark_allocated(c).unwrap();
            b.mark_allocated(c).unwrap();
        }
        assert_eq!(a.bytes(), b.bytes());
    }

    // ---------- Idempotency ----------

    #[test]
    fn marking_same_cluster_twice_keeps_bitmap_unchanged() {
        let g = geom_64mib();
        let mut bitmap = AllocationBitmap::new_empty(&g);
        bitmap.mark_allocated(5).unwrap();
        let snapshot: Vec<u8> = bitmap.bytes().to_vec();
        bitmap.mark_allocated(5).unwrap();
        assert_eq!(bitmap.bytes(), snapshot.as_slice());
    }

    // ---------- Cluster count exposure ----------

    #[test]
    fn cluster_count_matches_geometry() {
        let g = geom_4gib();
        let bitmap = AllocationBitmap::new_empty(&g);
        assert_eq!(bitmap.cluster_count(), g.cluster_count());
    }
}
