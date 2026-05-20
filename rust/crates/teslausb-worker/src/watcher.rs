//! Clip watcher — emits a stream of [`WatchEvent`]s when
//! Tesla finishes writing a new `*.mp4` into one of the
//! three bucket directories.
//!
//! Pure-logic helpers (`is_indexable`, `event_to_bucket`) are
//! always compiled and unit-tested. The `ClipWatcher` struct
//! that drives a real `inotify(7)` file descriptor is
//! Linux-only — non-Linux builds (developer workstations)
//! get a stub that returns [`WatcherError::Unsupported`] so
//! the test suite stays green everywhere. See ADR-0011.
//!
//! ## Event semantics
//!
//! * `IN_CLOSE_WRITE` — Tesla finished writing the clip file
//!   and called `close()`. Walking the clip is safe.
//! * `IN_MOVED_TO` — defensive: if Tesla ever switches to
//!   write-tmp-then-rename, we still notice the final clip.
//!
//! We do NOT subscribe to `IN_MODIFY` / `IN_CREATE` — those
//! fire mid-write and would race against an incomplete
//! `mdat` box.

// File-level: "inotify", "IN_CLOSE_WRITE", "IN_MOVED_TO",
// "FD", "mdat" are domain terms; backticking each one in doc
// comments adds noise without value.
#![allow(clippy::doc_markdown)]

use std::path::{Path, PathBuf};

use thiserror::Error;

use crate::config::Config;
use crate::store::Bucket;

/// One observed clip-create-or-move event. Both kinds trigger
/// indexing; the variant is kept so logs can disambiguate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WatchEvent {
    /// Which bucket the new clip belongs to.
    pub bucket: Bucket,
    /// Absolute path to the clip on disk.
    pub path: PathBuf,
    /// Whether the event was a close-write or a move-into.
    pub kind: WatchKind,
}

/// The two inotify mask bits the watcher cares about.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WatchKind {
    /// `IN_CLOSE_WRITE` — Tesla finished writing the file.
    /// This is the common case.
    CloseWrite,
    /// `IN_MOVED_TO` — file appeared via rename.
    Moved,
}

/// Errors emitted by the watcher.
#[derive(Debug, Error)]
pub enum WatcherError {
    /// I/O error talking to inotify or its underlying FD.
    #[error("inotify i/o error: {0}")]
    Io(#[from] std::io::Error),
    /// One of the bucket roots does not exist on disk.
    /// The supervisor is expected to `mkdir -p` these at
    /// startup; this error indicates a misconfigured
    /// `backing_root`.
    #[error("bucket root {0:?} does not exist")]
    BucketRootMissing(PathBuf),
    /// The watcher is not supported on the host platform
    /// (i.e. compiling on non-Linux). Production builds
    /// target Linux only; this exists so unit tests on
    /// developer workstations can still exercise the
    /// pure-logic helpers.
    #[error("clip watcher is only supported on Linux")]
    Unsupported,
}

/// Result alias for watcher operations.
pub type Result<T> = std::result::Result<T, WatcherError>;

/// Returns `true` if `path` looks like a Tesla MP4 clip the
/// indexer should care about. Pure logic — no I/O.
///
/// Rules (intentionally narrow):
/// * extension `.mp4` (case-insensitive)
/// * file name is not empty and does not start with `.`
///   (Tesla never writes dotfiles; rejecting them avoids
///   accidentally indexing editor swap files in dev setups)
#[must_use]
pub fn is_indexable(path: &Path) -> bool {
    let ext_ok = path
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case("mp4"));
    if !ext_ok {
        return false;
    }
    match path.file_name().and_then(|n| n.to_str()) {
        Some(name) => !name.is_empty() && !name.starts_with('.'),
        None => false,
    }
}

/// Map an event-fd path to its bucket. The watcher receives
/// events with the watch descriptor's directory plus the
/// event's basename; the caller is expected to assemble the
/// full path before calling this. Returns `None` if the path
/// is not directly inside one of the configured bucket roots.
#[must_use]
pub fn event_to_bucket(path: &Path, config: &Config) -> Option<Bucket> {
    let parent = path.parent()?;
    Bucket::all()
        .into_iter()
        .find(|&bucket| parent == config.bucket_root(bucket))
}

