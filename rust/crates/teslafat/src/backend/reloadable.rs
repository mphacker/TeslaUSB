//! [`ReloadableBackend`] — a thin [`BlockBackend`] wrapper that lets
//! the daemon re-walk its `backing_root` and atomically swap in a
//! freshly-built [`SynthBackend`] **without restarting the process**.
//!
//! ## Why this exists
//!
//! [`SynthBackend::open`] walks `backing_root` exactly once and then
//! serves an immutable FAT32/`exFAT` layout (directory entries, FAT /
//! allocation bitmap, cluster extents). A backing file that is
//! *added*, *removed*, or *resized* after open is invisible to the
//! synthesised view until the daemon re-walks. Before this wrapper the
//! only re-walk was a full process restart, which on the recording LUN
//! means detaching `TeslaCAM` — unacceptable during active recording.
//!
//! The lock-chime activation flow needs exactly this: after it
//! replaces `LockChime.wav` on the **media** LUN (a differently-sized
//! file), the synth view must re-render so the Tesla reads the new
//! bytes and the correct directory-entry length. A `SIGHUP` to the
//! media `teslafat` instance triggers [`ReloadableBackend::build_fresh`]
//! + [`ReloadableBackend::swap`] from the daemon's signal task.
//!
//! ## Concurrency contract
//!
//! The live backend is held behind a [`std::sync::RwLock`] of an
//! [`Arc`]. Every NBD request loads (clones) the current `Arc` and
//! drops the lock *before* it awaits any I/O — the lock is never held
//! across an `.await`, so a swap can never block an in-flight read or
//! write. A request that is already running keeps its cloned `Arc`
//! alive and completes against the *old* view; only requests that
//! start after the swap see the new view. This is the same
//! "load-then-use" discipline an `ArcSwap` would give, implemented
//! with std-only primitives (no new dependency — see ADR-0006 §deps).
//!
//! [`size`](BlockBackend::size) is derived solely from
//! `cfg.volume_size_gb` and is therefore **constant across reloads**,
//! satisfying the [`BlockBackend`] contract that the export size never
//! changes after the handshake.
//!
//! ## Write safety
//!
//! A swap abandons the old [`SynthBackend`]'s in-memory write state
//! (any not-yet-flushed `.partial` writes). That is safe **only when
//! the LUN is quiescent** at swap time. [`ReloadableBackend::try_go_live`]
//! enforces this in code: it consults [`SynthBackend::is_quiescent`] under
//! the swap lock and refuses to swap a busy LUN. The operator flow
//! additionally only ever drives a reload on the media LUN (read-mostly:
//! boombox / lock-chime), never the actively-recorded `TeslaCAM` LUN. The
//! freshly opened backend runs `recover_partials`, discarding stale
//! `.partial` files from the abandoned write, so the new view is
//! internally consistent.

use std::sync::{Arc, PoisonError, RwLock};

use teslausb_core::backend::{BackendResult, BlockBackend, WriteFlags};

use super::synth::{SynthBackend, SynthBackendError};
use crate::config::Config;

/// A [`BlockBackend`] that delegates to a swappable [`SynthBackend`].
///
/// Construct with [`ReloadableBackend::open`]; trigger a live re-walk
/// with [`ReloadableBackend::build_fresh`] (off the request path) then
/// [`ReloadableBackend::swap`].
pub struct ReloadableBackend {
    /// Retained so a reload can rebuild the synth view from the same
    /// volume geometry / filesystem type the daemon started with.
    cfg: Config,
    /// The currently-served backend. Swapped atomically on reload.
    current: RwLock<Arc<SynthBackend>>,
}

impl ReloadableBackend {
    /// Open the initial backend by walking `cfg.backing_root`.
    ///
    /// # Errors
    ///
    /// Propagates any [`SynthBackendError`] from
    /// [`SynthBackend::open`] (walk, layout planning, or recovery
    /// failure).
    pub fn open(cfg: &Config) -> Result<Self, SynthBackendError> {
        let backend = Arc::new(SynthBackend::open(cfg)?);
        Ok(Self {
            cfg: cfg.clone(),
            current: RwLock::new(backend),
        })
    }

    /// Clone the `Arc` to the currently-served backend.
    ///
    /// The [`RwLock`] read guard is released before this returns; the
    /// returned `Arc` keeps the backend alive for the caller's use
    /// even if a concurrent [`Self::swap`] replaces the live view in
    /// the meantime.
    #[must_use]
    pub fn current(&self) -> Arc<SynthBackend> {
        Arc::clone(&self.current.read().unwrap_or_else(PoisonError::into_inner))
    }

    /// Build a fresh [`SynthBackend`] by re-walking `backing_root`,
    /// **without** touching the live view.
    ///
    /// This is the expensive half of a reload (directory walk + FAT /
    /// `exFAT` layout planning) and is deliberately separated from
    /// [`Self::swap`] so the caller can run it off the NBD request
    /// path (e.g. on a blocking thread) and warm its read caches
    /// before making it live. If this fails, the live view is
    /// untouched.
    ///
    /// # Errors
    ///
    /// Propagates any [`SynthBackendError`] from
    /// [`SynthBackend::open`].
    pub fn build_fresh(&self) -> Result<SynthBackend, SynthBackendError> {
        SynthBackend::open(&self.cfg)
    }

