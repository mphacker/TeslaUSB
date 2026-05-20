//! Backing-tree types + filename validation (Phase 2.15).
//!
//! [`BackingTree`] is the in-memory description of a Linux directory
//! tree that the FAT32 / exFAT synthesizer will render into a
//! virtual volume. The tree is **filesystem-agnostic**: no cluster
//! numbers, no SFN aliases, no LFN encodings, no on-disk byte
//! layout. Those concerns belong to the cluster-layout planner
//! (Phase 2.16) and the per-FS dir-entry synthesizers (Phases 2.17
//! / 2.18).
//!
//! This module also owns [`validate_name`], the single
//! authoritative check that a leaf filename is safely embeddable
//! in both FAT32 (long-file-name) and exFAT directory entries.
//! Both filesystems share the same Microsoft-defined forbidden
//! character set and 255-UTF-16-code-unit limit, so one validator
//! covers both.
//!
//! ## What this module does NOT do
//!
//! * **No I/O.** [`teslausb-core`] is a pure-logic crate; the
//!   walker that fills a [`BackingTree`] from `std::fs::read_dir`
//!   lives in `teslafat::backing_walker` (Phase 2.15 second
//!   deliverable).
//! * **No cluster math.** Files and directories carry their size
//!   in bytes; the planner converts to clusters in Phase 2.16.
//! * **No deduplication of dir-entry ordering.** [`BackingDir`]
//!   exposes `subdirs` and `files` as separate `Vec`s. The
//!   walker is expected to sort each by name before handing the
//!   tree to the planner so cluster assignment is deterministic.
//!
//! ## Why a single shared validator
//!
//! Tesla's car-side FAT driver is lenient about filename content;
//! a Windows host attached over USB (the operator's PC, used to
//! pull clips) is not. The validator therefore enforces the
//! *stricter* of {FAT32 LFN, exFAT, Windows-NT-namespace}
//! constraints:
//!
//! | Constraint                | Rule                                                                   |
//! |---------------------------|------------------------------------------------------------------------|
//! | Length                    | ≤ 255 UTF-16 code units (both FS limits)                               |
//! | Forbidden bytes           | NUL + control chars `0x00..=0x1F`                                      |
//! | Forbidden chars (printable) | `"`, `*`, `/`, `:`, `<`, `>`, `?`, `\`, `|` (Win32 namespace + both FS) |
//! | Reserved names            | `.` and `..` (these are synthesized, never sourced from backing)       |
//! | Trailing characters       | Must not end in `.` or space (Windows compatibility)                   |
//!
//! The validator does NOT enforce reserved DOS device names
//! (`CON`, `PRN`, `AUX`, …) because:
//! * Linux happily creates such files on backing storage.
//! * The Tesla in-car player reads files by full path, not by
//!   shortened DOS-style name.
//! * Windows interactive shell access to such files is broken
//!   anyway (`type CON` etc.), which is the user's problem to
//!   not put a file called `CON.mp4` in their backing tree.

use std::path::PathBuf;
use std::time::SystemTime;

/// Both FAT32 LFN and exFAT cap a filename at 255 UTF-16 code
/// units (sometimes counted as "characters" — but exFAT
/// explicitly counts code units, and FAT32 LFN entries fit
/// 13 code units each up to a 20-entry chain, i.e. 260 code units
/// total of which the practical cap is 255).
pub const MAX_FILENAME_UTF16: usize = 255;

/// In-memory description of a Linux directory tree that the
/// synthesizer will render as a virtual FAT32 / exFAT volume.
///
/// The tree is filesystem-agnostic and carries no cluster or
/// on-disk-layout information. The root directory itself has a
/// fixed empty name (FAT32 root has no name on disk; exFAT puts
/// the volume label in the root, not the root's "name").
#[derive(Debug, Clone)]
pub struct BackingTree {
    /// The root directory of the tree.
    pub root: BackingDir,
}

