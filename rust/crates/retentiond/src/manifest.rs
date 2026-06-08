//! Slice 6.1a — **stable directory manifests** and the cross-pass stability gate.
//!
//! A "complete event folder" is defined strictly ([`docs/specs/retentiond.md`]
//! §3): an event is eligible for archiving only when its **full directory
//! manifest** — the set of files, plus each file's size, mtime, and content hash
//! — is **unchanged across consecutive `scannerd` passes**. This guards against
//! late-arriving `event.json`, thumbnails, or extra camera angles, and it is what
//! lets a *verified archive pass* (in [`crate::archive`]) bind to a fixed set of
//! bytes before any car-side delete is ever considered.
//!
//! The camera set is **not** hard-coded (Cybertruck/newer add cameras): a
//! [`DirManifest`] is an opaque, complete list of whatever regular files are
//! present, normalised so two observations compare logically.

use std::collections::HashMap;

use crate::io::ContentHash;

/// One regular file inside an event folder, as observed by `scannerd`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManifestEntry {
    /// File name relative to the event folder (e.g. `front.mp4`, `event.json`).
    pub rel_name: String,
    /// File length in bytes.
    pub size: u64,
    /// Modification time, milliseconds (whatever epoch `scannerd` reports; only
    /// equality across passes matters, not the absolute value).
    pub mtime_ms: i64,
    /// Content hash over the whole file.
    pub hash: ContentHash,
}

/// A 128-bit logical digest of a whole manifest. Two manifests with the same
/// set of `(name, size, mtime, hash)` tuples produce the same digest regardless
/// of observation order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ManifestDigest(pub u128);

/// The complete set of files in one event folder at a point in time.
///
/// Construction **sorts by name** so observation order never affects equality or
/// the [`ManifestDigest`]. An empty manifest is legal (an empty/forming folder)
/// but never counts as complete for archiving.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct DirManifest {
    entries: Vec<ManifestEntry>,
}

impl DirManifest {
    /// Build a manifest from observed entries, normalising order by file name.
    ///
    /// If the same name appears twice (a malformed listing), the entries are
    /// kept as-is after sorting — the digest will simply differ from a
    /// well-formed listing, so such a folder never looks "stable".
    #[must_use]
    pub fn from_entries(mut entries: Vec<ManifestEntry>) -> Self {
        entries.sort_by(|a, b| a.rel_name.cmp(&b.rel_name));
        Self { entries }
    }

    /// The files in this manifest, in normalised (name-sorted) order.
    #[must_use]
    pub fn entries(&self) -> &[ManifestEntry] {
        &self.entries
    }

    /// Whether the manifest has no files.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Number of files in the manifest.
    #[must_use]
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// Total bytes across all files.
    #[must_use]
    pub fn total_bytes(&self) -> u64 {
        self.entries
            .iter()
            .map(|e| e.size)
            .fold(0u64, u64::saturating_add)
    }

    /// A stable 128-bit logical digest over the normalised entries.
    #[must_use]
    pub fn digest(&self) -> ManifestDigest {
        // FNV-1a-128 over the sorted (name, size, mtime, hash) tuples. The
        // separators make field boundaries unambiguous so distinct manifests
        // cannot alias by run-together fields.
        const OFFSET: u128 = 0x6c62_272e_07bb_0142_62b8_2175_6295_c58d;
        const PRIME: u128 = 0x0000_0000_0100_0000_0000_0000_0000_013b;
        let mut h = OFFSET;
        let mut fold = |bytes: &[u8]| {
            for &b in bytes {
                h ^= u128::from(b);
                h = h.wrapping_mul(PRIME);
            }
        };
        for e in &self.entries {
            fold(e.rel_name.as_bytes());
            fold(&[0xff]);
            fold(&e.size.to_le_bytes());
            fold(&e.mtime_ms.to_le_bytes());
            fold(&e.hash.0);
            fold(&[0xfe]);
        }
        ManifestDigest(h)
    }

