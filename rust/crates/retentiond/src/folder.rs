//! `TeslaCam` folder identity and its **per-folder capability predicates**.
//!
//! The single most important correctness rule in `retentiond` is that the four
//! `TeslaCam` folders are **not interchangeable** ([`docs/specs/retentiond.md`]
//! §3). `RecentClips` is a car-rotated rolling buffer we must *never* delete from
//! the car; `SavedClips` is the highest-value, last-to-be-pruned class;
//! `SentryClips` can flood and is archived after Saved; `TeslaTrackMode` behaves
//! like a non-rotated event folder ranked between Sentry and Recent.
//!
//! Rather than a single broad "folder policy" object (which makes it easy to
//! apply the wrong rule), each distinct decision is a small, explicit predicate
//! keyed on [`FolderClass`]. In particular, **only an event folder can ever
//! become a car-side delete candidate** — `RecentClips` fails closed by type.

/// Source-folder classification of a clip on the car-visible volume.
///
/// Names match contract D1 (`clips.folder_class`). The Pi-side destination
/// (`ArchivedClips`) is intentionally *not* part of this enum: it is a copy
/// location, not a source policy.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum FolderClass {
    /// `SavedClips` — user pressed Save/honk. Highest value; kept on the car by
    /// default; its local archive copy is the **last of the no-loss class** to be
    /// pruned and is **never** auto-evicted while undurable.
    SavedClips,
    /// `SentryClips` — Sentry events; numerous and can flood the volume. Archived
    /// after Saved; car-side deleted after a verified pass under space pressure.
    SentryClips,
    /// `TeslaTrackMode` — track-mode recordings; not rotated by the car. Archived
    /// + verified like Sentry, at a priority between Sentry and Recent.
    TeslaTrackMode,
    /// `RecentClips` — the rolling dashcam buffer the car overwrites continuously.
    /// **Never** car-side deleted by us; mirrored best-effort into the archive.
    RecentClips,
}

/// Backpressure / scheduling rank from [`docs/specs/retentiond.md`] §3.5, lower
/// ordinal = higher priority. Rank `1` (car writes / FS integrity) and rank `7`
/// (cloud upload, owned by `uploadd`) are outside this enum — they bound it.
///
/// `RecentClips` splits into two ranks because event-adjacent segments (context
/// around a Saved/Sentry event) outrank the generic rolling mirror.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[repr(u8)]
pub enum ArchivePriority {
    /// `SavedClips` archive + verify.
    SavedClips = 2,
    /// `SentryClips` archive + verify.
    SentryClips = 3,
    /// `TeslaTrackMode` archive + verify.
    TeslaTrackMode = 4,
    /// `RecentClips` event-adjacent windows (context around an event).
    RecentEventAdjacent = 5,
    /// `RecentClips` generic rolling mirror.
    RecentGeneric = 6,
}

impl ArchivePriority {
    /// The §3.5 ordinal (lower wins under contention).
    #[must_use]
    pub const fn rank(self) -> u8 {
        self as u8
    }
}

