//! POSIX directory-tree writer adapter (Phase 3.3).
//!
//! The write-side counterpart to [`super::synth::SynthBackend`].
//! Phase 3.5 will wire it into `SynthBackend::write` so that the
//! pipeline becomes:
//!
//! 1. The NBD transmission loop calls
//!    [`BlockBackend::write`](teslausb_core::backend::BlockBackend::write)
//!    with `(offset, &[u8])`.
//! 2. [`teslausb_core::fs::fat32::parse::decode_write`] or
//!    [`teslausb_core::fs::exfat::parse::decode_write`] classifies
//!    each byte into a typed per-region chunk
//!    (`MainBootRegion`, `FatTable`, `DataCluster`, …).
//! 3. The `cluster_map` (Phase 3.4) translates a `DataCluster`
//!    chunk's `cluster_number` into a `(relative_path,
//!    byte_in_file)` pair when the cluster is known to belong to
//!    a tracked file.
//! 4. This module routes that pair plus the bytes onto the POSIX
//!    backing tree, materializing the write as a `<path>.partial`
//!    file that is atomically renamed to the final filename only
//!    after the host signals the file is closed (Tesla's dir-entry
//!    finalization, or an explicit `finalize` call).
//!
//! ## Atomicity model: `.partial` suffix + atomic rename
//!
//! Every write to a not-yet-finalized file lands in
//! `<backing_root>/<relative_path>.partial`. The final filename
//! materializes only via [`DirTreeWriter::finalize`], which
//! issues an atomic `rename(2)` on POSIX. On power loss the
//! `.partial` file remains on disk as evidence that a write was
//! in flight; [`DirTreeWriter::scan_partials`] enumerates them at
//! startup so the operator (or a Phase 3.6 reaper) can decide
//! whether to keep, discard, or finalize each.
//!
//! ## Concurrency
//!
//! The writer is intentionally cheap to clone (it holds only the
//! backing root path) and synchronizes nothing internally — the
//! caller (Phase 3.5 `SynthBackend::write`) is expected to
//! serialize writes per file via its own per-file lock or by
//! virtue of being single-threaded on the current-thread tokio
//! runtime. The `std::fs` calls used here are blocking; Phase 3.5
//! will call [`DirTreeWriter::apply_chunk`] from a
//! `tokio::task::spawn_blocking` if and when profiling shows the
//! current-thread runtime is bottlenecked on disk I/O.
//!
//! ## Path safety
//!
//! [`DirTreeWriter`] rejects absolute paths and any path
//! containing a `..` component. The resolved POSIX path is always
//! a descendant of `backing_root`. Symlinks under `backing_root`
//! are NOT followed for traversal-safety — `std::fs::create_dir_all`
//! treats symlinks-to-directories as the resolved target, which is
//! the same lenient behaviour `std::fs` uses for normal file ops,
//! and changing that requires a custom `O_NOFOLLOW` walker that
//! Phase 3.3 does not yet need.
//!
//! ## What this module does NOT do
//!
//! * It does not interpret on-disk filesystem metadata. Dir-entry
//!   parsing to discover filenames lives in Phase 3.5's wiring
//!   layer.
//! * It does not allocate or track clusters. That is the
//!   `cluster_map` (Phase 3.4).
//! * It does not call `fdatasync`. The FUA contract for
//!   [`teslausb_core::backend::WriteFlags::FUA`] is honoured by
//!   the caller (Phase 3.5) via a separate `flush` after the
//!   chunk routing completes.

use std::fs::OpenOptions;
use std::io::{self, Seek, SeekFrom, Write};
use std::path::{Component, Path, PathBuf};

/// Suffix appended to a target filename while the file is still
/// in flight. Chosen for visibility (an operator listing the
/// backing tree mid-write immediately sees what's incomplete) and
/// for shell-tab-completion friendliness (the suffix is a single
/// component appended to the name, not a hidden-file prefix).
pub const PARTIAL_SUFFIX: &str = ".partial";

