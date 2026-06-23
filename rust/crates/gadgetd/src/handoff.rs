//! The eject-handoff state machine (`gadgetd` responsibility #4).
//!
//! A handoff lets another service mutate the backing image while the car owns
//! the LUN, by **ejecting the medium** (clearing `lun.0/file`, not unbinding the
//! whole gadget), mounting the image locally read-write, applying a validated
//! op, and **re-presenting**. This module holds the *pure* orchestration and the
//! request/op types; all side effects go through the [`LunControl`],
//! [`SaveGuard`], and [`ImageMutator`] traits so the full control flow — every
//! safety branch — is unit-tested on any host with fakes.
//!
//! # Safety precedence (load-bearing)
//! Two invariants can conflict after an eject, and the order is deliberate:
//! 1. **Never two simultaneous writers.** If the local mutate path cannot prove
//!    it released the image (unmount/detach failed), the LUN is left ejected and
//!    a critical fault is raised — we never re-present onto a still-mounted image.
//! 2. **Always give the car its drive back.** In *every* other post-eject
//!    outcome (success or a failure that released the image) the LUN is
//!    re-presented before returning.
//!
//! Invariant (1) outranks (2): a missing drive is recoverable (retry / operator);
//! two writers corrupt the Tesla filesystem irrecoverably.

use serde::{Deserialize, Serialize};

/// Which exFAT partition a mutation targets. p1 = `TeslaCam` (dashcam), p2 =
/// media. Both live on the one emulated device.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) enum Partition {
    /// Partition 1 — the `TeslaCam` dashcam/Sentry partition.
    P1,
    /// Partition 2 — the media (chimes/lightshow/boombox/music) partition.
    P2,
}

impl Partition {
    /// Map a wire integer (1 or 2) to a partition, rejecting anything else.
    ///
    /// # Errors
    /// Returns an error string for any value other than 1 or 2.
    pub(crate) fn from_u8(value: u8) -> Result<Self, String> {
        match value {
            1 => Ok(Self::P1),
            2 => Ok(Self::P2),
            other => Err(format!("partition must be 1 or 2, got {other}")),
        }
    }

    /// The LUN index this partition maps to in the two-LUN gadget:
    /// `P1` (`TeslaCam`) → `lun.0`, `P2` (media) → `lun.1`.
    pub(crate) fn lun_index(self) -> u8 {
        match self {
            Self::P1 => 0,
            Self::P2 => 1,
        }
    }
}

/// A caller-supplied, validated mutation. `gadgetd` only **executes** these; it
/// never decides *what* to delete or install (that is the caller's authority).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub(crate) enum Mutation {
    /// Delete a file or directory (recursively) at `rel_path` under the mounted
    /// partition root.
    DeletePath {
        /// Partition-root-relative path to remove.
        rel_path: String,
    },
    /// Delete a SET of individual files in a SINGLE handoff (one eject), e.g. the
    /// camera-angle files of one clip. Unlike [`Mutation::DeletePath`], each entry
    /// must resolve to a regular file (never a directory) so a clip-granular delete
    /// can never recurse into and remove sibling clips or a whole event folder. An
    /// already-absent path is treated as success, so a retried delete is safe.
    DeletePaths {
        /// Partition-root-relative file paths to remove (1..=[`MAX_DELETE_PATHS`]).
        rel_paths: Vec<String>,
    },
    /// Copy a staged file from `source_path` (outside the image) to `rel_path`
    /// under the mounted partition root, via a temp file + atomic rename.
    InstallFile {
        /// Partition-root-relative destination path.
        rel_path: String,
        /// Absolute path of the staged source file on the Pi data area.
        source_path: String,
    },
    /// Remove the **empty** directory at `rel_path` (and any now-empty ancestors
    /// up the chain), under the mounted partition root. This exists because
    /// [`Mutation::DeletePaths`] deliberately refuses directories (so a clip
    /// delete can never recurse), which leaves an orphaned empty directory behind
    /// after a folder's files are deleted. Unlike [`Mutation::DeletePath`], this
    /// NEVER recurses: it uses an empty-only `remove_dir`, so it is impossible
    /// for it to delete any file — if the directory is non-empty it is left
    /// untouched. Protected directories (top-level partition roots and the
    /// TeslaCam structural roots) are refused. An already-absent directory is
    /// treated as success, so a retried prune is safe.
    RemoveEmptyDir {
        /// Partition-root-relative directory to prune (empty-only, never recursive).
        rel_path: String,
    },
}

/// Upper bound on the number of paths in a single [`Mutation::DeletePaths`]. A
/// clip has one file per camera (≤6 today); the cap leaves head-room while
/// keeping the bounded single-mount delete loop honest.
pub(crate) const MAX_DELETE_PATHS: usize = 16;

