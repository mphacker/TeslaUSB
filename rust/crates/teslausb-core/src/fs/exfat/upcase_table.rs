//! `exFAT` upcase table synthesizer.
//!
//! Phase 2.9 of the B-1 rewrite. This module produces the on-disk
//! **upcase table** that lives inside the cluster heap and serves
//! as the canonical UTF-16 code-unit → uppercase code-unit map
//! used by the kernel's `exFAT` driver for filename comparisons.
//!
//! ## Specification anchor
//!
//! Microsoft `exFAT` File System Specification v1.00 (August 27,
//! 2019). §7.2 Up-case Table Directory Entry,
//! §7.2.5 Up-case Table — describes both the uncompressed and
//! the compressed encodings. §7.2.5.3 specifies the table
//! checksum algorithm.
//!
//! ## What this module produces
//!
//! [`UpcaseTable::ascii_identity`] returns the simplest spec-valid
//! table:
//!
//! * 65 536 little-endian `u16` entries (one per BMP code unit) =
//!   **131 072 bytes** uncompressed.
//! * Entries `0x0061..=0x007A` (`a`..=`z`) map to
//!   `0x0041..=0x005A` (`A`..=`Z`).
//! * Every other entry maps to itself (identity), including all
//!   non-ASCII code units.
//!
//! This is the minimum the spec requires of a valid table (§7.2.4:
//! "If a character has no upper-case mapping, then the table
//! contains the character itself.") and is what the kernel
//! verifies via the embedded checksum.
//!
//! ## Why uncompressed?
//!
//! The spec also defines a compressed encoding (§7.2.5.4) that
//! uses `0xFFFF` as a "skip" marker. Microsoft's canonical table
//! compresses to ~5836 bytes. B-1 ships the uncompressed form
//! because:
//!
//! * Tesla camera filenames are ASCII timestamps; the ASCII fold
//!   is all the case folding the target use-case needs.
//! * 128 KiB of data fits in a single 128 KiB cluster (the
//!   largest cluster size B-1's geometry picks). The Phase 2.11
//!   dispatcher can serve the table out of one cluster.
//! * The uncompressed encoding is trivially auditable; the
//!   compressed encoding requires hand-verifying every skip-marker
//!   offset against the canonical reference.
//!
//! A future increment may swap to Microsoft's canonical compressed
//! table without changing this module's public API — callers
//! only ever see [`UpcaseTable::bytes`] and
//! [`UpcaseTable::checksum`].
//!
//! ## Checksum algorithm
//!
//! `exFAT` spec §7.2.5.3 specifies:
//!
//! ```text
//! checksum = 0
//! for byte in bytes:
//!     checksum = ((checksum >> 1) | (checksum << 31)) + byte
//!     checksum &= 0xFFFFFFFF
//! ```
//!
//! Same rotate-right-then-add as the boot checksum
//! ([`crate::fs::exfat::boot_sector`] §3.4) but with no excluded
//! bytes.

use core::fmt;

/// Number of UTF-16 code units the table covers — the entire
/// Basic Multilingual Plane.
pub const UPCASE_TABLE_ENTRIES: u32 = 65_536;

/// Bytes per entry (one little-endian `u16`).
pub const BYTES_PER_ENTRY: usize = 2;

/// Total size of the uncompressed upcase table in bytes
/// (`65_536 × 2 = 131_072`).
pub const UPCASE_TABLE_SIZE_BYTES: usize = (UPCASE_TABLE_ENTRIES as usize) * BYTES_PER_ENTRY;

/// ASCII code unit for `'a'`.
pub const ASCII_LOWER_A: u16 = 0x0061;

/// ASCII code unit for `'z'`.
pub const ASCII_LOWER_Z: u16 = 0x007A;

/// ASCII code unit for `'A'`.
pub const ASCII_UPPER_A: u16 = 0x0041;

/// Difference between ASCII lowercase and uppercase
/// (`'a' - 'A' = 0x20`).
pub const ASCII_CASE_DELTA: u16 = ASCII_LOWER_A - ASCII_UPPER_A;

const _: () = {
    assert!(UPCASE_TABLE_SIZE_BYTES == 131_072);
    assert!(ASCII_CASE_DELTA == 0x20);
};

/// `exFAT` upcase table.
///
/// Owns the full uncompressed byte representation and a cached
/// checksum. Construct with [`UpcaseTable::ascii_identity`].
#[derive(Clone)]
pub struct UpcaseTable {
    bytes: Vec<u8>,
    checksum: u32,
}

impl fmt::Debug for UpcaseTable {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("UpcaseTable")
            .field("size_bytes", &self.bytes.len())
            .field("checksum", &format_args!("{:#010x}", self.checksum))
            .finish()
    }
}