impl FolderClass {
    /// The D1 `folder_class` wire string.
    #[must_use]
    pub const fn as_db_str(self) -> &'static str {
        match self {
            Self::SavedClips => "SavedClips",
            Self::SentryClips => "SentryClips",
            Self::TeslaTrackMode => "TeslaTrackMode",
            Self::RecentClips => "RecentClips",
        }
    }

    /// Classify from a clip's directory path (case-insensitive, most specific
    /// first). An unrecognised path defaults to [`Self::RecentClips`] — the
    /// **safest** default, since Recent is never car-side deleted, so a
    /// mis-classification can never cause us to delete footage from the car.
    #[must_use]
    pub fn from_path(path: &str) -> Self {
        let lower = path.to_ascii_lowercase();
        if lower.contains("sentryclips") {
            Self::SentryClips
        } else if lower.contains("savedclips") {
            Self::SavedClips
        } else if lower.contains("teslatrackmode") || lower.contains("trackclips") {
            Self::TeslaTrackMode
        } else {
            Self::RecentClips
        }
    }

    /// Whether this folder is an **event folder** the car does *not* rotate
    /// (`SavedClips` / `SentryClips` / `TeslaTrackMode`). Only event folders are
    /// archived-and-verified for car-side deletion; `RecentClips` is mirrored.
    #[must_use]
    pub const fn is_event_folder(self) -> bool {
        matches!(
            self,
            Self::SavedClips | Self::SentryClips | Self::TeslaTrackMode
        )
    }

    /// Whether `retentiond` may **ever** request a car-side delete of this
    /// folder's footage (always via the `gadgetd` handoff, only after a verified
    /// pass under pressure). **`RecentClips` is always `false`** — the car owns
    /// and rotates it ([`docs/specs/retentiond.md`] §3.3).
    #[must_use]
    pub const fn may_car_delete(self) -> bool {
        self.is_event_folder()
    }

    /// Whether this folder is archived through the **copy + verify** event path
    /// (true for event folders) versus the best-effort rolling mirror
    /// (`RecentClips`, false).
    #[must_use]
    pub const fn archives_with_verify(self) -> bool {
        self.is_event_folder()
    }

    /// The base archive priority for this folder. `RecentClips` maps to the
    /// *generic* mirror rank; event-adjacent Recent segments are promoted
    /// separately by the mirror scheduler ([`crate::recent`]).
    #[must_use]
    pub const fn base_archive_priority(self) -> ArchivePriority {
        match self {
            Self::SavedClips => ArchivePriority::SavedClips,
            Self::SentryClips => ArchivePriority::SentryClips,
            Self::TeslaTrackMode => ArchivePriority::TeslaTrackMode,
            Self::RecentClips => ArchivePriority::RecentGeneric,
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{ArchivePriority, FolderClass};

    #[test]
    fn recent_is_never_car_deletable() {
        assert!(!FolderClass::RecentClips.may_car_delete());
        assert!(!FolderClass::RecentClips.archives_with_verify());
        assert!(!FolderClass::RecentClips.is_event_folder());
    }

    #[test]
    fn event_folders_are_verify_archived_and_car_deletable() {
        for f in [
            FolderClass::SavedClips,
            FolderClass::SentryClips,
            FolderClass::TeslaTrackMode,
        ] {
            assert!(f.is_event_folder(), "{f:?}");
            assert!(f.may_car_delete(), "{f:?}");
            assert!(f.archives_with_verify(), "{f:?}");
        }
    }

    #[test]
    fn priority_order_matches_spec_3_5() {
        // Saved < Sentry < TrackMode < RecentEventAdjacent < RecentGeneric.
        assert!(ArchivePriority::SavedClips < ArchivePriority::SentryClips);
        assert!(ArchivePriority::SentryClips < ArchivePriority::TeslaTrackMode);
        assert!(ArchivePriority::TeslaTrackMode < ArchivePriority::RecentEventAdjacent);
        assert!(ArchivePriority::RecentEventAdjacent < ArchivePriority::RecentGeneric);
        assert_eq!(ArchivePriority::SavedClips.rank(), 2);
        assert_eq!(ArchivePriority::RecentGeneric.rank(), 6);
    }

    #[test]
    fn unknown_path_defaults_to_recent_the_safe_class() {
        // A path we cannot classify must NOT become car-deletable.
        let c = FolderClass::from_path("TeslaCam/MysteryFolder/2026-06-01_20-10-04");
        assert_eq!(c, FolderClass::RecentClips);
        assert!(!c.may_car_delete());
    }

    #[test]
    fn classifies_known_paths() {
        assert_eq!(
            FolderClass::from_path("TeslaCam/SavedClips/2026"),
            FolderClass::SavedClips
        );
        assert_eq!(
            FolderClass::from_path("TeslaCam/SentryClips/2026"),
            FolderClass::SentryClips
        );
        assert_eq!(
            FolderClass::from_path("TeslaCam/TeslaTrackMode/2026"),
            FolderClass::TeslaTrackMode
        );
    }
}