/// A single directory in the backing tree.
///
/// `subdirs` and `files` are deliberately split (rather than a
/// single `Vec<Entry>` enum) because the cluster-layout planner
/// (Phase 2.16) treats them differently: subdirs always get at
/// least one cluster for their dir-entry array, files only get
/// clusters when their size > 0.
#[derive(Debug, Clone)]
pub struct BackingDir {
    /// Leaf name of this directory (no path separators). The
    /// root directory carries the empty string.
    pub name: String,
    /// Absolute path to this directory on the backing Linux
    /// filesystem. The walker fills this so file-content reads
    /// (Phase 2.19) can re-open the backing file by full path.
    pub backing_path: PathBuf,
    /// `mtime` from the backing directory's `stat`. Stamped into
    /// the synthesized FAT32 / exFAT directory entry for this
    /// dir so Tesla's UI shows a meaningful "last modified" date.
    pub mtime: SystemTime,
    /// Child subdirectories, sorted by `name` ascending. The
    /// walker is responsible for the sort; the planner relies
    /// on deterministic ordering for reproducible cluster
    /// assignment.
    pub subdirs: Vec<BackingDir>,
    /// Files directly inside this directory, sorted by `name`
    /// ascending. Same determinism note as `subdirs`.
    pub files: Vec<BackingFile>,
}

/// A single regular file in the backing tree.
///
/// The walker captures only what the planner + the per-FS
/// dir-entry synthesizers need: size for cluster assignment,
/// mtime for the directory entry's timestamp fields, and the
/// backing path so `read_cluster` (Phase 2.19) can re-open the
/// file with `pread` semantics.
#[derive(Debug, Clone)]
pub struct BackingFile {
    /// Leaf filename, validated through [`validate_name`].
    pub name: String,
    /// Absolute backing path. Owning a [`PathBuf`] here costs
    /// ~24 bytes plus the allocated path bytes; the materializer
    /// would have to hold this string anyway to open the file,
    /// so storing it once in the tree is the cheaper option.
    pub backing_path: PathBuf,
    /// File size in bytes, captured at walk time. The size at
    /// read time may differ if the backing file is rewritten
    /// between walk and read; the synthesizer treats walk-time
    /// size as authoritative and clamps reads at that bound.
    pub size: u64,
    /// `mtime` from the backing file's `stat`. Stamped into the
    /// synthesized directory entry.
    pub mtime: SystemTime,
}

/// Errors returned by [`validate_name`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NameError {
    /// The name is the empty string.
    Empty,
    /// The name exceeds [`MAX_FILENAME_UTF16`] UTF-16 code units.
    TooLong {
        /// Length of the supplied name in UTF-16 code units.
        len_utf16: usize,
        /// The hard limit (255).
        limit: usize,
    },
    /// The name contains a byte or character forbidden by both
    /// FAT32 LFN and exFAT. `position` is the **byte** index
    /// into the input `&str` so callers can surface a useful
    /// pointer back to the operator.
    InvalidChar {
        /// The offending character.
        c: char,
        /// Byte offset into the input where the character begins.
        position: usize,
    },
    /// The name is `.` or `..`. These are synthesized by the
    /// per-FS directory synthesizers; a backing file with one
    /// of these names would create a duplicate entry and break
    /// FAT/exFAT-conforming hosts.
    DotOrDotDot,
    /// The name ends in `.` or space, which Windows silently
    /// trims at open time. A backing file `foo.` would be
    /// opened as `foo` by a Windows host, masking another file
    /// of that name if one existed.
    EndsInDotOrSpace,
}

impl core::fmt::Display for NameError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::Empty => f.write_str("backing-tree filename is empty"),
            Self::TooLong { len_utf16, limit } => write!(
                f,
                "backing-tree filename is {len_utf16} UTF-16 code units; FAT32/exFAT limit is {limit}",
            ),
            Self::InvalidChar { c, position } => write!(
                f,
                "backing-tree filename contains invalid character {c:?} at byte position {position}",
            ),
            Self::DotOrDotDot => {
                f.write_str("backing-tree filename '.' or '..' collides with synthesized entries")
            }
            Self::EndsInDotOrSpace => f.write_str(
                "backing-tree filename ends in '.' or space; Windows would silently trim it",
            ),
        }
    }
}

impl std::error::Error for NameError {}

