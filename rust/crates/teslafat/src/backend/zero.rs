//! [`BlockBackend`] implementations local to the `teslafat`
//! daemon.
//!
//! ## Phase 1.6 state
//!
//! [`ZeroBackend`] is the placeholder backend used by the daemon
//! and by the Phase 1.7 smoke test. It satisfies the
//! [`BlockBackend`] contract without touching disk: `read` fills
//! the caller's buffer with zeros, `write` succeeds without
//! storing anything, `flush` is a no-op. Bounds are enforced via
//! the shared [`teslausb_core::backend::check_bounds`] helper, so
//! out-of-range requests return `BackendError::OutOfBounds`
//! exactly as `FileBackend` will.
//!
//! ## Phase 2+ plan
//!
//! `FileBackend` (the real FAT/exFAT synthesiser backed by a
//! regular file) lands here in Phase 2.1 and replaces
//! `ZeroBackend` at the `main.rs` call site. `ZeroBackend` stays
//! around as a debugging mode and as the smoke-test fixture; it
//! holds zero state and allocates zero bytes, so the cost of
//! keeping it is nil.
//!
//! ## Why not extend `teslausb_core::backend::mock`?
//!
//! `NullBackend` in `teslausb_core::backend::mock` allocates a
//! `Vec<u8>` of `size` bytes at construction. That's fine for a
//! unit test backing a 4 KiB export, but the daemon's export is
//! sized in gibibytes — a real allocation would OOM the Pi Zero
//! 2 W. `ZeroBackend` is sparse-by-construction: it stores only
//! the declared `size` and synthesises reads on demand. Keeping
//! it inside `teslafat` also means a `cargo build -p teslausb-core`
//! consumer never accidentally picks up the placeholder.

use teslausb_core::backend::{BackendResult, BlockBackend, WriteFlags, check_bounds};

/// Sparse, zero-allocation [`BlockBackend`] that returns zeros for
/// every in-range read, accepts every in-range write as a no-op,
/// and treats `flush` as a no-op.
///
/// Used by the Phase 1.6 daemon while the real FAT/exFAT synthesiser
/// is still being built (Phase 2), and by the Phase 1.7 smoke test
/// to verify that the wire path between `nbd-client` and `teslafat`
/// is correct end-to-end.
#[derive(Debug, Clone, Copy)]
pub struct ZeroBackend {
    size: u64,
}

impl ZeroBackend {
    /// Construct a `ZeroBackend` that reports `size` bytes of
    /// addressable space.
    #[must_use]
    pub const fn new(size: u64) -> Self {
        Self { size }
    }
}

impl BlockBackend for ZeroBackend {
    fn size(&self) -> u64 {
        self.size
    }

    async fn read(&self, offset: u64, buf: &mut [u8]) -> BackendResult<()> {
        check_bounds(offset, buf.len(), self.size)?;
        buf.fill(0);
        Ok(())
    }

    async fn write(&self, offset: u64, buf: &[u8], _flags: WriteFlags) -> BackendResult<()> {
        check_bounds(offset, buf.len(), self.size)?;
        Ok(())
    }

    async fn flush(&self) -> BackendResult<()> {
        Ok(())
    }
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::panic
)]
mod tests {
    use super::*;
    use teslausb_core::backend::BackendError;

    /// `size()` reports the value passed to `new()` unchanged.
    /// Catches a swap with a hardcoded constant or a unit
    /// conversion bug.
    #[test]
    fn size_returns_constructor_value() {
        assert_eq!(ZeroBackend::new(0).size(), 0);
        assert_eq!(ZeroBackend::new(4096).size(), 4096);
        assert_eq!(ZeroBackend::new(u64::MAX).size(), u64::MAX);
    }

    /// Read into a non-zero-initialised buffer; assert every byte
    /// is zero on return. The pre-fill with `0xAB` catches a bug
    /// where `read` accidentally became a no-op (leaving the
    /// caller's stale bytes in place).
    #[tokio::test]
    async fn read_fills_buffer_with_zeros() {
        let backend = ZeroBackend::new(4096);
        let mut buf = [0xABu8; 64];
        backend.read(0, &mut buf).await.unwrap();
        assert!(
            buf.iter().all(|&b| b == 0),
            "read should have zeroed the buffer, got: {buf:?}",
        );
    }