    /// Look up the expected identity-bearing entry for `rel_name` (used to
    /// re-validate a source file against the manifest after copy).
    #[must_use]
    pub fn entry(&self, rel_name: &str) -> Option<&ManifestEntry> {
        self.entries.iter().find(|e| e.rel_name == rel_name)
    }
}

/// Outcome of observing a folder's manifest on one scan pass.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ManifestStability {
    /// The manifest changed (or has not yet held steady long enough). Carries
    /// how many consecutive identical passes have been seen so far.
    Unstable {
        /// Consecutive identical observations of the current manifest.
        stable_passes: u32,
    },
    /// The manifest has been identical for `required_stable_passes` consecutive
    /// passes and is non-empty — eligible for a verified archive pass.
    Stable {
        /// The digest that became stable (binds a subsequent verified pass).
        digest: ManifestDigest,
    },
}

/// Per-folder cross-pass tracker. One instance lives for the daemon's lifetime;
/// [`Self::observe`] is called once per `scannerd` pass per folder.
#[derive(Debug)]
pub struct ManifestTracker {
    required_stable_passes: u32,
    states: HashMap<String, FolderState>,
}

#[derive(Debug, Clone, Copy)]
struct FolderState {
    digest: ManifestDigest,
    stable_passes: u32,
    empty: bool,
}

impl ManifestTracker {
    /// Create a tracker requiring `required_stable_passes` (≥ 2) consecutive
    /// identical observations before a manifest is declared stable.
    #[must_use]
    pub fn new(required_stable_passes: u32) -> Self {
        Self {
            required_stable_passes: required_stable_passes.max(2),
            states: HashMap::new(),
        }
    }

    /// Observe `folder_key`'s current manifest. Returns [`ManifestStability`].
    ///
    /// A change in the manifest (any file added/removed, or any size/mtime/hash
    /// changing) **resets** the streak — so a folder still gaining a late
    /// `event.json` or an extra camera angle can never be declared stable
    /// prematurely. An **empty** manifest is never stable.
    pub fn observe(&mut self, folder_key: &str, manifest: &DirManifest) -> ManifestStability {
        let digest = manifest.digest();
        let empty = manifest.is_empty();
        let state = self
            .states
            .entry(folder_key.to_owned())
            .or_insert(FolderState {
                digest,
                stable_passes: 0,
                empty,
            });

        if state.digest == digest && state.empty == empty {
            state.stable_passes = state.stable_passes.saturating_add(1);
        } else {
            state.digest = digest;
            state.empty = empty;
            state.stable_passes = 1;
        }

        if !empty && state.stable_passes >= self.required_stable_passes {
            ManifestStability::Stable { digest }
        } else {
            ManifestStability::Unstable {
                stable_passes: state.stable_passes,
            }
        }
    }

    /// Drop tracking state for folders no longer observed (e.g. archived + car
    /// deleted), to bound memory. Keys not present are ignored.
    pub fn forget(&mut self, folder_key: &str) {
        self.states.remove(folder_key);
    }

    /// Number of folders currently tracked (diagnostics).
    #[must_use]
    pub fn tracked_len(&self) -> usize {
        self.states.len()
    }
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]
mod tests {
    use super::{DirManifest, ManifestEntry, ManifestStability, ManifestTracker};
    use crate::io::ContentHash;

    fn entry(name: &str, size: u64, mtime: i64, h: u8) -> ManifestEntry {
        ManifestEntry {
            rel_name: name.to_owned(),
            size,
            mtime_ms: mtime,
            hash: ContentHash::new([h; 32]),
        }
    }

    fn manifest(entries: Vec<ManifestEntry>) -> DirManifest {
        DirManifest::from_entries(entries)
    }

    #[test]
    fn digest_is_order_independent() {
        let a = manifest(vec![
            entry("front.mp4", 10, 1, 1),
            entry("back.mp4", 20, 2, 2),
        ]);
        let b = manifest(vec![
            entry("back.mp4", 20, 2, 2),
            entry("front.mp4", 10, 1, 1),
        ]);
        assert_eq!(a.digest(), b.digest());
    }

