//! Filesystem walker that builds a [`BackingTree`] from a real
//! Linux directory (Phase 2.15 — I/O half).
//!
//! Type definitions and the shared filename validator live in
//! [`teslausb_core::fs::backing_tree`]; this module supplies the
//! `std::fs`-driven walker that the daemon (Phase 2.19) calls at
//! startup to discover what to expose.
//!
//! ## What the walker does
//!
//! 1. Reads `root` with [`std::fs::read_dir`].
//! 2. For each directory entry: stats the entry, validates the
//!    leaf name via [`teslausb_core::fs::backing_tree::validate_name`],
//!    rejects symlinks and special files (sockets, fifos,
//!    block/char devices), and recurses into subdirectories.
//! 3. Sorts `subdirs` and `files` by name within each directory
//!    so cluster assignment (Phase 2.16) is deterministic across
//!    runs and platforms.
//!
//! ## What the walker does NOT do
//!
//! * **No open of file contents.** Only metadata (`size`,
//!   `mtime`) is captured; file bytes are read on demand by the
//!   materializer in Phase 2.19.
//! * **No symlink resolution.** Symlinks are rejected with
//!   [`WalkError::Symlink`] to avoid loops and to keep the walk
//!   bounded.
//! * **No special-file handling.** FIFOs / sockets / device
//!   files are rejected with [`WalkError::SpecialFile`].
//! * **No mount-point detection.** A backing tree that spans
//!   filesystems is fine; the walker will happily cross mount
//!   points if the operator deliberately set things up that way.
//!
//! ## Determinism guarantees
//!
//! Two walks of the same on-disk tree (no concurrent writes)
//! produce byte-identical [`BackingTree`]s except for `mtime`
//! values, which reflect whatever the filesystem reports. Tests
//! pin alphabetical ordering of `subdirs` and `files` and
//! exact `backing_path` formatting.

use std::cmp::Ordering;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use teslausb_core::fs::backing_tree::{
    BackingDir, BackingFile, BackingTree, NameError, validate_name,
};

/// Maximum directory nesting depth the walker will descend
/// before refusing to recurse further.
///
/// FAT32 has no formal nesting limit but Windows Explorer
/// historically caps at ~50; exFAT also imposes no formal limit.
/// 32 levels is well past any realistic Tesla-camera or
/// archive-tool layout and serves as a stack-overflow / runaway-
/// loop safety net.
pub const MAX_DEPTH: usize = 32;

/// Errors returned by [`walk`].
///
/// Each variant carries the offending `path` so the operator
/// can fix the backing tree without re-running with a debugger.
#[derive(Debug)]
pub enum WalkError {
    /// An [`std::io`] error from [`fs::read_dir`], [`fs::metadata`],
    /// or [`fs::symlink_metadata`].
    Io {
        /// Absolute path that produced the error.
        path: PathBuf,
        /// Underlying [`std::io`] error.
        source: io::Error,
    },
    /// A directory entry's leaf filename failed
    /// [`validate_name`].
    InvalidName {
        /// Absolute path of the offending entry.
        path: PathBuf,
        /// Underlying name-validation error.
        source: NameError,
    },
    /// The directory entry's name was not valid UTF-8. FAT32
    /// LFN and exFAT both store names as UTF-16; round-tripping
    /// non-UTF-8 byte sequences from a POSIX filesystem through
    /// UTF-16 is lossy and the walker refuses to guess.
    NonUtf8Name {
        /// Absolute path of the offending entry.
        path: PathBuf,
    },
    /// The entry is a symlink. The walker refuses to follow
    /// (loop risk + escaping the backing root) and refuses to
    /// represent the link itself (FAT/exFAT have no symlink
    /// entry type).
    Symlink {
        /// Absolute path of the symlink.
        path: PathBuf,
    },
    /// The entry is a FIFO, socket, block device, or character
    /// device. FAT/exFAT cannot represent these.
    SpecialFile {
        /// Absolute path of the special file.
        path: PathBuf,
    },
    /// The walker reached [`MAX_DEPTH`] and refuses to recurse
    /// further.
    DepthExceeded {
        /// Absolute path at which recursion was refused.
        path: PathBuf,
        /// The depth limit ([`MAX_DEPTH`]).
        limit: usize,
    },
    /// `root` itself was not a directory.
    RootNotADirectory {
        /// The path the walker was asked to walk.
        path: PathBuf,
    },
}