/// Errors returned by [`DirTreeWriter`].
#[derive(Debug, thiserror::Error)]
pub enum DirTreeError {
    /// The backing root passed to [`DirTreeWriter::new`] does not
    /// exist, or is not a directory.
    #[error("backing root {path:?} is missing or not a directory")]
    BackingRootInvalid {
        /// The offending path.
        path: PathBuf,
    },
    /// A caller-supplied `relative_path` is absolute, contains a
    /// `..` component, or otherwise escapes the backing root.
    #[error("relative path {path:?} is invalid: must be relative and contain no `..` components")]
    InvalidRelativePath {
        /// The offending path.
        path: PathBuf,
    },
    /// [`DirTreeWriter::finalize`] was called for a file whose
    /// final filename already exists in the backing tree.
    #[error("collision: cannot finalize {path:?}, target already exists")]
    Collision {
        /// Path that already exists.
        path: PathBuf,
    },
    /// [`DirTreeWriter::finalize`] was called for a file that has
    /// no in-flight `.partial` file in the backing tree.
    #[error("no .partial file exists for {path:?}, cannot finalize")]
    NoPartialToFinalize {
        /// Final-name path the caller asked to finalize.
        path: PathBuf,
    },
    /// An `std::fs` call failed against a specific path.
    #[error("io error on {path:?}: {source}")]
    Io {
        /// The path that the failing operation targeted.
        path: PathBuf,
        /// The underlying `std::io::Error`.
        #[source]
        source: io::Error,
    },
}

impl DirTreeError {
    fn io(path: impl Into<PathBuf>, source: io::Error) -> Self {
        Self::Io {
            path: path.into(),
            source,
        }
    }
}

/// Routes decoded write chunks onto the POSIX backing tree using
/// `.partial`-suffix atomicity. See the module docs for the
/// pipeline this fits into.
///
/// Cheap to clone (one `PathBuf`); the caller is expected to
/// serialize writes per file.
#[derive(Debug, Clone)]
pub struct DirTreeWriter {
    backing_root: PathBuf,
}

impl DirTreeWriter {
    /// Construct a [`DirTreeWriter`] rooted at `backing_root`.
    ///
    /// `backing_root` must already exist and be a directory; this
    /// constructor does not create it. The expectation is that
    /// the operator's `backing_root` configuration value names a
    /// pre-provisioned directory (the same directory the
    /// read-side [`super::synth::SynthBackend`] walks).
    ///
    /// # Errors
    ///
    /// * [`DirTreeError::BackingRootInvalid`] if `backing_root`
    ///   does not exist or is not a directory.
    pub fn new(backing_root: PathBuf) -> Result<Self, DirTreeError> {
        if !backing_root.is_dir() {
            return Err(DirTreeError::BackingRootInvalid { path: backing_root });
        }
        Ok(Self { backing_root })
    }

    /// The backing root this writer is rooted at, as supplied at
    /// construction time. Exposed for diagnostics and for the
    /// Phase 3.6 power-cut harness.
    #[must_use]
    pub fn backing_root(&self) -> &Path {
        &self.backing_root
    }

