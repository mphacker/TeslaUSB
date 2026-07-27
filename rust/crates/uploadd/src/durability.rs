//! The **durability state** (`UPLOADED_VERIFIED`) that governs whether a local
//! archive copy may ever be evicted.
//!
//! `uploadd` deliberately has **no seam that sets this flag**. Under the v7
//! sealed-upload-set contract, `archive_items.durable` flips `0 → 1` through
//! exactly one path — `indexd`'s `cloud_finalize_parent_upload`, which re-checks
//! the full COMPLETE predicate against an immutable sealed snapshot and is
//! enforced by a DB trigger ([`indexd-cloud-schema.md`] §7.4). An earlier
//! per-child `mark_uploaded_verified` seam was removed precisely because a
//! child-level mark could confer durability before the whole set had landed.
//!
//! What `uploadd` records instead is *evidence*: `QueueStore::commit` writes the
//! observed hash, size and attempt id for one child object. Durability is then a
//! conclusion `indexd` draws from that evidence — never an assertion `uploadd`
//! makes. `uploadd` has no other authority over the file: it never deletes it
//! (single-deleter = `retentiond`).
//!
//! [`Durability`] mirrors `retentiond::durability::Durability` so the two agree
//! on the exact two-state meaning; conflating "archived" with "durable" is the
//! loss this guards.
//!
//! [`indexd-cloud-schema.md`]: ../../../../docs/specs/contracts/indexd-cloud-schema.md
//! [`uploadd.md`]: ../../../../docs/specs/uploadd.md

use serde::{Deserialize, Serialize};

/// Whether a **durable off-device copy** of an item exists. Set by `indexd` on a
/// finalized sealed upload set, never by `uploadd`. Mirrors
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