/// Validate a single leaf filename for FAT32 LFN + exFAT +
/// Windows-NT-namespace compatibility.
///
/// See the module-level docs for the full rule set. Validation
/// is pure: same input always returns the same result.
///
/// # Errors
///
/// Returns the first violation found, in this order:
/// [`NameError::Empty`], [`NameError::DotOrDotDot`],
/// [`NameError::TooLong`], [`NameError::InvalidChar`],
/// [`NameError::EndsInDotOrSpace`].
pub fn validate_name(name: &str) -> Result<(), NameError> {
    if name.is_empty() {
        return Err(NameError::Empty);
    }
    if name == "." || name == ".." {
        return Err(NameError::DotOrDotDot);
    }
    let len_utf16: usize = name.chars().map(char::len_utf16).sum();
    if len_utf16 > MAX_FILENAME_UTF16 {
        return Err(NameError::TooLong {
            len_utf16,
            limit: MAX_FILENAME_UTF16,
        });
    }
    for (position, c) in name.char_indices() {
        if is_forbidden_char(c) {
            return Err(NameError::InvalidChar { c, position });
        }
    }
    // Trailing-char rule is checked last because it's the most
    // "soft" of the failures (the file is openable on Linux; it
    // just behaves badly when shared with Windows). Surfacing
    // the harder forbidden-char failure first matches how a
    // human would triage the rejection.
    let last_char = name.chars().next_back().unwrap_or('\0');
    if last_char == '.' || last_char == ' ' {
        return Err(NameError::EndsInDotOrSpace);
    }
    Ok(())
}

