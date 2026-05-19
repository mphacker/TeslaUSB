//! FAT32 `FsInfo` sector synthesizer.
//!
//! Phase 2.3 of the B-1 rewrite. This module produces the 512-byte
//! **`FsInfo`** sector that lives at the volume offset advertised by
//! `BPB_FSInfo` in the boot sector (by convention sector 1; see
//! [`crate::fs::fat32::geometry::FSINFO_SECTOR_INDEX`]).
//!
//! The `FsInfo` sector is purely advisory: it gives the operating
//! system hints about free space and the next-free cluster so the
//! kernel doesn't have to scan the entire FAT on every allocation.
//! fatgen103 explicitly states that the values "may not be
//! correct" and that the kernel must validate them before use; if
//! either field is the magic value `0xFFFFFFFF` the kernel is
//! expected to compute the true value by scanning the FAT.
//!
//! ## Specification anchor
//!
//! Microsoft FAT Specification (fatgen103.pdf), **§5: FAT32
//! `FsInfo` Sector Structure and Backup Boot Sector**.
//!
//! ## Field layout
//!
//! ```text
//! Offset Size Field            Value
//! 0x000   4   FSI_LeadSig      fixed: 0x41615252 (LE: 52 52 61 41)
//! 0x004 480   FSI_Reserved1    fixed: zero
//! 0x1E4   4   FSI_StrucSig     fixed: 0x61417272 (LE: 72 72 41 61)
//! 0x1E8   4   FSI_Free_Count   caller-supplied free cluster count,
//!                              or 0xFFFFFFFF if unknown
//! 0x1EC   4   FSI_Nxt_Free     caller-supplied next-free cluster hint,
//!                              or 0xFFFFFFFF if unknown
//! 0x1F0  12   FSI_Reserved2    fixed: zero
//! 0x1FC   4   FSI_TrailSig     fixed: 0xAA550000 (LE: 00 00 55 AA)
//! ```
//!
//! The trailing 4-byte signature `0xAA550000` is constructed so
//! that its little-endian byte representation places `0x55, 0xAA`
//! at offsets `0x1FE`/`0x1FF` — matching the boot-sector signature
//! convention from fatgen103 §3.1.
//!
//! ## Why `Option<u32>` instead of a magic value at the API?
//!
//! `0xFFFFFFFF` is a perfectly valid `u32` and could collide with
//! a synthesizer that someday genuinely tracks a volume with
//! `u32::MAX` free clusters (impossible today — `MAX_FAT32_DATA_CLUSTERS`
//! is `0x0FFF_FFF4` — but the API should not depend on that).
//! Using `Option<u32>` makes "unknown" a separate state in the
//! type system and produces clearer call sites.
//!
//! ## Initial-format defaults
//!
//! For a freshly-synthesized FAT32 volume with one in-use cluster
//! (the root directory at cluster 2), the natural values are:
//!
//! * `free_count = data_cluster_count - 1`
//! * `next_free_hint = 3` (the first cluster after the root dir)
//!
//! The Phase 2.6 dispatcher will pick these defaults; this module
//! takes them as parameters so tests can exercise edge cases
//! (zero free, hint at the last cluster, etc.) and so a future
//! synthesizer that tracks actual usage can override them.

use core::fmt;

use crate::fs::fat32::boot_sector::ROOT_DIRECTORY_CLUSTER;
use crate::fs::fat32::geometry::Fat32Geometry;
use crate::fs::geometry::{Geometry, SECTOR_SIZE_BYTES};

/// Byte width of the `FsInfo` sector.
///
/// Equal to [`SECTOR_SIZE_BYTES`] and to the size of the array
/// [`synthesize`] returns.
pub const FSINFO_SECTOR_SIZE_BYTES: usize = SECTOR_SIZE_BYTES as usize;

/// `FSI_LeadSig` value at offset `0x000` (fatgen103 §5).
///
/// Little-endian bytes: `52 52 61 41`. ASCII `RRaA`. Pattern-
/// matches the "leading signature" identifier the FAT driver
/// looks for to confirm the sector is a real `FsInfo`.
pub const FSI_LEAD_SIG: u32 = 0x4161_5252;

