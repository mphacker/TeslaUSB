//! Indexer service — glues the clip watcher (4b.2b) to the
//! SEI walker (4b.1z) and the store (4b.2a).
//!
//! The indexer's job, in one sentence: every time a new clip
//! lands in `RecentClips`/`SavedClips`/`SentryClips`, walk it
//! with the SEI parser and persist its waypoints to the
//! SQLite store.
//!
//! Decoupled from `tokio` and from `inotify` so the unit
//! tests can drive `Indexer::handle_event` synchronously
//! against an in-memory store and a temp directory. The
//! supervisor (Phase 4b.4) provides the real runtime + the
//! real `ClipWatcher::next_batch` loop.

// File-level: "inotify", "SQLite", "SEI", "TeslaCam" are
// domain terms.
#![allow(clippy::doc_markdown)]

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use thiserror::Error;
use tracing::{debug, info, warn};

use crate::config::Config;
use crate::sei::{WalkError, walk_clip};
use crate::store::{Bucket, Store, StoreError};
use crate::watcher::{WatchEvent, WatchKind};

/// Errors emitted by the indexer service. Per-clip parse
/// failures are NOT errors — they are logged and skipped so
/// one bad clip cannot stall the daemon.
#[derive(Debug, Error)]
pub enum IndexerError {
    /// Underlying store error during a bootstrap walk.
    #[error("store error: {0}")]
    Store(#[from] StoreError),
    /// I/O error during a bootstrap directory walk.
    #[error("i/o error walking {path:?}: {source}")]
    Io {
        /// Directory we were walking.
        path: PathBuf,
        /// Underlying I/O error.
        #[source]
        source: std::io::Error,
    },
}

/// Result alias for indexer operations.
pub type Result<T> = std::result::Result<T, IndexerError>;

/// Summary returned by [`Indexer::bootstrap`]. Used in
/// startup logs and tests.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct BootstrapSummary {
    /// Clips seen across all bucket directories.
    pub seen: u32,
    /// Clips newly indexed by the bootstrap pass.
    pub indexed: u32,
    /// Clips skipped because the store already knew them.
    pub skipped: u32,
    /// Clips the SEI walker could not parse.
    pub failed: u32,
}

/// Glue service that owns the [`Store`] and applies SEI
/// walks to incoming events.
pub struct Indexer {
    config: Config,
    store: Store,
    /// `path -> last-handled timestamp` for debounce.
    /// Bounded purge below `MAX_DEBOUNCE_ENTRIES` so a long
    /// uptime cannot leak memory if Tesla emits unique paths
    /// forever.
    last_handled: HashMap<PathBuf, Instant>,
}

/// Cap on the in-memory debounce dictionary. If we hit it we
/// drop the oldest half — debounce is only ever a near-term
/// guard so older entries are harmless to forget.
const MAX_DEBOUNCE_ENTRIES: usize = 8_192;

impl Indexer {
    /// Build an indexer around the configured store.
    #[must_use]
    pub fn new(config: Config, store: Store) -> Self {
        Self {
            config,
            store,
            last_handled: HashMap::new(),
        }
    }

    /// Consume the indexer and return its inner store. Used
    /// by the supervisor to hand the store to the cleanup
    /// worker (cleanup needs `&Store`, indexer needs
    /// `&mut Store`, so they share via the supervisor).
    #[must_use]
    pub fn into_store(self) -> Store {
        self.store
    }

    /// One-shot startup pass: walk each bucket directory and
    /// index any `*.mp4` the store does not yet know about.
    /// Recovers from "the daemon was down while Tesla wrote
    /// clips".
    ///
    /// # Errors
    ///
    /// Returns `Err` on a store error or a fatal I/O error
    /// walking a bucket. Per-clip parse failures count toward
    /// `failed` in the summary but do NOT abort the pass.
    pub fn bootstrap(&mut self) -> Result<BootstrapSummary> {
        let mut summary = BootstrapSummary::default();
        for bucket in Bucket::all() {
            let root = self.config.bucket_root(bucket);
            if !root.is_dir() {
                debug!(
                    bucket = bucket.as_db_str(),
                    path = %root.display(),
                    "bucket directory does not exist; skipping",
                );
                continue;
            }
            let entries = std::fs::read_dir(&root).map_err(|e| IndexerError::Io {
                path: root.clone(),
                source: e,
            })?;
            for entry in entries {
                let entry = entry.map_err(|e| IndexerError::Io {
                    path: root.clone(),
                    source: e,
                })?;
                let path = entry.path();
                if !crate::watcher::is_indexable(&path) {
                    continue;
                }
                summary.seen += 1;
                let relative = relative_to_backing_root(&path, &self.config.backing_root);
                if self.store.knows_clip(&relative)? {
                    summary.skipped += 1;
                    continue;
                }
                match self.walk_and_record(bucket, &path) {
                    Ok(()) => summary.indexed += 1,
                    Err(walk_err) => {
                        summary.failed += 1;
                        warn!(
                            path = %path.display(),
                            error = %walk_err,
                            "bootstrap: SEI walk failed; skipping clip",
                        );
                    }
                }
            }
        }
        info!(
            seen = summary.seen,
            indexed = summary.indexed,
            skipped = summary.skipped,
            failed = summary.failed,
            "indexer bootstrap complete",
        );
        Ok(summary)
    }

