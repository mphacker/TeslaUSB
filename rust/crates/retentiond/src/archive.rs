//! Slices 6.1a / 6.1c — the **verified archive pass** and the **per-folder
//! event-archiving policy** (Saved / Sentry / `TeslaTrackMode`).
//!
//! Two pure, independently tested pieces plus their I/O seams:
//!
//! - [`run_verified_pass`] performs a verified archive pass over an
//!   [`ArchiveStore`]: every file in a **stable** [`DirManifest`] is copied and
//!   **re-hashed at the destination**, and after all copies the **source is
//!   re-validated** against the manifest. Only a clean pass yields a
//!   [`VerifiedArchivePass`]; any drift restarts (returns an error), exactly as
//!   [`docs/specs/retentiond.md`] §3 requires.
//! - [`decide_event_archive`] is the per-folder policy: it encodes the
//!   deliberately **different** Saved vs Sentry vs `TrackMode` rules and gates every
//!   car-side delete behind [`FolderClass::may_car_delete`] as a final safety net,
//!   so a policy bug can never turn into a car-side delete of `RecentClips`.
//!
//! `RecentClips` is **not** handled here — it never takes the verified/car-delete
//! path; see [`crate::recent`].

use crate::durability::{ArchiveVerification, VerifiedPassId};
use crate::folder::FolderClass;
use crate::io::{ContentHash, FileIdentity};
use crate::manifest::{DirManifest, ManifestDigest};

/// Copy + hash seam for the archive pass. The live implementation streams bytes
/// from the raw read path into the Pi-side archive; tests inject a fake that
/// replays a synthetic source.
pub trait ArchiveStore {
    /// Copy the source file at `src_rel` to `dest_rel` in the archive and return
    /// the content hash **computed by reading the bytes back at the destination**
    /// (so a partial/corrupt write is caught, not merely a successful `copy`).
    ///
    /// # Errors
    /// Any underlying I/O failure (source missing, write failed, read-back
    /// failed).
    fn copy_and_hash_dest(&self, src_rel: &str, dest_rel: &str) -> std::io::Result<ContentHash>;

    /// Re-read the **source** file's current identity (size + content hash),
    /// used to confirm the source still matches the manifest after the copy.
    ///
    /// # Errors
    /// Any underlying I/O failure (notably: the source disappeared mid-pass).
    fn source_identity(&self, src_rel: &str) -> std::io::Result<FileIdentity>;

    /// List the **current** set of file names directly in `src_dir` (relative to
    /// it, the same form as [`crate::manifest::ManifestEntry::rel_name`]). Used to
    /// detect a file that *appeared* in the source after the stable manifest was
    /// taken — a per-entry re-check alone cannot see a brand-new file, and
    /// archiving a still-growing event would mark it "verified" while uncopied
    /// footage remains on the car.
    ///
    /// # Errors
    /// Any underlying I/O failure reading the directory.
    fn list_source_rel_names(&self, src_dir: &str) -> std::io::Result<Vec<String>>;

    /// Remove an already-archived destination file at `dest_rel`.
    ///
    /// Used by `RecentClips` archiving to roll back already-copied angles when a
    /// multi-angle candidate aborts mid-copy.
    ///
    /// # Errors
    /// Any underlying I/O failure removing the destination file.
    fn remove_dest(&self, dest_rel: &str) -> std::io::Result<()>;

    /// Probe an already-landed destination file for container completeness.
    ///
    /// The path is resolved under the archive root by the implementation.
    ///
    /// # Errors
    /// Any underlying I/O failure while opening/reading the landed file.
    fn probe_dest_playability(
        &self,
        dest_rel: &str,
    ) -> std::io::Result<crate::probe::ArchivePlayability>;
}

/// Terminal result of a car-side delete handoff request (`gadgetd` is the final
/// gate; `retentiond` only requests).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HandoffOutcome {
    /// The car-side delete completed (folder removed via the handoff).
    Done,
    /// `gadgetd` refused (car mid-save, not quiet, or the on-car folder no longer
    /// matches the verified manifest). The footage is untouched.
    Refused(String),
    /// The handoff failed after starting; carries detail for the operator.
    Failed(String),
}

