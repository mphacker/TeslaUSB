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

use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

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

static DURABLE_TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Write JSON durably (`fsync(temp)` + rename + `fsync(parent)`).
///
/// Ensures parent directories exist and syncs the created directory chain
/// before publishing the renamed file.
///
/// # Errors
///
/// Returns an error when parent resolution, serialization, write/sync, or
/// rename fails.
pub fn write_json_durable<T: Serialize>(path: &Path, value: &T) -> io::Result<()> {
    let parent = path.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "path for durable json must include a parent directory",
        )
    })?;
    let sync_root = nearest_existing_ancestor(parent)?;
    fs::create_dir_all(parent)?;

    let canonical_sync_root = sync_root.canonicalize()?;
    let canonical_parent = parent.canonicalize()?;
    sync_dir_chain(&canonical_sync_root, &canonical_parent)?;

    let tmp_path = make_temp_path(path)?;
    let payload = serde_json::to_vec_pretty(value).map_err(io::Error::other)?;
    let result = (|| -> io::Result<()> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&tmp_path)?;
        file.write_all(&payload)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&tmp_path, path)?;
        sync_dir(&canonical_parent)?;
        Ok(())
    })();

    if result.is_err() {
        let _ = fs::remove_file(&tmp_path);
    }
    result
}

/// Canonicalize `path` and ensure it remains under the canonicalized `root`.
///
/// # Errors
///
/// Returns an error when canonicalization fails or the path escapes the jail.
pub fn canonicalize_under_root(root: &Path, path: &Path) -> io::Result<PathBuf> {
    let canonical_root = root.canonicalize()?;
    let canonical_path = path.canonicalize()?;
    if canonical_path.starts_with(&canonical_root) {
        Ok(canonical_path)
    } else {
        Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!(
                "path escapes jail (root={}, path={})",
                root.display(),
                path.display()
            ),
        ))
    }
}

/// `fsync` each directory from `leaf` up to and including `root`.
///
/// # Errors
///
/// Returns an error when `leaf` is outside `root` or a sync fails.
pub fn sync_dir_chain(root: &Path, leaf: &Path) -> io::Result<()> {
    let canonical_root = root.canonicalize()?;
    let mut current = leaf.to_path_buf();
    if !current.starts_with(&canonical_root) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!(
                "directory escapes jail during sync (root={}, path={})",
                canonical_root.display(),
                current.display()
            ),
        ));
    }
    loop {
        sync_dir(&current)?;
        if current == canonical_root {
            break;
        }
        let Some(parent) = current.parent() else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!(
                    "directory {} has no parent while syncing",
                    current.display()
                ),
            ));
        };
        current = parent.to_path_buf();
    }
    Ok(())
}

/// Build a unique temp path next to `dest_path`.
///
/// # Errors
///
/// Returns an error when `dest_path` has no filename.
pub fn make_temp_path(dest_path: &Path) -> io::Result<PathBuf> {
    let Some(file_name) = dest_path.file_name() else {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "destination path must include a file name",
        ));
    };
    let unique = DURABLE_TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let mut temp_name = OsString::from(file_name);
    temp_name.push(format!(".tmp-{}-{unique}", std::process::id()));
    Ok(dest_path.with_file_name(temp_name))
}

/// `fsync` a directory path.
///
/// # Errors
///
/// Returns an error when open/sync fails.
pub fn sync_dir(path: &Path) -> io::Result<()> {
    File::open(path)?.sync_all()
}

fn nearest_existing_ancestor(path: &Path) -> io::Result<PathBuf> {
    let mut current = path.to_path_buf();
    loop {
        match fs::symlink_metadata(&current) {
            Ok(meta) => {
                if meta.file_type().is_dir() {
                    return Ok(current);
                }
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!(
                        "existing parent ancestor is not a directory: {}",
                        current.display()
                    ),
                ));
            }
            Err(err) if err.kind() == io::ErrorKind::NotFound => {}
            Err(err) => return Err(err),
        }
        let Some(parent) = current.parent() else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!(
                    "path has no existing ancestor directory while creating parents: {}",
                    path.display()
                ),
            ));
        };
        current = parent.to_path_buf();
    }
}
