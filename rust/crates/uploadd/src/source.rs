//! The Pi-side archive read seam and the **archive-root invariant guard**.
//!
//! Invariant ([`uploadd.md`] §3, §6): `uploadd` sources **only** from the
//! archive directory and **never** reads the live car LUN. This module makes
//! that a *structural* guarantee rather than a discipline: an archive read can
//! only be issued through an [`ArchivePath`], and an [`ArchivePath`] can only be
//! built by [`ArchiveRoot::resolve`], which rejects anything that would escape
//! the configured archive root (absolute paths, `..` traversal, root-drive
//! prefixes). The live car LUN is a *different* mount, so it is unreachable by
//! construction — there is no API to read it.
//!
//! [`uploadd.md`]: ../../../../docs/specs/uploadd.md

use crate::error::SourceError;

/// Stable identity of one archived item (the unit the queue, lease, and
/// durability flag all act on). Matches D1 `archive_items.id`; mirrors
/// `retentiond::io::ArchiveItemId` so the two converge.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ArchiveItemId(pub i64);

/// A content digest (e.g. SHA-256) of a file or stream. Treated as an opaque,
/// comparable identity; the live [`ArchiveSource`]/[`crate::transfer::Uploader`]
/// chooses the algorithm. Mirrors `retentiond::io::ContentHash`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ContentHash(pub [u8; 32]);

impl ContentHash {
    /// Construct from raw digest bytes.
    #[must_use]
    pub const fn new(bytes: [u8; 32]) -> Self {
        Self(bytes)
    }
}

/// The configured archive root. The **only** factory for an [`ArchivePath`];
/// holds the on-device prefix every archive read is confined to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArchiveRoot {
    root: String,
}

impl ArchiveRoot {
    /// Build an archive root from an absolute on-device path (e.g.
    /// `/mnt/archive`). The trailing separator is normalized away.
    #[must_use]
    pub fn new(root: impl Into<String>) -> Self {
        let mut root = root.into();
        while root.ends_with('/') && root.len() > 1 {
            root.pop();
        }
        Self { root }
    }

    /// Resolve an item-relative path under this root into a guarded
    /// [`ArchivePath`].
    ///
    /// # Errors
    /// Returns [`SourceError::OutsideArchiveRoot`] if `relative` is absolute,
    /// contains a `..` component, or otherwise does not stay strictly under the
    /// root — the guard that makes the live LUN unreachable.
    pub fn resolve(&self, relative: &str) -> Result<ArchivePath, SourceError> {
        if relative.is_empty() {
            return Err(SourceError::OutsideArchiveRoot(relative.to_owned()));
        }
        // Reject absolute paths (POSIX `/...` and Windows-style drive/UNC
        // prefixes) — only paths *relative to the archive root* are legal.
        let bytes = relative.as_bytes();
        let absolute =
            relative.starts_with('/') || relative.starts_with('\\') || bytes.get(1) == Some(&b':');
        if absolute {
            return Err(SourceError::OutsideArchiveRoot(relative.to_owned()));
        }
        // Reject any traversal that could climb out of the root. We treat both
        // separators as boundaries so a `..` cannot hide as `a\..`.
        for segment in relative.split(['/', '\\']) {
            if segment == ".." {
                return Err(SourceError::OutsideArchiveRoot(relative.to_owned()));
            }
        }
        Ok(ArchivePath {
            full: format!("{}/{relative}", self.root),
        })
    }
}

/// A filesystem path **proven** to live under the archive root.
///
/// There is no public constructor: the only way to obtain one is
/// [`ArchiveRoot::resolve`], so possessing an `ArchivePath` is evidence the
/// path passed the guard. The [`ArchiveSource`] seam only accepts these.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArchivePath {
    full: String,
}

impl ArchivePath {
    /// The resolved absolute path, for the live `ArchiveSource` to open.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.full
    }
}

/// Read seam over the Pi-side archive. The live impl opens files on the ext4
/// archive filesystem; tests inject an in-memory fake. **Read-only** — there is
/// deliberately no write/delete/unlink method, because `uploadd` never mutates
/// archive content (single-deleter = `retentiond`).
pub trait ArchiveSource {
    /// Total size, in bytes, of the item's source file.
    ///
    /// # Errors
    /// Propagates a [`SourceError`] if the file is missing or cannot be stat'd.
    fn size(&self, path: &ArchivePath) -> Result<u64, SourceError>;

    /// Read up to `len` bytes starting at `offset`. A short read (fewer than
    /// `len` bytes, e.g. at EOF) is legal and signalled by the returned length.
    ///
    /// # Errors
    /// Propagates a [`SourceError`] on an I/O failure.
    fn read_chunk(
        &self,
        path: &ArchivePath,
        offset: u64,
        len: usize,
    ) -> Result<Vec<u8>, SourceError>;
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::ArchiveRoot;

    #[test]
    fn resolve_keeps_paths_under_the_root() {
        let root = ArchiveRoot::new("/mnt/archive/");
        let p = root.resolve("SentryClips/2026-06-08/event.mp4").unwrap();
        assert_eq!(p.as_str(), "/mnt/archive/SentryClips/2026-06-08/event.mp4");
    }

    #[test]
    fn resolve_rejects_absolute_paths() {
        let root = ArchiveRoot::new("/mnt/archive");
        // An absolute path could point at the live LUN mount — rejected.
        assert!(root.resolve("/mnt/cam/live/recent.mp4").is_err());
        assert!(root.resolve("\\\\server\\share").is_err());
        assert!(root.resolve("C:/cam/live.mp4").is_err());
    }

    #[test]
    fn resolve_rejects_parent_traversal() {
        let root = ArchiveRoot::new("/mnt/archive");
        assert!(root.resolve("../cam/live.mp4").is_err());
        assert!(root.resolve("SavedClips/../../cam/live.mp4").is_err());
        assert!(root.resolve("a\\..\\..\\cam").is_err());
    }

    #[test]
    fn resolve_rejects_empty() {
        assert!(ArchiveRoot::new("/mnt/archive").resolve("").is_err());
    }
}
