//! Retention filter (Phase 4.1).
//!
//! The `RecentClips` directory grows unboundedly as Tesla writes
//! dashcam footage. v1 solved this with a Python cleanup script
//! that physically deleted files older than a retention window;
//! the B-1 design splits that into two concerns:
//!
//! 1. **Hide-from-view** (this module) — aged files are omitted
//!    from the synthesized directory listing that Tesla sees,
//!    but the backing files are NOT touched. This keeps the
//!    Tesla UI window-bounded without giving up cleanup
//!    flexibility.
//! 2. **Actual deletion** — handled by the cleanup worker in
//!    Phase 4b. It honours preservation rules (Sentry, Saved,
//!    GPS-tagged clips) that the hide-from-view filter doesn't
//!    need to know about.
//!
//! This module is pure logic — all time inputs are passed in
//! explicitly so tests can use a frozen clock without monkey-
//! patching `SystemTime::now()`. Wiring into the synth happens
//! in Phase 4.3 (free-cluster reporting) and Phase 4.4 (config
//! plumbing).

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use teslausb_core::fs::backing_tree::{BackingDir, BackingTree};

/// The single top-level directory the retention rule applies to.
///
/// Tesla writes Sentry events to `SentryClips/`, manually-saved
/// clips to `SavedClips/`, and the rolling dashcam buffer to
/// `RecentClips/`. Only the rolling buffer is rotated; the
/// other two are user-curated archives that must remain visible
/// indefinitely. Files outside `RecentClips/` are always shown.
const RECENTCLIPS_DIR: &str = "RecentClips";

/// Retention policy applied by [`decide`] and [`apply`].
///
/// `recentclips_hide_after` is the maximum age a file under
/// `RecentClips/` may have before it is filtered from the
/// synthesized directory listing. The default value lives in
/// [`crate::config::RetentionConfig`]; this module never reads
/// config directly so unit tests can construct a policy without
/// loading TOML.
#[derive(Debug, Clone, Copy)]
pub struct Policy {
    /// Files under `RecentClips/` with an mtime older than `now -
    /// recentclips_hide_after` are hidden. A value of
    /// [`Duration::ZERO`] hides every file the moment its mtime
    /// is in the past; [`Duration::MAX`] effectively disables
    /// the filter.
    pub recentclips_hide_after: Duration,
}

impl Policy {
    /// Construct a policy from a hide-after duration.
    #[must_use]
    pub const fn new(recentclips_hide_after: Duration) -> Self {
        Self {
            recentclips_hide_after,
        }
    }
}

/// The decision returned by [`decide`].
///
/// Modelled as an explicit enum rather than `bool` to avoid the
/// boolean-trap anti-pattern (charter §"Anti-patterns") —
/// callers can `match` exhaustively and reviewers see intent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    /// The file should appear in the synthesized directory
    /// listing.
    Show,
    /// The file should be omitted from the listing. The backing
    /// file is NOT touched by this decision.
    Hide,
}

/// Decide whether a single backing file should appear in the
/// synthesized directory listing.
///
/// `relative_path` is the file's path relative to the backing
/// root (the path Tesla sees, modulo separator conventions).
/// Files whose first path component is not `RecentClips` are
/// always shown — the retention rule scopes to that one
/// directory by design (see `RECENTCLIPS_DIR`).
///
/// A file whose `mtime` is in the future relative to `now`
/// (e.g. clock skew between the Pi and whatever produced the
/// file) is always shown. The implementation treats negative
/// age as zero rather than wrapping or hiding, so a clock jump
/// can never cause silent data loss from the user's view.
#[must_use]
pub fn decide(
    relative_path: &Path,
    mtime: SystemTime,
    now: SystemTime,
    policy: &Policy,
) -> Decision {
    if !is_under_recentclips(relative_path) {
        return Decision::Show;
    }
    let age = now.duration_since(mtime).unwrap_or(Duration::ZERO);
    if age > policy.recentclips_hide_after {
        Decision::Hide
    } else {
        Decision::Show
    }
}

