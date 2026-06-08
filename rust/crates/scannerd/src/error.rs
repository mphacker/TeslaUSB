//! Crate-level error type.

use crate::reader::ReaderError;

/// Top-level errors from the raw reader / traversal layer.
#[derive(Debug, thiserror::Error)]
pub enum ScannerError {
    /// A read through the [`BlockReader`](crate::reader::BlockReader)
    /// failed.
    #[error("read error: {0}")]
    Reader(#[from] ReaderError),

    /// The MBR was malformed (bad signature, no exFAT partition).
    #[error("MBR parse error: {0}")]
    Mbr(&'static str),

    /// The exFAT boot sector / BPB was malformed.
    #[error("exFAT boot sector parse error: {0}")]
    BootSector(&'static str),

    /// A cluster number was outside the valid `2..=cluster_count+1`
    /// range, or cluster/offset arithmetic overflowed. Indicates a
    /// torn or adversarial on-disk structure.
    #[error("invalid cluster {cluster}: {reason}")]
    InvalidCluster {
        /// The offending cluster number.
        cluster: u32,
        /// Why it was rejected.
        reason: &'static str,
    },

    /// A FAT chain exceeded its bound (cycle, or longer than the
    /// file's declared length permits).
    #[error("FAT chain error from cluster {first}: {reason}")]
    ChainError {
        /// First cluster of the chain.
        first: u32,
        /// Why traversal aborted.
        reason: &'static str,
    },

    /// A traversal safety cap (recursion depth, entry count, directory
    /// count) was exceeded.
    #[error("traversal limit exceeded: {0}")]
    LimitExceeded(&'static str),
}
