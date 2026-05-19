//! Block-device backend abstraction for the `teslafat` NBD daemon.
//!
//! The [`BlockBackend`] trait is the dependency-inversion seam
//! between the NBD transmission loop (Phase 1.5) and the concrete
//! storage backing the export — initially a single regular file on
//! disk, eventually a striped layout or test stub. Keeping the
//! trait in `teslausb-core` lets the IPC vocabulary, the NBD wire
//! protocol, and the backend contract all be unit-tested in
//! isolation without pulling in a tokio runtime or touching the
//! filesystem.
//!
//! ## Native `async fn in trait`
//!
//! [`BlockBackend`] uses native `async fn` in trait position
//! (stabilised in Rust 1.75) rather than the `async-trait` crate.
//! The deliberate tradeoff: callers must dispatch statically
//! (i.e. `impl BlockBackend` or generic `<B: BlockBackend>`); there
//! is no `dyn BlockBackend`. For `teslafat` that is exactly what
//! we want — the backend is chosen once at startup from the
//! config and the transmission loop is generic over the chosen
//! type, mirroring the `<S: AsyncRead + AsyncWrite + Unpin>`
//! parameterisation used by the NBD handshake. The crate-wide
//! `async_fn_in_trait` lint is acknowledged at the trait
//! declaration; we are accepting the dyn-dispatch restriction in
//! exchange for not depending on `async-trait` (which would
//! re-introduce a `Box<dyn Future>` allocation per call and add a
//! transitive dep this domain crate is supposed to avoid).
//!
//! ## FUA contract
//!
//! The Forced Unit Access flag on [`BlockBackend::write`] mirrors
//! the NBD newstyle wire flag of the same name: a `write` issued
//! with [`WriteFlags::FUA`] must be durable on the underlying
//! medium before the future resolves — i.e. the implementation
//! must perform the equivalent of `fdatasync` (or stronger) for
//! the byte range covered by `buf` before returning `Ok(())`. A
//! plain `write` may rely on a subsequent [`BlockBackend::flush`]
//! for durability. This contract is verified by the
//! `fua_contract_*` unit tests below, which observe a
//! [`mock::MockBackend`] for an FUA-tagged write or a flush after
//! the operation completes.
//!
//! No `unsafe` is used anywhere in this module (workspace lint
//! `unsafe_code = "deny"` applies).

use core::fmt;

/// Wire-equivalent flags accepted by [`BlockBackend::write`].
///
/// Modelled as a newtype around `u32` instead of pulling in the
/// `bitflags` crate — the Phase 1.4 surface area is a single flag
/// (`FUA`) and the bitwise plumbing is a few lines. If a third
/// flag appears we will revisit and likely adopt `bitflags`.
///
/// The bit pattern intentionally matches the NBD newstyle command
/// flag `NBD_CMD_FLAG_FUA = 1 << 0` so the transmission loop can
/// translate the wire flags directly with no per-flag mapping
/// table.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct WriteFlags(u32);

impl WriteFlags {
    /// All-zero flag set — equivalent to a plain `write` with no
    /// durability requirement until a subsequent
    /// [`BlockBackend::flush`].
    pub const NONE: Self = Self(0);

    /// Forced Unit Access: the data covered by the write **must**
    /// be durable on the underlying medium before the future
    /// resolves. See the module-level "FUA contract" section.
    pub const FUA: Self = Self(1 << 0);

    /// Mask of every flag this module recognises. New flags must
    /// be `OR`-ed in here when added so [`Self::from_bits_truncate`]
    /// keeps round-tripping.
    const ALL_KNOWN_BITS: u32 = Self::FUA.0;

    /// Returns `true` if `self` contains every bit set in `other`.
    ///
    /// Empty `other` (i.e. [`Self::NONE`]) always returns `true`,
    /// matching the convention used by every popular bitflags
    /// crate.
    #[must_use]
    pub const fn contains(self, other: Self) -> bool {
        (self.0 & other.0) == other.0
    }