    /// Atomically replace the live backend with `fresh`.
    ///
    /// After this returns, every *new* NBD request is served by
    /// `fresh`; requests already in flight finish against the previous
    /// backend (which is dropped once its last cloned `Arc` is
    /// released). The swap itself only takes the write lock for the
    /// duration of a pointer store, so it never blocks an awaiting
    /// request.
    pub fn swap(&self, fresh: Arc<SynthBackend>) {
        let mut guard = self.current.write().unwrap_or_else(PoisonError::into_inner);
        *guard = fresh;
    }

    /// Swap in `fresh` **only if the live LUN is currently
    /// quiescent** (no in-flight host write). Returns `true` if the
    /// swap happened, `false` if the LUN was busy and the caller
    /// should retry on a later tick (re-using or rebuilding
    /// `fresh`).
    ///
    /// This is the production-preferred go-live path: a full layout
    /// swap abandons the old backend's in-memory write state, which
    /// is only safe while no host write is addressing the *current*
    /// layout. The quiescence check and the pointer store happen
    /// under the same write lock so no `current()` caller can begin
    /// a request between the check and the swap.
    ///
    /// Residual race (accepted for the read-mostly media LUN): a
    /// host write that cloned the `Arc` *before* this acquired the
    /// write lock may still be awaiting the inner write-state mutex;
    /// it completes against the old view and finalizes to the
    /// backing tree (persisted, not corrupted) but is not reflected
    /// in `fresh` until the next reload. The operator flow only ever
    /// drives this on the media LUN, never the actively-recorded
    /// `TeslaCAM` LUN, and follows the swap with a UDC rebind so the
    /// host re-reads the new layout.
    pub fn try_go_live(&self, fresh: Arc<SynthBackend>) -> bool {
        let mut guard = self.current.write().unwrap_or_else(PoisonError::into_inner);
        if !guard.is_quiescent() {
            return false;
        }
        *guard = fresh;
        true
    }
}

impl BlockBackend for ReloadableBackend {
    fn size(&self) -> u64 {
        self.current().size()
    }

    async fn read(&self, offset: u64, buf: &mut [u8]) -> BackendResult<()> {
        let backend = self.current();
        backend.read(offset, buf).await
    }

    async fn write(&self, offset: u64, buf: &[u8], flags: WriteFlags) -> BackendResult<()> {
        let backend = self.current();
        backend.write(offset, buf, flags).await
    }

    async fn flush(&self) -> BackendResult<()> {
        let backend = self.current();
        backend.flush().await
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::indexing_slicing)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::{Path, PathBuf};

    use crate::config::{Config, FsType, RetentionConfig};