/// A request for `gadgetd` to delete a verified-archived event folder from the
/// car-visible volume. Carries the verified digest so `gadgetd` can re-validate
/// the on-car folder still matches before deleting ([`docs/specs/retentiond.md`]
/// §3.2).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CarDeleteRequest {
    /// Partition-root-relative path of the event folder on the car volume.
    pub rel_path: String,
    /// The manifest digest the archive copy was verified against.
    pub expected_digest: ManifestDigest,
}

/// Car-side delete seam (the `gadgetd` eject-handoff client).
pub trait CarDeleteHandoff {
    /// Request `gadgetd` delete the event folder on the car volume via the
    /// eject-handoff. `gadgetd` owns the final idle/quiet + manifest-revalidate
    /// gate and may [`HandoffOutcome::Refused`] the request.
    fn request_car_delete(&self, req: &CarDeleteRequest) -> HandoffOutcome;
}

/// Why a verified archive pass did not complete (the pass must be **restarted**,
/// never marked verified — [`docs/specs/retentiond.md`] §3).
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum VerifyError {
    /// The manifest was empty — an empty/forming folder is never "complete".
    #[error("manifest is empty; nothing to verify")]
    EmptyManifest,
    /// A destination read-back hash did not match the manifest's expected hash
    /// (a corrupt or partial copy).
    #[error("destination hash mismatch for `{file}`")]
    DestHashMismatch {
        /// The offending file name.
        file: String,
    },
    /// The source file changed (size or hash) between the stable manifest and
    /// the post-copy re-validation — the manifest drifted mid-pass.
    #[error("source `{file}` changed during the pass (manifest drifted)")]
    SourceChanged {
        /// The offending file name.
        file: String,
    },
    /// An underlying I/O error (source vanished, read/write failed).
    #[error("i/o error on `{file}`: {detail}")]
    Io {
        /// The file being processed when the error occurred.
        file: String,
        /// Human-readable detail.
        detail: String,
    },
}

/// A successful verified archive pass: the event's footage demonstrably survives
/// in the archive, bound to the exact manifest digest that was verified.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VerifiedArchivePass {
    /// Random pass identity (the caller-supplied generation token).
    pub id: VerifiedPassId,
    /// The manifest digest this pass verified against.
    pub digest: ManifestDigest,
    /// Total bytes archived.
    pub bytes: u64,
}

impl VerifiedArchivePass {
    /// The [`ArchiveVerification`] state this pass establishes.
    #[must_use]
    pub const fn verification(&self) -> ArchiveVerification {
        ArchiveVerification::Verified { pass: self.id }
    }
}