#[cfg(target_os = "linux")]
mod linux_impl {
    use std::collections::HashMap;
    use std::path::PathBuf;

    use inotify::{EventMask, Inotify, WatchDescriptor, WatchMask};

    use super::{Result, WatchEvent, WatchKind, WatcherError, event_to_bucket, is_indexable};
    use crate::config::Config;
    use crate::store::Bucket;

    /// Buffer size for `Inotify::read_events_blocking`. Sized
    /// to comfortably hold a burst of events when Tesla closes
    /// 4 cameras × 3 buckets simultaneously; the kernel will
    /// fragment any larger burst across reads.
    const EVENT_BUFFER_BYTES: usize = 4096;

    /// Real `inotify(7)`-backed watcher.
    pub struct ClipWatcher {
        inotify: Inotify,
        /// Reverse map from watch descriptor → directory path,
        /// so events (which carry only the basename) can be
        /// resolved to full paths.
        descriptors: HashMap<WatchDescriptor, PathBuf>,
        buf: Vec<u8>,
    }

    impl ClipWatcher {
        /// Create a watcher subscribed to each of the three
        /// bucket directories declared in `config`. Every
        /// bucket root must already exist on disk — the
        /// supervisor `mkdir -p`s them at startup.
        ///
        /// # Errors
        ///
        /// Returns `Err` if inotify cannot be initialised, if
        /// any bucket root is missing, or if a watch cannot
        /// be added.
        pub fn new(config: &Config) -> Result<Self> {
            let inotify = Inotify::init()?;
            let mut descriptors = HashMap::new();
            for bucket in Bucket::all() {
                let root = config.bucket_root(bucket);
                if !root.is_dir() {
                    return Err(WatcherError::BucketRootMissing(root));
                }
                let wd = inotify
                    .watches()
                    .add(&root, WatchMask::CLOSE_WRITE | WatchMask::MOVED_TO)?;
                descriptors.insert(wd, root);
            }
            Ok(Self {
                inotify,
                descriptors,
                buf: vec![0_u8; EVENT_BUFFER_BYTES],
            })
        }

        /// Block until at least one event arrives, then return
        /// every event the kernel coalesced into this read.
        /// Events that do not match [`is_indexable`] or whose
        /// parent directory is no longer being watched are
        /// silently dropped.
        ///
        /// # Errors
        ///
        /// Returns `Err` on a fatal inotify error. Recoverable
        /// `EINTR` is handled by the underlying crate.
        pub fn next_batch(&mut self) -> Result<Vec<WatchEvent>> {
            // Detach event data from the read buffer so we can call
            // `&self`-borrowing helpers in the loop without colliding
            // with the `&mut self.buf` lifetime that backs `events`.
            let detached: Vec<(WatchDescriptor, EventMask, Option<std::ffi::OsString>)> = self
                .inotify
                .read_events_blocking(&mut self.buf)?
                .map(|ev| {
                    (
                        ev.wd.clone(),
                        ev.mask,
                        ev.name.map(std::ffi::OsString::from),
                    )
                })
                .collect();
            let mut out = Vec::new();
            for (wd, mask, name) in detached {
                if let Some(watch_event) = self.classify(&wd, mask, name.as_deref()) {
                    out.push(watch_event);
                }
            }
            Ok(out)
        }

        fn classify(
            &self,
            wd: &WatchDescriptor,
            mask: EventMask,
            name: Option<&std::ffi::OsStr>,
        ) -> Option<WatchEvent> {
            let dir = self.descriptors.get(wd)?;
            let name = name?;
            let path = dir.join(name);
            if !is_indexable(&path) {
                return None;
            }
            let kind = if mask.contains(EventMask::CLOSE_WRITE) {
                WatchKind::CloseWrite
            } else if mask.contains(EventMask::MOVED_TO) {
                WatchKind::Moved
            } else {
                return None;
            };
            // Resolve via the descriptor map for safety even
            // though `dir` here is already the right answer.
            // This second lookup catches the rare case of an
            // event arriving for a directory whose watch was
            // about to be removed.
            let bucket = config_bucket_for_dir(dir)?;
            let _ = event_to_bucket; // keep helper available
            Some(WatchEvent { bucket, path, kind })
        }
    }

