//! Two **independent** safety axes that gate the two kinds of deletion.
//!
//! It is a recurring bug to collapse these into one boolean, so they are
//! separate types ([`ArchiveVerification`] and [`Durability`]):
//!
//! - [`ArchiveVerification`] — "is the **Pi-side archive copy** trustworthy?" A
//!   `Verified` state means a full **verified archive pass** succeeded: every file
//!   in a *stable* directory manifest was copied, re-hashed at the destination,
//!   and the source re-validated afterwards ([`docs/specs/retentiond.md`] §3).
//!   This is what unlocks **car-side deletion** — we only delete from the car once
//!   the footage demonstrably survives in the archive.
//!
//! - [`Durability`] — "is there a **durable off-device copy**?" Set by `uploadd`
//!   when an upload is remotely verified ([`single-writer-lease.md`] §6). This is
//!   what unlocks **local-archive eviction** under the durability floor: an
//!   undurable `SavedClips` archive copy is **never** auto-evicted; undurable
//!   `SentryClips` only under Emergency + explicit opt-in
//!   ([`docs/specs/storage.md`] §3.1/§3.2).
//!
//! Conflating them would let "the file exists in the archive" masquerade as
//! "safe to delete from the car" or "safe to evict to reclaim space" — exactly
//! the loss this spec exists to prevent.

use serde::{Deserialize, Serialize};

/// A random 128-bit token identifying one successful verified archive pass.
///
/// Bound to the exact directory manifest that was verified, so a later manifest
/// change invalidates the pass. Random (never wall-clock) because the Pi has no
/// RTC — a clock reset must never collide two pass identities.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct VerifiedPassId(pub u128);

/// Whether the **Pi-side archive copy** of an event folder is trustworthy.
///
/// Only [`Self::Verified`] makes the event eligible for car-side deletion.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum ArchiveVerification {
    /// Not yet archived, or archived but not yet verified against a stable
    /// manifest (or the manifest changed and the pass was restarted).
    Unverified,
    /// A verified archive pass completed against a stable directory manifest.
    Verified {
        /// Identity of the pass (bound to the verified manifest).
        pass: VerifiedPassId,
    },
}

impl ArchiveVerification {
    /// Whether a verified archive pass exists — the precondition for any
    /// car-side delete request.
    #[must_use]
    pub const fn is_verified(self) -> bool {
        matches!(self, Self::Verified { .. })
    }
}

/// Whether a **durable off-device copy** of an item exists.
///
/// `uploadd` flips this to [`Self::Durable`] on a remotely-verified upload; it is
/// the gate for evicting the local archive copy to reclaim space.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Durability {
    /// Only the local Pi-side copy exists. Eviction of this copy would be
    /// permanent loss.
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
