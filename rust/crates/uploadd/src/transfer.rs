//! The **transfer backend seam** ([`Uploader`]) and integrity verification.
//!
//! A transfer is resumable and checksum-verified ([`uploadd.md`] §2.2): the
//! engine pushes the source in chunks (paced under the `WiFi` cap), then asks the
//! backend to [`Uploader::finalize`] and return the remote-computed digest,
//! which is checked against the item's expected hash by [`verify_digest`]. A
//! mismatch (a corrupt or partial transfer) is detected and retried — never
//! flagged durable.
//!
//! # Backend choice — `rclone` vs. a small Rust uploader (OPEN / ASK-FIRST)
//!
//! `uploadd.md` §2.2 leaves the backend a **"choose at build"** decision, and
//! [`wifi-upload-throttle.md`] OQ-4 confirms the self-pacing implementation
//! (rclone `--bwlimit` vs. a Rust token bucket) is the builder's call. This lane
//! deliberately does **not** pick one — the decision is abstracted behind
//! [`Uploader`] and reported to the supervisor as an ASK-FIRST item. The two
//! options, for the record:
//!
//! * **`rclone`** — broadest provider coverage (S3, B2, Drive, `WebDAV`, …),
//!   battle-tested resumable transfers and `--bwlimit`, and it matches the Python
//!   reference (`cloud_rclone_service.py`). Cost: a large external binary on a
//!   RAM-constrained Pi Zero 2 W, and shelling out / parsing its output.
//! * **a small Rust uploader** — minimal footprint (no external process), tight
//!   control over chunking and the token-bucket pace, and a single static
//!   binary. Cost: we must implement (and maintain) per-provider auth and
//!   multipart/resumable semantics ourselves, narrowing provider breadth.
//!
//! Recommendation carried to the supervisor: **start with `rclone`** for
//! provider breadth and parity with the reference, keeping [`Uploader`] as the
//! seam so a Rust uploader can replace it later without touching the core. No
//! choice is hardcoded in this crate.
//!
//! [`uploadd.md`]: ../../../../docs/specs/uploadd.md
//! [`wifi-upload-throttle.md`]: ../../../../docs/specs/contracts/wifi-upload-throttle.md

use crate::error::TransferError;
use crate::source::ContentHash;

/// Which concrete transfer backend the live binary wires up. **No `Default`** —
/// the choice is an explicit build/ops decision (ASK-FIRST), never silently
/// defaulted by the core.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransferBackend {
    /// Shell out to `rclone` (broad provider support; larger footprint).
    Rclone,
    /// A small in-process Rust uploader (minimal footprint; narrower providers).
    RustUploader,
}

/// The resumable, chunked transfer backend. The live impl is `rclone` or a Rust
/// uploader (see module docs); tests inject a deterministic mock that can fail
/// mid-transfer or return a wrong digest.
///
/// There is **no** delete/remove method — `uploadd` never removes the Pi-side
/// source, and remote cleanup is a separate retention concern, not part of the
/// upload transfer path.
pub trait Uploader {
    /// Push `data` to the remote object `remote_key` at byte `offset`. Idempotent
    /// at the offset level: re-sending the same offset after a resume overwrites
    /// the same range, so a retry cannot corrupt or duplicate content.
    ///
    /// # Errors
    /// Returns [`TransferError::Chunk`] (carrying `offset`) on a transmit
    /// failure, so the queue can resume from the last good checkpoint.
    fn put_chunk(&self, remote_key: &str, offset: u64, data: &[u8]) -> Result<(), TransferError>;

    /// Finalize the remote object and return its **remote-computed** content
    /// digest over `total_bytes`, for integrity verification.
    ///
    /// # Errors
    /// Returns [`TransferError::Finalize`] if the object cannot be finalized.
    fn finalize(&self, remote_key: &str, total_bytes: u64) -> Result<ContentHash, TransferError>;
}

/// Outcome of the integrity check after a transfer finalizes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Integrity {
    /// The remote digest matched the expected hash — the upload is trustworthy.
    Verified,
    /// The remote digest did **not** match — corrupt/partial transfer. The
    /// engine resets the resume checkpoint and retries; it is **never** flagged
    /// durable.
    Corrupt,
}

/// Compare the remote-computed digest to the expected whole-file hash.
#[must_use]
pub fn verify_digest(expected: ContentHash, remote: ContentHash) -> Integrity {
    if expected == remote {
        Integrity::Verified
    } else {
        Integrity::Corrupt
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{Integrity, verify_digest};
    use crate::source::ContentHash;

    #[test]
    fn matching_digest_is_verified() {
        let h = ContentHash::new([7u8; 32]);
        assert_eq!(verify_digest(h, h), Integrity::Verified);
    }

    #[test]
    fn mismatched_digest_is_corrupt() {
        let a = ContentHash::new([1u8; 32]);
        let b = ContentHash::new([2u8; 32]);
        assert_eq!(verify_digest(a, b), Integrity::Corrupt);
    }
}
