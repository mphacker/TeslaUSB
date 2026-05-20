//! The `Bucket` enum — which Tesla directory a clip lives in.
//!
//! Stored as TEXT in the `bucket` column of the `clips` table.
//! The enum prevents the cleanup worker from ever passing a
//! misspelled bucket name to a delete query.

use super::types::{Result, StoreError};

/// Which Tesla bucket a clip lives in.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Bucket {
    /// `RecentClips` — the rolling 1-hour dashcam buffer Tesla
    /// overwrites in place. The cleanup worker may delete
    /// no-GPS clips here once they age out.
    Recent,
    /// `SavedClips` — clips the driver explicitly saved (horn
    /// tap). Never deleted by the cleanup worker.
    Saved,
    /// `SentryClips` — clips Sentry mode triggered. Never
    /// deleted by the cleanup worker.
    Sentry,
}

impl Bucket {
    /// Stable DB string representation. NEVER change these
    /// without writing a migration.
    #[must_use]
    pub const fn as_db_str(self) -> &'static str {
        match self {
            Self::Recent => "recent",
            Self::Saved => "saved",
            Self::Sentry => "sentry",
        }
    }

    /// Tesla on-disk directory name. These are dictated by
    /// the Tesla firmware and MUST match exactly.
    #[must_use]
    pub const fn tesla_dir_name(self) -> &'static str {
        match self {
            Self::Recent => "RecentClips",
            Self::Saved => "SavedClips",
            Self::Sentry => "SentryClips",
        }
    }

    /// Inverse of [`Self::tesla_dir_name`]. Used by the
    /// watcher to map an event's path back to a bucket.
    #[must_use]
    pub fn from_tesla_dir_name(name: &str) -> Option<Self> {
        match name {
            "RecentClips" => Some(Self::Recent),
            "SavedClips" => Some(Self::Saved),
            "SentryClips" => Some(Self::Sentry),
            _ => None,
        }
    }

    /// All three buckets in a stable order. Used by the
    /// indexer's bootstrap walk.
    #[must_use]
    pub const fn all() -> [Self; 3] {
        [Self::Recent, Self::Saved, Self::Sentry]
    }

    /// Parse the DB-stored string back to a [`Bucket`]. Used
    /// by store queries that return rows. Crate-private:
    /// callers outside the store layer use the typed API.
    pub(super) fn from_db_str(s: &str) -> Result<Self> {
        match s {
            "recent" => Ok(Self::Recent),
            "saved" => Ok(Self::Saved),
            "sentry" => Ok(Self::Sentry),
            other => Err(StoreError::UnknownBucket(other.to_string())),
        }
    }
}
