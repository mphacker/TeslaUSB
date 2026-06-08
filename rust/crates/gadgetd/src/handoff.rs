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

    /// The 1-based partition index (used to build the `loopNpX` node name).
    pub(crate) fn index(self) -> u8 {
        match self {
            Self::P1 => 1,
            Self::P2 => 2,
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
    /// Copy a staged file from `source_path` (outside the image) to `rel_path`
    /// under the mounted partition root, via a temp file + atomic rename.
    InstallFile {
        /// Partition-root-relative destination path.
        rel_path: String,
        /// Absolute path of the staged source file on the Pi data area.
        source_path: String,
    },
}

impl Mutation {
    /// The validated, root-relative destination/target path of this mutation.
    pub(crate) fn rel_path(&self) -> &str {
        match self {
            Self::DeletePath { rel_path } | Self::InstallFile { rel_path, .. } => rel_path,
        }
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
            Self::CriticalFault(_) => "critical_fault",
        }
    }

    /// The human-readable detail, if any.
    pub(crate) fn detail(&self) -> Option<&str> {
        match self {
            Self::Done => None,
            Self::Refused(d) | Self::Failed(d) | Self::CriticalFault(d) => Some(d),
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
/// `allow_hot` permits ejecting while the host is actively enumerated. It
/// defaults OFF: the car's tolerance to a mid-use eject is unmeasured
/// (`SPEC.md` §9 prototype-unknown #2), so a production hot handoff is refused
/// until that is validated on the car and the operator opts in.
///
/// Serialization (never two concurrent handoffs) is the caller's responsibility
/// (a `try_lock` on the handoff mutex); this function assumes exclusive access.
pub(crate) fn run_handoff(
    lun: &dyn LunControl,
    guard: &dyn SaveGuard,
    mutator: &dyn ImageMutator,
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
    let configured = lun.udc_configured().unwrap_or(true);
    if configured && !allow_hot {
        return HandoffOutcome::Refused(
            "hot_handoff_unvalidated: host is enumerated and the car's mid-use eject \
             tolerance is unmeasured (SPEC.md §9 #2); enable only after HW validation"
                .to_owned(),
        );
    }

    match guard.is_save_active() {
        Ok(false) => {}
        Ok(true) => return HandoffOutcome::Refused("save_active".to_owned()),
        Err(e) => return HandoffOutcome::Refused(format!("save-guard failed: {e}")),
    }

    // --- Eject (from here the image is ours; the LUN must end re-presented
    //     unless the mutate path cannot prove it released the image) ---
    progress(HandoffPhase::Ejecting);
    if let Err(e) = lun.eject() {
        // Nothing was handed to the local path yet; the eject either took (rare
        // partial) or did not. Re-presenting is safe and restores the medium.
        let represent = lun.represent();
        return represent.map_or_else(
            |re| {
                HandoffOutcome::CriticalFault(format!(
                    "eject failed ({e}) and re-present failed ({re})"
                ))
            },
            |()| HandoffOutcome::Failed(format!("eject failed: {e}")),
        );
    }

    // --- Mount + mutate (self-cleaning) ---
    progress(HandoffPhase::Applying);
    match mutator.apply(partition, mutation) {
        Ok(()) => represent_after(lun, &mut progress, HandoffOutcome::Done),
        Err(me) if me.image_released => {
            represent_after(lun, &mut progress, HandoffOutcome::Failed(me.detail))
        }
        Err(me) => HandoffOutcome::CriticalFault(format!(
            "mutate failed and image NOT released ({}); LUN left ejected to \
             prevent a double-writer — recovery required",
            me.detail
        )),
    }
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
        SaveGuard, run_handoff, validate_rel_path,
    };
    use std::cell::RefCell;

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

    // ---- Fakes for the orchestration tests ----
    #[allow(clippy::struct_excessive_bools)]
    struct FakeLun {
        bound: bool,
        configured: bool,
        eject_fails: bool,
        represent_fails: bool,
        events: RefCell<Vec<&'static str>>,
    }
    impl FakeLun {
        fn ok() -> Self {
            Self {
                bound: true,
                configured: false,
                eject_fails: false,
                represent_fails: false,
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
            Ok(false)
        }
        fn eject(&self) -> std::io::Result<()> {
            self.events.borrow_mut().push("eject");
            if self.eject_fails {
                Err(std::io::Error::other("eject boom"))
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
            Partition::P1,
            &del(),
            true,
            |_| {},
        );
        assert_eq!(out, HandoffOutcome::Done);
    }

    #[test]
    fn refuses_when_save_active() {
        let lun = FakeLun::ok();
        let out = run_handoff(
            &lun,
            &FakeGuard(true),
            &FakeMutator::ok(),
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
    fn mutate_failure_without_release_leaves_lun_ejected() {
        let lun = FakeLun::ok();
        let out = run_handoff(
            &lun,
            &FakeGuard(false),
            &FakeMutator::err(false),
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
}