    /// Handle one [`WatchEvent`] from the watcher. Returns
    /// `Ok(true)` if the event triggered a successful index,
    /// `Ok(false)` if it was debounced or filtered, `Err`
    /// only on a store error. SEI parse failures log at WARN
    /// and return `Ok(false)`.
    ///
    /// # Errors
    ///
    /// Returns `Err` on an underlying store error. Parse
    /// failures are not propagated.
    pub fn handle_event(&mut self, event: &WatchEvent) -> Result<bool> {
        if !crate::watcher::is_indexable(&event.path) {
            return Ok(false);
        }
        let now = Instant::now();
        if self.is_debounced(&event.path, now) {
            debug!(
                path = %event.path.display(),
                "event debounced",
            );
            return Ok(false);
        }
        self.remember_handled(event.path.clone(), now);
        match self.walk_and_record(event.bucket, &event.path) {
            Ok(()) => {
                debug!(
                    bucket = event.bucket.as_db_str(),
                    path = %event.path.display(),
                    kind = ?event.kind,
                    "indexed clip",
                );
                Ok(true)
            }
            Err(e) => {
                warn!(
                    path = %event.path.display(),
                    error = %e,
                    "SEI walk failed; clip not indexed",
                );
                Ok(false)
            }
        }
    }

    /// Borrow the inner store (for the cleanup worker / tests).
    #[must_use]
    pub fn store(&self) -> &Store {
        &self.store
    }

    /// `WatchKind`s are recorded only for log clarity; this
    /// helper lets the supervisor surface them in startup
    /// logs without leaking the enum.
    #[must_use]
    pub const fn kind_label(kind: WatchKind) -> &'static str {
        match kind {
            WatchKind::CloseWrite => "close_write",
            WatchKind::Moved => "moved",
        }
    }

    fn is_debounced(&self, path: &Path, now: Instant) -> bool {
        let Some(last) = self.last_handled.get(path) else {
            return false;
        };
        now.saturating_duration_since(*last) < self.debounce_window()
    }

    fn remember_handled(&mut self, path: PathBuf, when: Instant) {
        if self.last_handled.len() >= MAX_DEBOUNCE_ENTRIES {
            self.purge_oldest_half();
        }
        self.last_handled.insert(path, when);
    }

    fn purge_oldest_half(&mut self) {
        // Collect, sort by timestamp ascending, drop the older
        // half. O(n log n) on a 8K-entry map = milliseconds.
        let mut by_time: Vec<(PathBuf, Instant)> = self
            .last_handled
            .iter()
            .map(|(k, v)| (k.clone(), *v))
            .collect();
        by_time.sort_by_key(|(_, t)| *t);
        let drop_count = by_time.len() / 2;
        for (path, _) in by_time.into_iter().take(drop_count) {
            self.last_handled.remove(&path);
        }
    }

    fn debounce_window(&self) -> Duration {
        self.config.indexer.debounce()
    }

    fn walk_and_record(
        &mut self,
        bucket: Bucket,
        path: &Path,
    ) -> std::result::Result<(), WalkAndRecordError> {
        let walk = walk_clip(path, self.config.indexer.sei_sample_rate)
            .map_err(WalkAndRecordError::Walk)?;
        let relative = relative_to_backing_root(path, &self.config.backing_root);
        self.store
            .record_clip(bucket, &relative, &walk)
            .map_err(WalkAndRecordError::Store)?;
        Ok(())
    }
}

#[derive(Debug, Error)]
enum WalkAndRecordError {
    #[error("{0}")]
    Walk(WalkError),
    #[error("{0}")]
    Store(StoreError),
}

