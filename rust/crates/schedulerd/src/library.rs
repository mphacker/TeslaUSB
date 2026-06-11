//! The chime **library** on the root filesystem (`/data/teslausb/chimes/`).
//!
//! The library is the set of uploaded `*.wav` files an operator can schedule or
//! activate. It lives on the rootfs data partition — **not** on the gadget MEDIA
//! exFAT — so reading and writing it is a plain filesystem operation that never
//! touches the `gadgetd` write queue. `schedulerd` is the sole writer of this
//! directory (webd forwards uploads here as staged temp files), mirroring the
//! single-writer discipline used for the schedule state.
//!
//! Activation (swapping the live `LockChime.wav` on the MEDIA partition) is a
//! separate, `gadgetd`-gated concern handled by the enforcement loop: it makes a
//! throwaway staged copy of a library file and enqueues a `gadgetd` mutation, so
//! the library files themselves are never handed to `gadgetd` (which reclaims
//! and unlinks the blobs it applies).

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::model::{ValidationError, validate_chime_filename};

/// One file in the chime library.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LibraryEntry {
    /// The single-segment `*.wav` filename.
    pub filename: String,
    /// File size in bytes.
    pub bytes: u64,
}

/// Errors from library operations.
#[derive(Debug, thiserror::Error)]
pub enum LibraryError {
    /// The filename failed validation.
    #[error("validation: {0}")]
    Validation(#[from] ValidationError),
    /// A requested file does not exist.
    #[error("not found")]
    NotFound,
    /// An underlying filesystem error.
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
}

/// Scan `dir` for `*.wav` files, returning them sorted by filename. A missing
/// directory yields an empty list (the library simply hasn't been populated).
/// Entries with unsafe names are skipped defensively.
///
/// # Errors
/// Propagates I/O errors other than a missing directory.
pub fn scan(dir: &Path) -> Result<Vec<LibraryEntry>, LibraryError> {
    let read = match std::fs::read_dir(dir) {
        Ok(r) => r,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e.into()),
    };
    let mut out = Vec::new();
    for entry in read {
        let entry = entry?;
        if !entry.file_type()?.is_file() {
            continue;
        }
        let Ok(filename) = entry.file_name().into_string() else {
            continue;
        };
        if validate_chime_filename(&filename).is_err() {
            continue;
        }
        let bytes = entry.metadata()?.len();
        out.push(LibraryEntry { filename, bytes });
    }
    out.sort_by(|a, b| {
        a.filename
            .to_ascii_lowercase()
            .cmp(&b.filename.to_ascii_lowercase())
    });
    Ok(out)
}

/// Resolve `filename` to an absolute path under `dir`, validating it is a safe
/// single-segment `*.wav` name first.
///
/// # Errors
/// [`LibraryError::Validation`] if the name is unsafe.
pub fn resolve(dir: &Path, filename: &str) -> Result<PathBuf, LibraryError> {
    validate_chime_filename(filename)?;
    Ok(dir.join(filename))
}

/// Adopt a `staged` temp file into the library as `filename` (validated). The
/// staged file is moved (atomic rename within the same fs, else copy+remove) so
/// the destination appears atomically. Returns the new [`LibraryEntry`].
///
/// # Errors
/// [`LibraryError::Validation`] for an unsafe name; [`LibraryError::Io`] on a
/// filesystem failure.
pub fn adopt(dir: &Path, staged: &Path, filename: &str) -> Result<LibraryEntry, LibraryError> {
    validate_chime_filename(filename)?;
    std::fs::create_dir_all(dir)?;
    let dest = dir.join(filename);
    // Try a fast rename first; fall back to copy+remove across filesystems.
    if std::fs::rename(staged, &dest).is_err() {
        std::fs::copy(staged, &dest)?;
        let _ = std::fs::remove_file(staged);
    }
    let bytes = std::fs::metadata(&dest)?.len();
    Ok(LibraryEntry {
        filename: filename.to_owned(),
        bytes,
    })
}

/// Remove `filename` from the library. Returns whether a file was removed.
///
/// # Errors
/// [`LibraryError::Validation`] for an unsafe name; [`LibraryError::Io`] on a
/// filesystem failure other than "not found".
pub fn remove(dir: &Path, filename: &str) -> Result<bool, LibraryError> {
    validate_chime_filename(filename)?;
    let path = dir.join(filename);
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(e.into()),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::panic, clippy::indexing_slicing)]
mod tests {
    use super::*;

    fn tmp_dir(tag: &str) -> PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        std::env::temp_dir().join(format!("chimelib-{tag}-{nanos}"))
    }

    #[test]
    fn scan_missing_dir_is_empty() {
        let dir = tmp_dir("missing");
        assert!(scan(&dir).unwrap().is_empty());
    }

    #[test]
    fn adopt_scan_remove_roundtrip() {
        let dir = tmp_dir("rt");
        let staged = std::env::temp_dir().join(format!(
            "stage-{}.tmp",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::write(&staged, b"RIFFfake").unwrap();
        let entry = adopt(&dir, &staged, "Classic.wav").unwrap();
        assert_eq!(entry.filename, "Classic.wav");
        assert_eq!(entry.bytes, 8);

        let listed = scan(&dir).unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].filename, "Classic.wav");

        assert!(remove(&dir, "Classic.wav").unwrap());
        assert!(!remove(&dir, "Classic.wav").unwrap());
        assert!(scan(&dir).unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn adopt_rejects_unsafe_name() {
        let dir = tmp_dir("unsafe");
        let staged = std::env::temp_dir().join("stage-unsafe.tmp");
        std::fs::write(&staged, b"x").unwrap();
        assert!(adopt(&dir, &staged, "../evil.wav").is_err());
        let _ = std::fs::remove_file(&staged);
    }

    #[test]
    fn scan_skips_non_wav_and_sorts() {
        let dir = tmp_dir("sort");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("Zed.wav"), b"a").unwrap();
        std::fs::write(dir.join("alpha.wav"), b"bb").unwrap();
        std::fs::write(dir.join("notes.txt"), b"ccc").unwrap();
        let listed = scan(&dir).unwrap();
        let names: Vec<_> = listed.iter().map(|e| e.filename.as_str()).collect();
        assert_eq!(names, vec!["alpha.wav", "Zed.wav"]);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