/// Apply [`decide`] recursively across a [`BackingTree`],
/// dropping files whose decision is [`Decision::Hide`].
///
/// Subdirectories are kept even if all their files are hidden;
/// the synth's directory-entry emitter handles empty directories
/// fine, and dropping a directory might confuse Tesla if it
/// remembered the directory from a previous mount. Symmetric to
/// how `RecentClips/` itself stays visible even with zero
/// visible files.
///
/// `backing_root` is the absolute path the walker was rooted at;
/// each file's `backing_path` is stripped of this prefix to
/// compute the relative path passed to [`decide`]. Files whose
/// `backing_path` does NOT lie under `backing_root` (a
/// pathological case that should never happen with the v2.15
/// walker) are conservatively kept visible — losing user data
/// to a defensive check is worse than showing a confusing entry.
pub fn apply(
    tree: &mut BackingTree,
    backing_root: &Path,
    now: SystemTime,
    policy: &Policy,
) -> ApplyStats {
    let mut stats = ApplyStats::default();
    apply_dir(&mut tree.root, backing_root, now, policy, &mut stats);
    stats
}

/// Counters returned by [`apply`] so callers (and logs) can
/// observe how many files the filter dropped from a given walk.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct ApplyStats {
    /// Number of [`Decision::Hide`] decisions taken.
    pub hidden: usize,
    /// Number of [`Decision::Show`] decisions taken (across
    /// every file, including those outside `RecentClips/`).
    pub shown: usize,
}

fn apply_dir(
    dir: &mut BackingDir,
    backing_root: &Path,
    now: SystemTime,
    policy: &Policy,
    stats: &mut ApplyStats,
) {
    dir.files.retain(|f| {
        let Ok(relative) = f.backing_path.strip_prefix(backing_root) else {
            // Defensive: a walker bug could in principle
            // produce a backing_path outside backing_root.
            // Keep the file visible rather than silently
            // dropping it.
            stats.shown += 1;
            return true;
        };
        match decide(relative, f.mtime, now, policy) {
            Decision::Show => {
                stats.shown += 1;
                true
            }
            Decision::Hide => {
                stats.hidden += 1;
                false
            }
        }
    });
    for sub in &mut dir.subdirs {
        apply_dir(sub, backing_root, now, policy, stats);
    }
}

fn is_under_recentclips(relative_path: &Path) -> bool {
    relative_path
        .components()
        .next()
        .is_some_and(|c| c.as_os_str() == RECENTCLIPS_DIR)
}

/// Set of backing-tree-relative paths that Tesla has marked as
/// deleted via a directory-entry mutation (FAT32: SFN leading byte
/// rewritten to `0xE5`; exFAT: File entry `InUse` bit cleared).
///
/// Phase 4.2 intercepts those deletions instead of honoring them
/// against the backing tree: the write-state machine records the
/// path here and leaves the backing file present on disk. The
/// cleanup worker (Phase 4b) consults this set, the file's mtime,
/// and the SEI / GPS index to decide whether to actually reap the
/// file or to keep it because it carries event metadata Tesla's
/// blind round-robin reuse would otherwise destroy.
///
/// The set carries no timestamps: time-of-deletion is not useful
/// to the cleanup policy (which keys off the file's own mtime), and
/// omitting it keeps the struct trivially serializable for the
/// Phase 4.4 IPC snapshot.
#[derive(Debug, Default, Clone)]
pub struct DeletedSet {
    paths: HashSet<PathBuf>,
}

impl DeletedSet {
    /// Construct an empty set.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Record `relative_path` as deleted. Returns `true` when this
    /// is the first time the path has been marked, `false` if it
    /// was already present (Tesla rewrites the same dir cluster
    /// many times; re-decodes will replay the deletion).
    pub fn mark(&mut self, relative_path: PathBuf) -> bool {
        self.paths.insert(relative_path)
    }

    /// `true` if `relative_path` has been recorded as deleted.
    #[must_use]
    pub fn contains(&self, relative_path: &Path) -> bool {
        self.paths.contains(relative_path)
    }

    /// Remove `relative_path` from the set. Returns `true` if it
    /// was present. Used by the cleanup worker to acknowledge
    /// reaping (or by the write-state machine when the kernel
    /// re-creates a file with the same name in the same dir, in
    /// which case the prior deletion is no longer relevant).
    pub fn forget(&mut self, relative_path: &Path) -> bool {
        self.paths.remove(relative_path)
    }

    /// Iterate the recorded deleted paths. Iteration order is
    /// unspecified (backed by `HashSet`).
    pub fn iter(&self) -> impl Iterator<Item = &Path> {
        self.paths.iter().map(PathBuf::as_path)
    }

    /// Number of recorded deletions.
    #[must_use]
    pub fn len(&self) -> usize {
        self.paths.len()
    }

    /// `true` when no deletions are recorded.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.paths.is_empty()
    }
}