impl Mutation {
    /// Validate every path this mutation carries (and any set-size bounds)
    /// up-front, so a malformed request is refused before the LUN is ever
    /// ejected.
    ///
    /// # Errors
    /// Returns a human-readable reason for the first rejected path or bound.
    pub(crate) fn validate(&self) -> Result<(), String> {
        match self {
            Self::DeletePath { rel_path } | Self::InstallFile { rel_path, .. } => {
                validate_rel_path(rel_path)?;
            }
            Self::RemoveEmptyDir { rel_path } => {
                validate_rel_path(rel_path)?;
                if is_protected_dir(rel_path) {
                    return Err(format!(
                        "remove_empty_dir: refusing to prune a protected directory: {rel_path}"
                    ));
                }
            }
            Self::DeletePaths { rel_paths } => {
                if rel_paths.is_empty() {
                    return Err("delete_paths: empty path set".to_owned());
                }
                if rel_paths.len() > MAX_DELETE_PATHS {
                    return Err(format!(
                        "delete_paths: {} paths exceed the cap of {MAX_DELETE_PATHS}",
                        rel_paths.len()
                    ));
                }
                for rel_path in rel_paths {
                    validate_rel_path(rel_path)?;
                }
            }
        }
        Ok(())
    }
}

/// Validate a partition-root-relative path supplied by a (trusted-but-fallible)
/// caller against a **car-controlled, untrusted** exFAT filesystem.
///
/// Rejects absolute paths, any `.`/`..`/empty component, embedded NUL, and the
/// empty path itself. A non-empty, traversal-free relative path is returned with
/// its components rejoined with `/`. The live mutator additionally re-checks the
/// resolved path stays within the mount after opening (defence against TOCTOU /
/// symlink escape).
///
/// # Errors
/// Returns a human-readable reason for any rejected path.
pub(crate) fn validate_rel_path(raw: &str) -> Result<String, String> {
    if raw.is_empty() {
        return Err("empty path".to_owned());
    }
    if raw.contains('\0') {
        return Err("path contains NUL".to_owned());
    }
    if raw.starts_with('/') {
        return Err("path must be relative (no leading `/`)".to_owned());
    }
    let mut parts = Vec::new();
    for comp in raw.split('/') {
        match comp {
            "" => return Err("path has an empty component (`//` or trailing `/`)".to_owned()),
            "." => return Err("path component `.` is not allowed".to_owned()),
            ".." => return Err("path component `..` is not allowed".to_owned()),
            other => parts.push(other),
        }
    }
    Ok(parts.join("/"))
}

/// Directories a [`Mutation::RemoveEmptyDir`] prune must NEVER remove, even when
/// empty: any top-level partition directory (a single path component, e.g.
/// `Music`, `TeslaCam`, `Chimes`, `Boombox`) and the TeslaCam structural roots
/// the car expects to exist (`TeslaCam/SavedClips`, `SentryClips`, `RecentClips`).
///
/// `RemoveEmptyDir` is only ever issued by the media folder-delete path (for a
/// user-created subfolder two-or-more levels deep), but gadgetd does not trust
/// its callers: this is defence-in-depth against a bug or malformed request
/// pruning a structural directory the car relies on. Comparison is
/// case-insensitive because the backing exFAT filesystem is.
pub(crate) fn is_protected_dir(rel_path: &str) -> bool {
    let comps: Vec<&str> = rel_path.split('/').filter(|c| !c.is_empty()).collect();
    // A single component (or none) is a top-level partition root — never prune.
    if comps.len() < 2 {
        return true;
    }
    comps.len() == 2
        && comps[0].eq_ignore_ascii_case("TeslaCam")
        && (comps[1].eq_ignore_ascii_case("SavedClips")
            || comps[1].eq_ignore_ascii_case("SentryClips")
            || comps[1].eq_ignore_ascii_case("RecentClips"))
}

/// Progress markers emitted as the handoff advances (for status reporting).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum HandoffPhase {
    /// Guards passed; about to eject.
    Ejecting,
    /// Ejected; mounting + applying the mutation.
    Applying,
    /// Mutation applied (or failed-but-released); re-presenting the LUN.
    Representing,
    /// Re-presented; handoff complete.
    Done,
}

impl HandoffPhase {
    /// Lowercase wire string for status responses.
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Ejecting => "ejecting",
            Self::Applying => "applying",
            Self::Representing => "representing",
            Self::Done => "done",
        }
    }
}