impl core::fmt::Display for WalkError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::Io { path, source } => {
                write!(f, "I/O error walking {}: {source}", path.display())
            }
            Self::InvalidName { path, source } => {
                write!(
                    f,
                    "invalid backing-tree name at {}: {source}",
                    path.display()
                )
            }
            Self::NonUtf8Name { path } => {
                write!(
                    f,
                    "backing-tree entry has non-UTF-8 name: {}",
                    path.display()
                )
            }
            Self::Symlink { path } => write!(
                f,
                "backing-tree entry {} is a symlink (not representable in FAT/exFAT)",
                path.display(),
            ),
            Self::SpecialFile { path } => write!(
                f,
                "backing-tree entry {} is a special file (FIFO/socket/device)",
                path.display(),
            ),
            Self::DepthExceeded { path, limit } => write!(
                f,
                "backing-tree depth exceeds limit {limit} at {}",
                path.display(),
            ),
            Self::RootNotADirectory { path } => {
                write!(f, "backing-tree root {} is not a directory", path.display())
            }
        }
    }
}

impl std::error::Error for WalkError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            Self::InvalidName { source, .. } => Some(source),
            Self::NonUtf8Name { .. }
            | Self::Symlink { .. }
            | Self::SpecialFile { .. }
            | Self::DepthExceeded { .. }
            | Self::RootNotADirectory { .. } => None,
        }
    }
}

/// Walk `root` and produce a [`BackingTree`].
///
/// The root directory's `name` is the empty string; the walker
/// does not encode any name for the root because the synthesizer
/// places the volume label (not the root's POSIX directory name)
/// at the top of the synthesized volume.
///
/// # Errors
///
/// Returns the first [`WalkError`] hit during the walk. The
/// walker stops at the first failure rather than collecting all
/// errors; partial trees would mislead the planner.
pub fn walk(root: &Path) -> Result<BackingTree, WalkError> {
    let root_meta = fs::symlink_metadata(root).map_err(|e| WalkError::Io {
        path: root.to_path_buf(),
        source: e,
    })?;
    if root_meta.file_type().is_symlink() {
        return Err(WalkError::Symlink {
            path: root.to_path_buf(),
        });
    }
    if !root_meta.is_dir() {
        return Err(WalkError::RootNotADirectory {
            path: root.to_path_buf(),
        });
    }
    let root_dir = walk_dir(root, String::new(), &root_meta, 0)?;
    Ok(BackingTree { root: root_dir })
}

/// Walk a single directory and return its [`BackingDir`].
///
/// `name` is the leaf name to record on the returned
/// [`BackingDir`] (empty for the root). `meta` is the result of
/// `symlink_metadata` on `path` — passed in so the caller can
/// reuse a stat call when it already has one (the root walk and
/// each subdirectory recursion).
fn walk_dir(
    path: &Path,
    name: String,
    meta: &fs::Metadata,
    depth: usize,
) -> Result<BackingDir, WalkError> {
    if depth > MAX_DEPTH {
        return Err(WalkError::DepthExceeded {
            path: path.to_path_buf(),
            limit: MAX_DEPTH,
        });
    }

    let read_dir = fs::read_dir(path).map_err(|e| WalkError::Io {
        path: path.to_path_buf(),
        source: e,
    })?;

    let mut subdirs: Vec<BackingDir> = Vec::new();
    let mut files: Vec<BackingFile> = Vec::new();

    for entry in read_dir {
        let entry = entry.map_err(|e| WalkError::Io {
            path: path.to_path_buf(),
            source: e,
        })?;
        let entry_path = entry.path();
        let leaf_os = entry.file_name();
        let leaf = leaf_os.to_str().ok_or_else(|| WalkError::NonUtf8Name {
            path: entry_path.clone(),
        })?;
        validate_name(leaf).map_err(|source| WalkError::InvalidName {
            path: entry_path.clone(),
            source,
        })?;

        // symlink_metadata: follow=false. The walker rejects
        // symlinks outright; calling metadata() (which follows)
        // would let a symlink to a regular file masquerade as
        // a real entry.
        let entry_meta = fs::symlink_metadata(&entry_path).map_err(|e| WalkError::Io {
            path: entry_path.clone(),
            source: e,
        })?;
        let file_type = entry_meta.file_type();

        if file_type.is_symlink() {
            return Err(WalkError::Symlink { path: entry_path });
        }
        if file_type.is_dir() {
            let child = walk_dir(&entry_path, leaf.to_owned(), &entry_meta, depth + 1)?;
            subdirs.push(child);
        } else if file_type.is_file() {
            let mtime = entry_meta.modified().map_err(|e| WalkError::Io {
                path: entry_path.clone(),
                source: e,
            })?;
            files.push(BackingFile {
                name: leaf.to_owned(),
                backing_path: entry_path,
                size: entry_meta.len(),
                mtime,
            });
        } else {
            return Err(WalkError::SpecialFile { path: entry_path });
        }
    }

    // Sort by name so cluster assignment is deterministic across
    // platforms (Linux read_dir gives no ordering guarantee and
    // Windows / tmpfs / ext4 all differ).
    subdirs.sort_by(|a, b| name_cmp(&a.name, &b.name));
    files.sort_by(|a, b| name_cmp(&a.name, &b.name));

    let mtime = meta.modified().map_err(|e| WalkError::Io {
        path: path.to_path_buf(),
        source: e,
    })?;

    Ok(BackingDir {
        name,
        backing_path: path.to_path_buf(),
        mtime,
        subdirs,
        files,
    })
}

