#[cfg(unix)]
use std::fs::File;
#[cfg(unix)]
use std::os::unix::fs::FileExt;
use std::path::Path;

use scannerd::reader::{BlockReader, ReaderError};

/// Stable identity of the opened backing image, used to detect a
/// re-provisioned/replaced image while the daemon stays alive (so cached
/// volume geometry is never combined with a freshly-recreated image).
#[cfg(unix)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ImageIdentity {
    pub(crate) dev: u64,
    pub(crate) ino: u64,
    pub(crate) size: u64,
    pub(crate) mtime: i64,
    pub(crate) mtime_nsec: i64,
}

#[cfg(unix)]
#[derive(Debug)]
pub(crate) struct PreadBlockReader {
    file: File,
    size: u64,
}

#[cfg(unix)]
impl PreadBlockReader {
    pub(crate) fn open(path: &Path) -> std::io::Result<Self> {
        let file = File::open(path)?;
        let size = file.metadata()?.len();
        Ok(Self { file, size })
    }

    /// Identity of the currently-open image (read from the open descriptor,
    /// so it reflects exactly the file these reads will hit — no TOCTOU re-stat).
    pub(crate) fn image_identity(&self) -> std::io::Result<ImageIdentity> {
        use std::os::unix::fs::MetadataExt;
        let m = self.file.metadata()?;
        Ok(ImageIdentity {
            dev: m.dev(),
            ino: m.ino(),
            size: m.size(),
            mtime: m.mtime(),
            mtime_nsec: m.mtime_nsec(),
        })
    }
}

#[cfg(unix)]
impl BlockReader for PreadBlockReader {
    fn size_bytes(&self) -> u64 {
        self.size
    }

    fn read_exact_at(&self, offset: u64, buf: &mut [u8]) -> Result<(), ReaderError> {
        let len = buf.len();
        let end = offset
            .checked_add(u64::try_from(len).unwrap_or(u64::MAX))
            .ok_or(ReaderError::OutOfRange {
                offset,
                len,
                size: self.size,
            })?;
        if end > self.size {
            return Err(ReaderError::OutOfRange {
                offset,
                len,
                size: self.size,
            });
        }
        self.file
            .read_exact_at(buf, offset)
            .map_err(|err| ReaderError::Io {
                offset,
                len,
                source_msg: err.to_string(),
            })
    }
}

#[cfg(not(unix))]
#[derive(Debug)]
pub(crate) struct PreadBlockReader;

#[cfg(not(unix))]
impl PreadBlockReader {
    pub(crate) fn open(_path: &Path) -> std::io::Result<Self> {
        Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "pread block reader requires unix",
        ))
    }
}

#[cfg(not(unix))]
impl BlockReader for PreadBlockReader {
    fn size_bytes(&self) -> u64 {
        0
    }

    fn read_exact_at(&self, offset: u64, buf: &mut [u8]) -> Result<(), ReaderError> {
        Err(ReaderError::Io {
            offset,
            len: buf.len(),
            source_msg: "pread block reader requires unix".to_owned(),
        })
    }
}