/// Terminal result of a handoff.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum HandoffOutcome {
    /// Mutation applied and LUN re-presented.
    Done,
    /// Refused before ejecting (guard failed) — the LUN was never touched.
    Refused(String),
    /// The mutation failed, but the image was released and the LUN re-presented
    /// (the car has its drive back).
    Failed(String),
    /// The LUN was busy (the car holds the medium) so the eject could not take.
    /// Nothing was mutated and the backing medium is confirmed still present, so
    /// this is transient: the mutation must stay queued and be retried at a later
    /// safe window — it must NEVER be dropped/failed-fatal.
    Busy(String),
    /// The image could not be proven released; the LUN is **left ejected** to
    /// avoid a double-writer. Requires recovery (handled at next `serve` start).
    CriticalFault(String),
}

impl HandoffOutcome {
    /// Lowercase wire string for `last_result` / status reporting.
    pub(crate) fn kind(&self) -> &'static str {
        match self {
            Self::Done => "done",
            Self::Refused(_) => "refused",
            Self::Failed(_) => "failed",
            Self::Busy(_) => "busy",
            Self::CriticalFault(_) => "critical_fault",
        }
    }

    /// The human-readable detail, if any.
    pub(crate) fn detail(&self) -> Option<&str> {
        match self {
            Self::Done => None,
            Self::Refused(d) | Self::Failed(d) | Self::Busy(d) | Self::CriticalFault(d) => Some(d),
        }
    }
}

/// Controls the gadget LUN's backing medium (live impl writes configfs).
pub(crate) trait LunControl {
    /// Is the gadget bound to a UDC?
    fn is_bound(&self) -> std::io::Result<bool>;
    /// Has the host enumerated the gadget (`udc_state == configured`)?
    fn udc_configured(&self) -> std::io::Result<bool>;
    /// Is `lun.0/file` currently empty (medium ejected)?
    fn lun_is_empty(&self) -> std::io::Result<bool>;
    /// Eject: clear `lun.0/file` so the kernel releases the backing image.
    fn eject(&self) -> std::io::Result<()>;
    /// Re-present: point `lun.0/file` back at the backing image.
    fn represent(&self) -> std::io::Result<()>;
}

/// Coordinates the persistent read-only media mount around a media (P2) handoff.
/// Suspend must release the RO mount BEFORE the RW mutate; resume re-establishes
/// it AFTER re-present. Only ever invoked for P2 (media); P1/TeslaCam never has
/// a Pi-side RO mount.
pub(crate) trait ReadMountGate {
    /// Tear down the RO media mount so the RW mutate can loop-mount the image.
    /// Ok if already absent. Err => caller must refuse (avoid a double mount).
    fn suspend(&self) -> std::io::Result<()>;
    /// Re-establish the RO media mount after re-present. Best-effort.
    fn resume(&self) -> std::io::Result<()>;
}

/// Heuristic detector for "the car is mid-save" (live impl samples image mtime).
pub(crate) trait SaveGuard {
    /// Best-effort: did the backing image see writes inside the quiet window?
    fn is_save_active(&self) -> std::io::Result<bool>;
}

/// Failure of the local mutate path, carrying whether the image was released.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct MutateError {
    /// Human-readable failure detail.
    pub(crate) detail: String,
    /// `true` iff the loop device and mount were torn down (image is no longer
    /// held locally, so it is safe to re-present).
    pub(crate) image_released: bool,
}

/// Mounts the image locally and applies a mutation (live impl uses losetup/mount).
pub(crate) trait ImageMutator {
    /// Clean any stale loop devices / mounts left on the image by an interrupted
    /// handoff. Must succeed (image fully released) before a recovery re-present.
    ///
    /// # Errors
    /// Returns an error if a stale loop/mount could not be cleared.
    fn cleanup_stale(&self) -> std::io::Result<()>;
    /// Loop-mount `partition` RW, apply `mutation`, then unmount + detach. The
    /// implementation owns its own cleanup and reports — via
    /// [`MutateError::image_released`] — whether the image was released even on
    /// failure.
    fn apply(&self, partition: Partition, mutation: &Mutation) -> Result<(), MutateError>;
}

