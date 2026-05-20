//! Phase 2.13 — deferred deep-directory materialization.
//!
//! [`ExfatSynth`](super::synth::ExfatSynth) hardcodes the cluster
//! heap layout — root @ 2, bitmap, upcase, then zero-fill. That
//! works for the boot path that only needs metadata visible, but a
//! device-backed volume also needs to serve cluster contents for
//! user files and for sub-directory entries that span many
//! clusters. With a 10 000-file synthetic tree the dispatcher
//! cannot pre-materialize every cluster up-front without missing
//! the 1 s cold-start budget (see `docs/00-PLAN.md` row 2.14).
//!
//! This module provides one abstraction and one wrapper:
//!
//! * [`ClusterMaterializer`] — the user-supplied bridge from
//!   "cluster N" to "the bytes that belong there", typically a
//!   deep-directory or backing-file read.
//! * [`LazyClusterCache`] — caches materialized cluster bytes
//!   under a bounded FIFO budget. The wrapper is thread-safe
//!   (`Send + Sync` when `M: Send + Sync`); concurrent readers
//!   of the same cluster share a single materialization via
//!   [`std::sync::Arc`].
//!
//! ## Concurrency contract
//!
//! Two readers asking for the same uncached cluster at the same
//! moment **may** each run [`ClusterMaterializer::materialize`]
//! once before either inserts into the cache — the cache only
//! guarantees *eventual* dedup, not at-most-once. The post-insert
//! double-check window means the later inserter discards its
//! buffer in favour of the existing one, so both readers see the
//! same `Arc` after the race. Tests document this with a
//! materializer that counts invocations across N threads.
//!
//! ## Eviction
//!
//! FIFO by insertion order. The choice is deliberate: a true LRU
//! would need an LRU crate (charter §"Best Architecture
//! Practices" — keep deps minimal) and the workload — a Tesla
//! SCSI host walking the directory tree once then reading files
//! sequentially — does not benefit from LRU recency tracking.
//! FIFO is enough to bound steady-state memory.

#![allow(clippy::cast_possible_truncation)]

use core::fmt;
use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex};

/// User-supplied bridge from "cluster N" to "the bytes that
/// belong there".
///
/// Implementations must be `Send + Sync` because the cache holds
/// the materializer by value and is itself `Sync`. The cache
/// invokes [`Self::materialize`] outside its internal lock so a
/// slow backend does not block concurrent readers of other
/// clusters.
pub trait ClusterMaterializer: Send + Sync {
    /// Fill `out` with the bytes of `cluster` from the backing
    /// store. `out.len()` always equals the cluster size the
    /// cache was constructed with.
    ///
    /// # Errors
    ///
    /// Returns a [`MaterializeError`] when the cluster does not
    /// exist in the backing store or the backend reports an I/O
    /// failure.
    fn materialize(&self, cluster: u32, out: &mut [u8]) -> Result<(), MaterializeError>;
}

/// Errors returned by [`ClusterMaterializer::materialize`]
/// implementations.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MaterializeError {
    /// The cluster number is outside the backend's valid range.
    ClusterOutOfRange {
        /// The offending cluster number.
        cluster: u32,
    },
    /// Backend-specific failure. The message is for logging only
    /// and **never** for control-flow matching.
    Backend(String),
}

impl fmt::Display for MaterializeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ClusterOutOfRange { cluster } => {
                write!(f, "cluster {cluster} is outside the backend's valid range")
            }
            Self::Backend(msg) => write!(f, "backend failure during materialization: {msg}"),
        }
    }
}

impl core::error::Error for MaterializeError {}

/// Errors returned by [`LazyClusterCache::new`] and
/// [`LazyClusterCache::read_cluster_chunk`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LazyLoadError {
    /// [`LazyClusterCache::new`] was given a cluster size of 0.
    ZeroClusterSize,
    /// [`LazyClusterCache::read_cluster_chunk`] was given an
    /// `intra_offset` at or beyond the cluster size.
    OffsetBeyondCluster {
        /// The caller's intra-cluster byte offset.
        offset: usize,
        /// The cache's configured cluster size.
        cluster_size: usize,
    },
    /// `intra_offset + out.len()` exceeds the cluster size.
    BufferExceedsCluster {
        /// The caller's intra-cluster byte offset.
        offset: usize,
        /// The caller's buffer length in bytes.
        length: usize,
        /// The cache's configured cluster size.
        cluster_size: usize,
    },
    /// The cache's internal lock was poisoned by a panicking
    /// thread. This indicates a bug elsewhere in the process —
    /// the cache itself never panics while holding the lock.
    LockPoisoned,
    /// The materializer rejected the request.
    Materialize(MaterializeError),
}