    /// Resolve a caller-supplied `relative_path` against the
    /// backing root, rejecting absolute paths and any `..`
    /// component.
    fn resolve(&self, relative_path: &Path) -> Result<PathBuf, DirTreeError> {
        if relative_path.is_absolute() {
            return Err(DirTreeError::InvalidRelativePath {
                path: relative_path.to_path_buf(),
            });
        }
        for component in relative_path.components() {
            match component {
                Component::Normal(_) | Component::CurDir => {}
                Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                    return Err(DirTreeError::InvalidRelativePath {
                        path: relative_path.to_path_buf(),
                    });
                }
            }
        }
        Ok(self.backing_root.join(relative_path))
    }

    /// Resolve to the in-flight `.partial` path for
    /// `relative_path`.
    fn partial_path(&self, relative_path: &Path) -> Result<PathBuf, DirTreeError> {
        let resolved = self.resolve(relative_path)?;
        let mut as_os = resolved.into_os_string();
        as_os.push(PARTIAL_SUFFIX);
        Ok(PathBuf::from(as_os))
    }

    /// Write `bytes` at offset `byte_in_file` of the file at
    /// `relative_path`, materializing the write into the
    /// `.partial` companion file.
    ///
    /// Idempotent: writing the same `(byte_in_file, bytes)` twice
    /// is a no-op the second time. Sparse: writes past EOF extend
    /// the file with a hole if the platform supports sparse
    /// files. An empty `bytes` slice is a no-op.
    ///
    /// Creates parent directories under the backing root if they
    /// do not exist; the kernel's mkdir of a sub-directory in the
    /// FAT volume is normally what creates them, but a
    /// kernel-issued write to a file under a fresh sub-directory
    /// could in principle reach this function before the dir
    /// itself has been finalized, so we defensively `mkdir -p`.
    ///
    /// # Errors
    ///
    /// * [`DirTreeError::InvalidRelativePath`] if `relative_path`
    ///   is absolute or contains a `..` component.
    /// * [`DirTreeError::Io`] for any underlying `std::fs` error
    ///   (with the offending path attached).
    pub fn apply_chunk(
        &self,
        relative_path: &Path,
        byte_in_file: u64,
        bytes: &[u8],
    ) -> Result<(), DirTreeError> {
        if bytes.is_empty() {
            return Ok(());
        }
        let target = self.partial_path(relative_path)?;
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|source| DirTreeError::io(parent.to_path_buf(), source))?;
        }
        let mut file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(false)
            .open(&target)
            .map_err(|source| DirTreeError::io(target.clone(), source))?;
        file.seek(SeekFrom::Start(byte_in_file))
            .map_err(|source| DirTreeError::io(target.clone(), source))?;
        file.write_all(bytes)
            .map_err(|source| DirTreeError::io(target.clone(), source))?;
        Ok(())
    }

    /// Atomically rename `<relative_path>.partial` to
    /// `<relative_path>`. Fails if `.partial` is missing or if
    /// the final name already exists (collision).
    ///
    /// POSIX `rename(2)` is atomic with respect to crash recovery
    /// when source and destination are on the same filesystem,
    /// which they always are here (both are descendants of
    /// `backing_root`).
    ///
    /// # Errors
    ///
    /// * [`DirTreeError::InvalidRelativePath`] if `relative_path`
    ///   is absolute or contains a `..` component.
    /// * [`DirTreeError::NoPartialToFinalize`] if no `.partial`
    ///   file exists for this path.
    /// * [`DirTreeError::Collision`] if the final filename
    ///   already exists.
    /// * [`DirTreeError::Io`] for any underlying `std::fs` error.
    pub fn finalize(&self, relative_path: &Path) -> Result<(), DirTreeError> {
        let partial = self.partial_path(relative_path)?;
        let target = self.resolve(relative_path)?;
        if !partial.exists() {
            return Err(DirTreeError::NoPartialToFinalize { path: target });
        }
        if target.exists() {
            return Err(DirTreeError::Collision { path: target });
        }
        std::fs::rename(&partial, &target)
            .map_err(|source| DirTreeError::io(partial.clone(), source))?;
        Ok(())
    }

    /// Remove `<relative_path>.partial` if it exists.
    ///
    /// Used when the host signals that a write should be
    /// abandoned (e.g. a partial file overwritten before
    /// finalize). Missing-file is a no-op, not an error — the
    /// caller's contract is "ensure no `.partial` exists", which
    /// matches whether or not one was there to begin with.
    ///
    /// # Errors
    ///
    /// * [`DirTreeError::InvalidRelativePath`] if `relative_path`
    ///   is absolute or contains a `..` component.
    /// * [`DirTreeError::Io`] for any underlying `std::fs` error
    ///   other than `NotFound`.
    pub fn discard(&self, relative_path: &Path) -> Result<(), DirTreeError> {
        let partial = self.partial_path(relative_path)?;
        match std::fs::remove_file(&partial) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(source) => Err(DirTreeError::io(partial, source)),
        }
    }

    /// Remove the finalized file at `relative_path` if it exists.
    ///
    /// Used when the host signals that a previously-finalized
    /// file should be deleted (Tesla's directory-entry deletion).
    /// Missing-file is a no-op for the same reason as
    /// [`Self::discard`].
    ///
    /// # Errors
    ///
    /// * [`DirTreeError::InvalidRelativePath`] if `relative_path`
    ///   is absolute or contains a `..` component.
    /// * [`DirTreeError::Io`] for any underlying `std::fs` error
    ///   other than `NotFound`.
    pub fn unlink(&self, relative_path: &Path) -> Result<(), DirTreeError> {
        let target = self.resolve(relative_path)?;
        match std::fs::remove_file(&target) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(source) => Err(DirTreeError::io(target, source)),
        }
    }

    /// Return the relative paths of every `.partial` file under
    /// the backing root, recursively.
    ///
    /// On startup, the daemon enumerates these to determine which
    /// writes were in flight at the previous shutdown / power
    /// cut. The Phase 3.6 power-cut harness asserts that every
    /// `.partial` found here corresponds to an in-flight write.
    /// Returned paths are relative to `backing_root` and do NOT
    /// include the `.partial` suffix (so the caller can directly
    /// pass them to [`Self::finalize`] or [`Self::discard`]).
    ///
    /// # Errors
    ///
    /// * [`DirTreeError::Io`] for any underlying `std::fs` error.
    pub fn scan_partials(&self) -> Result<Vec<PathBuf>, DirTreeError> {
        let mut out = Vec::new();
        scan_partials_recursive(&self.backing_root, &self.backing_root, &mut out)?;
        out.sort();
        Ok(out)
    }
}

