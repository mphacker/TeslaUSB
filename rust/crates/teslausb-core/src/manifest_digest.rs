//! Canonical `manifest_digest_v1`: the stable 128-bit digest over a Tesla event
//! folder's normalised file manifest.
//!
//! This is the ONE authoritative definition of the manifest digest that two
//! crates must agree on byte-for-byte:
//!
//! * `retentiond` computes it at archive time from the on-disk event folder
//!   (`retentiond::manifest::DirManifest::digest`, whose hand-written fold is
//!   pinned to this one by the shared [`MANIFEST_DIGEST_V1_GOLDEN`] vector), and
//! * `indexd` reconstructs it from a sealed cloud upload set to prove the set is
//!   complete (no child omitted, none substituted) before authorising cloud
//!   durability.
//!
//! The digest is **FNV-1a-128** over the entries sorted by `rel_name`, with
//! unambiguous field separators (`0xff` after the name, `0xfe` after each
//! entry) so distinct manifests cannot alias by run-together fields. It is a
//! completeness/identity check for a **trusted producer**, NOT an adversarial
//! (collision-resistant) binding — per-file byte-integrity is the SHA-256
//! carried and verified separately.

/// One file in a manifest, as folded into [`manifest_digest_v1`].
#[derive(Debug, Clone, Copy)]
pub struct ManifestDigestEntry<'a> {
    /// File name relative to the event folder (e.g. `front.mp4`). Folded as raw
    /// UTF-8 bytes with NO normalisation — case, Unicode form, and path
    /// separators are all significant, so `A.mp4` ≠ `a.mp4`.
    pub rel_name: &'a str,
    /// File length in bytes.
    pub size: u64,
    /// Modification time in milliseconds (any epoch; only the exact `i64` value
    /// matters, and it may be negative for a pre-epoch mtime).
    pub mtime_ms: i64,
    /// The 32 raw bytes of the file's content hash (SHA-256).
    pub hash: [u8; 32],
}

/// FNV-1a-128 offset basis.
const OFFSET: u128 = 0x6c62_272e_07bb_0142_62b8_2175_6295_c58d;
/// FNV-1a-128 prime.
const PRIME: u128 = 0x0000_0000_0100_0000_0000_0000_0000_013b;

/// The canonical 128-bit manifest digest over `entries`.
///
/// The entries are sorted by `rel_name` internally, so observation order never
/// affects the result. Empty input yields the bare offset basis.
#[must_use]
pub fn manifest_digest_v1(entries: &[ManifestDigestEntry<'_>]) -> u128 {
    let mut sorted: Vec<ManifestDigestEntry<'_>> = entries.to_vec();
    sorted.sort_by(|a, b| a.rel_name.cmp(b.rel_name));
    let mut h = OFFSET;
    let mut fold = |bytes: &[u8]| {
        for &b in bytes {
            h ^= u128::from(b);
            h = h.wrapping_mul(PRIME);
        }
    };
    for e in &sorted {
        fold(e.rel_name.as_bytes());
        fold(&[0xff]);
        fold(&e.size.to_le_bytes());
        fold(&e.mtime_ms.to_le_bytes());
        fold(&e.hash);
        fold(&[0xfe]);
    }
    h
}

/// [`manifest_digest_v1`] rendered as the canonical lowercase, zero-padded
/// 32-hex string used on the wire and stored in `archive_items.manifest_digest`.
#[must_use]
pub fn manifest_digest_v1_hex(entries: &[ManifestDigestEntry<'_>]) -> String {
    format!("{:032x}", manifest_digest_v1(entries))
}

/// Pinned golden vector: the 32-hex digest of the two-entry manifest
/// `[("back.mp4", 20, 2, [0x02; 32]), ("front.mp4", 10, 1, [0x01; 32])]`.
///
/// Asserted in both this crate and `retentiond::manifest` so that any drift in
/// either fold implementation fails a test immediately.
pub const MANIFEST_DIGEST_V1_GOLDEN: &str = "cda62d2b9624b94bc04f823b50b2a17a";

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{
        MANIFEST_DIGEST_V1_GOLDEN, ManifestDigestEntry, manifest_digest_v1, manifest_digest_v1_hex,
    };

    fn e(rel_name: &str, size: u64, mtime_ms: i64, h: u8) -> ManifestDigestEntry<'_> {
        ManifestDigestEntry {
            rel_name,
            size,
            mtime_ms,
            hash: [h; 32],
        }
    }

    #[test]
    fn golden_vector_pins_v1_digest() {
        let entries = [e("back.mp4", 20, 2, 0x02), e("front.mp4", 10, 1, 0x01)];
        assert_eq!(manifest_digest_v1_hex(&entries), MANIFEST_DIGEST_V1_GOLDEN);
    }

    #[test]
    fn digest_is_order_independent() {
        let a = [e("back.mp4", 20, 2, 0x02), e("front.mp4", 10, 1, 0x01)];
        let b = [e("front.mp4", 10, 1, 0x01), e("back.mp4", 20, 2, 0x02)];
        assert_eq!(manifest_digest_v1(&a), manifest_digest_v1(&b));
    }

    #[test]
    fn digest_changes_on_any_field() {
        let base = [e("front.mp4", 10, 1, 0x01)];
        assert_ne!(
            manifest_digest_v1(&base),
            manifest_digest_v1(&[e("front.mp4", 11, 1, 0x01)])
        );
        assert_ne!(
            manifest_digest_v1(&base),
            manifest_digest_v1(&[e("front.mp4", 10, 2, 0x01)])
        );
        assert_ne!(
            manifest_digest_v1(&base),
            manifest_digest_v1(&[e("front.mp4", 10, 1, 0x09)])
        );
        assert_ne!(
            manifest_digest_v1(&base),
            manifest_digest_v1(&[e("rear.mp4", 10, 1, 0x01)])
        );
    }

    #[test]
    fn negative_mtime_folds_deterministically() {
        // A pre-1970 mtime is a legal i64 and must fold, not be rejected.
        let a = [e("front.mp4", 10, -1, 0x01)];
        let b = [e("front.mp4", 10, -1, 0x01)];
        assert_eq!(manifest_digest_v1(&a), manifest_digest_v1(&b));
        assert_ne!(
            manifest_digest_v1(&a),
            manifest_digest_v1(&[e("front.mp4", 10, 1, 0x01)])
        );
    }
}