/// Run one verified archive pass for `manifest` from `src_dir` to `dest_dir`.
///
/// `pass_id` is a caller-supplied random 128-bit token identifying this pass (the
/// live caller draws it from a CSPRNG — never wall-clock, the Pi has no RTC).
///
/// Order matters: **copy + dest-hash every file first**, then **re-validate
/// every source** against the manifest. Re-validating after all copies is what
/// catches a source that changed while we were copying a *different* file — the
/// pass is then restarted rather than marked verified.
///
/// # Errors
/// Returns [`VerifyError`] on an empty manifest, a destination hash mismatch, a
/// source that drifted from the manifest, or any I/O failure. On error the
/// caller must **restart** the pass; it must never record verification.
pub fn run_verified_pass(
    store: &dyn ArchiveStore,
    src_dir: &str,
    dest_dir: &str,
    manifest: &DirManifest,
    pass_id: u128,
) -> Result<VerifiedArchivePass, VerifyError> {
    if manifest.is_empty() {
        return Err(VerifyError::EmptyManifest);
    }

    // Phase 1 — copy each file and verify the bytes landed correctly by hashing
    // the DESTINATION (not trusting copy()'s Ok).
    for e in manifest.entries() {
        let src = join(src_dir, &e.rel_name);
        let dest = join(dest_dir, &e.rel_name);
        let dest_hash = store
            .copy_and_hash_dest(&src, &dest)
            .map_err(|err| VerifyError::Io {
                file: e.rel_name.clone(),
                detail: err.to_string(),
            })?;
        if dest_hash != e.hash {
            return Err(VerifyError::DestHashMismatch {
                file: e.rel_name.clone(),
            });
        }
    }

    // Phase 2 — re-validate the SOURCE against the manifest. Two checks, because
    // the manifest is a snapshot and the event may still be growing:
    //
    //   2a. The source's file *set* must still equal the manifest's set. A file
    //       that APPEARED (a new clip the camera wrote mid-pass) is invisible to
    //       a per-entry check, yet means the event is incomplete — archiving it
    //       now would mark a partial event "verified" and risk a later car-side
    //       delete losing the uncopied footage.
    //   2b. Each manifest entry's source identity (size + hash) must still match.
    //       A file that changed (or vanished) while we copied others means drift.
    //
    // Either failure restarts the pass rather than claiming a false "verified".
    let listed = store
        .list_source_rel_names(src_dir)
        .map_err(|err| VerifyError::Io {
            file: src_dir.to_owned(),
            detail: err.to_string(),
        })?;
    let mut listed_sorted: Vec<&str> = listed.iter().map(String::as_str).collect();
    listed_sorted.sort_unstable();
    let mut expected_sorted: Vec<&str> = manifest
        .entries()
        .iter()
        .map(|e| e.rel_name.as_str())
        .collect();
    expected_sorted.sort_unstable();
    if listed_sorted != expected_sorted {
        // Report a representative drifted name (an addition, else a removal).
        let drifted = listed_sorted
            .iter()
            .find(|n| !expected_sorted.contains(*n))
            .or_else(|| expected_sorted.iter().find(|n| !listed_sorted.contains(*n)));
        return Err(VerifyError::SourceChanged {
            file: drifted.map_or_else(|| src_dir.to_owned(), |n| (*n).to_owned()),
        });
    }

    for e in manifest.entries() {
        let src = join(src_dir, &e.rel_name);
        let ident = store.source_identity(&src).map_err(|err| VerifyError::Io {
            file: e.rel_name.clone(),
            detail: err.to_string(),
        })?;
        if ident.size != e.size || ident.hash != e.hash {
            return Err(VerifyError::SourceChanged {
                file: e.rel_name.clone(),
            });
        }
    }

    Ok(VerifiedArchivePass {
        id: VerifiedPassId(pass_id),
        digest: manifest.digest(),
        bytes: manifest.total_bytes(),
    })
}

/// Inputs to the per-folder event-archiving decision.
#[derive(Debug, Clone, Copy)]
pub struct EventArchiveContext {
    /// The event folder's class (must be an event folder; `RecentClips` is
    /// handled by [`crate::recent`] and is treated here as never-car-deletable).
    pub folder: FolderClass,
    /// Whether the folder's manifest has held steady long enough to archive.
    pub manifest_stable: bool,
    /// Whether a verified archive pass already exists for this event.
    pub verification: ArchiveVerification,
    /// Whether the **car-visible** volume's free space is below its configured
    /// cleanup threshold (the trigger for car-side deletes).
    pub car_volume_pressured: bool,
    /// Whether the configured cloud durability policy (if any) is satisfied.
    /// Only gates `SavedClips` car-side deletion ([`docs/specs/retentiond.md`]
    /// §3.1); Sentry/TrackMode car-delete needs only a verified pass + pressure.
    pub cloud_policy_satisfied: bool,
}

/// What to do with one event folder this cycle.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventArchiveAction {
    /// Manifest not yet stable — keep observing, copy nothing.
    WaitForStableManifest,
    /// Stable but not yet verified — run a verified archive pass.
    RunVerifiedPass,
    /// Verified; policy says keep the car copy for now (no pressure, or Saved by
    /// default, or cloud policy not yet satisfied).
    KeepOnCar,
    /// Verified and policy permits a car-side delete via the `gadgetd` handoff.
    RequestCarDelete,
}