impl UpcaseTable {
    /// Build the ASCII-fold-plus-identity upcase table described
    /// at the module level.
    ///
    /// Allocates [`UPCASE_TABLE_SIZE_BYTES`] = 131 072 bytes on
    /// the heap. The computation is `O(N)` over the entry count
    /// and runs in microseconds even on a Pi Zero 2 W.
    #[must_use]
    pub fn ascii_identity() -> Self {
        let mut bytes = Vec::with_capacity(UPCASE_TABLE_SIZE_BYTES);
        for code_unit in 0_u32..UPCASE_TABLE_ENTRIES {
            // Truncating to u16 is safe: the range is exactly
            // 0..65_536 by construction.
            let cu_u16 = u16::try_from(code_unit).unwrap_or(u16::MAX);
            let folded = ascii_fold(cu_u16);
            bytes.extend_from_slice(&folded.to_le_bytes());
        }
        debug_assert_eq!(bytes.len(), UPCASE_TABLE_SIZE_BYTES);
        let checksum = compute_table_checksum(&bytes);
        Self { bytes, checksum }
    }

    /// Borrow the on-disk byte representation.
    ///
    /// Length equals [`Self::size_bytes`]; suitable for serving
    /// directly to the Phase 2.11 read dispatcher.
    #[must_use]
    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }

    /// Cached checksum used by the upcase table directory entry's
    /// `TableChecksum` field (`exFAT` spec §7.2.3).
    #[must_use]
    pub fn checksum(&self) -> u32 {
        self.checksum
    }

    /// Size of the table in bytes — always
    /// [`UPCASE_TABLE_SIZE_BYTES`].
    #[must_use]
    pub fn size_bytes(&self) -> usize {
        self.bytes.len()
    }

    /// Look up the uppercase form of `code_unit`.
    ///
    /// Equivalent to reading two little-endian bytes at offset
    /// `code_unit * 2` from [`Self::bytes`].
    #[must_use]
    #[allow(clippy::indexing_slicing)] // index range is bounded by the u16 input
    pub fn uppercase(&self, code_unit: u16) -> u16 {
        let off = (code_unit as usize) * BYTES_PER_ENTRY;
        u16::from_le_bytes([self.bytes[off], self.bytes[off + 1]])
    }
}

/// Map `code_unit` to its ASCII-fold uppercase form.
///
/// Lowercase ASCII (`a..=z`) folds to uppercase ASCII (`A..=Z`).
/// Everything else (including all non-ASCII code units) maps to
/// itself.
const fn ascii_fold(code_unit: u16) -> u16 {
    if code_unit >= ASCII_LOWER_A && code_unit <= ASCII_LOWER_Z {
        code_unit - ASCII_CASE_DELTA
    } else {
        code_unit
    }
}

