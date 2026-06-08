//! Shared low-level I/O value types, plus a single `io::` facade that
//! re-exports every side-effect **seam** trait used by the policy core.
//!
//! The seams themselves are defined next to the pure orchestration they serve
//! (mirroring `gadgetd`, where `LunControl`/`ImageMutator` live beside
//! `run_handoff`): [`ArchiveStore`]/[`CarDeleteHandoff`] in [`crate::archive`],
//! [`Statfs`] in [`crate::governor`], [`ArchiveDeleteOps`]/[`IndexClient`] in
//! [`crate::delete`], and [`Clock`] in [`crate::time`]. They are re-exported here
//! so callers (and the live binary) can depend on one `io` module.

pub use crate::archive::{ArchiveStore, CarDeleteHandoff};
pub use crate::delete::{ArchiveDeleteOps, IndexClient};
pub use crate::governor::Statfs;
pub use crate::time::Clock;

/// A content digest of a file (e.g. SHA-256). The policy core treats it as an
/// opaque, comparable identity; the live [`ArchiveStore`] chooses the algorithm.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ContentHash(pub [u8; 32]);

impl ContentHash {
    /// Construct from raw digest bytes.
    #[must_use]
    pub const fn new(bytes: [u8; 32]) -> Self {
        Self(bytes)
    }
}

/// The current identity of a file: its byte length and content hash. Used to
/// re-validate that a source file still matches its manifest after a copy.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct FileIdentity {
    /// File length in bytes.
    pub size: u64,
    /// Content hash over the whole file.
    pub hash: ContentHash,
}

/// A point-in-time `statfs` reading for one path, grouped by device id.
///
/// Both **bytes** and **inodes** are carried because thumbnails and Recent
/// segments can exhaust inodes long before bytes ([`docs/specs/storage.md`] §2).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FsStat {
    /// `st_dev` of the filesystem (used to collapse paths that share a device
    /// into one budget — [`docs/specs/storage.md`] §2).
    pub dev_id: u64,
    /// Free bytes available to an unprivileged writer.
    pub free_bytes: u64,
    /// Total bytes of the filesystem.
    pub total_bytes: u64,
    /// Free inodes.
    pub free_inodes: u64,
    /// Total inodes.
    pub total_inodes: u64,
}

impl FsStat {
    /// Free space as a fraction of total (0.0..=1.0); `0.0` if `total_bytes` is 0.
    #[must_use]
    pub fn free_bytes_frac(&self) -> f64 {
        if self.total_bytes == 0 {
            0.0
        } else {
            // Precision loss here is acceptable: this drives a coarse tier
            // comparison, never an exact byte accounting.
            #[allow(clippy::cast_precision_loss)]
            {
                self.free_bytes as f64 / self.total_bytes as f64
            }
        }
    }

    /// Free inodes as a fraction of total (0.0..=1.0); `0.0` if `total_inodes`
    /// is 0 (a filesystem that does not report inodes, e.g. some FUSE mounts).
    #[must_use]
    pub fn free_inodes_frac(&self) -> f64 {
        if self.total_inodes == 0 {
            0.0
        } else {
            #[allow(clippy::cast_precision_loss)]
            {
                self.free_inodes as f64 / self.total_inodes as f64
            }
        }
    }
}

/// Stable identity of one archived item (the unit the governor evicts and the
/// lease/delete-state protocol acts on). Matches D1 `archive_items.id`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ArchiveItemId(pub i64);