impl fmt::Display for LazyLoadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ZeroClusterSize => f.write_str("cluster size must be greater than zero"),
            Self::OffsetBeyondCluster {
                offset,
                cluster_size,
            } => write!(
                f,
                "intra-cluster offset {offset} is at or beyond the cluster size {cluster_size}",
            ),
            Self::BufferExceedsCluster {
                offset,
                length,
                cluster_size,
            } => write!(
                f,
                "read of {length} bytes at intra-cluster offset {offset} exceeds the cluster size {cluster_size}",
            ),
            Self::LockPoisoned => f.write_str("lazy-load cache lock was poisoned by a panic"),
            Self::Materialize(err) => write!(f, "materializer failed: {err}"),
        }
    }
}

impl core::error::Error for LazyLoadError {
    fn source(&self) -> Option<&(dyn core::error::Error + 'static)> {
        match self {
            Self::Materialize(err) => Some(err),
            Self::ZeroClusterSize
            | Self::OffsetBeyondCluster { .. }
            | Self::BufferExceedsCluster { .. }
            | Self::LockPoisoned => None,
        }
    }
}

/// Bounded-FIFO cluster cache wrapping a [`ClusterMaterializer`].
///
/// Holds at most `capacity` recently-materialized cluster buffers.
/// On overflow the oldest entry is evicted. Reads are served from
/// the cache when possible and fall through to
/// [`ClusterMaterializer::materialize`] when the requested cluster
/// is absent.
pub struct LazyClusterCache<M> {
    materializer: M,
    cluster_size_bytes: usize,
    capacity: usize,
    state: Mutex<CacheState>,
}

impl<M> fmt::Debug for LazyClusterCache<M> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("LazyClusterCache")
            .field("cluster_size_bytes", &self.cluster_size_bytes)
            .field("capacity", &self.capacity)
            .finish_non_exhaustive()
    }
}

#[derive(Default)]
struct CacheState {
    entries: BTreeMap<u32, Arc<Vec<u8>>>,
    insertion_order: VecDeque<u32>,
}

impl<M: ClusterMaterializer> LazyClusterCache<M> {
    /// Build a new cache wrapping `materializer`. `capacity` is
    /// clamped to at least 1 — a zero-capacity cache would defeat
    /// the cache's whole point of single-materialization-per-
    /// cluster.
    ///
    /// # Errors
    ///
    /// Returns [`LazyLoadError::ZeroClusterSize`] when
    /// `cluster_size_bytes == 0`.
    pub fn new(
        materializer: M,
        cluster_size_bytes: usize,
        capacity: usize,
    ) -> Result<Self, LazyLoadError> {
        if cluster_size_bytes == 0 {
            return Err(LazyLoadError::ZeroClusterSize);
        }
        Ok(Self {
            materializer,
            cluster_size_bytes,
            capacity: capacity.max(1),
            state: Mutex::new(CacheState::default()),
        })
    }

    /// The cluster size in bytes this cache was built for.
    #[must_use]
    pub const fn cluster_size_bytes(&self) -> usize {
        self.cluster_size_bytes
    }

    /// The maximum number of cluster buffers the cache keeps
    /// before evicting the oldest.
    #[must_use]
    pub const fn capacity(&self) -> usize {
        self.capacity
    }

    /// Number of cluster buffers currently resident in the cache.
    ///
    /// # Errors
    ///
    /// Returns [`LazyLoadError::LockPoisoned`] if the internal
    /// lock was poisoned by a panic elsewhere.
    pub fn cache_len(&self) -> Result<usize, LazyLoadError> {
        let state = self.state.lock().map_err(|_| LazyLoadError::LockPoisoned)?;
        Ok(state.entries.len())
    }