    /// Read at a non-zero offset still returns zeros (offset is
    /// not used as an index into anything since the backend is
    /// stateless). Catches a misuse of `offset` as a buffer
    /// starting position.
    #[tokio::test]
    async fn read_at_nonzero_offset_returns_zeros() {
        let backend = ZeroBackend::new(1024);
        let mut buf = [0xCDu8; 32];
        backend.read(512, &mut buf).await.unwrap();
        assert!(buf.iter().all(|&b| b == 0));
    }

    /// Read of a zero-length slice succeeds and does nothing.
    #[tokio::test]
    async fn read_of_zero_length_succeeds() {
        let backend = ZeroBackend::new(1024);
        let mut buf: [u8; 0] = [];
        backend.read(0, &mut buf).await.unwrap();
    }

    /// Read that would extend past `size` returns
    /// `BackendError::OutOfBounds` with the correct fields. Pins
    /// that bounds-checking actually runs and uses the right
    /// `size` value.
    #[tokio::test]
    async fn read_past_end_returns_out_of_bounds() {
        let backend = ZeroBackend::new(100);
        let mut buf = [0u8; 50];
        let err = backend.read(75, &mut buf).await.unwrap_err();
        match err {
            BackendError::OutOfBounds { offset, len, size } => {
                assert_eq!(offset, 75);
                assert_eq!(len, 50);
                assert_eq!(size, 100);
            }
            other => panic!("expected OutOfBounds, got {other:?}"),
        }
    }

    /// Read whose `offset + len` would overflow `u64` returns
    /// `OutOfBounds` rather than panicking on the arithmetic.
    #[tokio::test]
    async fn read_overflowing_arithmetic_returns_out_of_bounds() {
        let backend = ZeroBackend::new(1024);
        let mut buf = [0u8; 32];
        let err = backend.read(u64::MAX - 4, &mut buf).await.unwrap_err();
        assert!(matches!(err, BackendError::OutOfBounds { .. }));
    }

    /// Write succeeds and does NOT mutate any externally
    /// observable state. Verified indirectly: a subsequent read
    /// at the same offset still returns zeros (not the written
    /// pattern).
    #[tokio::test]
    async fn write_succeeds_and_does_not_alter_subsequent_reads() {
        let backend = ZeroBackend::new(4096);
        let payload = [0xFFu8; 64];
        backend
            .write(128, &payload, WriteFlags::NONE)
            .await
            .unwrap();
        let mut read_back = [0xAAu8; 64];
        backend.read(128, &mut read_back).await.unwrap();
        assert!(
            read_back.iter().all(|&b| b == 0),
            "ZeroBackend write must not persist data; got {read_back:?}",
        );
    }

    /// Write with FUA also succeeds and does not store. FUA is
    /// just an instruction to make a hypothetical persisted write
    /// durable; with no storage there is nothing to make durable.
    #[tokio::test]
    async fn write_with_fua_succeeds() {
        let backend = ZeroBackend::new(4096);
        let payload = [0x55u8; 32];
        backend.write(0, &payload, WriteFlags::FUA).await.unwrap();
    }

    /// Out-of-bounds write returns OutOfBounds, same as
    /// out-of-bounds read. Verifies the bounds check is wired
    /// into the write path too (and not silently skipped because
    /// the no-op happens to be safe regardless).
    #[tokio::test]
    async fn write_past_end_returns_out_of_bounds() {
        let backend = ZeroBackend::new(100);
        let payload = [0u8; 50];
        let err = backend
            .write(80, &payload, WriteFlags::NONE)
            .await
            .unwrap_err();
        match err {
            BackendError::OutOfBounds { offset, len, size } => {
                assert_eq!(offset, 80);
                assert_eq!(len, 50);
                assert_eq!(size, 100);
            }
            other => panic!("expected OutOfBounds, got {other:?}"),
        }
    }

    /// `flush` always succeeds (there is nothing to make durable).
    #[tokio::test]
    async fn flush_is_noop_success() {
        let backend = ZeroBackend::new(4096);
        backend.flush().await.unwrap();
    }

    /// `ZeroBackend` is `Copy` (it's just a `u64`), so passing it
    /// by value into the server's `serve` is allocation-free.
    /// This is the load-bearing property — if someone adds an
    /// internal `Vec` or `Mutex` to the struct the daemon's
    /// memory footprint changes silently.
    #[test]
    fn zero_backend_is_pod_sized() {
        assert_eq!(std::mem::size_of::<ZeroBackend>(), 8);
    }
}
