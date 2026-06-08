//! Block-level read abstraction.
//!
//! All traversal logic in this crate reads the backing image through
//! the [`BlockReader`] trait, never through `std::fs` directly. This
//! keeps the parsing/traversal/gating logic pure and host-testable
//! (tests use [`SliceReader`] over an in-memory image), while the real
//! syscalls (`pread`) live in the binary's reader implementation.
//!
//! The trait reads at **absolute byte offsets into the whole backing
//! image** (not partition-relative). Callers translate
//! partition/cluster coordinates to absolute offsets before reading.

use std::sync::Mutex;

/// Errors a [`BlockReader`] can return.
#[derive(Debug, thiserror::Error)]
pub enum ReaderError {
    /// The requested range `[offset, offset + len)` lies outside the
    /// backing image. A torn or adversarial on-disk structure that
    /// points past the end of the device lands here rather than
    /// panicking.
    #[error("read of {len} byte(s) at offset {offset} exceeds image size {size}")]
    OutOfRange {
        /// Absolute byte offset requested.
        offset: u64,
        /// Number of bytes requested.
        len: usize,
        /// Total backing image size in bytes.
        size: u64,
    },
    /// An underlying I/O error from the real device (the binary's
    /// `pread` implementation). Carried as a string because
    /// `std::io::Error` is neither `Clone` nor `PartialEq`, which the
    /// pure traversal layer wants for testing.
    #[error("i/o error reading {len} byte(s) at offset {offset}: {source_msg}")]
    Io {
        /// Absolute byte offset requested.
        offset: u64,
        /// Number of bytes requested.
        len: usize,
        /// Stringified underlying error.
        source_msg: String,
    },
}

/// Reads exact byte ranges from the backing image by absolute offset.
///
/// Implementations MUST fill `buf` completely or return an error; a
/// short read (fewer bytes than `buf.len()`) is an error, never a
/// silent truncation — the traversal layer relies on this to bound its
/// parsing.
pub trait BlockReader {
    /// Total size of the backing image in bytes.
    fn size_bytes(&self) -> u64;

    /// Read exactly `buf.len()` bytes starting at absolute byte
    /// `offset` into `buf`.
    ///
    /// # Errors
    ///
    /// * [`ReaderError::OutOfRange`] if the range exceeds the image.
    /// * [`ReaderError::Io`] on an underlying device error.
    fn read_exact_at(&self, offset: u64, buf: &mut [u8]) -> Result<(), ReaderError>;

    /// Convenience: allocate and read `len` bytes at `offset`.
    ///
    /// # Errors
    ///
    /// Same as [`BlockReader::read_exact_at`].
    fn read_vec_at(&self, offset: u64, len: usize) -> Result<Vec<u8>, ReaderError> {
        let mut buf = vec![0_u8; len];
        self.read_exact_at(offset, &mut buf)?;
        Ok(buf)
    }
}

/// An in-memory [`BlockReader`] over a byte slice, for tests and for
/// the property-test harness that mutates a raw image in place.
///
/// The image is wrapped in a [`Mutex`] so a test harness can mutate it
/// between scans through [`SliceReader::with_image`] while the reader
/// is shared.
#[derive(Debug)]
pub struct SliceReader {
    image: Mutex<Vec<u8>>,
}

impl SliceReader {
    /// Wrap `image` as a readable backing store.
    #[must_use]
    pub fn new(image: Vec<u8>) -> Self {
        Self {
            image: Mutex::new(image),
        }
    }

    /// Mutate the underlying image (simulating a concurrent writer in
    /// the stability property tests).
    ///
    /// # Panics
    ///
    /// Panics only if the internal mutex is poisoned, which cannot
    /// happen in single-threaded tests.
    #[cfg(test)]
    #[allow(clippy::expect_used)]
    pub fn with_image<F: FnOnce(&mut Vec<u8>)>(&self, f: F) {
        let mut guard = self.image.lock().expect("slice reader mutex poisoned");
        f(&mut guard);
    }
}

impl BlockReader for SliceReader {
    fn size_bytes(&self) -> u64 {
        // Lock is only contended in the test harness; map poisoning to
        // a zero size so a poisoned mutex degrades to OutOfRange rather
        // than panicking the traversal layer.
        self.image
            .lock()
            .map_or(0, |g| u64::try_from(g.len()).unwrap_or(u64::MAX))
    }

    fn read_exact_at(&self, offset: u64, buf: &mut [u8]) -> Result<(), ReaderError> {
        let Ok(guard) = self.image.lock() else {
            return Err(ReaderError::Io {
                offset,
                len: buf.len(),
                source_msg: "slice reader mutex poisoned".to_owned(),
            });
        };
        let size = u64::try_from(guard.len()).unwrap_or(u64::MAX);
        let end = offset
            .checked_add(buf.len() as u64)
            .filter(|e| *e <= size)
            .ok_or(ReaderError::OutOfRange {
                offset,
                len: buf.len(),
                size,
            })?;
        let start_usize = usize::try_from(offset).map_err(|_| ReaderError::OutOfRange {
            offset,
            len: buf.len(),
            size,
        })?;
        let end_usize = usize::try_from(end).map_err(|_| ReaderError::OutOfRange {
            offset,
            len: buf.len(),
            size,
        })?;
        let Some(src) = guard.get(start_usize..end_usize) else {
            return Err(ReaderError::OutOfRange {
                offset,
                len: buf.len(),
                size,
            });
        };
        buf.copy_from_slice(src);
        Ok(())
    }
}