    fn write_file(path: &Path, bytes: &[u8]) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, bytes).unwrap();
    }

    fn sample_cfg(backing_root: PathBuf, fs_type: FsType) -> Config {
        Config {
            backing_root,
            volume_size_gb: 4,
            volume_label: "TESTLABEL".to_string(),
            cluster_size: None,
            fs_type,
            retention: RetentionConfig::default(),
            spill_dir: None,
            reload_on_sighup: true,
        }
    }

    /// `size()` is config-derived and must therefore be identical
    /// before and after a reload, even when the file set changes —
    /// the NBD export size is fixed at handshake time.
    #[tokio::test]
    async fn size_is_constant_across_reload() {
        let dir = tempfile::tempdir().unwrap();
        write_file(&dir.path().join("alpha.bin"), &[0x11; 4096]);
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
        let backend = ReloadableBackend::open(&cfg).unwrap();
        let before = backend.size();

        write_file(&dir.path().join("beta.bin"), &[0x22; 8192]);
        let fresh = Arc::new(backend.build_fresh().unwrap());
        backend.swap(fresh);

        assert_eq!(backend.size(), before, "export size changed across reload");
    }

    /// A file added to the backing tree *after* open is invisible
    /// until a reload, then becomes visible — the core capability the
    /// SIGHUP re-walk exists to provide.
    #[tokio::test]
    async fn reload_makes_a_newly_added_file_visible() {
        let dir = tempfile::tempdir().unwrap();
        write_file(&dir.path().join("first.bin"), &[0x33; 4096]);
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
        let backend = ReloadableBackend::open(&cfg).unwrap();
        let count_before = backend.current().file_count();

        write_file(&dir.path().join("second.bin"), &[0x44; 4096]);
        // Not visible yet: the live view is still the original walk.
        assert_eq!(
            backend.current().file_count(),
            count_before,
            "new file should be invisible before reload"
        );

        let fresh = Arc::new(backend.build_fresh().unwrap());
        backend.swap(fresh);

        assert!(
            backend.current().file_count() > count_before,
            "new file should be visible after reload ({} !> {})",
            backend.current().file_count(),
            count_before
        );
    }

    /// After a reload that replaces a backing file with new contents,
    /// a read of that file's first cluster returns the *new* bytes.
    #[tokio::test]
    async fn reload_serves_new_backing_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let old_payload: Vec<u8> = (0..4096u32).map(|i| (i % 251) as u8).collect();
        write_file(&dir.path().join("chime.wav"), &old_payload);
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
        let backend = ReloadableBackend::open(&cfg).unwrap();

        let first_byte = backend
            .current()
            .first_file_byte()
            .expect("at least one extent");

        let new_payload: Vec<u8> = (0..4096u32).map(|i| ((i + 7) % 251) as u8).collect();
        assert_ne!(old_payload, new_payload);
        write_file(&dir.path().join("chime.wav"), &new_payload);

        let fresh = Arc::new(backend.build_fresh().unwrap());
        backend.swap(fresh);

        let mut buf = vec![0u8; new_payload.len()];
        backend.read(first_byte, &mut buf).await.expect("read ok");
        assert_eq!(buf, new_payload, "reload did not surface new backing bytes");
    }

    /// A failed `build_fresh` must leave the live view untouched: the
    /// caller swaps only on success, so a transient walk error can
    /// never blank out a working LUN.
    #[tokio::test]
    async fn failed_build_leaves_live_view_intact() {
        let dir = tempfile::tempdir().unwrap();
        write_file(&dir.path().join("keep.bin"), &[0x55; 4096]);
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
        let backend = ReloadableBackend::open(&cfg).unwrap();
        let count_before = backend.current().file_count();

        // Point a *second* reloadable at a non-existent root to force
        // build_fresh to fail, proving the error path is a clean
        // Err (the caller's "swap only on Ok" contract then trivially
        // preserves the live view).
        let bad_cfg = sample_cfg(dir.path().join("does-not-exist"), FsType::Fat32);
        let bad = ReloadableBackend::open(&bad_cfg);
        // open() over a missing root surfaces an error rather than a
        // silent empty view.
        assert!(bad.is_err(), "expected open over missing root to fail");

        // The good backend is still fully intact.
        assert_eq!(backend.current().file_count(), count_before);
    }

    /// `try_go_live` swaps when the live LUN is quiescent (the
    /// common case: a read-mostly media LUN). The newly-added file
    /// becomes visible.
    #[tokio::test]
    async fn try_go_live_swaps_when_quiescent() {
        let dir = tempfile::tempdir().unwrap();
        write_file(&dir.path().join("first.bin"), &[0x33; 4096]);
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
        let backend = ReloadableBackend::open(&cfg).unwrap();
        let count_before = backend.current().file_count();

        write_file(&dir.path().join("second.bin"), &[0x44; 4096]);
        let fresh = Arc::new(backend.build_fresh().unwrap());

        assert!(
            backend.try_go_live(fresh),
            "quiescent LUN should accept the swap"
        );
        assert!(
            backend.current().file_count() > count_before,
            "swap should have surfaced the new file"
        );
    }

    /// `try_go_live` refuses to swap while a host write is
    /// mid-flight — abandoning live write state under an active
    /// write could corrupt the in-flight file. The live view is
    /// left untouched so the caller can retry on a later tick.
    #[tokio::test]
    async fn try_go_live_refuses_when_busy() {
        let dir = tempfile::tempdir().unwrap();
        write_file(&dir.path().join("first.bin"), &[0x33; 4096]);
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
        let backend = ReloadableBackend::open(&cfg).unwrap();
        let count_before = backend.current().file_count();

        write_file(&dir.path().join("second.bin"), &[0x44; 4096]);
        let fresh = Arc::new(backend.build_fresh().unwrap());

        // Simulate an in-flight Tesla write on the live backend.
        backend.current().mark_inflight_for_test();

        assert!(!backend.try_go_live(fresh), "busy LUN must reject the swap");
        assert_eq!(
            backend.current().file_count(),
            count_before,
            "rejected swap must leave the live view untouched"
        );
    }

    /// The gate is filesystem-agnostic: an exFAT media LUN reloads
    /// through the exact same quiescence-gated path as FAT32.
    #[tokio::test]
    async fn try_go_live_swaps_when_quiescent_exfat() {
        let dir = tempfile::tempdir().unwrap();
        write_file(&dir.path().join("first.bin"), &[0x33; 4096]);
        let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Exfat);
        let backend = ReloadableBackend::open(&cfg).unwrap();
        let count_before = backend.current().file_count();

        write_file(&dir.path().join("second.bin"), &[0x44; 4096]);
        let fresh = Arc::new(backend.build_fresh().unwrap());

        assert!(
            backend.try_go_live(fresh),
            "quiescent exFAT LUN should accept the swap"
        );
        assert!(
            backend.current().file_count() > count_before,
            "swap should have surfaced the new file on exFAT"
        );
    }
}
