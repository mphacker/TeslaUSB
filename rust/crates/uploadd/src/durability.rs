//! The **durability signal** (`UPLOADED_VERIFIED`) `uploadd` raises on a
//! remotely-verified upload, and the [`DurabilityClient`] seam that carries it to
//! `indexd`.
//!
//! On a verified success, `uploadd` asks `indexd` to set
//! `archive_items.durable = 1` ([`single-writer-lease.md`] §6,
//! [`uploadd.md`] §2.2). That flag — and **only** that flag — is what later lets
//! `retentiond` treat the local copy as safe to evict under the durability
//! floor. `uploadd` has **no other authority over the file**: it never deletes
//! it (single-deleter = `retentiond`); it merely records that a durable
//! off-device copy now exists.
//!
//! [`Durability`] mirrors `retentiond::durability::Durability` so the producer
//! (`uploadd`) and the consumer (`retentiond`) agree on the exact two-state
//! meaning; conflating "archived" with "durable" is the loss this guards.
//!
//! [`single-writer-lease.md`]: ../../../../docs/specs/contracts/single-writer-lease.md
//! [`uploadd.md`]: ../../../../docs/specs/uploadd.md

use serde::{Deserialize, Serialize};

use crate::error::IndexError;
use crate::source::ArchiveItemId;

/// Whether a **durable off-device copy** of an item exists. `uploadd` flips this
/// to [`Self::Durable`] on a remotely-verified upload. Mirrors
/// `retentiond::durability::Durability`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Durability {
    /// Only the local Pi-side copy exists. Eviction would be permanent loss.
    Undurable,
    /// A durable copy exists off-device (uploaded + remotely verified).
    Durable,
}

impl Durability {
    /// Whether a durable off-device copy exists.
    #[must_use]
    pub const fn is_durable(self) -> bool {
        matches!(self, Self::Durable)
    }
}

/// The `indexd` RPC seam for the durability flag. The live impl is a UDS RPC to
/// `indexd` (the sole `SQLite` writer); tests inject a fake that records calls.
pub trait DurabilityClient {
    /// Idempotently mark `item` as `UPLOADED_VERIFIED` (durable). Safe to
    /// re-apply: marking an already-durable item is a no-op, so a crash between
    /// the upload and this call simply re-marks on resume.
    ///
    /// # Errors
    /// Propagates an [`IndexError`] if the RPC/transaction fails (the item stays
    /// `Undurable`; `retentiond` will not evict it — fail-safe).
    fn mark_uploaded_verified(&self, item: ArchiveItemId) -> Result<(), IndexError>;
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::Durability;

    #[test]
    fn durable_predicate() {
        assert!(Durability::Durable.is_durable());
        assert!(!Durability::Undurable.is_durable());
    }
}