/// Decide what to do with an event folder, encoding the **per-folder
/// differences** and gating every delete behind [`FolderClass::may_car_delete`].
///
/// - `SavedClips`: kept on the car by default; car-deleted only under high
///   volume pressure **and** once the cloud policy is satisfied.
/// - `SentryClips` / `TeslaTrackMode`: car-deleted after a verified pass once the
///   car volume is pressured (no cloud gate — they can flood).
/// - Anything not car-deletable (`RecentClips`, or a mis-classified folder) can
///   never reach [`EventArchiveAction::RequestCarDelete`].
#[must_use]
pub fn decide_event_archive(ctx: EventArchiveContext) -> EventArchiveAction {
    if !ctx.manifest_stable {
        return EventArchiveAction::WaitForStableManifest;
    }
    if !ctx.verification.is_verified() {
        return EventArchiveAction::RunVerifiedPass;
    }

    // Verified. Decide whether policy wants a car-side delete this cycle.
    let wants_delete = match ctx.folder {
        FolderClass::SavedClips => ctx.car_volume_pressured && ctx.cloud_policy_satisfied,
        FolderClass::SentryClips | FolderClass::TeslaTrackMode => ctx.car_volume_pressured,
        // RecentClips is never deleted from the car; handled elsewhere.
        FolderClass::RecentClips => false,
    };

    // Final safety net: never delete from the car for a non-car-deletable
    // folder, no matter what the policy branch concluded.
    if wants_delete && ctx.folder.may_car_delete() {
        EventArchiveAction::RequestCarDelete
    } else {
        EventArchiveAction::KeepOnCar
    }
}