/// Run a single handoff through the injected seams, emitting `progress` markers.
///
/// `allow_hot` permits a P1/TeslaCam hot handoff while the host is actively
/// enumerated. It defaults OFF: the car's tolerance to a mid-use eject on the
/// dashcam partition is unmeasured (`SPEC.md` §9 prototype-unknown #2), so a
/// production P1 hot handoff is refused until that is validated on the car and
/// the operator opts in. P2/media handoffs are allowed through this gate by
/// default because the car only reads that image.
///
/// Serialization (never two concurrent handoffs) is the caller's responsibility
/// (a `try_lock` on the handoff mutex); this function assumes exclusive access.
// The four injected seams (`lun`/`guard`/`mutator`/`gate`) plus the policy
// flag and progress sink are a cohesive set; bundling them into a struct would
// add indirection without improving clarity.
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_handoff(
    lun: &dyn LunControl,
    guard: &dyn SaveGuard,
    mutator: &dyn ImageMutator,
    gate: &dyn ReadMountGate,
    partition: Partition,
    mutation: &Mutation,
    allow_hot: bool,
    mut progress: impl FnMut(HandoffPhase),
) -> HandoffOutcome {
    // --- Guards (refuse before touching the LUN) ---
    match lun.is_bound() {
        Ok(true) => {}
        Ok(false) => return HandoffOutcome::Refused("gadget not bound".to_owned()),
        Err(e) => return HandoffOutcome::Refused(format!("bound-check failed: {e}")),
    }

    // Fail safe: if we cannot read the UDC state, assume the host is connected.
    let hot_allowed = allow_hot || partition == Partition::P2;
    let configured = lun.udc_configured().unwrap_or(true);
    if configured && !hot_allowed {
        return HandoffOutcome::Refused(
            "hot_handoff_unvalidated: host is enumerated on the TeslaCam (P1) dashcam \
             partition and the car's mid-use eject tolerance is unmeasured (SPEC.md §9 #2); \
             enable only after HW validation"
                .to_owned(),
        );
    }

    match guard.is_save_active() {
        Ok(false) => {}
        Ok(true) => return HandoffOutcome::Refused("save_active".to_owned()),
        Err(e) => return HandoffOutcome::Refused(format!("save-guard failed: {e}")),
    }

    if partition == Partition::P2 {
        if let Err(e) = gate.suspend() {
            return HandoffOutcome::Refused(format!("media_ro_suspend_failed: {e}"));
        }
    }

    let outcome = run_handoff_core(lun, mutator, partition, mutation, &mut progress);

    // We suspended the read-only media mount above, so we MUST attempt to resume
    // it on every terminal outcome — including `CriticalFault`. Skipping resume on
    // a fault leaves the web read path (chime/music/lightshow preview, download,
    // and activate) permanently 404ing until gadgetd restarts: a fault as common
    // as an eject EBUSY (the car holding the media LUN) would otherwise strand the
    // mount with no self-heal. Resuming is fail-safe even when the mutate path may
    // have left a stale RW loop on the image: `MediaRoMount::ensure_mounted`
    // refuses to mount while any loop is attached, so it can never stack a second
    // mount on the volume — it records the error and leaves the read path down
    // until the next startup recovery clears the loop.
    if partition == Partition::P2 {
        if let Err(e) = gate.resume() {
            eprintln!("gadgetd handoff: media RO resume failed: {e}");
        }
    }

    outcome
}

fn run_handoff_core(
    lun: &dyn LunControl,
    mutator: &dyn ImageMutator,
    partition: Partition,
    mutation: &Mutation,
    progress: &mut impl FnMut(HandoffPhase),
) -> HandoffOutcome {
    // --- Eject (from here the image is ours; the LUN must end re-presented
    //     unless the mutate path cannot prove it released the image) ---
    progress(HandoffPhase::Ejecting);
    if let Err(e) = lun.eject() {
        // Eject failed. Best-effort restore the medium, then READ BACK the LUN to
        // decide the true outcome. The medium's presence — not represent()'s
        // return — is the source of truth: when the car holds the LUN, BOTH eject
        // and re-present can EBUSY while the backing image is still fully present
        // and untouched (the failed eject write never changed lun.N/file). We must
        // never drop a mutation in that case — it is transient.
        let represent = lun.represent();
        return match lun.lun_is_empty() {
            Ok(false) => {
                // Medium present: the car still has its drive and nothing mutated.
                if is_busy_error(&e) {
                    HandoffOutcome::Busy(format!("eject busy, medium intact: {e}"))
                } else {
                    HandoffOutcome::Failed(format!("eject failed: {e}"))
                }
            }
            Ok(true) => HandoffOutcome::CriticalFault(format!(
                "eject failed ({e}) and medium not restored (represent: {represent:?}); \
                 LUN empty — recovery required"
            )),
            Err(re) => HandoffOutcome::CriticalFault(format!(
                "eject failed ({e}); medium state unreadable ({re}) — recovery required"
            )),
        };
    }

    // --- Mount + mutate (self-cleaning) ---
    progress(HandoffPhase::Applying);
    match mutator.apply(partition, mutation) {
        Ok(()) => represent_after(lun, progress, HandoffOutcome::Done),
        Err(me) if me.image_released => {
            represent_after(lun, progress, HandoffOutcome::Failed(me.detail))
        }
        Err(me) => HandoffOutcome::CriticalFault(format!(
            "mutate failed and image NOT released ({}); LUN left ejected to \
             prevent a double-writer — recovery required",
            me.detail
        )),
    }
}

