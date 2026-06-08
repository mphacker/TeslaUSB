//! Unix `pread`-backed [`BlockReader`] for the real device.
//!
//! Uses `std::os::unix::fs::FileExt::read_exact_at` (a positioned read
//! that does not move a file cursor and is safe to call concurrently),
//! so scannerd reads the backing image at absolute offsets without ever
//! mounting it.

use std::fs::File;
use std::os::unix::fs::FileExt;
use std::path::Path;

use scannerd::reader::{BlockReader, ReaderError};

/// A read-only positioned reader over the backing image file.
pub struct PreadReader {
    file: File,
    size: u64,
}

impl PreadReader {
    /// Open `path` read-only and record its size.
    ///
    /// # Errors
    ///
    /// Propagates any `std::io` error from opening or stat-ing.
    pub fn open(path: &Path) -> std::io::Result<Self> {
        let file = File::open(path)?;
        let size = file.metadata()?.len();
        Ok(Self { file, size })
    }
}

impl BlockReader for PreadReader {
    fn size_bytes(&self) -> u64 {
        self.size
    }

    fn read_exact_at(&self, offset: u64, buf: &mut [u8]) -> Result<(), ReaderError> {
        let len = buf.len();
        let end = offset
            .checked_add(len as u64)
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
            .map_err(|e| ReaderError::Io {
                offset,
                len,
                source_msg: e.to_string(),
            })
    }
}