/// `FSI_StrucSig` value at offset `0x1E4` (fatgen103 §5).
///
/// Little-endian bytes: `72 72 41 61`. ASCII `rrAa`. A second
/// "structure signature" the FAT driver cross-checks before
/// trusting the free-count / next-free fields.
pub const FSI_STRUC_SIG: u32 = 0x6141_7272;

/// `FSI_TrailSig` value at offset `0x1FC` (fatgen103 §5).
///
/// Little-endian bytes: `00 00 55 AA`. Places the canonical
/// `0x55, 0xAA` signature at offsets `0x1FE`/`0x1FF`, matching
/// the boot-sector end-signature convention from fatgen103 §3.1.
pub const FSI_TRAIL_SIG: u32 = 0xAA55_0000;

/// Sentinel value for "unknown" in the `FSI_Free_Count` and
/// `FSI_Nxt_Free` fields (fatgen103 §5).
///
/// When either field carries this value the kernel is expected
/// to compute the true value by scanning the FAT. [`synthesize`]
/// writes this when the caller passes `None`.
pub const FSI_UNKNOWN: u32 = 0xFFFF_FFFF;

/// Errors returned by [`synthesize`].
#[derive(Debug, PartialEq, Eq)]
pub enum FsInfoError {
    /// The caller-supplied `free_count` exceeds the geometry's
    /// total addressable data clusters — the volume cannot
    /// contain more free clusters than it has clusters.
    FreeCountExceedsDataClusters {
        /// The free-count value the caller passed.
        free_count: u32,
        /// The geometry's reported data-cluster count.
        data_cluster_count: u32,
    },
    /// The caller-supplied `next_free_hint` is below the first
    /// addressable data cluster (cluster 2 per fatgen103 §4.1).
    /// Clusters 0 and 1 are reserved and not allocatable.
    NextFreeBelowFirstDataCluster {
        /// The next-free hint the caller passed.
        next_free: u32,
    },
    /// The caller-supplied `next_free_hint` is above the highest
    /// addressable data cluster (`data_cluster_count + 1`, because
    /// cluster numbering starts at 2).
    NextFreeAboveMaxCluster {
        /// The next-free hint the caller passed.
        next_free: u32,
        /// The highest valid cluster number for this geometry.
        max_cluster: u32,
    },
}

impl fmt::Display for FsInfoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::FreeCountExceedsDataClusters {
                free_count,
                data_cluster_count,
            } => write!(
                f,
                "FSI_Free_Count = {free_count} exceeds the geometry's {data_cluster_count} data clusters"
            ),
            Self::NextFreeBelowFirstDataCluster { next_free } => write!(
                f,
                "FSI_Nxt_Free = {next_free} is below the first addressable cluster ({ROOT_DIRECTORY_CLUSTER})"
            ),
            Self::NextFreeAboveMaxCluster {
                next_free,
                max_cluster,
            } => write!(
                f,
                "FSI_Nxt_Free = {next_free} is above the highest addressable cluster ({max_cluster})"
            ),
        }
    }
}

impl core::error::Error for FsInfoError {}