/// A configfs eject/represent that fails with `EBUSY` means the host (the car)
/// still holds the LUN — a transient condition, distinct from a genuine fault.
fn is_busy_error(e: &std::io::Error) -> bool {
    e.kind() == std::io::ErrorKind::ResourceBusy
}

/// Re-present the LUN, returning `success` on success or a critical fault if the
/// medium could not be restored.
fn represent_after(
    lun: &dyn LunControl,
    progress: &mut impl FnMut(HandoffPhase),
    success: HandoffOutcome,
) -> HandoffOutcome {
    progress(HandoffPhase::Representing);
    match lun.represent() {
        Ok(()) => {
            progress(HandoffPhase::Done);
            success
        }
        Err(e) => HandoffOutcome::CriticalFault(format!(
            "image released but re-present failed ({e}); LUN absent — recovery required"
        )),
    }
}

#[cfg(test)]
#[allow(clippy::panic, clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::{
        HandoffOutcome, HandoffPhase, ImageMutator, LunControl, MutateError, Mutation, Partition,
        ReadMountGate, SaveGuard, is_protected_dir, run_handoff, validate_rel_path,
    };
    use std::cell::{Cell, RefCell};

    #[test]
    fn partition_from_u8_only_accepts_1_and_2() {
        assert_eq!(Partition::from_u8(1).unwrap(), Partition::P1);
        assert_eq!(Partition::from_u8(2).unwrap(), Partition::P2);
        assert!(Partition::from_u8(0).is_err());
        assert!(Partition::from_u8(3).is_err());
    }

    #[test]
    fn validate_rel_path_rejects_traversal_and_absolute() {
        assert!(validate_rel_path("").is_err());
        assert!(validate_rel_path("/etc/passwd").is_err());
        assert!(validate_rel_path("../escape").is_err());
        assert!(validate_rel_path("a/../b").is_err());
        assert!(validate_rel_path("a/./b").is_err());
        assert!(validate_rel_path("a//b").is_err());
        assert!(validate_rel_path("a/b/").is_err());
        assert!(validate_rel_path("a\0b").is_err());
        assert_eq!(
            validate_rel_path("TeslaCam/clip.mp4").unwrap(),
            "TeslaCam/clip.mp4"
        );
        assert_eq!(validate_rel_path("event123").unwrap(), "event123");
    }

    #[test]
    fn mutation_validate_bounds_delete_paths_set() {
        // Good: a small set of valid file paths.
        let ok = Mutation::DeletePaths {
            rel_paths: vec![
                "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-front.mp4".to_owned(),
                "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-back.mp4".to_owned(),
            ],
        };
        assert!(ok.validate().is_ok());

        // Empty set is refused (nothing to do, and an empty handoff is wasteful).
        assert!(
            Mutation::DeletePaths { rel_paths: vec![] }
                .validate()
                .is_err()
        );

        // Over the cap is refused before any eject.
        let too_many = Mutation::DeletePaths {
            rel_paths: (0..=super::MAX_DELETE_PATHS)
                .map(|i| format!("TeslaCam/SavedClips/e/{i}.mp4"))
                .collect(),
        };
        assert!(too_many.validate().is_err());

        // A single traversal entry poisons the whole set.
        let bad = Mutation::DeletePaths {
            rel_paths: vec!["TeslaCam/ok.mp4".to_owned(), "../escape".to_owned()],
        };
        assert!(bad.validate().is_err());
    }

    #[test]
    fn is_protected_dir_guards_roots_and_structural_dirs() {
        // Top-level (single component) partition roots are always protected.
        assert!(is_protected_dir("Music"));
        assert!(is_protected_dir("TeslaCam"));
        assert!(is_protected_dir("Chimes"));
        // TeslaCam structural second-level roots (case-insensitive, exFAT).
        assert!(is_protected_dir("TeslaCam/SavedClips"));
        assert!(is_protected_dir("TeslaCam/SentryClips"));
        assert!(is_protected_dir("TeslaCam/RecentClips"));
        assert!(is_protected_dir("teslacam/savedclips"));
        // User subfolders (depth >= 2, not a structural root) are prunable.
        assert!(!is_protected_dir("Music/Artist"));
        assert!(!is_protected_dir("Music/Artist/Album"));
        assert!(!is_protected_dir("TeslaCam/SavedClips/2026-06-01_20-10-04"));
    }

    #[test]
    fn mutation_validate_guards_remove_empty_dir() {
        // A normal user subfolder validates.
        assert!(
            Mutation::RemoveEmptyDir {
                rel_path: "Music/Artist/Album".to_owned(),
            }
            .validate()
            .is_ok()
        );
        // Traversal is refused.
        assert!(
            Mutation::RemoveEmptyDir {
                rel_path: "../escape".to_owned(),
            }
            .validate()
            .is_err()
        );
        // A protected top-level root is refused.
        assert!(
            Mutation::RemoveEmptyDir {
                rel_path: "Music".to_owned(),
            }
            .validate()
            .is_err()
        );
        // A TeslaCam structural root is refused.
        assert!(
            Mutation::RemoveEmptyDir {
                rel_path: "TeslaCam/SentryClips".to_owned(),
            }
            .validate()
            .is_err()
        );
    }

    // ---- Fakes for the orchestration tests ----
    #[allow(clippy::struct_excessive_bools)]
    struct FakeLun {
        bound: bool,
        configured: bool,
        eject_fails: bool,
        eject_busy: bool,
        represent_fails: bool,
        medium_present: bool,
        events: RefCell<Vec<&'static str>>,
    }
    impl FakeLun {
        fn ok() -> Self {
            Self {
                bound: true,
                configured: false,
                eject_fails: false,
                eject_busy: false,
                represent_fails: false,
                medium_present: true,
                events: RefCell::new(Vec::new()),
            }
        }
    }
    impl LunControl for FakeLun {
        fn is_bound(&self) -> std::io::Result<bool> {
            Ok(self.bound)
        }
        fn udc_configured(&self) -> std::io::Result<bool> {
            Ok(self.configured)
        }
        fn lun_is_empty(&self) -> std::io::Result<bool> {
            Ok(!self.medium_present)
        }
        fn eject(&self) -> std::io::Result<()> {
            self.events.borrow_mut().push("eject");
            if self.eject_fails {
                if self.eject_busy {
                    Err(std::io::Error::from(std::io::ErrorKind::ResourceBusy))
                } else {
                    Err(std::io::Error::other("eject boom"))
                }
            } else {
                Ok(())
            }
        }
        fn represent(&self) -> std::io::Result<()> {
            self.events.borrow_mut().push("represent");
            if self.represent_fails {
                Err(std::io::Error::other("represent boom"))
            } else {
                Ok(())
            }
        }
    }

    struct FakeGuard(bool);
    impl SaveGuard for FakeGuard {
        fn is_save_active(&self) -> std::io::Result<bool> {
            Ok(self.0)
        }
    }

    struct NoopGate;
    impl ReadMountGate for NoopGate {
        fn suspend(&self) -> std::io::Result<()> {
            Ok(())
        }

        fn resume(&self) -> std::io::Result<()> {
            Ok(())
        }
    }

    struct RecordingGate {
        suspends: Cell<u32>,
        resumes: Cell<u32>,
        suspend_err: bool,
        resume_err: bool,
    }
    impl ReadMountGate for RecordingGate {
        fn suspend(&self) -> std::io::Result<()> {
            self.suspends.set(self.suspends.get() + 1);
            if self.suspend_err {
                Err(std::io::Error::other("suspend boom"))
            } else {
                Ok(())
            }
        }

        fn resume(&self) -> std::io::Result<()> {
            self.resumes.set(self.resumes.get() + 1);
            if self.resume_err {
                Err(std::io::Error::other("resume boom"))
            } else {
                Ok(())
            }
        }
    }

    struct FakeMutator {
        result: RefCell<Option<Result<(), MutateError>>>,
    }
    impl FakeMutator {
        fn ok() -> Self {
            Self {
                result: RefCell::new(Some(Ok(()))),
            }
        }
        fn err(image_released: bool) -> Self {
            Self {
                result: RefCell::new(Some(Err(MutateError {
                    detail: "mutate boom".to_owned(),
                    image_released,
                }))),
            }
        }
    }
    impl ImageMutator for FakeMutator {
        fn cleanup_stale(&self) -> std::io::Result<()> {
            Ok(())
        }
        fn apply(&self, _p: Partition, _m: &Mutation) -> Result<(), MutateError> {
            self.result.borrow_mut().take().expect("apply called once")
        }
    }

    fn del() -> Mutation {
        Mutation::DeletePath {
            rel_path: "x".to_owned(),
        }
    }

    #[test]
    fn happy_path_ejects_applies_and_represents() {
        let lun = FakeLun::ok();
        let out = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &NoopGate,
            Partition::P1,
            &del(),
            false,
            |_| {},
        );
        assert_eq!(out, HandoffOutcome::Done);
        assert_eq!(*lun.events.borrow(), ["eject", "represent"]);
    }

    #[test]
    fn refuses_when_not_bound_without_touching_lun() {
        let mut lun = FakeLun::ok();
        lun.bound = false;
        let out = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &NoopGate,
            Partition::P1,
            &del(),
            true,
            |_| {},
        );
        assert!(matches!(out, HandoffOutcome::Refused(_)));
        assert!(
            lun.events.borrow().is_empty(),
            "LUN must be untouched on refusal"
        );
    }

    #[test]
    fn refuses_hot_handoff_when_configured_and_not_allowed() {
        let mut lun = FakeLun::ok();
        lun.configured = true;
        let out = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &NoopGate,
            Partition::P1,
            &del(),
            false,
            |_| {},
        );
        match out {
            HandoffOutcome::Refused(r) => assert!(r.contains("hot_handoff_unvalidated")),
            other => panic!("expected hot refusal, got {other:?}"),
        }
        assert!(lun.events.borrow().is_empty());
    }

    #[test]
    fn allows_hot_handoff_when_opted_in() {
        let mut lun = FakeLun::ok();
        lun.configured = true;
        let out = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &NoopGate,
            Partition::P1,
            &del(),
            true,
            |_| {},
        );
        assert_eq!(out, HandoffOutcome::Done);
    }

    #[test]
    fn allows_media_p2_handoff_when_configured_even_without_flag() {
        let mut lun = FakeLun::ok();
        lun.configured = true;
        let out = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &NoopGate,
            Partition::P2,
            &del(),
            false,
            |_| {},
        );
        assert_eq!(out, HandoffOutcome::Done);
        assert_eq!(*lun.events.borrow(), ["eject", "represent"]);
    }

    #[test]
    fn still_refuses_media_p2_when_save_active() {
        let mut lun = FakeLun::ok();
        lun.configured = true;
        let out = run_handoff(
            &lun,
            &FakeGuard(true),
            &FakeMutator::ok(),
            &NoopGate,
            Partition::P2,
            &del(),
            false,
            |_| {},
        );
        assert_eq!(out, HandoffOutcome::Refused("save_active".to_owned()));
        assert!(lun.events.borrow().is_empty());
    }

    #[test]
    fn refuses_when_save_active() {
        let lun = FakeLun::ok();
        let out = run_handoff(
            &lun,
            &FakeGuard(true),
            &FakeMutator::ok(),
            &NoopGate,
            Partition::P1,
            &del(),
            false,
            |_| {},
        );
        assert_eq!(out, HandoffOutcome::Refused("save_active".to_owned()));
        assert!(lun.events.borrow().is_empty());
    }

    #[test]
    fn mutate_failure_that_released_still_represents() {
        let lun = FakeLun::ok();
        let out = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::err(true),
            &NoopGate,
            Partition::P1,
            &del(),
            false,
            |_| {},
        );
        assert!(matches!(out, HandoffOutcome::Failed(_)));
        // Critically, the LUN was re-presented (car gets its drive back).
        assert_eq!(*lun.events.borrow(), ["eject", "represent"]);
    }

    #[test]
    fn eject_busy_with_medium_present_is_busy() {
        let mut lun = FakeLun::ok();
        lun.eject_fails = true;
        lun.eject_busy = true;
        lun.medium_present = true;
        let out = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &NoopGate,
            Partition::P1,
            &del(),
            false,
            |_| {},
        );
        assert!(matches!(out, HandoffOutcome::Busy(_)));
        assert_eq!(*lun.events.borrow(), ["eject", "represent"]);
    }

    #[test]
    fn eject_busy_but_medium_lost_is_critical() {
        let mut lun = FakeLun::ok();
        lun.eject_fails = true;
        lun.eject_busy = true;
        lun.medium_present = false;
        let out = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &NoopGate,
            Partition::P1,
            &del(),
            false,
            |_| {},
        );
        assert!(matches!(out, HandoffOutcome::CriticalFault(_)));
    }

    #[test]
    fn eject_nonbusy_with_medium_present_is_failed() {
        let mut lun = FakeLun::ok();
        lun.eject_fails = true;
        lun.eject_busy = false;
        lun.medium_present = true;
        let out = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &NoopGate,
            Partition::P1,
            &del(),
            false,
            |_| {},
        );
        assert!(matches!(out, HandoffOutcome::Failed(_)));
        assert_eq!(*lun.events.borrow(), ["eject", "represent"]);
    }

    #[test]
    fn mutate_failure_without_release_leaves_lun_ejected() {
        let lun = FakeLun::ok();
        let out = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::err(false),
            &NoopGate,
            Partition::P1,
            &del(),
            false,
            |_| {},
        );
        assert!(matches!(out, HandoffOutcome::CriticalFault(_)));
        // The never-double-writer precedence: we did NOT re-present.
        assert_eq!(
            *lun.events.borrow(),
            ["eject"],
            "must not re-present onto a held image"
        );
    }

    #[test]
    fn represent_failure_after_success_is_critical() {
        let mut lun = FakeLun::ok();
        lun.represent_fails = true;
        let out = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &NoopGate,
            Partition::P1,
            &del(),
            false,
            |_| {},
        );
        assert!(matches!(out, HandoffOutcome::CriticalFault(_)));
    }

    #[test]
    fn progress_reaches_done_on_happy_path() {
        let lun = FakeLun::ok();
        let seen = RefCell::new(Vec::new());
        let _ = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &NoopGate,
            Partition::P1,
            &del(),
            false,
            |p: HandoffPhase| seen.borrow_mut().push(p.as_str()),
        );
        assert_eq!(
            *seen.borrow(),
            ["ejecting", "applying", "representing", "done"]
        );
    }

    #[test]
    fn p1_handoff_never_uses_media_gate() {
        let lun = FakeLun::ok();
        let gate = RecordingGate {
            suspends: Cell::new(0),
            resumes: Cell::new(0),
            suspend_err: false,
            resume_err: false,
        };
        let outcome = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &gate,
            Partition::P1,
            &del(),
            false,
            |_| {},
        );
        assert_eq!(outcome, HandoffOutcome::Done);
        assert_eq!(gate.suspends.get(), 0);
        assert_eq!(gate.resumes.get(), 0);
    }

    #[test]
    fn p2_success_suspends_and_resumes() {
        let lun = FakeLun::ok();
        let gate = RecordingGate {
            suspends: Cell::new(0),
            resumes: Cell::new(0),
            suspend_err: false,
            resume_err: false,
        };
        let outcome = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &gate,
            Partition::P2,
            &del(),
            false,
            |_| {},
        );
        assert_eq!(outcome, HandoffOutcome::Done);
        assert_eq!(gate.suspends.get(), 1);
        assert_eq!(gate.resumes.get(), 1);
        assert_eq!(*lun.events.borrow(), ["eject", "represent"]);
    }

    #[test]
    fn p2_suspend_error_refuses_before_eject() {
        let lun = FakeLun::ok();
        let gate = RecordingGate {
            suspends: Cell::new(0),
            resumes: Cell::new(0),
            suspend_err: true,
            resume_err: false,
        };
        let outcome = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &gate,
            Partition::P2,
            &del(),
            false,
            |_| {},
        );
        assert!(
            matches!(outcome, HandoffOutcome::Refused(msg) if msg.contains("media_ro_suspend_failed"))
        );
        assert_eq!(gate.suspends.get(), 1);
        assert_eq!(gate.resumes.get(), 0);
        assert!(lun.events.borrow().is_empty());
    }

    #[test]
    fn p2_resume_error_does_not_change_done_outcome() {
        let lun = FakeLun::ok();
        let gate = RecordingGate {
            suspends: Cell::new(0),
            resumes: Cell::new(0),
            suspend_err: false,
            resume_err: true,
        };
        let outcome = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::ok(),
            &gate,
            Partition::P2,
            &del(),
            false,
            |_| {},
        );
        assert_eq!(outcome, HandoffOutcome::Done);
        assert_eq!(gate.resumes.get(), 1);
    }

    #[test]
    fn p2_critical_fault_still_resumes() {
        // A P2 handoff that suspended the read mount MUST resume it even when the
        // core handoff ends in CriticalFault — otherwise the web read path is
        // stranded 404ing until gadgetd restarts. The resume itself is fail-safe
        // (MediaRoMount refuses to re-mount over a stale loop).
        let lun = FakeLun::ok();
        let gate = RecordingGate {
            suspends: Cell::new(0),
            resumes: Cell::new(0),
            suspend_err: false,
            resume_err: false,
        };
        let outcome = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::err(false),
            &gate,
            Partition::P2,
            &del(),
            false,
            |_| {},
        );
        assert!(matches!(outcome, HandoffOutcome::CriticalFault(_)));
        assert_eq!(gate.suspends.get(), 1);
        assert_eq!(gate.resumes.get(), 1);
    }

    #[test]
    fn p2_critical_fault_resume_error_preserves_outcome() {
        // A resume failure on the CriticalFault path is logged but never masks the
        // original CriticalFault outcome the caller must act on.
        let lun = FakeLun::ok();
        let gate = RecordingGate {
            suspends: Cell::new(0),
            resumes: Cell::new(0),
            suspend_err: false,
            resume_err: true,
        };
        let outcome = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::err(false),
            &gate,
            Partition::P2,
            &del(),
            false,
            |_| {},
        );
        assert!(matches!(outcome, HandoffOutcome::CriticalFault(_)));
        assert_eq!(gate.suspends.get(), 1);
        assert_eq!(gate.resumes.get(), 1);
    }
}