/// Convert an absolute clip path into the relative path the
/// store uses as its unique key. The store key is stable
/// across `backing_root` moves (e.g. a backup restored to a
/// different mount point).
fn relative_to_backing_root(path: &Path, backing_root: &Path) -> PathBuf {
    path.strip_prefix(backing_root)
        .map_or_else(|_| path.to_path_buf(), Path::to_path_buf)
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
    use std::time::Duration;

    use super::*;
    use crate::config::Config;
    use crate::watcher::{WatchEvent, WatchKind};

    fn make_config(backing: &Path) -> Config {
        let toml = format!(
            "backing_root = \"{}\"\n\n[indexer]\ndebounce_ms = 50\n",
            backing.to_string_lossy().replace('\\', "/"),
        );
        toml::from_str(&toml).unwrap()
    }

    fn write_tiny_unparseable_clip(path: &Path) {
        // Smaller than `MIN_CLIP_BYTES` so the walker rejects
        // it cleanly — we want a controlled failure, not a
        // SEGV.
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(path, b"").unwrap();
    }

    fn write_garbage_clip(path: &Path) {
        // Past MIN_CLIP_BYTES but not a real MP4 — walker
        // returns a structured Mp4 error.
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(path, vec![0_u8; 64]).unwrap();
    }

    #[test]
    fn bootstrap_skips_when_buckets_missing() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = make_config(dir.path());
        let store = Store::open_in_memory().unwrap();
        let mut indexer = Indexer::new(cfg, store);
        let s = indexer.bootstrap().unwrap();
        assert_eq!(s, BootstrapSummary::default());
    }

    #[test]
    fn bootstrap_counts_seen_and_failed() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = make_config(dir.path());
        let recent = cfg.bucket_root(Bucket::Recent);
        std::fs::create_dir_all(&recent).unwrap();
        write_garbage_clip(&recent.join("a.mp4"));
        write_garbage_clip(&recent.join("b.mp4"));

        let store = Store::open_in_memory().unwrap();
        let mut indexer = Indexer::new(cfg, store);
        let s = indexer.bootstrap().unwrap();
        assert_eq!(s.seen, 2);
        assert_eq!(s.indexed, 0);
        assert_eq!(s.failed, 2);
        assert_eq!(s.skipped, 0);
    }

    #[test]
    fn bootstrap_skips_already_indexed_clips() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = make_config(dir.path());
        let recent = cfg.bucket_root(Bucket::Recent);
        std::fs::create_dir_all(&recent).unwrap();
        let p = recent.join("a.mp4");
        write_garbage_clip(&p);

        let mut store = Store::open_in_memory().unwrap();
        // Pre-load: indexer should treat this as already known.
        let relative = relative_to_backing_root(&p, &cfg.backing_root);
        store
            .record_clip(
                Bucket::Recent,
                &relative,
                &crate::sei::ClipWalk {
                    clip_started_utc: None,
                    timescale: 90_000,
                    frame_count: 0,
                    waypoints: vec![],
                },
            )
            .unwrap();
        let mut indexer = Indexer::new(cfg, store);
        let s = indexer.bootstrap().unwrap();
        assert_eq!(s.seen, 1);
        assert_eq!(s.skipped, 1);
        assert_eq!(s.indexed, 0);
        assert_eq!(s.failed, 0);
    }

    #[test]
    fn bootstrap_ignores_non_mp4_files() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = make_config(dir.path());
        let recent = cfg.bucket_root(Bucket::Recent);
        std::fs::create_dir_all(&recent).unwrap();
        std::fs::write(recent.join("notes.txt"), b"junk").unwrap();
        std::fs::write(recent.join(".swap.mp4"), b"junk").unwrap();
        let store = Store::open_in_memory().unwrap();
        let mut indexer = Indexer::new(cfg, store);
        let s = indexer.bootstrap().unwrap();
        assert_eq!(s.seen, 0);
    }

    #[test]
    fn handle_event_returns_false_for_unparseable_clip() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = make_config(dir.path());
        let recent = cfg.bucket_root(Bucket::Recent);
        std::fs::create_dir_all(&recent).unwrap();
        let path = recent.join("bad.mp4");
        write_tiny_unparseable_clip(&path);

        let store = Store::open_in_memory().unwrap();
        let mut indexer = Indexer::new(cfg, store);
        let ev = WatchEvent {
            bucket: Bucket::Recent,
            path,
            kind: WatchKind::CloseWrite,
        };
        assert!(!indexer.handle_event(&ev).unwrap());
        assert_eq!(indexer.store().clip_count().unwrap(), 0);
    }

    #[test]
    fn handle_event_debounces_repeated_events() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = make_config(dir.path());
        let recent = cfg.bucket_root(Bucket::Recent);
        std::fs::create_dir_all(&recent).unwrap();
        let path = recent.join("a.mp4");
        write_tiny_unparseable_clip(&path);

        let store = Store::open_in_memory().unwrap();
        let mut indexer = Indexer::new(cfg, store);
        let ev = WatchEvent {
            bucket: Bucket::Recent,
            path: path.clone(),
            kind: WatchKind::CloseWrite,
        };
        // First event: handled (returns false because parse
        // fails on a 0-byte file, but it was NOT debounced).
        assert!(!indexer.handle_event(&ev).unwrap());
        // Second event arriving immediately: must be debounced.
        // Tap a private accessor through the public surface
        // by checking the `last_handled` count.
        assert_eq!(indexer.last_handled.len(), 1);
        assert!(!indexer.handle_event(&ev).unwrap());
        // Still one entry; we did not re-stamp because the
        // second call short-circuited.
        assert_eq!(indexer.last_handled.len(), 1);
    }

    #[test]
    fn handle_event_re_handles_after_debounce_window() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = make_config(dir.path());
        let recent = cfg.bucket_root(Bucket::Recent);
        std::fs::create_dir_all(&recent).unwrap();
        let path = recent.join("a.mp4");
        write_tiny_unparseable_clip(&path);

        let store = Store::open_in_memory().unwrap();
        let mut indexer = Indexer::new(cfg, store);
        let ev = WatchEvent {
            bucket: Bucket::Recent,
            path,
            kind: WatchKind::CloseWrite,
        };
        indexer.handle_event(&ev).unwrap();
        std::thread::sleep(Duration::from_millis(80));
        // After the 50 ms config window, the handler must
        // not short-circuit.
        indexer.handle_event(&ev).unwrap();
        // Last-handled timestamp updated; still one entry but
        // a newer Instant. We assert via the timestamp delta.
        let now = Instant::now();
        let last = indexer.last_handled.values().copied().max().unwrap();
        assert!(now.duration_since(last) < Duration::from_millis(50));
    }

    #[test]
    fn handle_event_filters_non_mp4_paths() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = make_config(dir.path());
        let store = Store::open_in_memory().unwrap();
        let mut indexer = Indexer::new(cfg, store);
        let ev = WatchEvent {
            bucket: Bucket::Recent,
            path: PathBuf::from("/srv/RecentClips/a.txt"),
            kind: WatchKind::CloseWrite,
        };
        assert!(!indexer.handle_event(&ev).unwrap());
        assert_eq!(indexer.last_handled.len(), 0);
    }

    #[test]
    fn purge_oldest_half_caps_debounce_dict() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = make_config(dir.path());
        let store = Store::open_in_memory().unwrap();
        let mut indexer = Indexer::new(cfg, store);
        // Stuff in MAX_DEBOUNCE_ENTRIES + 1 entries with
        // monotonically advancing timestamps so the sort
        // order is deterministic.
        let base = Instant::now();
        for i in 0..MAX_DEBOUNCE_ENTRIES {
            indexer.last_handled.insert(
                PathBuf::from(format!("/p/{i}.mp4")),
                base + Duration::from_nanos(i as u64),
            );
        }
        assert_eq!(indexer.last_handled.len(), MAX_DEBOUNCE_ENTRIES);
        // Trigger one more remember to force the purge path.
        indexer.remember_handled(PathBuf::from("/p/new.mp4"), base + Duration::from_secs(1));
        assert!(indexer.last_handled.len() <= MAX_DEBOUNCE_ENTRIES);
        // The newest entry survived.
        assert!(indexer.last_handled.contains_key(Path::new("/p/new.mp4")));
    }

    #[test]
    fn into_store_returns_inner_store() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = make_config(dir.path());
        let store = Store::open_in_memory().unwrap();
        let indexer = Indexer::new(cfg, store);
        let store = indexer.into_store();
        assert_eq!(store.clip_count().unwrap(), 0);
    }

    #[test]
    fn kind_label_distinguishes_close_write_and_moved() {
        assert_eq!(Indexer::kind_label(WatchKind::CloseWrite), "close_write");
        assert_eq!(Indexer::kind_label(WatchKind::Moved), "moved");
    }

    #[test]
    fn relative_to_backing_root_strips_prefix() {
        let backing = Path::new("/srv/teslausb");
        let abs = Path::new("/srv/teslausb/TeslaCam/RecentClips/a.mp4");
        assert_eq!(
            relative_to_backing_root(abs, backing),
            PathBuf::from("TeslaCam/RecentClips/a.mp4"),
        );
    }

    #[test]
    fn relative_to_backing_root_returns_path_unchanged_if_not_prefix() {
        let backing = Path::new("/srv/teslausb");
        let abs = Path::new("/elsewhere/a.mp4");
        assert_eq!(
            relative_to_backing_root(abs, backing),
            PathBuf::from("/elsewhere/a.mp4"),
        );
    }
}