    /// Read `out.len()` bytes from `cluster` starting at
    /// `intra_offset` inside the cluster. Empty `out` is a no-op
    /// and returns `Ok(())` without consulting the materializer.
    ///
    /// # Errors
    ///
    /// * [`LazyLoadError::OffsetBeyondCluster`] if `intra_offset`
    ///   is at or beyond the cluster size and `out` is non-empty.
    /// * [`LazyLoadError::BufferExceedsCluster`] if
    ///   `intra_offset + out.len()` exceeds the cluster size.
    /// * [`LazyLoadError::Materialize`] if the materializer
    ///   rejects the cluster.
    /// * [`LazyLoadError::LockPoisoned`] if the cache's internal
    ///   lock is poisoned.
    pub fn read_cluster_chunk(
        &self,
        cluster: u32,
        intra_offset: usize,
        out: &mut [u8],
    ) -> Result<(), LazyLoadError> {
        if out.is_empty() {
            return Ok(());
        }
        if intra_offset >= self.cluster_size_bytes {
            return Err(LazyLoadError::OffsetBeyondCluster {
                offset: intra_offset,
                cluster_size: self.cluster_size_bytes,
            });
        }
        let end =
            intra_offset
                .checked_add(out.len())
                .ok_or(LazyLoadError::BufferExceedsCluster {
                    offset: intra_offset,
                    length: out.len(),
                    cluster_size: self.cluster_size_bytes,
                })?;
        if end > self.cluster_size_bytes {
            return Err(LazyLoadError::BufferExceedsCluster {
                offset: intra_offset,
                length: out.len(),
                cluster_size: self.cluster_size_bytes,
            });
        }

        let arc = self.get_or_materialize(cluster)?;
        let slice = arc
            .get(intra_offset..end)
            .ok_or(LazyLoadError::BufferExceedsCluster {
                offset: intra_offset,
                length: out.len(),
                cluster_size: self.cluster_size_bytes,
            })?;
        out.copy_from_slice(slice);
        Ok(())
    }