fn scan_partials_recursive(
    root: &Path,
    dir: &Path,
    out: &mut Vec<PathBuf>,
) -> Result<(), DirTreeError> {
    let entries =
        std::fs::read_dir(dir).map_err(|source| DirTreeError::io(dir.to_path_buf(), source))?;
    for entry in entries {
        let entry = entry.map_err(|source| DirTreeError::io(dir.to_path_buf(), source))?;
        let path = entry.path();
        let metadata = entry
            .metadata()
            .map_err(|source| DirTreeError::io(path.clone(), source))?;
        if metadata.is_dir() {
            scan_partials_recursive(root, &path, out)?;
            continue;
        }
        let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if !name.ends_with(PARTIAL_SUFFIX) {
            continue;
        }
        let trimmed_name = &name[..name.len() - PARTIAL_SUFFIX.len()];
        let mut final_path = path.clone();
        final_path.set_file_name(trimmed_name);
        let relative = final_path.strip_prefix(root).map_err(|_| {
            DirTreeError::io(final_path.clone(), io::Error::other("path not under root"))
        })?;
        out.push(relative.to_path_buf());
    }
    Ok(())
}

#[cfg(test)]
#[allow(
    clippy::cognitive_complexity,
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::panic,
    clippy::unwrap_used
)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use tempfile::TempDir;

    fn writer() -> (TempDir, DirTreeWriter) {
        let tmp = TempDir::new().expect("tempdir creation");
        let writer = DirTreeWriter::new(tmp.path().to_path_buf()).expect("writer construction");
        (tmp, writer)
    }

    #[test]
    fn new_rejects_missing_backing_root() {
        let bogus = PathBuf::from("Q:/this/path/does/not/exist/teslausb-b1-test");
        let err = DirTreeWriter::new(bogus.clone()).expect_err("missing dir is rejected");
        assert!(matches!(err, DirTreeError::BackingRootInvalid { ref path } if path == &bogus));
    }

    #[test]
    fn new_rejects_file_path_as_backing_root() {
        let tmp = TempDir::new().expect("tempdir");
        let file_path = tmp.path().join("not-a-dir");
        std::fs::write(&file_path, b"x").expect("write");
        let err = DirTreeWriter::new(file_path.clone()).expect_err("file path is rejected");
        assert!(matches!(err, DirTreeError::BackingRootInvalid { ref path } if path == &file_path));
    }

    #[test]
    fn backing_root_accessor_returns_constructor_path() {
        let (tmp, w) = writer();
        assert_eq!(w.backing_root(), tmp.path());
    }

    #[test]
    fn apply_chunk_rejects_absolute_path() {
        let (_tmp, w) = writer();
        let abs = if cfg!(windows) {
            PathBuf::from("C:/somewhere/foo.bin")
        } else {
            PathBuf::from("/etc/passwd")
        };
        let err = w
            .apply_chunk(&abs, 0, b"hello")
            .expect_err("absolute is rejected");
        assert!(matches!(err, DirTreeError::InvalidRelativePath { .. }));
    }

    #[test]
    fn apply_chunk_rejects_parent_dir_traversal() {
        let (_tmp, w) = writer();
        let rel = PathBuf::from("a/../../escape.bin");
        let err = w
            .apply_chunk(&rel, 0, b"hello")
            .expect_err("parent-dir is rejected");
        assert!(matches!(err, DirTreeError::InvalidRelativePath { .. }));
    }

    #[test]
    fn apply_chunk_empty_bytes_is_noop() {
        let (tmp, w) = writer();
        let rel = PathBuf::from("foo.bin");
        w.apply_chunk(&rel, 0, &[]).expect("empty write OK");
        let partial = tmp.path().join("foo.bin.partial");
        assert!(
            !partial.exists(),
            "no .partial file created for empty write"
        );
    }

    #[test]
    fn apply_chunk_writes_partial_file_at_offset_zero() {
        let (tmp, w) = writer();
        let rel = PathBuf::from("foo.bin");
        let payload = b"hello world";
        w.apply_chunk(&rel, 0, payload).expect("write OK");
        let partial = tmp.path().join("foo.bin.partial");
        let read_back = std::fs::read(&partial).expect("read partial");
        assert_eq!(read_back.as_slice(), payload);
    }

    #[test]
    fn apply_chunk_creates_parent_directories() {
        let (tmp, w) = writer();
        let rel = PathBuf::from("Sentry/2026-05-20_12-00-00/cam.mp4");
        w.apply_chunk(&rel, 0, b"sentry-clip")
            .expect("nested write OK");
        let partial = tmp
            .path()
            .join("Sentry/2026-05-20_12-00-00/cam.mp4.partial");
        assert!(partial.is_file(), "nested .partial exists");
    }

    #[test]
    fn apply_chunk_writes_at_nonzero_offset_creates_sparse_file() {
        let (tmp, w) = writer();
        let rel = PathBuf::from("sparse.bin");
        w.apply_chunk(&rel, 1024, b"chunk")
            .expect("sparse write OK");
        let partial = tmp.path().join("sparse.bin.partial");
        let meta = std::fs::metadata(&partial).expect("meta");
        assert_eq!(meta.len(), 1024 + 5);
        let read_back = std::fs::read(&partial).expect("read partial");
        assert_eq!(&read_back[0..1024], &[0u8; 1024]);
        assert_eq!(&read_back[1024..], b"chunk");
    }

    #[test]
    fn apply_chunk_is_idempotent_for_same_input() {
        let (tmp, w) = writer();
        let rel = PathBuf::from("idem.bin");
        w.apply_chunk(&rel, 0, b"abc").expect("first write OK");
        w.apply_chunk(&rel, 0, b"abc").expect("second write OK");
        let read_back = std::fs::read(tmp.path().join("idem.bin.partial")).expect("read");
        assert_eq!(read_back.as_slice(), b"abc");
    }

    #[test]
    fn apply_chunk_later_write_at_overlapping_offset_overwrites() {
        let (tmp, w) = writer();
        let rel = PathBuf::from("ovwt.bin");
        w.apply_chunk(&rel, 0, b"aaaa").expect("first write OK");
        w.apply_chunk(&rel, 1, b"BB").expect("overlap write OK");
        let read_back = std::fs::read(tmp.path().join("ovwt.bin.partial")).expect("read");
        assert_eq!(read_back.as_slice(), b"aBBa");
    }

    #[test]
    fn finalize_renames_partial_to_final_name() {
        let (tmp, w) = writer();
        let rel = PathBuf::from("done.bin");
        w.apply_chunk(&rel, 0, b"finalized").expect("write OK");
        w.finalize(&rel).expect("finalize OK");
        assert!(!tmp.path().join("done.bin.partial").exists());
        assert_eq!(
            std::fs::read(tmp.path().join("done.bin")).expect("read final"),
            b"finalized"
        );
    }

    #[test]
    fn finalize_without_partial_returns_error() {
        let (_tmp, w) = writer();
        let rel = PathBuf::from("missing.bin");
        let err = w.finalize(&rel).expect_err("missing partial is rejected");
        assert!(matches!(err, DirTreeError::NoPartialToFinalize { .. }));
    }

    #[test]
    fn finalize_rejects_collision_with_existing_final_file() {
        let (tmp, w) = writer();
        let rel = PathBuf::from("collide.bin");
        std::fs::write(tmp.path().join("collide.bin"), b"old").expect("seed final");
        w.apply_chunk(&rel, 0, b"new").expect("write OK");
        let err = w.finalize(&rel).expect_err("collision is rejected");
        assert!(matches!(err, DirTreeError::Collision { .. }));
    }

    #[test]
    fn discard_removes_partial_if_present() {
        let (tmp, w) = writer();
        let rel = PathBuf::from("drop.bin");
        w.apply_chunk(&rel, 0, b"abandoned").expect("write OK");
        assert!(tmp.path().join("drop.bin.partial").exists());
        w.discard(&rel).expect("discard OK");
        assert!(!tmp.path().join("drop.bin.partial").exists());
    }

    #[test]
    fn discard_missing_partial_is_noop() {
        let (_tmp, w) = writer();
        let rel = PathBuf::from("ghost.bin");
        w.discard(&rel).expect("missing partial discard is OK");
    }

    #[test]
    fn unlink_removes_final_file_if_present() {
        let (tmp, w) = writer();
        let rel = PathBuf::from("delete.bin");
        std::fs::write(tmp.path().join("delete.bin"), b"goodbye").expect("seed");
        w.unlink(&rel).expect("unlink OK");
        assert!(!tmp.path().join("delete.bin").exists());
    }

    #[test]
    fn unlink_missing_final_is_noop() {
        let (_tmp, w) = writer();
        let rel = PathBuf::from("ghost.bin");
        w.unlink(&rel).expect("missing final unlink is OK");
    }

    #[test]
    fn scan_partials_returns_empty_when_tree_is_clean() {
        let (_tmp, w) = writer();
        let found = w.scan_partials().expect("scan OK");
        assert!(found.is_empty());
    }

    #[test]
    fn scan_partials_finds_only_partial_files_and_strips_suffix() {
        let (tmp, w) = writer();
        // Mix finalized files, .partial files, and unrelated junk.
        std::fs::write(tmp.path().join("final.bin"), b"x").expect("seed final");
        w.apply_chunk(&PathBuf::from("a.bin"), 0, b"x")
            .expect("write a");
        w.apply_chunk(&PathBuf::from("sub/b.bin"), 0, b"x")
            .expect("write b");
        std::fs::write(tmp.path().join("sub/other.txt"), b"x").expect("seed other");
        let found = w.scan_partials().expect("scan OK");
        let expected = [PathBuf::from("a.bin"), PathBuf::from("sub").join("b.bin")];
        assert_eq!(found, expected);
    }

    #[test]
    fn finalize_after_scan_partials_round_trips() {
        let (tmp, w) = writer();
        w.apply_chunk(&PathBuf::from("recovery.bin"), 0, b"abc")
            .expect("write");
        let found = w.scan_partials().expect("scan");
        assert_eq!(found.len(), 1);
        w.finalize(&found[0]).expect("finalize from scan");
        assert!(tmp.path().join("recovery.bin").exists());
        assert!(!tmp.path().join("recovery.bin.partial").exists());
    }
}
