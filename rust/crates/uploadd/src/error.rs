//! Typed error boundary for the upload core ([`SPEC.md`] §7: no `unwrap`/`panic`
//! in library code; errors are `thiserror` types at the seams).
//!
//! [`SPEC.md`]: ../../../../docs/specs/SPEC.md

use thiserror::Error;

/// Error reading the Pi-side archive source ([`crate::source`]).
#[derive(Debug, Error)]
pub enum SourceError {
    /// A path was rejected because it does not resolve under the configured
    /// archive root — the invariant guard that makes the live car LUN
    /// unreachable by construction.
    #[error("path is outside the archive root: {0}")]
    OutsideArchiveRoot(String),
    /// The underlying read failed (missing file, I/O error, truncated read).
    #[error("archive read failed: {0}")]
    Io(String),
}

/// Error raised by the transfer backend ([`crate::transfer`]).
#[derive(Debug, Error)]
pub enum TransferError {
    /// A chunk failed to transmit (network/backend error). The byte offset is
    /// retained so the queue can resume.
    #[error("chunk transfer failed at offset {offset}: {reason}")]
    Chunk {
        /// Byte offset at which the failure occurred.
        offset: u64,
        /// Human-readable reason for diagnostics.
        reason: String,
    },
    /// Finalizing the remote object failed before a digest could be confirmed.
    #[error("finalize failed: {0}")]
    Finalize(String),
}

/// Error talking to the `indexd` RPC seams (lease / queue / durability).
#[derive(Debug, Error)]
#[error("index rpc `{op}` failed: {reason}")]
pub struct IndexError {
    /// The logical operation that failed (e.g. `acquire_lease`, `checkpoint`).
    pub op: &'static str,
    /// Human-readable reason for diagnostics.
    pub reason: String,
}

impl IndexError {
    /// Construct an [`IndexError`] for operation `op`.
    #[must_use]
    pub fn new(op: &'static str, reason: impl Into<String>) -> Self {
        Self {
            op,
            reason: reason.into(),
        }
    }
}

/// An **infrastructure** failure while processing an item — a queue-store or
/// durability RPC error. Transfer/integrity/lease failures are deliberately
/// **not** modeled here: those are normal, retryable outcomes the durable queue
/// handles ([`crate::engine::StepOutcome`]), not errors. Only a failure to
/// durably record state (which would risk losing progress or double-work)
/// surfaces as an `EngineError` for the live loop to back off on.
#[derive(Debug, Error)]
pub enum EngineError {
    /// An `indexd` RPC (queue persist / durability flag) failed.
    #[error(transparent)]
    Index(#[from] IndexError),
}