/// Synthesize the 512-byte FAT32 `FsInfo` sector for `geometry`.
///
/// `free_count = Some(n)` writes `n` to `FSI_Free_Count` after
/// validating it against `geometry`'s data-cluster count;
/// `free_count = None` writes [`FSI_UNKNOWN`] (`0xFFFFFFFF`),
/// telling the OS to scan the FAT for the true value.
///
/// `next_free_hint = Some(c)` writes `c` to `FSI_Nxt_Free` after
/// validating `2 <= c <= data_cluster_count + 1` (fatgen103 §4.1
/// reserves clusters 0 and 1); `next_free_hint = None` writes
/// [`FSI_UNKNOWN`].
///
/// # Errors
///
/// * [`FsInfoError::FreeCountExceedsDataClusters`] when
///   `free_count > geometry.data_cluster_count()`.
/// * [`FsInfoError::NextFreeBelowFirstDataCluster`] when
///   `next_free_hint < 2`.
/// * [`FsInfoError::NextFreeAboveMaxCluster`] when
///   `next_free_hint > geometry.data_cluster_count() + 1`.
pub fn synthesize(
    geometry: &Fat32Geometry,
    free_count: Option<u32>,
    next_free_hint: Option<u32>,
) -> Result<[u8; FSINFO_SECTOR_SIZE_BYTES], FsInfoError> {
    let data_cluster_count = geometry.data_cluster_count();
    let max_cluster = data_cluster_count
        .checked_add(ROOT_DIRECTORY_CLUSTER - 1)
        .unwrap_or(u32::MAX);

    let free_count_field = match free_count {
        None => FSI_UNKNOWN,
        Some(n) if n > data_cluster_count => {
            return Err(FsInfoError::FreeCountExceedsDataClusters {
                free_count: n,
                data_cluster_count,
            });
        }
        Some(n) => n,
    };

    let next_free_field = match next_free_hint {
        None => FSI_UNKNOWN,
        Some(c) if c < ROOT_DIRECTORY_CLUSTER => {
            return Err(FsInfoError::NextFreeBelowFirstDataCluster { next_free: c });
        }
        Some(c) if c > max_cluster => {
            return Err(FsInfoError::NextFreeAboveMaxCluster {
                next_free: c,
                max_cluster,
            });
        }
        Some(c) => c,
    };

    let mut sector = [0_u8; FSINFO_SECTOR_SIZE_BYTES];
    write_u32_le(&mut sector, 0x000, FSI_LEAD_SIG);
    // 0x004..0x1E4: FSI_Reserved1 — already zero.
    write_u32_le(&mut sector, 0x1E4, FSI_STRUC_SIG);
    write_u32_le(&mut sector, 0x1E8, free_count_field);
    write_u32_le(&mut sector, 0x1EC, next_free_field);
    // 0x1F0..0x1FC: FSI_Reserved2 — already zero.
    write_u32_le(&mut sector, 0x1FC, FSI_TRAIL_SIG);
    Ok(sector)
}

