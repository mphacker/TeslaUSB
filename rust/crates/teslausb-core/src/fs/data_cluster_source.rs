//! Pluggable cluster-content source for FAT32 / exFAT synth.
//!
//! Phase 2.6 (FAT32) and Phase 2.11 (exFAT) introduced read
//! dispatchers that already know how to serve the boot, FAT,
//! and bitmap/upcase regions byte-perfectly from cached
//! buffers. They left the **data region** zero-filled because
//! a Phase-2 synth had no notion of "this cluster holds these
//! 32-byte directory entries" or "this cluster holds the first
//! 32 KiB of `/foo/bar.mp4`".
//!
//! [`DataClusterSource`] is the dependency injection point that
//! closes that gap. The synth holds an
//! `Option<Box<dyn DataClusterSource + Send + Sync>>`; when
//! present, the data-region read path consults the source for
//! each cluster's bytes instead of zero-filling.
//!
//! ## Why a separate trait (not a method on `ExfatLayout`)
//!
//! Three independent implementations are anticipated:
//!
//! 1. **`ExfatLayout`** — serves the pre-computed directory
//!    cluster bytes from a
//!    `BTreeMap<u32, Vec<u8>>`. File-content clusters fall
//!    through to zero-fill at this layer.
//! 2. **`DirTreeMaterializer`** (lives in
//!    `teslafat`) — wraps a layout and additionally opens the
//!    backing file for any cluster that maps to a file
//!    placement, serving real file bytes via `pread`-style
//!    reads.
//! 3. **`ZeroDataSource`** (test fixture) — explicitly fills
//!    with zeros for tests that want to assert "no
//!    materializer" behaviour without `Option::None` plumbing.
//!
//! The trait is FS-agnostic by design: the source contract
//! talks in bytes within a single cluster, not in FS-specific
//! entry kinds.
//!
//! ## Contract
//!
//! Implementors must:
//!
//! * Be `Send + Sync` so the synth (which is itself `Send +
//!   Sync`) can be shared across NBD handler threads.
//! * Treat any cluster outside their knowledge as a logical
//!   zero-fill: the synth's tests assume that an unknown
//!   cluster produces all-zero bytes (matching the pre-2.17
//!   contract).
//! * Honour `byte_in_cluster` strictly: the synth dispatcher
//!   only ever calls with `byte_in_cluster + out.len() <=
//!   bytes_per_cluster`, but a defensive implementation
//!   should clamp rather than panic.

/// Source of byte-level cluster content for the data region of
/// a synthesized FAT32 / exFAT volume.
///
/// See the module-level docs for the trait's role in the
/// pipeline and the contract implementors must uphold.
pub trait DataClusterSource: Send + Sync + core::fmt::Debug {
    /// Fill `out` with bytes from cluster number `cluster`,
    /// starting at offset `byte_in_cluster` within that
    /// cluster.
    ///
    /// `cluster` is a data-region cluster number (always
    /// ≥ [`crate::fs::cluster_layout::FIRST_DATA_CLUSTER`]).
    /// The caller (the synth dispatcher) guarantees:
    ///
    /// * `byte_in_cluster + out.len() <= bytes_per_cluster`
    /// * `out` is non-empty
    ///
    /// Implementations that don't know about `cluster` must
    /// write all zeros to `out` (the pre-2.17 zero-fill
    /// behaviour). This keeps the dispatcher path infallible —
    /// the trait method returns `()` rather than a `Result` to
    /// reflect that any cluster lookup is logically a hit (with
    /// "unknown" implicitly mapping to zeros).
    fn read_cluster_bytes(&self, cluster: u32, byte_in_cluster: usize, out: &mut [u8]);
}

/// Zero-filling source. Useful as an explicit "no
/// materializer" fixture in tests and as the documented
/// default behaviour the trait promises for unknown clusters.
#[derive(Debug, Default, Clone, Copy)]
pub struct ZeroDataSource;

impl DataClusterSource for ZeroDataSource {
    fn read_cluster_bytes(&self, _cluster: u32, _byte_in_cluster: usize, out: &mut [u8]) {
        out.fill(0);
    }
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

    #[test]
    fn zero_source_writes_all_zeros_regardless_of_cluster() {
        let src = ZeroDataSource;
        let mut buf = [0xAAu8; 32];
        src.read_cluster_bytes(2, 0, &mut buf);
        assert!(buf.iter().all(|&b| b == 0));
        let mut buf2 = [0xFFu8; 16];
        src.read_cluster_bytes(99_999_999, 7, &mut buf2);
        assert!(buf2.iter().all(|&b| b == 0));
    }

    #[test]
    fn zero_source_no_op_on_empty_slice() {
        let src = ZeroDataSource;
        let mut buf: [u8; 0] = [];
        src.read_cluster_bytes(2, 0, &mut buf);
    }

    // A `DataClusterSource` must be usable behind `Box<dyn ... + Send + Sync>`.
    // If this stops compiling, the trait has accidentally grown a
    // non-object-safe method.
    #[test]
    fn trait_object_safety_holds() {
        let _boxed: Box<dyn DataClusterSource + Send + Sync> = Box::new(ZeroDataSource);
    }
}