/// Compute the `exFAT` §7.2.5.3 table checksum over `bytes`.
///
/// Same rotate-right-then-add algorithm as the boot checksum
/// ([`crate::fs::exfat::boot_sector`] §3.4) but with no excluded
/// bytes.
#[must_use]
pub fn compute_table_checksum(bytes: &[u8]) -> u32 {
    let mut checksum: u32 = 0;
    for &byte in bytes {
        checksum = checksum.rotate_right(1).wrapping_add(u32::from(byte));
    }
    checksum
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

    fn table() -> UpcaseTable {
        UpcaseTable::ascii_identity()
    }

    // ---------- Size invariants ----------

    #[test]
    fn table_is_exactly_131072_bytes() {
        let t = table();
        assert_eq!(t.size_bytes(), 131_072);
        assert_eq!(t.bytes().len(), UPCASE_TABLE_SIZE_BYTES);
    }

    #[test]
    fn entries_count_constant_squared_with_size() {
        assert_eq!(
            (UPCASE_TABLE_ENTRIES as usize) * BYTES_PER_ENTRY,
            UPCASE_TABLE_SIZE_BYTES
        );
    }

    // ---------- ASCII case folding ----------

    #[test]
    fn lowercase_a_through_z_fold_to_uppercase() {
        let t = table();
        for offset in 0..26_u16 {
            let lower = ASCII_LOWER_A + offset;
            let upper = ASCII_UPPER_A + offset;
            assert_eq!(
                t.uppercase(lower),
                upper,
                "lowercase {lower:#06x} folds to {upper:#06x}"
            );
        }
    }

    #[test]
    fn uppercase_a_through_z_fold_to_themselves() {
        let t = table();
        for offset in 0..26_u16 {
            let upper = ASCII_UPPER_A + offset;
            assert_eq!(t.uppercase(upper), upper);
        }
    }

    #[test]
    fn ascii_digits_fold_to_themselves() {
        let t = table();
        for digit in b'0'..=b'9' {
            assert_eq!(t.uppercase(u16::from(digit)), u16::from(digit));
        }
    }

    #[test]
    fn ascii_punctuation_folds_to_itself() {
        let t = table();
        for &punct in b"!?.,-_/\\" {
            assert_eq!(t.uppercase(u16::from(punct)), u16::from(punct));
        }
    }

    #[test]
    fn null_code_unit_folds_to_itself() {
        let t = table();
        assert_eq!(t.uppercase(0), 0);
    }

    #[test]
    fn space_folds_to_itself() {
        let t = table();
        assert_eq!(t.uppercase(0x0020), 0x0020);
    }

    // ---------- Non-ASCII identity ----------

    #[test]
    fn latin_1_supplement_lowercase_does_not_fold_in_ascii_table() {
        let t = table();
        // 'à' = U+00E0, 'É' = U+00C9. We deliberately do NOT fold
        // these — the ASCII-only table maps them to themselves.
        assert_eq!(t.uppercase(0x00E0), 0x00E0);
        assert_eq!(t.uppercase(0x00C9), 0x00C9);
    }

    #[test]
    fn cyrillic_codepoints_map_to_themselves() {
        let t = table();
        // U+0430 (а), U+0410 (А) — Cyrillic lowercase / uppercase.
        // We don't fold; we identity-map.
        assert_eq!(t.uppercase(0x0430), 0x0430);
        assert_eq!(t.uppercase(0x0410), 0x0410);
    }

    #[test]
    fn cjk_codepoints_map_to_themselves() {
        let t = table();
        assert_eq!(t.uppercase(0x4E2D), 0x4E2D); // 中
        assert_eq!(t.uppercase(0x6587), 0x6587); // 文
    }

    #[test]
    fn surrogate_range_maps_to_itself() {
        let t = table();
        for cu in [0xD800_u16, 0xDC00, 0xDFFF] {
            assert_eq!(t.uppercase(cu), cu);
        }
    }

    #[test]
    fn last_code_unit_in_bmp_maps_to_itself() {
        let t = table();
        assert_eq!(t.uppercase(0xFFFF), 0xFFFF);
    }

    // ---------- Byte layout ----------

    #[test]
    fn entries_are_little_endian_in_bytes() {
        let t = table();
        // Entry at code unit 0x1234 should be byte 0x34 0x12.
        let entry_index = 0x1234_usize;
        let off = entry_index * BYTES_PER_ENTRY;
        // 0x1234 is not in the ASCII lowercase range, so it folds
        // to itself.
        assert_eq!(t.bytes()[off], 0x34);
        assert_eq!(t.bytes()[off + 1], 0x12);
    }

    #[test]
    fn entry_for_lower_a_is_upper_a_little_endian() {
        let t = table();
        let off = (ASCII_LOWER_A as usize) * BYTES_PER_ENTRY;
        assert_eq!(t.bytes()[off], 0x41);
        assert_eq!(t.bytes()[off + 1], 0x00);
    }

    // ---------- Checksum ----------

    #[test]
    fn checksum_matches_independent_reference() {
        let t = table();
        let reference = reference_table_checksum(t.bytes());
        assert_eq!(t.checksum(), reference);
    }

    #[test]
    fn checksum_function_handles_empty_input() {
        assert_eq!(compute_table_checksum(&[]), 0);
    }

    #[test]
    fn checksum_function_handles_single_byte() {
        // Hand-computed: ((0 >> 1) | (0 << 31)) + 0xAA = 0xAA.
        assert_eq!(compute_table_checksum(&[0xAA]), 0xAA);
    }

    #[test]
    fn checksum_function_handles_two_bytes() {
        // After byte 0xAA: sum = 0xAA.
        // After byte 0xBB: rotate_right(1) of 0xAA = 0x55
        //   (0xAA = 0b10101010 → rotated = 0b01010101 = 0x55,
        //    plus high bit = 0 since 0xAA is even)
        // 0x55 + 0xBB = 0x110, masked to u32 = 0x110.
        assert_eq!(compute_table_checksum(&[0xAA, 0xBB]), 0x110);
    }

    #[test]
    fn checksum_changes_when_a_byte_changes() {
        let t = table();
        let original = t.checksum();
        let mut modified = t.bytes().to_vec();
        modified[100] ^= 0x55;
        let new_sum = compute_table_checksum(&modified);
        assert_ne!(original, new_sum);
    }

    #[test]
    fn checksum_is_deterministic_across_two_independent_builds() {
        let t1 = UpcaseTable::ascii_identity();
        let t2 = UpcaseTable::ascii_identity();
        assert_eq!(t1.checksum(), t2.checksum());
    }

    /// Independent reference implementation of `exFAT` §7.2.5.3.
    fn reference_table_checksum(bytes: &[u8]) -> u32 {
        let mut sum: u32 = 0;
        for &byte in bytes {
            let rotated = if sum & 1 != 0 {
                (sum >> 1) | 0x8000_0000
            } else {
                sum >> 1
            };
            sum = rotated.wrapping_add(u32::from(byte));
        }
        sum
    }

    // ---------- Determinism ----------

    #[test]
    fn two_builds_produce_identical_bytes() {
        let t1 = UpcaseTable::ascii_identity();
        let t2 = UpcaseTable::ascii_identity();
        assert_eq!(t1.bytes(), t2.bytes());
    }

    // ---------- Debug formatter doesn't dump the whole 128 KiB ----------

    #[test]
    fn debug_impl_is_concise() {
        let t = table();
        let s = format!("{t:?}");
        assert!(s.contains("UpcaseTable"));
        assert!(s.contains("size_bytes"));
        assert!(s.contains("checksum"));
        // Sanity bound — debug output must not include thousands
        // of bytes.
        assert!(s.len() < 256, "Debug output too verbose: {} bytes", s.len());
    }
}