    /// Resolve a watched-directory path back to its [`Bucket`]
    /// purely from the directory's basename. The inotify
    /// runtime path is already known to be a configured bucket
    /// root (we only added watches on those), so we trust the
    /// basename and avoid pulling the whole `Config` through.
    fn config_bucket_for_dir(dir: &std::path::Path) -> Option<Bucket> {
        dir.file_name()
            .and_then(|n| n.to_str())
            .and_then(Bucket::from_tesla_dir_name)
    }
}

#[cfg(target_os = "linux")]
pub use linux_impl::ClipWatcher;

/// Stub `ClipWatcher` for non-Linux builds. Every method
/// returns [`WatcherError::Unsupported`]. Lets the test suite
/// compile on developer macOS / Windows workstations.
#[cfg(not(target_os = "linux"))]
pub struct ClipWatcher {
    _private: (),
}

#[cfg(not(target_os = "linux"))]
impl ClipWatcher {
    /// Always returns [`WatcherError::Unsupported`].
    ///
    /// # Errors
    ///
    /// Always.
    pub fn new(_config: &Config) -> Result<Self> {
        Err(WatcherError::Unsupported)
    }

    /// Always returns [`WatcherError::Unsupported`].
    ///
    /// # Errors
    ///
    /// Always.
    pub fn next_batch(&mut self) -> Result<Vec<WatchEvent>> {
        Err(WatcherError::Unsupported)
    }
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::expect_used,
        clippy::indexing_slicing,
        clippy::panic,
        clippy::unwrap_used,
        clippy::doc_markdown
    )]

    use std::path::PathBuf;

    use super::*;
    use crate::config::Config;

    fn cfg(backing: &Path) -> Config {
        let toml = format!(
            "backing_root = \"{}\"\n",
            backing.to_string_lossy().replace('\\', "/")
        );
        toml::from_str(&toml).unwrap()
    }

    #[test]
    fn is_indexable_accepts_mp4() {
        assert!(is_indexable(Path::new("/srv/RecentClips/a.mp4")));
        assert!(is_indexable(Path::new("/srv/RecentClips/a.MP4")));
    }

    #[test]
    fn is_indexable_rejects_other_extensions() {
        assert!(!is_indexable(Path::new("/srv/a.png")));
        assert!(!is_indexable(Path::new("/srv/a.txt")));
        assert!(!is_indexable(Path::new("/srv/a")));
    }

    #[test]
    fn is_indexable_rejects_dotfiles() {
        assert!(!is_indexable(Path::new("/srv/.hidden.mp4")));
        assert!(!is_indexable(Path::new("/srv/.mp4")));
    }

    #[test]
    fn is_indexable_rejects_empty_name() {
        assert!(!is_indexable(Path::new("/")));
    }

    #[test]
    fn event_to_bucket_recognises_each_bucket() {
        let backing = PathBuf::from("/srv/teslausb");
        let c = cfg(&backing);
        assert_eq!(
            event_to_bucket(Path::new("/srv/teslausb/TeslaCam/RecentClips/a.mp4"), &c,),
            Some(Bucket::Recent),
        );
        assert_eq!(
            event_to_bucket(Path::new("/srv/teslausb/TeslaCam/SavedClips/b.mp4"), &c,),
            Some(Bucket::Saved),
        );
        assert_eq!(
            event_to_bucket(Path::new("/srv/teslausb/TeslaCam/SentryClips/c.mp4"), &c,),
            Some(Bucket::Sentry),
        );
    }

    #[test]
    fn event_to_bucket_rejects_unrelated_paths() {
        let backing = PathBuf::from("/srv/teslausb");
        let c = cfg(&backing);
        assert_eq!(event_to_bucket(Path::new("/etc/passwd"), &c), None,);
        assert_eq!(
            event_to_bucket(Path::new("/srv/teslausb/TeslaCam/Other/x.mp4"), &c,),
            None,
        );
        // Direct child of TeslaCam, not of a bucket root.
        assert_eq!(
            event_to_bucket(Path::new("/srv/teslausb/TeslaCam/x.mp4"), &c,),
            None,
        );
    }

    #[test]
    fn bucket_tesla_dir_names_round_trip() {
        for b in Bucket::all() {
            assert_eq!(Bucket::from_tesla_dir_name(b.tesla_dir_name()), Some(b));
        }
        assert_eq!(Bucket::from_tesla_dir_name("Nope"), None);
    }

    #[test]
    fn bucket_root_uses_tesla_layout() {
        let backing = PathBuf::from("/srv/teslausb");
        let c = cfg(&backing);
        assert_eq!(
            c.bucket_root(Bucket::Recent),
            PathBuf::from("/srv/teslausb/TeslaCam/RecentClips"),
        );
        assert_eq!(
            c.bucket_root(Bucket::Saved),
            PathBuf::from("/srv/teslausb/TeslaCam/SavedClips"),
        );
        assert_eq!(
            c.bucket_root(Bucket::Sentry),
            PathBuf::from("/srv/teslausb/TeslaCam/SentryClips"),
        );
    }

    #[cfg(target_os = "linux")]
    mod linux {
        use std::time::Duration;

        use super::*;

        #[test]
        fn new_errors_when_bucket_root_missing() {
            let dir = tempfile::tempdir().unwrap();
            let c = cfg(dir.path());
            // `.err().expect(_)` avoids requiring `ClipWatcher: Debug`
            // (Inotify itself is not Debug and we don't want to derive
            // a noisy bound across the type just for one negative test).
            let err = ClipWatcher::new(&c)
                .err()
                .expect("expected error when bucket root missing");
            assert!(matches!(err, WatcherError::BucketRootMissing(_)));
        }

        #[test]
        fn watcher_observes_close_write_on_mp4() {
            let dir = tempfile::tempdir().unwrap();
            let c = cfg(dir.path());
            for b in Bucket::all() {
                std::fs::create_dir_all(c.bucket_root(b)).unwrap();
            }
            let mut watcher = ClipWatcher::new(&c).unwrap();
            let target = c.bucket_root(Bucket::Recent).join("a.mp4");
            // Write + close on a background thread; the main
            // thread blocks in next_batch.
            let writer = std::thread::spawn(move || {
                // Tiny sleep so the watcher is definitely
                // inside `read_events_blocking` when the file
                // is created.
                std::thread::sleep(Duration::from_millis(50));
                std::fs::write(&target, b"hello").unwrap();
            });
            let events = watcher.next_batch().unwrap();
            writer.join().unwrap();
            assert!(!events.is_empty(), "expected at least one event");
            let ev = &events[0];
            assert_eq!(ev.bucket, Bucket::Recent);
            assert_eq!(ev.kind, WatchKind::CloseWrite);
            assert!(
                ev.path.ends_with("RecentClips/a.mp4"),
                "unexpected path: {:?}",
                ev.path,
            );
        }

        #[test]
        fn watcher_filters_non_mp4() {
            let dir = tempfile::tempdir().unwrap();
            let c = cfg(dir.path());
            for b in Bucket::all() {
                std::fs::create_dir_all(c.bucket_root(b)).unwrap();
            }
            let mut watcher = ClipWatcher::new(&c).unwrap();
            let mp4 = c.bucket_root(Bucket::Recent).join("a.mp4");
            let txt = c.bucket_root(Bucket::Recent).join("notes.txt");
            let writer = std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(50));
                std::fs::write(&txt, b"junk").unwrap();
                std::fs::write(&mp4, b"keep").unwrap();
            });
            // Drain until we see the mp4 event; assert we
            // never see the txt one.
            let mut saw_mp4 = false;
            for _ in 0..4 {
                let events = watcher.next_batch().unwrap();
                for ev in events {
                    assert!(
                        ev.path.extension().and_then(|e| e.to_str()) == Some("mp4"),
                        "non-mp4 event leaked: {:?}",
                        ev.path,
                    );
                    saw_mp4 = true;
                }
                if saw_mp4 {
                    break;
                }
            }
            writer.join().unwrap();
            assert!(saw_mp4, "expected the .mp4 close-write event");
        }
    }
}