    /// Returns `true` if every bit in `self` is zero.
    #[must_use]
    pub const fn is_empty(self) -> bool {
        self.0 == 0
    }

    /// Raw bit pattern, suitable for forwarding to the NBD wire.
    #[must_use]
    pub const fn bits(self) -> u32 {
        self.0
    }

    /// Build a [`WriteFlags`] from a raw bit pattern, silently
    /// dropping any unknown bits.
    ///
    /// The "truncate" variant is the right choice for forward
    /// compatibility with future NBD command flags we have not
    /// yet implemented — an unknown bit is treated as if it were
    /// absent rather than rejected, mirroring the forward-compat
    /// stance the IPC envelope takes for unknown fields.
    #[must_use]
    pub const fn from_bits_truncate(bits: u32) -> Self {
        Self(bits & Self::ALL_KNOWN_BITS)
    }
}

impl core::ops::BitOr for WriteFlags {
    type Output = Self;

    fn bitor(self, rhs: Self) -> Self {
        Self(self.0 | rhs.0)
    }
}

impl core::ops::BitOrAssign for WriteFlags {
    fn bitor_assign(&mut self, rhs: Self) {
        self.0 |= rhs.0;
    }
}

impl core::ops::BitAnd for WriteFlags {
    type Output = Self;

    fn bitand(self, rhs: Self) -> Self {
        Self(self.0 & rhs.0)
    }
}

impl fmt::Display for WriteFlags {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.is_empty() {
            return f.write_str("NONE");
        }
        if self.contains(Self::FUA) {
            f.write_str("FUA")?;
        }
        Ok(())
    }
}