/// Write `value` as a little-endian u32 at byte `offset` in
/// `buf`.
///
/// Every call site in this module uses a compile-time-constant
/// offset taken straight from the fatgen103 §5 field table, so
/// the `indexing_slicing` lint is safe to suppress.
#[inline]
#[allow(clippy::indexing_slicing)]
fn write_u32_le(buf: &mut [u8; FSINFO_SECTOR_SIZE_BYTES], offset: usize, value: u32) {
    buf[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
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
    use crate::fs::fat32::geometry::Fat32Geometry;

    const MIB: u64 = 1024 * 1024;
    const GIB: u64 = 1024 * 1024 * 1024;

    fn geo_4gib() -> Fat32Geometry {
        Fat32Geometry::for_volume_size(4 * GIB).expect("valid 4 GiB geometry")
    }

    fn read_u32_le(sector: &[u8; 512], offset: usize) -> u32 {
        u32::from_le_bytes(sector[offset..offset + 4].try_into().unwrap())
    }

    // --- Fixed signatures at known offsets ------------------------------

    #[test]
    fn lead_signature_at_offset_0() {
        let s = synthesize(&geo_4gib(), None, None).expect("valid");
        assert_eq!(read_u32_le(&s, 0x000), FSI_LEAD_SIG);
        // Explicit byte order: 0x41615252 LE = 52 52 61 41.
        assert_eq!(&s[0x000..0x004], &[0x52, 0x52, 0x61, 0x41]);
    }

    #[test]
    fn lead_signature_spells_rraa_in_ascii() {
        // fatgen103 §5 calls this out as the ASCII "leading sig"
        // identifier.
        let s = synthesize(&geo_4gib(), None, None).expect("valid");
        assert_eq!(&s[0x000..0x004], b"RRaA");
    }

    #[test]
    fn struct_signature_at_offset_0x1e4() {
        let s = synthesize(&geo_4gib(), None, None).expect("valid");
        assert_eq!(read_u32_le(&s, 0x1E4), FSI_STRUC_SIG);
        // Explicit byte order: 0x61417272 LE = 72 72 41 61.
        assert_eq!(&s[0x1E4..0x1E8], &[0x72, 0x72, 0x41, 0x61]);
    }

    #[test]
    fn struct_signature_spells_rraa_lowercase_in_ascii() {
        let s = synthesize(&geo_4gib(), None, None).expect("valid");
        assert_eq!(&s[0x1E4..0x1E8], b"rrAa");
    }

    #[test]
    fn trail_signature_at_offset_0x1fc() {
        let s = synthesize(&geo_4gib(), None, None).expect("valid");
        assert_eq!(read_u32_le(&s, 0x1FC), FSI_TRAIL_SIG);
        // Explicit byte order: 0xAA550000 LE = 00 00 55 AA.
        assert_eq!(&s[0x1FC..0x200], &[0x00, 0x00, 0x55, 0xAA]);
    }

    #[test]
    fn trail_signature_places_55aa_at_sector_end() {
        // This is the whole reason fatgen103 chose the 0xAA550000
        // encoding for FSI_TrailSig — the sector ends with the
        // same 0x55 0xAA the boot sector ends with, so a FAT
        // driver that scans for "looks like a valid sector" finds
        // it. The boot-sector signature constant lives in the
        // sibling boot_sector module and is asserted there.
        let s = synthesize(&geo_4gib(), None, None).expect("valid");
        assert_eq!(s[0x1FE], 0x55);
        assert_eq!(s[0x1FF], 0xAA);
    }

    // --- Variable fields: free count + next-free hint -------------------

    #[test]
    fn unknown_free_count_writes_ffffffff() {
        let s = synthesize(&geo_4gib(), None, Some(3)).expect("valid");
        assert_eq!(read_u32_le(&s, 0x1E8), FSI_UNKNOWN);
        assert_eq!(&s[0x1E8..0x1EC], &[0xFF, 0xFF, 0xFF, 0xFF]);
    }

    #[test]
    fn unknown_next_free_writes_ffffffff() {
        let s = synthesize(&geo_4gib(), Some(0), None).expect("valid");
        assert_eq!(read_u32_le(&s, 0x1EC), FSI_UNKNOWN);
        assert_eq!(&s[0x1EC..0x1F0], &[0xFF, 0xFF, 0xFF, 0xFF]);
    }

    #[test]
    fn free_count_round_trips_little_endian() {
        // 0x000F_8E40 = 1 019 968 — within 4 GiB's ~1.04M data
        // cluster count and with 4 distinct LE bytes
        // (40 8E 0F 00) so byte-order regressions show up clearly.
        let s = synthesize(&geo_4gib(), Some(0x000F_8E40), Some(2)).expect("valid");
        assert_eq!(read_u32_le(&s, 0x1E8), 0x000F_8E40);
        assert_eq!(&s[0x1E8..0x1EC], &[0x40, 0x8E, 0x0F, 0x00]);
    }

    #[test]
    fn next_free_round_trips_little_endian() {
        // 0x000A_BCDE = 703 710 — within 4 GiB's max cluster and
        // 4 distinct LE bytes (DE BC 0A 00).
        let s = synthesize(&geo_4gib(), Some(0), Some(0x000A_BCDE)).expect("valid");
        assert_eq!(read_u32_le(&s, 0x1EC), 0x000A_BCDE);
        assert_eq!(&s[0x1EC..0x1F0], &[0xDE, 0xBC, 0x0A, 0x00]);
    }

    #[test]
    fn free_count_zero_is_accepted() {
        // A fully-allocated volume has zero free clusters — must
        // accept (don't conflate Some(0) with None).
        let s = synthesize(&geo_4gib(), Some(0), None).expect("valid");
        assert_eq!(read_u32_le(&s, 0x1E8), 0);
    }

    #[test]
    fn free_count_equal_to_data_cluster_count_is_accepted() {
        let geo = geo_4gib();
        let n = geo.data_cluster_count();
        let s = synthesize(&geo, Some(n), None).expect("free_count == data_count is valid");
        assert_eq!(read_u32_le(&s, 0x1E8), n);
    }

    #[test]
    fn free_count_above_data_cluster_count_is_rejected() {
        let geo = geo_4gib();
        let too_many = geo.data_cluster_count() + 1;
        let err = synthesize(&geo, Some(too_many), None)
            .expect_err("free_count > data_cluster_count must be rejected");
        match err {
            FsInfoError::FreeCountExceedsDataClusters {
                free_count,
                data_cluster_count,
            } => {
                assert_eq!(free_count, too_many);
                assert_eq!(data_cluster_count, geo.data_cluster_count());
            }
            other => panic!("expected FreeCountExceedsDataClusters, got {other:?}"),
        }
    }

    #[test]
    fn next_free_at_first_data_cluster_is_accepted() {
        let s = synthesize(&geo_4gib(), None, Some(2)).expect("cluster 2 is valid");
        assert_eq!(read_u32_le(&s, 0x1EC), 2);
    }

    #[test]
    fn next_free_at_max_cluster_is_accepted() {
        let geo = geo_4gib();
        // Max cluster = data_cluster_count + 1 (because numbering
        // starts at 2).
        let max = geo.data_cluster_count() + 1;
        let s = synthesize(&geo, None, Some(max)).expect("max cluster is valid");
        assert_eq!(read_u32_le(&s, 0x1EC), max);
    }

    #[test]
    fn next_free_below_first_data_cluster_is_rejected() {
        // Clusters 0 and 1 are reserved per fatgen103 §4.1 — must
        // reject for the hint.
        for bad in 0..ROOT_DIRECTORY_CLUSTER {
            let err =
                synthesize(&geo_4gib(), None, Some(bad)).expect_err("cluster < 2 must be rejected");
            match err {
                FsInfoError::NextFreeBelowFirstDataCluster { next_free } => {
                    assert_eq!(next_free, bad);
                }
                other => panic!("expected NextFreeBelowFirstDataCluster, got {other:?}"),
            }
        }
    }

    #[test]
    fn next_free_above_max_cluster_is_rejected() {
        let geo = geo_4gib();
        let max = geo.data_cluster_count() + 1;
        let err =
            synthesize(&geo, None, Some(max + 1)).expect_err("cluster > max must be rejected");
        match err {
            FsInfoError::NextFreeAboveMaxCluster {
                next_free,
                max_cluster,
            } => {
                assert_eq!(next_free, max + 1);
                assert_eq!(max_cluster, max);
            }
            other => panic!("expected NextFreeAboveMaxCluster, got {other:?}"),
        }
    }

    #[test]
    fn free_count_validation_runs_before_next_free_validation() {
        // If both fields are invalid, the caller should learn
        // about the free_count problem first (it's the field they
        // most likely got wrong if they're computing from
        // geometry). Test is brittle by design — if the order
        // ever changes, the change must be deliberate.
        let geo = geo_4gib();
        let err = synthesize(&geo, Some(geo.data_cluster_count() + 1), Some(0))
            .expect_err("both fields invalid");
        assert!(
            matches!(err, FsInfoError::FreeCountExceedsDataClusters { .. }),
            "expected FreeCountExceedsDataClusters first, got {err:?}"
        );
    }

    // --- Reserved areas are zero-filled ---------------------------------

    #[test]
    fn fsi_reserved1_480_bytes_are_zero() {
        // Even when free_count and next_free are populated, the
        // 480-byte reserved area at 0x004..0x1E4 must remain zero.
        let s = synthesize(&geo_4gib(), Some(1000), Some(1000)).expect("valid");
        for (i, &b) in s[0x004..0x1E4].iter().enumerate() {
            assert_eq!(b, 0, "FSI_Reserved1 byte at offset {:#x} is {b}", 0x004 + i);
        }
    }

    #[test]
    fn fsi_reserved2_12_bytes_are_zero() {
        let s = synthesize(&geo_4gib(), Some(1000), Some(1000)).expect("valid");
        assert_eq!(&s[0x1F0..0x1FC], &[0_u8; 12]);
    }

    // --- Geometry-dependent max_cluster bound varies correctly ----------

    #[test]
    fn max_cluster_grows_with_volume_size() {
        let geo_small = Fat32Geometry::for_volume_size(34 * MIB).expect("valid");
        let geo_huge = Fat32Geometry::for_volume_size(64 * GIB).expect("valid");

        let max_small = geo_small.data_cluster_count() + 1;
        let max_huge = geo_huge.data_cluster_count() + 1;
        assert!(
            max_small < max_huge,
            "max cluster must grow: small={max_small}, huge={max_huge}"
        );

        // The same cluster number that's valid for the huge
        // volume must be rejected for the small one if it's
        // above the small max.
        let probe = max_small + 1;
        if probe <= max_huge {
            synthesize(&geo_huge, None, Some(probe)).expect("valid for huge");
            let err = synthesize(&geo_small, None, Some(probe))
                .expect_err("probe must be rejected by small");
            assert!(matches!(err, FsInfoError::NextFreeAboveMaxCluster { .. }));
        }
    }

    // --- Full-buffer expected comparison --------------------------------

    #[test]
    fn full_sector_matches_hand_built_expected_for_known_values() {
        // Deterministic case: free=1000, next_free=42.
        let s = synthesize(&geo_4gib(), Some(1000), Some(42)).expect("valid");

        let mut expected = [0_u8; 512];
        expected[0x000..0x004].copy_from_slice(&0x4161_5252_u32.to_le_bytes());
        // 0x004..0x1E4 zero (FSI_Reserved1).
        expected[0x1E4..0x1E8].copy_from_slice(&0x6141_7272_u32.to_le_bytes());
        expected[0x1E8..0x1EC].copy_from_slice(&1000_u32.to_le_bytes());
        expected[0x1EC..0x1F0].copy_from_slice(&42_u32.to_le_bytes());
        // 0x1F0..0x1FC zero (FSI_Reserved2).
        expected[0x1FC..0x200].copy_from_slice(&0xAA55_0000_u32.to_le_bytes());

        assert_eq!(s, expected, "byte-by-byte mismatch vs hand-built");
    }
    #[test]
    fn full_sector_matches_hand_built_expected_for_both_unknown() {
        // The "freshly-formatted, kernel-please-scan" case.
        let s = synthesize(&geo_4gib(), None, None).expect("valid");

        let mut expected = [0_u8; 512];
        expected[0x000..0x004].copy_from_slice(&0x4161_5252_u32.to_le_bytes());
        expected[0x1E4..0x1E8].copy_from_slice(&0x6141_7272_u32.to_le_bytes());
        expected[0x1E8..0x1EC].copy_from_slice(&FSI_UNKNOWN.to_le_bytes());
        expected[0x1EC..0x1F0].copy_from_slice(&FSI_UNKNOWN.to_le_bytes());
        expected[0x1FC..0x200].copy_from_slice(&0xAA55_0000_u32.to_le_bytes());

        assert_eq!(s, expected, "byte-by-byte mismatch vs hand-built");
    }

    // --- Geometry sweep invariants --------------------------------------

    #[test]
    fn all_three_signatures_present_across_volume_sweep() {
        const STEP: u64 = 17 * MIB;
        let mut size = 34 * MIB;
        while size <= 64 * GIB {
            let geo = Fat32Geometry::for_volume_size(size).expect("sweep size valid");
            let s = synthesize(&geo, None, None).expect("synth valid");
            assert_eq!(read_u32_le(&s, 0x000), FSI_LEAD_SIG, "lead at size {size}");
            assert_eq!(
                read_u32_le(&s, 0x1E4),
                FSI_STRUC_SIG,
                "struc at size {size}"
            );
            assert_eq!(
                read_u32_le(&s, 0x1FC),
                FSI_TRAIL_SIG,
                "trail at size {size}"
            );
            assert_eq!(s[0x1FE], 0x55);
            assert_eq!(s[0x1FF], 0xAA);
            size += STEP;
        }
    }
}