    fn get_or_materialize(&self, cluster: u32) -> Result<Arc<Vec<u8>>, LazyLoadError> {
        // Peek with the lock held briefly.
        {
            let state = self.state.lock().map_err(|_| LazyLoadError::LockPoisoned)?;
            if let Some(arc) = state.entries.get(&cluster) {
                return Ok(arc.clone());
            }
        }

        // Materialize without holding the lock so a slow backend
        // does not block readers of other (already-cached)
        // clusters.
        let mut buf = vec![0u8; self.cluster_size_bytes];
        self.materializer
            .materialize(cluster, &mut buf)
            .map_err(LazyLoadError::Materialize)?;
        let candidate = Arc::new(buf);

        // Insert under the lock. Double-check in case another
        // thread won the race and inserted first — discard our
        // buffer in that case so both threads end up sharing the
        // same Arc.
        let mut state = self.state.lock().map_err(|_| LazyLoadError::LockPoisoned)?;
        if let Some(existing) = state.entries.get(&cluster) {
            return Ok(existing.clone());
        }
        state.entries.insert(cluster, Arc::clone(&candidate));
        state.insertion_order.push_back(cluster);
        while state.insertion_order.len() > self.capacity {
            if let Some(oldest) = state.insertion_order.pop_front() {
                state.entries.remove(&oldest);
            }
        }
        Ok(candidate)
    }
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::cognitive_complexity,
        clippy::expect_used,
        clippy::indexing_slicing,
        clippy::panic,
        clippy::unwrap_used
    )]

    use super::*;
    use std::sync::Barrier;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::thread;

    const CLUSTER: usize = 4096;

    /// Materializer that records every (cluster, call) it sees.
    /// Used to assert single-materialization invariants.
    struct CountingMaterializer {
        calls: AtomicUsize,
        per_cluster: Mutex<BTreeMap<u32, usize>>,
        pattern: u8,
    }

    impl CountingMaterializer {
        fn new(pattern: u8) -> Self {
            Self {
                calls: AtomicUsize::new(0),
                per_cluster: Mutex::new(BTreeMap::new()),
                pattern,
            }
        }

        fn total_calls(&self) -> usize {
            self.calls.load(Ordering::SeqCst)
        }

        fn calls_for(&self, cluster: u32) -> usize {
            *self.per_cluster.lock().unwrap().get(&cluster).unwrap_or(&0)
        }
    }

    impl ClusterMaterializer for CountingMaterializer {
        fn materialize(&self, cluster: u32, out: &mut [u8]) -> Result<(), MaterializeError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            *self.per_cluster.lock().unwrap().entry(cluster).or_insert(0) += 1;
            // Distinctive per-cluster fill: high byte = cluster
            // low byte, low byte = constructor pattern.
            for (i, slot) in out.iter_mut().enumerate() {
                *slot = self.pattern ^ ((cluster as u8).wrapping_add(i as u8));
            }
            Ok(())
        }
    }

    /// Always-fail materializer for the error-path tests.
    struct FailingMaterializer {
        message: &'static str,
    }

    impl ClusterMaterializer for FailingMaterializer {
        fn materialize(&self, _cluster: u32, _out: &mut [u8]) -> Result<(), MaterializeError> {
            Err(MaterializeError::Backend(String::from(self.message)))
        }
    }

    fn expected_byte(cluster: u32, pattern: u8, i: usize) -> u8 {
        pattern ^ ((cluster as u8).wrapping_add(i as u8))
    }

    // ── Constructor invariants ────────────────────────────────────

    #[test]
    fn new_rejects_zero_cluster_size() {
        let err = LazyClusterCache::new(CountingMaterializer::new(0xAA), 0, 4)
            .expect_err("zero cluster size must be rejected");
        assert_eq!(err, LazyLoadError::ZeroClusterSize);
    }

    #[test]
    fn new_clamps_capacity_to_at_least_one() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0), CLUSTER, 0)
            .expect("zero capacity should be clamped not rejected");
        assert_eq!(cache.capacity(), 1);
    }

    #[test]
    fn cluster_size_round_trips_from_constructor() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0), CLUSTER, 8).unwrap();
        assert_eq!(cache.cluster_size_bytes(), CLUSTER);
    }

    // ── Cache hits and misses ─────────────────────────────────────

    #[test]
    fn first_read_calls_materializer_exactly_once() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0x5A), CLUSTER, 4).unwrap();
        let mut buf = [0u8; CLUSTER];
        cache.read_cluster_chunk(7, 0, &mut buf).unwrap();
        assert_eq!(cache.materializer.total_calls(), 1);
        assert_eq!(cache.materializer.calls_for(7), 1);
        assert_eq!(cache.cache_len().unwrap(), 1);
    }

    #[test]
    fn repeated_reads_of_same_cluster_call_materializer_once() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0x33), CLUSTER, 4).unwrap();
        let mut buf = [0u8; CLUSTER];
        for _ in 0..50 {
            cache.read_cluster_chunk(9, 0, &mut buf).unwrap();
        }
        assert_eq!(cache.materializer.total_calls(), 1);
    }

    #[test]
    fn reads_of_distinct_clusters_call_materializer_per_cluster() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0), CLUSTER, 16).unwrap();
        let mut buf = [0u8; CLUSTER];
        for c in 0..10_u32 {
            cache.read_cluster_chunk(c, 0, &mut buf).unwrap();
        }
        assert_eq!(cache.materializer.total_calls(), 10);
        assert_eq!(cache.cache_len().unwrap(), 10);
    }

    #[test]
    fn read_returns_materializer_supplied_bytes() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0xC3), CLUSTER, 4).unwrap();
        let mut buf = [0u8; CLUSTER];
        cache.read_cluster_chunk(11, 0, &mut buf).unwrap();
        for (i, &b) in buf.iter().enumerate() {
            assert_eq!(b, expected_byte(11, 0xC3, i), "byte {i}");
        }
    }

    #[test]
    fn partial_reads_return_the_correct_intra_cluster_slice() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0x77), CLUSTER, 4).unwrap();
        let mut buf = [0u8; 32];
        cache.read_cluster_chunk(3, 100, &mut buf).unwrap();
        for (i, &b) in buf.iter().enumerate() {
            assert_eq!(b, expected_byte(3, 0x77, 100 + i), "byte {i} of slice");
        }
    }

    #[test]
    fn empty_read_is_noop_and_does_not_materialize() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0), CLUSTER, 4).unwrap();
        cache.read_cluster_chunk(42, 0, &mut []).unwrap();
        assert_eq!(cache.materializer.total_calls(), 0);
        assert_eq!(cache.cache_len().unwrap(), 0);
    }

    #[test]
    fn empty_read_at_cluster_size_offset_is_still_noop() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0), CLUSTER, 4).unwrap();
        cache.read_cluster_chunk(0, CLUSTER, &mut []).unwrap();
        assert_eq!(cache.materializer.total_calls(), 0);
    }

    // ── Bounds checking ───────────────────────────────────────────

    #[test]
    fn offset_at_cluster_size_with_data_is_rejected() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0), CLUSTER, 4).unwrap();
        let err = cache
            .read_cluster_chunk(0, CLUSTER, &mut [0u8; 1])
            .unwrap_err();
        assert!(matches!(err, LazyLoadError::OffsetBeyondCluster { .. }));
        assert_eq!(cache.materializer.total_calls(), 0);
    }

    #[test]
    fn offset_past_cluster_size_is_rejected() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0), CLUSTER, 4).unwrap();
        let err = cache
            .read_cluster_chunk(0, CLUSTER + 10, &mut [0u8; 1])
            .unwrap_err();
        assert!(matches!(err, LazyLoadError::OffsetBeyondCluster { .. }));
    }

    #[test]
    fn buffer_exceeding_cluster_is_rejected() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0), CLUSTER, 4).unwrap();
        let err = cache
            .read_cluster_chunk(0, CLUSTER - 10, &mut [0u8; 100])
            .unwrap_err();
        assert!(matches!(err, LazyLoadError::BufferExceedsCluster { .. }));
        assert_eq!(cache.materializer.total_calls(), 0);
    }

    #[test]
    fn buffer_exceeding_via_overflow_is_rejected() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0), CLUSTER, 4).unwrap();
        let err = cache
            .read_cluster_chunk(0, usize::MAX, &mut [0u8; 1])
            .unwrap_err();
        // usize::MAX > CLUSTER, so OffsetBeyondCluster wins.
        assert!(matches!(err, LazyLoadError::OffsetBeyondCluster { .. }));
    }

    // ── Eviction ──────────────────────────────────────────────────

    #[test]
    fn fifo_evicts_oldest_when_capacity_exceeded() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0), CLUSTER, 3).unwrap();
        let mut buf = [0u8; 8];
        // Insert clusters 1, 2, 3, 4 — capacity is 3, so cluster
        // 1 must be evicted.
        for c in 1..=4_u32 {
            cache.read_cluster_chunk(c, 0, &mut buf).unwrap();
        }
        assert_eq!(cache.cache_len().unwrap(), 3);
        assert_eq!(cache.materializer.total_calls(), 4);

        // Reading cluster 1 again forces re-materialization
        // (was evicted) — count goes from 4 to 5.
        cache.read_cluster_chunk(1, 0, &mut buf).unwrap();
        assert_eq!(cache.materializer.total_calls(), 5);
    }

    #[test]
    fn repeated_reads_of_recently_evicted_clusters_re_materialize_each_time() {
        let cache = LazyClusterCache::new(CountingMaterializer::new(0), CLUSTER, 2).unwrap();
        let mut buf = [0u8; 8];
        cache.read_cluster_chunk(10, 0, &mut buf).unwrap();
        cache.read_cluster_chunk(20, 0, &mut buf).unwrap();
        // Capacity 2, so reading 30 evicts 10.
        cache.read_cluster_chunk(30, 0, &mut buf).unwrap();
        // Reading 10 evicts 20.
        cache.read_cluster_chunk(10, 0, &mut buf).unwrap();
        // Reading 20 evicts 30.
        cache.read_cluster_chunk(20, 0, &mut buf).unwrap();
        // 5 reads, 5 materializations because each evicted by
        // the next read.
        assert_eq!(cache.materializer.total_calls(), 5);
    }

    // ── Materializer error propagation ────────────────────────────

    #[test]
    fn materializer_backend_error_propagates() {
        let cache = LazyClusterCache::new(
            FailingMaterializer {
                message: "simulated disk failure",
            },
            CLUSTER,
            4,
        )
        .unwrap();
        let err = cache.read_cluster_chunk(7, 0, &mut [0u8; 8]).unwrap_err();
        match err {
            LazyLoadError::Materialize(MaterializeError::Backend(msg)) => {
                assert_eq!(msg, "simulated disk failure");
            }
            other => panic!("expected Backend error, got {other:?}"),
        }
    }

    #[test]
    fn failed_materialization_is_not_cached() {
        let cache =
            LazyClusterCache::new(FailingMaterializer { message: "boom" }, CLUSTER, 4).unwrap();
        let _ = cache.read_cluster_chunk(1, 0, &mut [0u8; 8]);
        assert_eq!(cache.cache_len().unwrap(), 0);
    }

    // ── Display + Error wiring ────────────────────────────────────

    #[test]
    fn errors_have_useful_display_strings() {
        let m = MaterializeError::ClusterOutOfRange { cluster: 99 };
        assert!(format!("{m}").contains("99"));
        let m = MaterializeError::Backend(String::from("kaboom"));
        assert!(format!("{m}").contains("kaboom"));

        let e = LazyLoadError::ZeroClusterSize;
        assert!(!format!("{e}").is_empty());
        let e = LazyLoadError::OffsetBeyondCluster {
            offset: 5,
            cluster_size: 4,
        };
        assert!(format!("{e}").contains('5'));
        let e = LazyLoadError::BufferExceedsCluster {
            offset: 1,
            length: 4,
            cluster_size: 4,
        };
        assert!(format!("{e}").contains('4'));
        let e = LazyLoadError::LockPoisoned;
        assert!(format!("{e}").contains("poisoned"));
        let e = LazyLoadError::Materialize(MaterializeError::ClusterOutOfRange { cluster: 1 });
        assert!(format!("{e}").contains("materializer"));
    }

    #[test]
    fn materialize_error_chains_through_lazy_load_error_source() {
        let err = LazyLoadError::Materialize(MaterializeError::ClusterOutOfRange { cluster: 17 });
        let source = core::error::Error::source(&err).expect("Materialize variant carries source");
        assert!(format!("{source}").contains("17"));
    }

    // ── Concurrency ───────────────────────────────────────────────
    //
    // The cache promises *eventual* dedup, not at-most-once. With
    // N threads racing on the same cluster, between 1 and N
    // materialize calls are valid, but the final cache state must
    // contain exactly one entry and every reader must observe
    // byte-identical content.

    #[test]
    fn concurrent_readers_of_same_cluster_share_a_single_cache_entry() {
        let cache =
            Arc::new(LazyClusterCache::new(CountingMaterializer::new(0x88), CLUSTER, 4).unwrap());
        let n_threads = 16;
        let barrier = Arc::new(Barrier::new(n_threads));
        let mut handles = Vec::with_capacity(n_threads);
        for _ in 0..n_threads {
            let cache = Arc::clone(&cache);
            let barrier = Arc::clone(&barrier);
            handles.push(thread::spawn(move || {
                let mut buf = [0u8; CLUSTER];
                barrier.wait();
                cache.read_cluster_chunk(42, 0, &mut buf).unwrap();
                buf
            }));
        }
        let results: Vec<[u8; CLUSTER]> = handles.into_iter().map(|h| h.join().unwrap()).collect();
        // All readers must have observed the same bytes.
        for buf in &results[1..] {
            assert_eq!(&buf[..], &results[0][..]);
        }
        // Bytes must be the materializer's pattern for cluster 42.
        for (i, &b) in results[0].iter().enumerate() {
            assert_eq!(b, expected_byte(42, 0x88, i));
        }
        // At least one materialize call; at most n_threads.
        let calls = cache.materializer.total_calls();
        assert!(calls >= 1 && calls <= n_threads, "calls={calls}");
        // Exactly one cache entry after the race.
        assert_eq!(cache.cache_len().unwrap(), 1);
    }

    #[test]
    fn concurrent_readers_of_distinct_clusters_each_materialize_at_least_once() {
        let cache =
            Arc::new(LazyClusterCache::new(CountingMaterializer::new(0x5C), CLUSTER, 32).unwrap());
        let n_threads = 16;
        let barrier = Arc::new(Barrier::new(n_threads));
        let mut handles = Vec::with_capacity(n_threads);
        for tid in 0..n_threads {
            let cache = Arc::clone(&cache);
            let barrier = Arc::clone(&barrier);
            handles.push(thread::spawn(move || {
                let mut buf = [0u8; CLUSTER];
                barrier.wait();
                cache
                    .read_cluster_chunk(u32::try_from(tid).unwrap(), 0, &mut buf)
                    .unwrap();
                buf
            }));
        }
        let results: Vec<[u8; CLUSTER]> = handles.into_iter().map(|h| h.join().unwrap()).collect();
        // Each thread's bytes must match the materializer pattern
        // for that thread's cluster.
        for (tid, buf) in results.iter().enumerate() {
            let cluster = u32::try_from(tid).unwrap();
            for (i, &b) in buf.iter().enumerate() {
                assert_eq!(b, expected_byte(cluster, 0x5C, i), "tid {tid} byte {i}");
            }
        }
        // Exactly one materialize call per distinct cluster
        // (no races because the cluster keys are all different).
        assert_eq!(cache.materializer.total_calls(), n_threads);
        assert_eq!(cache.cache_len().unwrap(), n_threads);
    }
}