#[cfg(test)]
#[allow(
    clippy::missing_panics_doc,
    clippy::unwrap_used,
    clippy::indexing_slicing
)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use teslausb_core::fs::backing_tree::{BackingDir, BackingFile, BackingTree};

    /// A fixed point in time for tests — chosen far enough into
    /// the future of the Unix epoch that subtracting an hour
    /// can't underflow.
    fn frozen_now() -> SystemTime {
        SystemTime::UNIX_EPOCH + Duration::from_secs(1_700_000_000)
    }

    fn one_hour() -> Duration {
        Duration::from_secs(3600)
    }

    fn policy_1h() -> Policy {
        Policy::new(one_hour())
    }

    fn file(name: &str, backing_path: PathBuf, age: Duration) -> BackingFile {
        BackingFile {
            name: name.to_string(),
            backing_path,
            size: 1024,
            mtime: frozen_now() - age,
        }
    }

    fn dir(name: &str, backing_path: PathBuf, files: Vec<BackingFile>) -> BackingDir {
        BackingDir {
            name: name.to_string(),
            backing_path,
            mtime: frozen_now(),
            subdirs: Vec::new(),
            files,
        }
    }

    // ---------- decide() ----------

    #[test]
    fn decide_outside_recentclips_always_shows_even_when_ancient() {
        // SentryClips and SavedClips are user-curated archives;
        // retention must not touch them no matter how old.
        let ancient = frozen_now() - Duration::from_secs(86_400 * 365);
        assert_eq!(
            decide(
                Path::new("SentryClips/2026-01-01_event"),
                ancient,
                frozen_now(),
                &policy_1h(),
            ),
            Decision::Show,
        );
        assert_eq!(
            decide(
                Path::new("SavedClips/2026-01-01_event"),
                ancient,
                frozen_now(),
                &policy_1h(),
            ),
            Decision::Show,
        );
        // Root-level files also stay visible.
        assert_eq!(
            decide(
                Path::new("LockChime.wav"),
                ancient,
                frozen_now(),
                &policy_1h(),
            ),
            Decision::Show,
        );
    }

    #[test]
    fn decide_recentclips_within_window_shows() {
        // Half an hour old, window is one hour → still visible.
        let mtime = frozen_now() - Duration::from_secs(1800);
        assert_eq!(
            decide(
                Path::new("RecentClips/2026-05-20_12-00-00-front.mp4"),
                mtime,
                frozen_now(),
                &policy_1h(),
            ),
            Decision::Show,
        );
    }

    #[test]
    fn decide_recentclips_beyond_window_hides() {
        // Two hours old, window is one hour → hidden.
        let mtime = frozen_now() - Duration::from_secs(7200);
        assert_eq!(
            decide(
                Path::new("RecentClips/2026-05-20_10-00-00-front.mp4"),
                mtime,
                frozen_now(),
                &policy_1h(),
            ),
            Decision::Hide,
        );
    }

    #[test]
    fn decide_at_exact_threshold_shows_strict_greater_than() {
        // Exactly equal to the window must NOT be hidden — the
        // rule is `age > threshold`, not `>=`. This pins the
        // boundary so a refactor can't silently flip it.
        let mtime = frozen_now() - one_hour();
        assert_eq!(
            decide(
                Path::new("RecentClips/edge.mp4"),
                mtime,
                frozen_now(),
                &policy_1h(),
            ),
            Decision::Show,
        );
    }

    #[test]
    fn decide_one_second_past_threshold_hides() {
        let mtime = frozen_now() - (one_hour() + Duration::from_secs(1));
        assert_eq!(
            decide(
                Path::new("RecentClips/edge.mp4"),
                mtime,
                frozen_now(),
                &policy_1h(),
            ),
            Decision::Hide,
        );
    }

    #[test]
    fn decide_mtime_in_future_treats_age_as_zero_and_shows() {
        // Clock skew between Pi and whatever wrote the file
        // (e.g. Tesla with a slightly faster clock) must not
        // cause silent data loss from the user's view. Negative
        // age must clamp to zero, not wrap.
        let mtime = frozen_now() + Duration::from_secs(60);
        assert_eq!(
            decide(
                Path::new("RecentClips/future.mp4"),
                mtime,
                frozen_now(),
                &policy_1h(),
            ),
            Decision::Show,
        );
    }

    #[test]
    fn decide_zero_window_hides_anything_in_the_past() {
        // Edge case: a Policy with Duration::ZERO. Any file
        // with mtime strictly older than `now` is hidden;
        // mtime == now is shown (boundary is exclusive).
        let policy = Policy::new(Duration::ZERO);
        let one_ms = Duration::from_millis(1);
        assert_eq!(
            decide(
                Path::new("RecentClips/a.mp4"),
                frozen_now() - one_ms,
                frozen_now(),
                &policy,
            ),
            Decision::Hide,
        );
        assert_eq!(
            decide(
                Path::new("RecentClips/now.mp4"),
                frozen_now(),
                frozen_now(),
                &policy,
            ),
            Decision::Show,
        );
    }

    #[test]
    fn decide_max_window_never_hides() {
        let policy = Policy::new(Duration::MAX);
        let ancient = frozen_now() - Duration::from_secs(86_400 * 365);
        assert_eq!(
            decide(
                Path::new("RecentClips/ancient.mp4"),
                ancient,
                frozen_now(),
                &policy,
            ),
            Decision::Show,
        );
    }

    #[test]
    fn decide_recentclips_substring_match_does_not_hide() {
        // A leaf path like `RecentClipsBackup/x.mp4` must NOT
        // be considered under RecentClips/. The match is on
        // exact path component equality, not prefix.
        let mtime = frozen_now() - Duration::from_secs(7200);
        assert_eq!(
            decide(
                Path::new("RecentClipsBackup/x.mp4"),
                mtime,
                frozen_now(),
                &policy_1h(),
            ),
            Decision::Show,
        );
    }

    #[test]
    fn decide_nested_recentclips_path_still_applies() {
        // RecentClips/<event>/<file>.mp4 is the actual layout
        // Tesla uses. The filter must match by first component
        // regardless of how deep the file lives.
        let mtime = frozen_now() - Duration::from_secs(7200);
        assert_eq!(
            decide(
                Path::new("RecentClips/2026-05-20_10-00-00/front.mp4"),
                mtime,
                frozen_now(),
                &policy_1h(),
            ),
            Decision::Hide,
        );
    }

    // ---------- apply() ----------

    fn sample_tree(backing_root: &Path) -> BackingTree {
        // Build a tree with the realistic Tesla layout:
        //   /SentryClips/old_event/front.mp4  (ancient — must stay)
        //   /SavedClips/keepme.mp4             (ancient — must stay)
        //   /RecentClips/young/front.mp4       (recent — stays)
        //   /RecentClips/old/front.mp4         (ancient — hidden)
        let sentry = dir(
            "old_event",
            backing_root.join("SentryClips/old_event"),
            vec![file(
                "front.mp4",
                backing_root.join("SentryClips/old_event/front.mp4"),
                Duration::from_secs(86_400 * 30),
            )],
        );
        let mut sentry_top = dir("SentryClips", backing_root.join("SentryClips"), vec![]);
        sentry_top.subdirs.push(sentry);

        let mut saved_top = dir(
            "SavedClips",
            backing_root.join("SavedClips"),
            vec![file(
                "keepme.mp4",
                backing_root.join("SavedClips/keepme.mp4"),
                Duration::from_secs(86_400 * 30),
            )],
        );

        let young_evt = dir(
            "young",
            backing_root.join("RecentClips/young"),
            vec![file(
                "front.mp4",
                backing_root.join("RecentClips/young/front.mp4"),
                Duration::from_secs(60),
            )],
        );
        let old_evt = dir(
            "old",
            backing_root.join("RecentClips/old"),
            vec![file(
                "front.mp4",
                backing_root.join("RecentClips/old/front.mp4"),
                Duration::from_secs(7200),
            )],
        );
        let mut recent_top = dir("RecentClips", backing_root.join("RecentClips"), vec![]);
        recent_top.subdirs.push(young_evt);
        recent_top.subdirs.push(old_evt);

        saved_top.files.sort_by(|a, b| a.name.cmp(&b.name));
        BackingTree {
            root: BackingDir {
                name: String::new(),
                backing_path: backing_root.to_path_buf(),
                mtime: frozen_now(),
                subdirs: vec![recent_top, saved_top, sentry_top],
                files: Vec::new(),
            },
        }
    }

    #[test]
    fn apply_drops_only_aged_recentclips_files_and_keeps_directories() {
        let root = PathBuf::from("/var/teslacam");
        let mut tree = sample_tree(&root);

        let stats = apply(&mut tree, &root, frozen_now(), &policy_1h());

        // 1 file hidden (RecentClips/old/front.mp4), 3 shown
        // (RecentClips/young/front.mp4, SavedClips/keepme.mp4,
        // SentryClips/old_event/front.mp4).
        assert_eq!(stats.hidden, 1);
        assert_eq!(stats.shown, 3);

        let recent = tree
            .root
            .subdirs
            .iter()
            .find(|d| d.name == "RecentClips")
            .unwrap();
        // Directory entries for both events stay (Show even when
        // empty — Tesla's UI handles empty event dirs fine).
        let names: Vec<&str> = recent.subdirs.iter().map(|d| d.name.as_str()).collect();
        assert_eq!(names, vec!["young", "old"]);

        // Young event still has its file.
        let young = &recent.subdirs[0];
        assert_eq!(young.files.len(), 1);
        // Old event is empty.
        let old = &recent.subdirs[1];
        assert_eq!(old.files.len(), 0);

        // Sentry + Saved untouched regardless of age.
        let sentry = tree
            .root
            .subdirs
            .iter()
            .find(|d| d.name == "SentryClips")
            .unwrap();
        assert_eq!(sentry.subdirs[0].files.len(), 1);
        let saved = tree
            .root
            .subdirs
            .iter()
            .find(|d| d.name == "SavedClips")
            .unwrap();
        assert_eq!(saved.files.len(), 1);
    }

    #[test]
    fn apply_with_max_window_drops_nothing() {
        let root = PathBuf::from("/var/teslacam");
        let mut tree = sample_tree(&root);
        let stats = apply(&mut tree, &root, frozen_now(), &Policy::new(Duration::MAX));
        assert_eq!(stats.hidden, 0);
        assert_eq!(stats.shown, 4);
    }

    #[test]
    fn apply_with_zero_window_hides_all_recentclips_files() {
        let root = PathBuf::from("/var/teslacam");
        let mut tree = sample_tree(&root);
        let stats = apply(&mut tree, &root, frozen_now(), &Policy::new(Duration::ZERO));
        // Both RecentClips files hidden (young 60s old, old 2h
        // old — both > zero); Sentry + Saved still shown.
        assert_eq!(stats.hidden, 2);
        assert_eq!(stats.shown, 2);
    }

    #[test]
    fn apply_keeps_files_with_backing_path_outside_root_defensively() {
        // Pathological case: a BackingFile whose backing_path
        // somehow doesn't lie under backing_root. We do NOT
        // want to silently delete user data on a walker bug —
        // keep it visible and let the operator notice.
        let root = PathBuf::from("/var/teslacam");
        let orphan = BackingFile {
            name: "orphan.mp4".to_string(),
            backing_path: PathBuf::from("/elsewhere/orphan.mp4"),
            size: 1024,
            mtime: frozen_now() - Duration::from_secs(86_400),
        };
        let mut tree = BackingTree {
            root: BackingDir {
                name: String::new(),
                backing_path: root.clone(),
                mtime: frozen_now(),
                subdirs: Vec::new(),
                files: vec![orphan],
            },
        };
        let stats = apply(&mut tree, &root, frozen_now(), &Policy::new(Duration::ZERO));
        assert_eq!(stats.hidden, 0);
        assert_eq!(stats.shown, 1);
        assert_eq!(tree.root.files.len(), 1);
    }

    // ---- DeletedSet ----

    #[test]
    fn deleted_set_starts_empty() {
        let d = DeletedSet::new();
        assert!(d.is_empty());
        assert_eq!(d.len(), 0);
        assert!(!d.contains(Path::new("RecentClips/foo.mp4")));
    }

    #[test]
    fn deleted_set_mark_returns_true_first_time_false_after() {
        let mut d = DeletedSet::new();
        let p = PathBuf::from("RecentClips/2024-01-01_12-00-00-front.mp4");
        assert!(d.mark(p.clone()), "first mark should report novel");
        assert!(!d.mark(p.clone()), "second mark should report duplicate");
        assert_eq!(d.len(), 1);
        assert!(d.contains(&p));
    }

    #[test]
    fn deleted_set_forget_removes_recorded_entry() {
        let mut d = DeletedSet::new();
        let p = PathBuf::from("SentryClips/2024-01-01_12-00-00/event.json");
        d.mark(p.clone());
        assert!(d.forget(&p));
        assert!(!d.forget(&p), "forget on missing path is false");
        assert!(d.is_empty());
    }

    #[test]
    fn deleted_set_iter_yields_all_marked() {
        let mut d = DeletedSet::new();
        let a = PathBuf::from("RecentClips/a.mp4");
        let b = PathBuf::from("RecentClips/b.mp4");
        d.mark(a.clone());
        d.mark(b.clone());
        let collected: HashSet<PathBuf> = d.iter().map(Path::to_path_buf).collect();
        assert_eq!(collected.len(), 2);
        assert!(collected.contains(&a));
        assert!(collected.contains(&b));
    }
}