/// Join a directory and a relative file name with `/` (forward slash; the
/// archive paths are POSIX on the Pi). A trailing slash on `dir` is tolerated.
fn join(dir: &str, name: &str) -> String {
    if dir.is_empty() {
        name.to_owned()
    } else if dir.ends_with('/') {
        format!("{dir}{name}")
    } else {
        format!("{dir}/{name}")
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
    use std::cell::RefCell;
    use std::collections::HashMap;

    use super::{
        ArchiveStore, EventArchiveAction, EventArchiveContext, VerifyError, decide_event_archive,
        run_verified_pass,
    };
    use crate::durability::ArchiveVerification;
    use crate::folder::FolderClass;
    use crate::io::{ContentHash, FileIdentity};
    use crate::manifest::{DirManifest, ManifestEntry};

    fn h(b: u8) -> ContentHash {
        ContentHash::new([b; 32])
    }

    fn entry(name: &str, size: u64, hb: u8) -> ManifestEntry {
        ManifestEntry {
            rel_name: name.to_owned(),
            size,
            mtime_ms: 1,
            hash: h(hb),
        }
    }

    /// A fake store driven by two maps: dest read-back hashes and current source
    /// identities, both keyed by full path. Missing keys raise an I/O error.
    /// `listing` holds the rel-names the source dir currently reports (defaults
    /// to exactly the manifest set; a test can add a late-arriving file).
    struct FakeStore {
        dest_hash: HashMap<String, ContentHash>,
        source: HashMap<String, FileIdentity>,
        listing: Vec<String>,
        copies: RefCell<Vec<String>>,
    }
    impl ArchiveStore for FakeStore {
        fn copy_and_hash_dest(
            &self,
            src_rel: &str,
            _dest_rel: &str,
        ) -> std::io::Result<ContentHash> {
            self.copies.borrow_mut().push(src_rel.to_owned());
            self.dest_hash
                .get(src_rel)
                .copied()
                .ok_or_else(|| std::io::Error::other(format!("no dest for {src_rel}")))
        }
        fn source_identity(&self, src_rel: &str) -> std::io::Result<FileIdentity> {
            self.source
                .get(src_rel)
                .copied()
                .ok_or_else(|| std::io::Error::other(format!("source gone: {src_rel}")))
        }
        fn list_source_rel_names(&self, _src_dir: &str) -> std::io::Result<Vec<String>> {
            Ok(self.listing.clone())
        }
        fn remove_dest(&self, _dest_rel: &str) -> std::io::Result<()> {
            Ok(())
        }
        fn probe_dest_playability(
            &self,
            _dest_rel: &str,
        ) -> std::io::Result<crate::probe::ArchivePlayability> {
            Ok(crate::probe::ArchivePlayability::Playable)
        }
    }

    fn ok_store(files: &[(&str, u64, u8)]) -> FakeStore {
        let mut dest_hash = HashMap::new();
        let mut source = HashMap::new();
        let mut listing = Vec::new();
        for (name, size, hb) in files {
            let full = format!("src/ev1/{name}");
            dest_hash.insert(full.clone(), h(*hb));
            source.insert(
                full,
                FileIdentity {
                    size: *size,
                    hash: h(*hb),
                },
            );
            listing.push((*name).to_owned());
        }
        FakeStore {
            dest_hash,
            source,
            listing,
            copies: RefCell::new(Vec::new()),
        }
    }

    #[test]
    fn verified_pass_succeeds_when_dest_and_source_match() {
        let files = [("front.mp4", 10, 1), ("back.mp4", 20, 2)];
        let store = ok_store(&files);
        let manifest =
            DirManifest::from_entries(vec![entry("front.mp4", 10, 1), entry("back.mp4", 20, 2)]);
        let pass = run_verified_pass(&store, "src/ev1", "arch/ev1", &manifest, 0xABCD).unwrap();
        assert_eq!(pass.bytes, 30);
        assert_eq!(pass.digest, manifest.digest());
        // Both files were copied.
        assert_eq!(store.copies.borrow().len(), 2);
    }

    #[test]
    fn dest_hash_mismatch_fails_closed() {
        // Destination read-back differs from the manifest hash → corrupt copy.
        let mut store = ok_store(&[("front.mp4", 10, 1)]);
        store
            .dest_hash
            .insert("src/ev1/front.mp4".to_owned(), h(99));
        let manifest = DirManifest::from_entries(vec![entry("front.mp4", 10, 1)]);
        let err = run_verified_pass(&store, "src/ev1", "arch/ev1", &manifest, 1).unwrap_err();
        assert_eq!(
            err,
            VerifyError::DestHashMismatch {
                file: "front.mp4".to_owned()
            }
        );
    }

    #[test]
    fn source_changed_mid_pass_restarts() {
        // The source of front.mp4 changes (hash) after the manifest was taken;
        // the post-copy re-validation must catch it.
        let mut store = ok_store(&[("front.mp4", 10, 1), ("back.mp4", 20, 2)]);
        // dest still hashes to the manifest value (copy looked fine), but the
        // live source now reads a different hash.
        store.source.insert(
            "src/ev1/front.mp4".to_owned(),
            FileIdentity {
                size: 10,
                hash: h(42),
            },
        );
        let manifest =
            DirManifest::from_entries(vec![entry("front.mp4", 10, 1), entry("back.mp4", 20, 2)]);
        let err = run_verified_pass(&store, "src/ev1", "arch/ev1", &manifest, 1).unwrap_err();
        assert_eq!(
            err,
            VerifyError::SourceChanged {
                file: "front.mp4".to_owned()
            }
        );
    }

    #[test]
    fn source_vanished_mid_pass_is_io_error_not_success() {
        let mut store = ok_store(&[("front.mp4", 10, 1)]);
        store.source.remove("src/ev1/front.mp4");
        let manifest = DirManifest::from_entries(vec![entry("front.mp4", 10, 1)]);
        let err = run_verified_pass(&store, "src/ev1", "arch/ev1", &manifest, 1).unwrap_err();
        assert!(matches!(err, VerifyError::Io { .. }));
    }

    #[test]
    fn late_arriving_source_file_fails_the_pass() {
        // Bug #1: a new clip appears in the source dir after the manifest was
        // taken (the event was still being written). Every manifest entry copies
        // and re-validates fine, but the event is INCOMPLETE — the pass must fail
        // (SourceChanged) rather than mark a partial event verified.
        let mut store = ok_store(&[("front.mp4", 10, 1), ("back.mp4", 20, 2)]);
        store.listing.push("left.mp4".to_owned());
        let manifest =
            DirManifest::from_entries(vec![entry("front.mp4", 10, 1), entry("back.mp4", 20, 2)]);
        let err = run_verified_pass(&store, "src/ev1", "arch/ev1", &manifest, 1).unwrap_err();
        assert_eq!(
            err,
            VerifyError::SourceChanged {
                file: "left.mp4".to_owned()
            }
        );
    }

    #[test]
    fn empty_manifest_is_rejected() {
        let store = ok_store(&[]);
        let err = run_verified_pass(&store, "src/ev1", "arch/ev1", &DirManifest::default(), 1)
            .unwrap_err();
        assert_eq!(err, VerifyError::EmptyManifest);
    }

    // ---- Per-folder policy ----

    fn ctx(
        folder: FolderClass,
        verified: bool,
        pressured: bool,
        cloud_ok: bool,
    ) -> EventArchiveContext {
        EventArchiveContext {
            folder,
            manifest_stable: true,
            verification: if verified {
                ArchiveVerification::Verified {
                    pass: crate::durability::VerifiedPassId(7),
                }
            } else {
                ArchiveVerification::Unverified
            },
            car_volume_pressured: pressured,
            cloud_policy_satisfied: cloud_ok,
        }
    }

    #[test]
    fn unstable_manifest_waits() {
        let mut c = ctx(FolderClass::SentryClips, false, true, true);
        c.manifest_stable = false;
        assert_eq!(
            decide_event_archive(c),
            EventArchiveAction::WaitForStableManifest
        );
    }

    #[test]
    fn stable_unverified_runs_pass() {
        let c = ctx(FolderClass::SentryClips, false, true, true);
        assert_eq!(decide_event_archive(c), EventArchiveAction::RunVerifiedPass);
    }

    #[test]
    fn saved_kept_on_car_by_default_even_when_verified() {
        // No pressure → keep.
        let c = ctx(FolderClass::SavedClips, true, false, true);
        assert_eq!(decide_event_archive(c), EventArchiveAction::KeepOnCar);
    }

    #[test]
    fn saved_car_delete_requires_pressure_and_cloud_policy() {
        // Pressured but cloud policy not satisfied → keep (the Saved-specific gate).
        assert_eq!(
            decide_event_archive(ctx(FolderClass::SavedClips, true, true, false)),
            EventArchiveAction::KeepOnCar
        );
        // Pressured AND cloud satisfied → delete.
        assert_eq!(
            decide_event_archive(ctx(FolderClass::SavedClips, true, true, true)),
            EventArchiveAction::RequestCarDelete
        );
    }

    #[test]
    fn sentry_and_track_car_delete_on_pressure_without_cloud_gate() {
        // Sentry/Track differ from Saved: pressure alone (after verify) deletes,
        // even if cloud policy is not satisfied (they can flood the volume).
        for folder in [FolderClass::SentryClips, FolderClass::TeslaTrackMode] {
            assert_eq!(
                decide_event_archive(ctx(folder, true, true, false)),
                EventArchiveAction::RequestCarDelete,
                "{folder:?}"
            );
            // No pressure → keep.
            assert_eq!(
                decide_event_archive(ctx(folder, true, false, false)),
                EventArchiveAction::KeepOnCar,
                "{folder:?}"
            );
        }
    }

    #[test]
    fn recent_is_never_car_deleted_even_if_it_reaches_this_function() {
        // Defensive: even with pressure and (nonsensically) "verified", a
        // RecentClips folder can never become a car delete.
        let c = ctx(FolderClass::RecentClips, true, true, true);
        assert_eq!(decide_event_archive(c), EventArchiveAction::KeepOnCar);
    }
}