/// Errors returned by [`BlockBackend`] methods.
///
/// Per charter §"Rust standards" — typed errors at the library
/// boundary via `thiserror`. The transmission loop in `teslafat`
/// (an `anyhow`-using binary) is free to wrap these with
/// `.context(...)` for the NBD reply path.
#[derive(Debug, thiserror::Error)]
pub enum BackendError {
    /// The underlying I/O layer reported an error. The concrete
    /// `std::io::Error` is preserved verbatim so callers can
    /// inspect `kind()` (for example to map `UnexpectedEof` onto a
    /// short-read condition).
    #[error("backend I/O error: {0}")]
    Io(#[from] std::io::Error),

    /// The requested operation would access bytes outside the
    /// backend's reported [`BlockBackend::size`].
    ///
    /// `len` is the byte count of the operation, not its end
    /// offset; computing `offset + len` could overflow `u64` so
    /// callers and the trait contract both treat the pair as the
    /// authoritative description.
    #[error("offset {offset} + len {len} exceeds backend size {size}")]
    OutOfBounds {
        /// First byte the operation tried to touch.
        offset: u64,
        /// Number of bytes the operation tried to touch.
        len: u64,
        /// Reported backend size at the time of the check.
        size: u64,
    },

    /// The caller passed an argument that violates the trait
    /// contract independently of the backend state (for example a
    /// `buf.len()` that does not fit in `u64`).
    #[error("invalid backend argument: {0}")]
    InvalidArgument(&'static str),
}

/// Result alias used throughout this module.
pub type BackendResult<T> = core::result::Result<T, BackendError>;

/// Read / write contract for the storage backing the NBD export.
///
/// Implementors are responsible for bounds-checking every
/// `(offset, length)` pair against [`Self::size`] before touching
/// the underlying medium. Three behavioural rules apply:
///
/// 1. `read` fills `buf` exactly. A short read is an error, not
///    a normal return — the NBD wire protocol has no notion of
///    "partial read", and a backend that cannot satisfy the
///    requested range must surface [`BackendError::Io`] (or
///    [`BackendError::OutOfBounds`] if the caller's request was
///    out of range to begin with).
/// 2. `write` writes `buf` exactly with the same all-or-nothing
///    semantics. If `flags` contains [`WriteFlags::FUA`] the
///    bytes must be durable on the medium before the future
///    resolves; see the module-level "FUA contract" section.
/// 3. `flush` issues an `fdatasync`-equivalent for **all**
///    outstanding writes that have not yet been forced durable.
///
/// # Threading
///
/// The trait does not require `Send + Sync` itself — the
/// transmission loop is single-threaded on the current-thread
/// tokio runtime locked in ADR-0003 — but concrete implementors
/// that wrap a `std::fs::File` will naturally be `Send + Sync`
/// and the trait does not preclude that.
#[allow(async_fn_in_trait)] // see module-level "Native async fn in trait" note
pub trait BlockBackend {
    /// Total size of the backend in bytes. Must be a constant for
    /// the lifetime of `self` — NBD clients are told the export
    /// size during the handshake and assume it never changes.
    fn size(&self) -> u64;

    /// Read exactly `buf.len()` bytes starting at `offset` into
    /// `buf`.
    ///
    /// # Errors
    ///
    /// * [`BackendError::OutOfBounds`] if `offset + buf.len()`
    ///   would exceed [`Self::size`].
    /// * [`BackendError::InvalidArgument`] if `buf.len()` does
    ///   not fit in `u64`.
    /// * [`BackendError::Io`] for any error from the underlying
    ///   medium, including a short read.
    async fn read(&self, offset: u64, buf: &mut [u8]) -> BackendResult<()>;

    /// Write exactly `buf.len()` bytes starting at `offset` from
    /// `buf`.
    ///
    /// If `flags` contains [`WriteFlags::FUA`] the bytes must be
    /// durable on the underlying medium before the future
    /// resolves. Plain writes (`flags == WriteFlags::NONE`) may
    /// rely on a subsequent [`Self::flush`] for durability.
    ///
    /// # Errors
    ///
    /// * [`BackendError::OutOfBounds`] if `offset + buf.len()`
    ///   would exceed [`Self::size`].
    /// * [`BackendError::InvalidArgument`] if `buf.len()` does
    ///   not fit in `u64`.
    /// * [`BackendError::Io`] for any error from the underlying
    ///   medium, including a short write or an FUA flush failure.
    async fn write(&self, offset: u64, buf: &[u8], flags: WriteFlags) -> BackendResult<()>;

    /// Issue an `fdatasync`-equivalent for **all** outstanding
    /// writes that have not yet been forced durable by an
    /// FUA-flagged write.
    ///
    /// # Errors
    ///
    /// * [`BackendError::Io`] if the underlying medium rejects
    ///   the sync.
    async fn flush(&self) -> BackendResult<()>;
}

/// Helper that returns [`BackendError::OutOfBounds`] when the
/// `(offset, len)` pair would access bytes past `size`.
///
/// Exposed at module scope so concrete impls (e.g. the
/// file-backed backend in Phase 1.5) can share the overflow-safe
/// bounds-check logic with the [`mock::NullBackend`] /
/// [`mock::MockBackend`] reference implementations below.
///
/// # Errors
///
/// * [`BackendError::InvalidArgument`] if `len` does not fit in
///   `u64`.
/// * [`BackendError::OutOfBounds`] if `offset + len > size`.
pub fn check_bounds(offset: u64, len: usize, size: u64) -> BackendResult<()> {
    let len_u64 = u64::try_from(len)
        .map_err(|_| BackendError::InvalidArgument("buffer length does not fit in u64"))?;
    let end = offset
        .checked_add(len_u64)
        .ok_or(BackendError::OutOfBounds {
            offset,
            len: len_u64,
            size,
        })?;
    if end > size {
        return Err(BackendError::OutOfBounds {
            offset,
            len: len_u64,
            size,
        });
    }
    Ok(())
}

/// Reference [`BlockBackend`] implementations for testing and
/// development.
///
/// Both are always compiled (i.e. not behind a `mock` feature) —
/// the types are tiny and `cargo build --release` will dead-strip
/// any references the production binary does not make. Gating
/// behind a feature would force every dev-build site that wants
/// to use them to remember the flag, which the inc-1.4 charter
/// review noted as friction worth more than the byte savings.
pub mod mock {
    use super::{BackendError, BackendResult, BlockBackend, WriteFlags, check_bounds};
    use std::sync::{Mutex, PoisonError};

    /// Backend backed entirely by an in-memory `Vec<u8>`.
    ///
    /// Useful as a degenerate stand-in for the production
    /// file-backed backend in Phase 1.5 — every read and write
    /// touches an internally-mutable buffer and `flush` is a
    /// no-op. Does **not** record operations; for that use
    /// [`MockBackend`].
    pub struct NullBackend {
        bytes: Mutex<Vec<u8>>,
    }

    impl NullBackend {
        /// Construct a `NullBackend` of `size` zero-filled bytes.
        #[must_use]
        pub fn new(size: usize) -> Self {
            Self {
                bytes: Mutex::new(vec![0_u8; size]),
            }
        }

        /// Snapshot of the current contents for test assertions.
        #[must_use]
        pub fn snapshot(&self) -> Vec<u8> {
            self.bytes
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .clone()
        }
    }

    impl BlockBackend for NullBackend {
        fn size(&self) -> u64 {
            let guard = self.bytes.lock().unwrap_or_else(PoisonError::into_inner);
            u64::try_from(guard.len()).unwrap_or(u64::MAX)
        }

        async fn read(&self, offset: u64, buf: &mut [u8]) -> BackendResult<()> {
            let size = self.size();
            check_bounds(offset, buf.len(), size)?;
            let guard = self.bytes.lock().unwrap_or_else(PoisonError::into_inner);
            let start = usize::try_from(offset)
                .map_err(|_| BackendError::InvalidArgument("offset exceeds usize"))?;
            let end = start
                .checked_add(buf.len())
                .ok_or(BackendError::InvalidArgument("offset + len exceeds usize"))?;
            let src = guard
                .get(start..end)
                .ok_or(BackendError::InvalidArgument("internal bounds mismatch"))?;
            buf.copy_from_slice(src);
            Ok(())
        }

        async fn write(&self, offset: u64, buf: &[u8], _flags: WriteFlags) -> BackendResult<()> {
            let size = self.size();
            check_bounds(offset, buf.len(), size)?;
            let mut guard = self.bytes.lock().unwrap_or_else(PoisonError::into_inner);
            let start = usize::try_from(offset)
                .map_err(|_| BackendError::InvalidArgument("offset exceeds usize"))?;
            let end = start
                .checked_add(buf.len())
                .ok_or(BackendError::InvalidArgument("offset + len exceeds usize"))?;
            let dst = guard
                .get_mut(start..end)
                .ok_or(BackendError::InvalidArgument("internal bounds mismatch"))?;
            dst.copy_from_slice(buf);
            Ok(())
        }

        async fn flush(&self) -> BackendResult<()> {
            Ok(())
        }
    }

    /// Records every backend call and the [`WriteFlags`] the
    /// caller passed. Used by the FUA contract tests to assert
    /// that the transmission loop (and any other [`BlockBackend`]
    /// caller) honours the flag-to-sync mapping.
    ///
    /// Storage is identical to [`NullBackend`] (in-memory
    /// `Vec<u8>`); the only addition is the observable call log.
    pub struct MockBackend {
        bytes: Mutex<Vec<u8>>,
        ops: Mutex<Vec<MockOp>>,
    }

    /// Single recorded call against a [`MockBackend`].
    #[derive(Clone, Debug, PartialEq, Eq)]
    pub enum MockOp {
        /// `read(offset, len)`.
        Read {
            /// Starting offset of the read.
            offset: u64,
            /// Length of the read in bytes.
            len: usize,
        },
        /// `write(offset, len, flags)`.
        Write {
            /// Starting offset of the write.
            offset: u64,
            /// Length of the write in bytes.
            len: usize,
            /// Flags the caller passed (e.g. `FUA`).
            flags: WriteFlags,
        },
        /// `flush()`.
        Flush,
    }

    impl MockBackend {
        /// Construct a `MockBackend` of `size` zero-filled bytes.
        #[must_use]
        pub fn new(size: usize) -> Self {
            Self {
                bytes: Mutex::new(vec![0_u8; size]),
                ops: Mutex::new(Vec::new()),
            }
        }

        /// Snapshot the operation log for assertions.
        #[must_use]
        pub fn ops(&self) -> Vec<MockOp> {
            self.ops
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .clone()
        }

        /// Snapshot the current contents for assertions.
        #[must_use]
        pub fn snapshot(&self) -> Vec<u8> {
            self.bytes
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .clone()
        }

        /// Returns `true` if any recorded write contained
        /// [`WriteFlags::FUA`] **or** any [`MockOp::Flush`] was
        /// recorded. This is the durability-observation hook the
        /// FUA contract test asserts against.
        #[must_use]
        pub fn observed_any_durability(&self) -> bool {
            self.ops().iter().any(|op| match op {
                MockOp::Write { flags, .. } => flags.contains(WriteFlags::FUA),
                MockOp::Flush => true,
                MockOp::Read { .. } => false,
            })
        }

        fn push_op(&self, op: MockOp) {
            self.ops
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .push(op);
        }
    }

    impl BlockBackend for MockBackend {
        fn size(&self) -> u64 {
            let guard = self.bytes.lock().unwrap_or_else(PoisonError::into_inner);
            u64::try_from(guard.len()).unwrap_or(u64::MAX)
        }

        async fn read(&self, offset: u64, buf: &mut [u8]) -> BackendResult<()> {
            let size = self.size();
            check_bounds(offset, buf.len(), size)?;
            self.push_op(MockOp::Read {
                offset,
                len: buf.len(),
            });
            let guard = self.bytes.lock().unwrap_or_else(PoisonError::into_inner);
            let start = usize::try_from(offset)
                .map_err(|_| BackendError::InvalidArgument("offset exceeds usize"))?;
            let end = start
                .checked_add(buf.len())
                .ok_or(BackendError::InvalidArgument("offset + len exceeds usize"))?;
            let src = guard
                .get(start..end)
                .ok_or(BackendError::InvalidArgument("internal bounds mismatch"))?;
            buf.copy_from_slice(src);
            Ok(())
        }

        async fn write(&self, offset: u64, buf: &[u8], flags: WriteFlags) -> BackendResult<()> {
            let size = self.size();
            check_bounds(offset, buf.len(), size)?;
            self.push_op(MockOp::Write {
                offset,
                len: buf.len(),
                flags,
            });
            let mut guard = self.bytes.lock().unwrap_or_else(PoisonError::into_inner);
            let start = usize::try_from(offset)
                .map_err(|_| BackendError::InvalidArgument("offset exceeds usize"))?;
            let end = start
                .checked_add(buf.len())
                .ok_or(BackendError::InvalidArgument("offset + len exceeds usize"))?;
            let dst = guard
                .get_mut(start..end)
                .ok_or(BackendError::InvalidArgument("internal bounds mismatch"))?;
            dst.copy_from_slice(buf);
            Ok(())
        }

        async fn flush(&self) -> BackendResult<()> {
            self.push_op(MockOp::Flush);
            Ok(())
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::mock::{MockBackend, MockOp, NullBackend};
    use super::{BackendError, BlockBackend, WriteFlags, check_bounds};
    use pollster::block_on;

    #[test]
    fn write_flags_none_is_empty() {
        assert!(WriteFlags::NONE.is_empty());
        assert_eq!(WriteFlags::NONE.bits(), 0);
    }

    #[test]
    fn write_flags_fua_is_bit_zero() {
        assert_eq!(WriteFlags::FUA.bits(), 1);
        assert!(WriteFlags::FUA.contains(WriteFlags::FUA));
        assert!(WriteFlags::FUA.contains(WriteFlags::NONE));
        assert!(!WriteFlags::NONE.contains(WriteFlags::FUA));
    }

    #[test]
    fn write_flags_bitor_combines() {
        let combined = WriteFlags::NONE | WriteFlags::FUA;
        assert_eq!(combined, WriteFlags::FUA);
        let mut acc = WriteFlags::NONE;
        acc |= WriteFlags::FUA;
        assert_eq!(acc, WriteFlags::FUA);
    }

    #[test]
    fn write_flags_bitand_masks() {
        let masked = WriteFlags::FUA & WriteFlags::FUA;
        assert_eq!(masked, WriteFlags::FUA);
        let masked_off = WriteFlags::FUA & WriteFlags::NONE;
        assert_eq!(masked_off, WriteFlags::NONE);
    }

    #[test]
    fn write_flags_from_bits_truncate_drops_unknown() {
        let raw = WriteFlags::from_bits_truncate(0xFFFF_FFFF);
        assert_eq!(raw, WriteFlags::FUA);
        assert_eq!(raw.bits(), WriteFlags::FUA.bits());
    }

    #[test]
    fn write_flags_display_none() {
        assert_eq!(format!("{}", WriteFlags::NONE), "NONE");
    }

    #[test]
    fn write_flags_display_fua() {
        assert_eq!(format!("{}", WriteFlags::FUA), "FUA");
    }

    #[test]
    fn write_flags_default_is_none() {
        let default: WriteFlags = WriteFlags::default();
        assert_eq!(default, WriteFlags::NONE);
    }

    #[test]
    fn check_bounds_accepts_in_range() {
        check_bounds(0, 4, 4).unwrap();
        check_bounds(2, 2, 4).unwrap();
        check_bounds(4, 0, 4).unwrap();
    }

    #[test]
    fn check_bounds_rejects_overflow_past_size() {
        let err = check_bounds(3, 2, 4).unwrap_err();
        assert!(
            matches!(
                err,
                BackendError::OutOfBounds {
                    offset: 3,
                    len: 2,
                    size: 4,
                }
            ),
            "expected OutOfBounds(offset=3, len=2, size=4), got {err:?}"
        );
    }

    #[test]
    fn check_bounds_rejects_u64_addition_overflow() {
        let err = check_bounds(u64::MAX, 1, u64::MAX).unwrap_err();
        assert!(matches!(err, BackendError::OutOfBounds { .. }));
    }

    #[test]
    fn null_backend_roundtrips_bytes() {
        let backend = NullBackend::new(16);
        assert_eq!(backend.size(), 16);
        block_on(backend.write(4, &[0xAA, 0xBB, 0xCC, 0xDD], WriteFlags::NONE)).unwrap();
        let mut out = [0_u8; 4];
        block_on(backend.read(4, &mut out)).unwrap();
        assert_eq!(out, [0xAA, 0xBB, 0xCC, 0xDD]);
        let snap = backend.snapshot();
        let written = snap.get(4..8).unwrap();
        assert_eq!(written, &[0xAA, 0xBB, 0xCC, 0xDD]);
    }

    #[test]
    fn null_backend_out_of_bounds_read_rejected() {
        let backend = NullBackend::new(4);
        let mut out = [0_u8; 8];
        let err = block_on(backend.read(0, &mut out)).unwrap_err();
        assert!(matches!(err, BackendError::OutOfBounds { .. }));
    }

    #[test]
    fn null_backend_out_of_bounds_write_rejected() {
        let backend = NullBackend::new(4);
        let err = block_on(backend.write(3, &[1_u8; 4], WriteFlags::NONE)).unwrap_err();
        assert!(matches!(err, BackendError::OutOfBounds { .. }));
    }

    #[test]
    fn null_backend_flush_is_noop() {
        let backend = NullBackend::new(4);
        block_on(backend.flush()).unwrap();
    }

    #[test]
    fn mock_backend_records_read() {
        let backend = MockBackend::new(8);
        let mut out = [0_u8; 4];
        block_on(backend.read(2, &mut out)).unwrap();
        let ops = backend.ops();
        assert_eq!(ops, vec![MockOp::Read { offset: 2, len: 4 }]);
    }

    #[test]
    fn mock_backend_records_write_with_flags() {
        let backend = MockBackend::new(8);
        block_on(backend.write(0, &[1, 2, 3, 4], WriteFlags::FUA)).unwrap();
        let ops = backend.ops();
        assert_eq!(
            ops,
            vec![MockOp::Write {
                offset: 0,
                len: 4,
                flags: WriteFlags::FUA,
            }]
        );
    }

    #[test]
    fn mock_backend_records_flush() {
        let backend = MockBackend::new(0);
        block_on(backend.flush()).unwrap();
        assert_eq!(backend.ops(), vec![MockOp::Flush]);
    }

    #[test]
    fn mock_backend_snapshot_reflects_writes() {
        let backend = MockBackend::new(4);
        block_on(backend.write(0, &[9, 9], WriteFlags::NONE)).unwrap();
        let snap = backend.snapshot();
        assert_eq!(snap, vec![9, 9, 0, 0]);
    }

    /// FUA contract part 1: a plain write does **not** by itself
    /// satisfy the durability observable.
    #[test]
    fn fua_contract_plain_write_is_not_durable_on_its_own() {
        let backend = MockBackend::new(8);
        block_on(backend.write(0, &[1, 2, 3, 4], WriteFlags::NONE)).unwrap();
        assert!(
            !backend.observed_any_durability(),
            "plain write must not be observable as durable until flush"
        );
    }

    /// FUA contract part 2: a write tagged with FUA **is** durable
    /// the moment the future resolves.
    #[test]
    fn fua_contract_fua_write_is_durable_immediately() {
        let backend = MockBackend::new(8);
        block_on(backend.write(0, &[1, 2, 3, 4], WriteFlags::FUA)).unwrap();
        assert!(
            backend.observed_any_durability(),
            "FUA write must be observable as durable on completion"
        );
    }

    /// FUA contract part 3: a subsequent flush after a plain
    /// write flips the durability observable to `true`.
    #[test]
    fn fua_contract_flush_after_plain_write_makes_durable() {
        let backend = MockBackend::new(8);
        block_on(backend.write(0, &[1, 2, 3, 4], WriteFlags::NONE)).unwrap();
        assert!(!backend.observed_any_durability());
        block_on(backend.flush()).unwrap();
        assert!(backend.observed_any_durability());
    }

    #[test]
    fn backend_error_display_mentions_offset_and_size() {
        let err = BackendError::OutOfBounds {
            offset: 100,
            len: 4,
            size: 64,
        };
        let s = format!("{err}");
        assert!(s.contains("100"));
        assert!(s.contains("64"));
        assert!(s.contains('4'));
    }

    #[test]
    fn backend_error_from_io_error_preserves_kind() {
        let io = std::io::Error::new(std::io::ErrorKind::UnexpectedEof, "short read");
        let err: BackendError = io.into();
        let kind = if let BackendError::Io(inner) = &err {
            Some(inner.kind())
        } else {
            None
        };
        assert_eq!(
            kind,
            Some(std::io::ErrorKind::UnexpectedEof),
            "expected Io(UnexpectedEof), got {err:?}"
        );
    }
}