/// Byte-wise comparison of two leaf names.
///
/// FAT32 and exFAT both store names case-insensitively at the
/// directory-entry layer (exFAT via the upcase table). Sorting
/// case-insensitively here would mean two distinct backing
/// entries `Foo.mp4` and `foo.mp4` map to adjacent dir entries —
/// already a problem at synthesis time, but not one the walker
/// has authority to resolve. Sort case-sensitively (byte order)
/// and let the planner (Phase 2.16) detect the collision when
/// it builds the dir-entry array.
fn name_cmp(a: &str, b: &str) -> Ordering {
    a.cmp(b)
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
    use std::fs::{File, create_dir_all, write};
    use std::io::Write;

    use tempfile::TempDir;

    fn fixture_root() -> TempDir {
        tempfile::tempdir().expect("create tempdir")
    }

    // ---- happy-path walks ------------------------------------

    #[test]
    fn walks_empty_directory() {
        let dir = fixture_root();
        let tree = walk(dir.path()).unwrap();
        assert_eq!(tree.root.name, "");
        assert_eq!(tree.root.backing_path, dir.path());
        assert!(tree.root.subdirs.is_empty());
        assert!(tree.root.files.is_empty());
    }

    #[test]
    fn walks_single_file() {
        let dir = fixture_root();
        let file = dir.path().join("front.mp4");
        write(&file, b"hello world").unwrap();
        let tree = walk(dir.path()).unwrap();
        assert_eq!(tree.root.files.len(), 1);
        assert_eq!(tree.root.files[0].name, "front.mp4");
        assert_eq!(tree.root.files[0].size, 11);
        assert_eq!(tree.root.files[0].backing_path, file);
    }

    #[test]
    fn walks_nested_directories() {
        let dir = fixture_root();
        create_dir_all(dir.path().join("SavedClips/2026-05-19_18-00-00")).unwrap();
        write(
            dir.path().join("SavedClips/2026-05-19_18-00-00/front.mp4"),
            b"abc",
        )
        .unwrap();
        write(
            dir.path().join("SavedClips/2026-05-19_18-00-00/back.mp4"),
            b"defg",
        )
        .unwrap();
        let tree = walk(dir.path()).unwrap();
        assert_eq!(tree.root.subdirs.len(), 1);
        let saved = &tree.root.subdirs[0];
        assert_eq!(saved.name, "SavedClips");
        assert_eq!(saved.subdirs.len(), 1);
        let inst = &saved.subdirs[0];
        assert_eq!(inst.name, "2026-05-19_18-00-00");
        assert_eq!(inst.files.len(), 2);
        // Alphabetical sort: back.mp4 before front.mp4.
        assert_eq!(inst.files[0].name, "back.mp4");
        assert_eq!(inst.files[1].name, "front.mp4");
    }

    #[test]
    fn captures_file_size_and_backing_path() {
        let dir = fixture_root();
        let path = dir.path().join("v.mp4");
        let mut f = File::create(&path).unwrap();
        f.write_all(&vec![0u8; 4096]).unwrap();
        drop(f);
        let tree = walk(dir.path()).unwrap();
        assert_eq!(tree.root.files[0].size, 4096);
        assert_eq!(tree.root.files[0].backing_path, path);
    }

    #[test]
    fn sorts_entries_alphabetically_within_each_directory() {
        let dir = fixture_root();
        // Create files in non-alphabetical order; the walker
        // must reorder them.
        for name in ["zeta.mp4", "alpha.mp4", "mu.mp4"] {
            write(dir.path().join(name), b"x").unwrap();
        }
        for name in ["zdir", "adir", "mdir"] {
            create_dir_all(dir.path().join(name)).unwrap();
        }
        let tree = walk(dir.path()).unwrap();
        let file_names: Vec<&str> = tree.root.files.iter().map(|f| f.name.as_str()).collect();
        assert_eq!(file_names, vec!["alpha.mp4", "mu.mp4", "zeta.mp4"]);
        let subdir_names: Vec<&str> = tree.root.subdirs.iter().map(|d| d.name.as_str()).collect();
        assert_eq!(subdir_names, vec!["adir", "mdir", "zdir"]);
    }

    #[test]
    fn root_name_is_empty_string() {
        let dir = fixture_root();
        write(dir.path().join("v.mp4"), b"x").unwrap();
        let tree = walk(dir.path()).unwrap();
        assert_eq!(tree.root.name, "");
    }

    // ---- rejection cases -------------------------------------

    #[test]
    fn rejects_missing_root() {
        let dir = fixture_root();
        let missing = dir.path().join("nope");
        let err = walk(&missing).unwrap_err();
        match err {
            WalkError::Io { path, .. } => assert_eq!(path, missing),
            other => panic!("expected Io, got {other:?}"),
        }
    }

    #[test]
    fn rejects_root_that_is_a_file() {
        let dir = fixture_root();
        let file = dir.path().join("not-a-dir");
        write(&file, b"x").unwrap();
        let err = walk(&file).unwrap_err();
        match err {
            WalkError::RootNotADirectory { path } => assert_eq!(path, file),
            other => panic!("expected RootNotADirectory, got {other:?}"),
        }
    }

    #[cfg(unix)]
    #[test]
    fn rejects_invalid_leaf_name_inside_tree() {
        let dir = fixture_root();
        // Trailing dot — rejected by validate_name. Unix-only
        // because Windows silently strips trailing dots at file-
        // creation time, so the file ends up named `foo` and
        // passes validation. The `validate_name` unit tests
        // cover the cross-platform validation logic; this test
        // is solely about propagation through the walker.
        let invalid = dir.path().join("foo.");
        write(&invalid, b"x").unwrap();
        let err = walk(dir.path()).unwrap_err();
        match err {
            WalkError::InvalidName { source, .. } => {
                assert_eq!(source, NameError::EndsInDotOrSpace);
            }
            other => panic!("expected InvalidName, got {other:?}"),
        }
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlink_in_tree() {
        use std::os::unix::fs::symlink;
        let dir = fixture_root();
        write(dir.path().join("target.mp4"), b"x").unwrap();
        symlink(dir.path().join("target.mp4"), dir.path().join("link.mp4")).unwrap();
        let err = walk(dir.path()).unwrap_err();
        match err {
            WalkError::Symlink { path } => {
                assert_eq!(path, dir.path().join("link.mp4"));
            }
            other => panic!("expected Symlink, got {other:?}"),
        }
    }

    #[cfg(unix)]
    #[test]
    fn rejects_root_that_is_a_symlink() {
        use std::os::unix::fs::symlink;
        let dir = fixture_root();
        let target = dir.path().join("real");
        create_dir_all(&target).unwrap();
        let link = dir.path().join("link");
        symlink(&target, &link).unwrap();
        let err = walk(&link).unwrap_err();
        match err {
            WalkError::Symlink { path } => assert_eq!(path, link),
            other => panic!("expected Symlink, got {other:?}"),
        }
    }

    #[cfg(unix)]
    #[test]
    fn rejects_unix_socket_special_file() {
        use std::os::unix::net::UnixListener;
        let dir = fixture_root();
        let sock_path = dir.path().join("ctrl.sock");
        let listener = UnixListener::bind(&sock_path).unwrap();
        // Hold the listener alive until we've walked — dropping
        // it would close the socket but the file entry remains
        // on the filesystem either way; this matches what the
        // walker will see on the live daemon when an admin
        // accidentally co-locates a socket with the backing
        // tree.
        let err = walk(dir.path()).unwrap_err();
        drop(listener);
        match err {
            WalkError::SpecialFile { path } => assert_eq!(path, sock_path),
            other => panic!("expected SpecialFile, got {other:?}"),
        }
    }

    #[test]
    fn captures_directory_mtime() {
        let dir = fixture_root();
        let tree = walk(dir.path()).unwrap();
        // Just pin that mtime is populated to *some* SystemTime
        // strictly later than UNIX_EPOCH — we can't pin an exact
        // value without racing the test harness, but a zero
        // value would indicate the walker forgot to capture it.
        assert!(tree.root.mtime > std::time::UNIX_EPOCH);
    }

    #[test]
    fn captures_file_mtime() {
        let dir = fixture_root();
        write(dir.path().join("v.mp4"), b"x").unwrap();
        let tree = walk(dir.path()).unwrap();
        assert!(tree.root.files[0].mtime > std::time::UNIX_EPOCH);
    }

    // ---- depth limit -----------------------------------------

    #[test]
    fn accepts_depth_at_limit() {
        let dir = fixture_root();
        let mut path = dir.path().to_path_buf();
        // MAX_DEPTH levels deep — exactly at limit, should pass.
        for i in 0..MAX_DEPTH {
            path = path.join(format!("d{i}"));
        }
        create_dir_all(&path).unwrap();
        write(path.join("v.mp4"), b"x").unwrap();
        let tree = walk(dir.path()).unwrap();
        // Drill down and confirm the file is present.
        let mut node = &tree.root;
        for i in 0..MAX_DEPTH {
            assert_eq!(node.subdirs.len(), 1, "missing subdir at level {i}");
            node = &node.subdirs[0];
        }
        assert_eq!(node.files.len(), 1);
        assert_eq!(node.files[0].name, "v.mp4");
    }

    #[test]
    fn rejects_depth_past_limit() {
        let dir = fixture_root();
        let mut path = dir.path().to_path_buf();
        // MAX_DEPTH + 1 levels — should be rejected.
        for i in 0..=MAX_DEPTH {
            path = path.join(format!("d{i}"));
        }
        if create_dir_all(&path).is_err() {
            return; // host PATH_MAX hit before the limit; skip
        }
        let err = walk(dir.path()).unwrap_err();
        match err {
            WalkError::DepthExceeded { limit, .. } => assert_eq!(limit, MAX_DEPTH),
            other => panic!("expected DepthExceeded, got {other:?}"),
        }
    }

    // ---- Display / Error -------------------------------------

    #[test]
    fn walk_error_implements_std_error_and_chain() {
        let dir = fixture_root();
        // Trigger an Io error by walking a missing path.
        let err = walk(&dir.path().join("nope")).unwrap_err();
        let chain: Vec<String> =
            std::iter::successors(Some(&err as &dyn std::error::Error), |e| e.source())
                .map(ToString::to_string)
                .collect();
        assert!(chain.len() >= 2, "expected source chain, got: {chain:?}");
        assert!(
            chain[0].contains("nope"),
            "outer error missing path: {chain:?}"
        );
    }

    #[test]
    fn display_includes_path_for_each_variant() {
        let p = PathBuf::from("/tmp/foo");
        assert!(
            WalkError::Symlink { path: p.clone() }
                .to_string()
                .contains("/tmp/foo"),
        );
        assert!(
            WalkError::SpecialFile { path: p.clone() }
                .to_string()
                .contains("/tmp/foo"),
        );
        assert!(
            WalkError::DepthExceeded {
                path: p.clone(),
                limit: MAX_DEPTH,
            }
            .to_string()
            .contains("/tmp/foo"),
        );
        assert!(
            WalkError::RootNotADirectory { path: p.clone() }
                .to_string()
                .contains("/tmp/foo"),
        );
        assert!(
            WalkError::NonUtf8Name { path: p }
                .to_string()
                .contains("/tmp/foo"),
        );
    }
}