    #[test]
    fn digest_changes_on_any_field() {
        let base = manifest(vec![entry("front.mp4", 10, 1, 1)]);
        assert_ne!(
            base.digest(),
            manifest(vec![entry("front.mp4", 11, 1, 1)]).digest()
        );
        assert_ne!(
            base.digest(),
            manifest(vec![entry("front.mp4", 10, 2, 1)]).digest()
        );
        assert_ne!(
            base.digest(),
            manifest(vec![entry("front.mp4", 10, 1, 9)]).digest()
        );
        assert_ne!(
            base.digest(),
            manifest(vec![entry("rear.mp4", 10, 1, 1)]).digest()
        );
    }

    #[test]
    fn variable_camera_set_is_not_hard_coded() {
        // A 3-camera Model 3 and a 6-camera Cybertruck both produce valid,
        // distinct, stable manifests — nothing assumes a fixed camera set.
        let model3 = manifest(vec![
            entry("front.mp4", 1, 1, 1),
            entry("left_repeater.mp4", 1, 1, 2),
            entry("right_repeater.mp4", 1, 1, 3),
        ]);
        let cybertruck = manifest(vec![
            entry("front.mp4", 1, 1, 1),
            entry("back.mp4", 1, 1, 2),
            entry("left_repeater.mp4", 1, 1, 3),
            entry("right_repeater.mp4", 1, 1, 4),
            entry("left_pillar.mp4", 1, 1, 5),
            entry("right_pillar.mp4", 1, 1, 6),
        ]);
        assert_eq!(model3.len(), 3);
        assert_eq!(cybertruck.len(), 6);
        assert_ne!(model3.digest(), cybertruck.digest());
    }

    #[test]
    fn becomes_stable_only_after_required_passes() {
        let mut t = ManifestTracker::new(2);
        let m = manifest(vec![entry("front.mp4", 10, 1, 1)]);
        assert!(matches!(
            t.observe("ev1", &m),
            ManifestStability::Unstable { stable_passes: 1 }
        ));
        assert!(matches!(
            t.observe("ev1", &m),
            ManifestStability::Stable { .. }
        ));
    }

    #[test]
    fn late_arriving_file_resets_stability() {
        // The classic trap: a folder looks complete, then a late event.json or
        // extra camera angle arrives. It must NOT be declared stable across the
        // change.
        let mut t = ManifestTracker::new(2);
        let partial = manifest(vec![entry("front.mp4", 10, 1, 1)]);
        let full = manifest(vec![
            entry("front.mp4", 10, 1, 1),
            entry("event.json", 2, 1, 7),
        ]);
        assert!(matches!(
            t.observe("ev1", &partial),
            ManifestStability::Unstable { .. }
        ));
        // event.json appears on pass 2 → streak resets, not stable.
        assert!(matches!(
            t.observe("ev1", &full),
            ManifestStability::Unstable { stable_passes: 1 }
        ));
        // Now it holds steady → stable on the full manifest.
        assert!(matches!(
            t.observe("ev1", &full),
            ManifestStability::Stable { .. }
        ));
    }

    #[test]
    fn empty_manifest_is_never_stable() {
        let mut t = ManifestTracker::new(2);
        let empty = DirManifest::default();
        for _ in 0..5 {
            assert!(matches!(
                t.observe("ev1", &empty),
                ManifestStability::Unstable { .. }
            ));
        }
    }

    #[test]
    fn required_passes_floored_at_two() {
        // Even if a caller asks for 1, we never declare stable on a single
        // observation (a single look cannot rule out an in-flight write).
        let mut t = ManifestTracker::new(1);
        let m = manifest(vec![entry("front.mp4", 10, 1, 1)]);
        assert!(matches!(
            t.observe("ev1", &m),
            ManifestStability::Unstable { .. }
        ));
        assert!(matches!(
            t.observe("ev1", &m),
            ManifestStability::Stable { .. }
        ));
    }
}