/// Returns `true` for any character forbidden by either FAT32
/// LFN or exFAT (the union of the two sets — they happen to
/// match exactly).
fn is_forbidden_char(c: char) -> bool {
    matches!(
        c,
        '\0'..='\x1F' | '"' | '*' | '/' | ':' | '<' | '>' | '?' | '\\' | '|'
    )
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

    // ---- validate_name: happy path ---------------------------

    #[test]
    fn validates_simple_ascii_name() {
        validate_name("video.mp4").unwrap();
        validate_name("SavedClips").unwrap();
        validate_name("a").unwrap();
    }

    #[test]
    fn validates_unicode_name() {
        validate_name("vidéo.mp4").unwrap();
        validate_name("撮影.mp4").unwrap();
        validate_name("🎥.mp4").unwrap();
    }

    #[test]
    fn validates_max_length_ascii() {
        let n = "a".repeat(MAX_FILENAME_UTF16);
        validate_name(&n).unwrap();
    }

    #[test]
    fn validates_internal_dots_and_spaces() {
        validate_name("a.b.c.mp4").unwrap();
        validate_name("front cam.mp4").unwrap();
        validate_name(".hidden.mp4").unwrap();
    }

    // ---- validate_name: rejection cases ----------------------

    #[test]
    fn rejects_empty_name() {
        assert_eq!(validate_name(""), Err(NameError::Empty));
    }

    #[test]
    fn rejects_dot_and_dotdot() {
        assert_eq!(validate_name("."), Err(NameError::DotOrDotDot));
        assert_eq!(validate_name(".."), Err(NameError::DotOrDotDot));
    }

    #[test]
    fn rejects_oversize_ascii() {
        let n = "a".repeat(MAX_FILENAME_UTF16 + 1);
        assert_eq!(
            validate_name(&n),
            Err(NameError::TooLong {
                len_utf16: MAX_FILENAME_UTF16 + 1,
                limit: MAX_FILENAME_UTF16,
            }),
        );
    }

    #[test]
    fn rejects_oversize_when_counting_utf16_surrogates() {
        // Each 🎥 is 1 char but 2 UTF-16 code units; 128 of them
        // is 256 code units > 255 limit, even though the
        // user-visible "character" count is 128.
        let n = "🎥".repeat(128);
        match validate_name(&n) {
            Err(NameError::TooLong { len_utf16, limit }) => {
                assert_eq!(len_utf16, 256);
                assert_eq!(limit, MAX_FILENAME_UTF16);
            }
            other => panic!("expected TooLong, got {other:?}"),
        }
    }

    #[test]
    fn rejects_each_forbidden_printable_char() {
        for c in ['"', '*', '/', ':', '<', '>', '?', '\\', '|'] {
            let name = format!("a{c}b");
            match validate_name(&name) {
                Err(NameError::InvalidChar { c: got_c, position }) => {
                    assert_eq!(got_c, c, "wrong char in error for {c:?}");
                    assert_eq!(position, 1, "wrong position for {c:?}");
                }
                other => panic!("expected InvalidChar for {c:?}, got {other:?}"),
            }
        }
    }

    #[test]
    fn rejects_nul_and_control_chars() {
        for c in ['\0', '\x01', '\x1F', '\n', '\r', '\t'] {
            let name = format!("a{c}b");
            match validate_name(&name) {
                Err(NameError::InvalidChar { c: got_c, .. }) => assert_eq!(got_c, c),
                other => panic!("expected InvalidChar for {c:?}, got {other:?}"),
            }
        }
    }

    #[test]
    fn rejects_trailing_dot() {
        assert_eq!(validate_name("foo."), Err(NameError::EndsInDotOrSpace));
        assert_eq!(validate_name("a."), Err(NameError::EndsInDotOrSpace));
    }

    #[test]
    fn rejects_trailing_space() {
        assert_eq!(validate_name("foo "), Err(NameError::EndsInDotOrSpace));
    }

    #[test]
    fn empty_takes_precedence_over_other_errors() {
        assert_eq!(validate_name(""), Err(NameError::Empty));
    }

    #[test]
    fn dotdot_takes_precedence_over_trailing_dot_rule() {
        // The trailing-dot rule would also fire on ".." but the
        // dot-dot reject is checked first because the resulting
        // collision with a synthesized `..` entry is the more
        // severe bug for a Tesla / Windows host.
        assert_eq!(validate_name(".."), Err(NameError::DotOrDotDot));
    }

    #[test]
    fn position_is_byte_index_not_char_index() {
        // 🎥 is 4 UTF-8 bytes; the bad char ':' starts at byte 4.
        match validate_name("🎥:foo") {
            Err(NameError::InvalidChar { c, position }) => {
                assert_eq!(c, ':');
                assert_eq!(position, 4);
            }
            other => panic!("expected InvalidChar, got {other:?}"),
        }
    }

    // ---- Display impl ----------------------------------------

    #[test]
    fn display_mentions_each_variant_distinctively() {
        assert!(NameError::Empty.to_string().contains("empty"));
        assert!(
            NameError::TooLong {
                len_utf16: 300,
                limit: 255
            }
            .to_string()
            .contains("300"),
        );
        assert!(
            NameError::InvalidChar {
                c: '?',
                position: 2,
            }
            .to_string()
            .contains("'?'"),
        );
        assert!(NameError::DotOrDotDot.to_string().contains("'.'"));
        assert!(NameError::EndsInDotOrSpace.to_string().contains("trim"),);
    }

    #[test]
    fn name_error_implements_std_error() {
        // Pin the std::error::Error impl so library callers can
        // attach context via `anyhow::Error::from(name_err)`.
        fn assert_error<E: std::error::Error>(_: &E) {}
        assert_error(&NameError::Empty);
    }

    // ---- BackingTree / BackingDir / BackingFile --------------

    #[test]
    fn backing_file_clones_independently() {
        // Pin Clone so the planner can clone files into its own
        // owned representation without lifetime entanglement.
        let f = BackingFile {
            name: "v.mp4".to_string(),
            backing_path: PathBuf::from("/var/teslacam/v.mp4"),
            size: 4096,
            mtime: SystemTime::UNIX_EPOCH,
        };
        let cloned = f.clone();
        assert_eq!(cloned.name, "v.mp4");
        assert_eq!(cloned.size, 4096);
    }

    #[test]
    fn backing_dir_default_is_empty_tree() {
        // Constructing an empty root by hand is the unit
        // smoke-test the walker (Phase 2.15 second deliverable)
        // and the integration tests rely on.
        let root = BackingDir {
            name: String::new(),
            backing_path: PathBuf::from("/var/teslacam"),
            mtime: SystemTime::UNIX_EPOCH,
            subdirs: Vec::new(),
            files: Vec::new(),
        };
        let tree = BackingTree { root };
        assert_eq!(tree.root.name, "");
        assert!(tree.root.subdirs.is_empty());
        assert!(tree.root.files.is_empty());
    }
}
