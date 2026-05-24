//! Bounded FIFO spill buffer for data-cluster writes that arrived
//! before the cluster's owning file or FAT chain was known.
//!
//! ## Why this exists
//!
//! Both the FAT32 and exFAT write-state machines (see
//! [`super::fat32_write`] and [`super::exfat_write`]) face the same
//! out-of-order problem: the Linux block layer can issue a write to
//! a data cluster *before* it issues the FAT update or directory
//! entry that tells us which file owns that cluster. We stash those
//! bytes in a per-cluster spill map and replay them once the
//! ownership reveals itself.
//!
//! ## The bug this fixes
//!
//! Before this module existed, both crates owned a private
//! `HashMap<u32, Vec<PendingDataChunk>>` with no eviction policy.
//! Any cluster whose owner *never* materialized (Tesla crash mid-clip,
//! filesystem driver quirk, write to a hole that's later trimmed,
//! etc.) leaked its bytes forever.
//!
//! On 2026-05-24 the Pi Zero 2 W (512 MB RAM) OOM-killed `teslafat`
//! twice within 26 minutes (RSS 346 MB, then 357 MB) on the TESLACAM
//! 256 GiB exFAT volume. The cascading failure took the USB gadget
//! offline and Tesla stopped recording. See `docs/01-PROGRESS.md`
//! Phase P entry for the full incident write-up.
//!
//! ## Behaviour
//!
//! * Insertion order is tracked per *cluster* (not per *chunk*) so
//!   that a cluster receiving many follow-up writes does not pretend
//!   to be "newer" than its first arrival.
//! * When `total_bytes` exceeds `max_bytes`, the oldest **cluster**
//!   (and all chunks it accumulated) is dropped with a
//!   `tracing::warn!` carrying the cluster number, chunk count, and
//!   bytes evicted. The operator-visible counter
//!   ([`PendingSpill::evicted_chunks_total`]) lets `system_health`
//!   surface the condition.
//! * Eviction is FIFO, not LRU, because a cluster that keeps
//!   accumulating new writes without resolution is *more* suspicious
//!   than one that arrived early and sat quietly — promoting it on
//!   each write would hide a runaway write loop forever.
//! * The default cap is 16 MiB, chosen to be:
//!   * Generous enough that healthy Tesla write bursts (60 s of
//!     `TeslaCam` at ~10 Mbps = ~75 MB total, of which only a small
//!     fraction is ever unresolved at any instant) fit comfortably.
//!   * Tight enough that even worst-case eviction can't push the
//!     daemon RSS past ~50 MB total, leaving room on a 512 MB Pi
//!     Zero 2 W for the rest of the userspace.

use std::collections::{HashMap, VecDeque};

/// Default cap, see module docs.
pub(crate) const DEFAULT_MAX_SPILL_BYTES: usize = 16 * 1024 * 1024;

/// One stashed data-cluster write that arrived before the cluster's
/// owning file was known.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PendingDataChunk {
    /// Byte offset within the cluster that this chunk starts at.
    pub byte_in_cluster: usize,
    /// The bytes the kernel asked us to write.
    pub bytes: Vec<u8>,
}

/// Bounded FIFO spill buffer. See module docs.
#[derive(Debug)]
pub(crate) struct PendingSpill {
    chunks: HashMap<u32, Vec<PendingDataChunk>>,
    /// First-insertion order for clusters. A cluster appears at
    /// most once; it is added on first push and removed on take or
    /// eviction.
    insertion_order: VecDeque<u32>,
    total_bytes: usize,
    max_bytes: usize,
    evicted_clusters_total: u64,
    evicted_chunks_total: u64,
    evicted_bytes_total: u64,
}

impl Default for PendingSpill {
    fn default() -> Self {
        Self::with_capacity(DEFAULT_MAX_SPILL_BYTES)
    }
}

impl PendingSpill {
    /// Construct a spill buffer with the default cap.
    pub fn new() -> Self {
        Self::default()
    }

    /// Construct a spill buffer with a caller-chosen byte cap.
    /// Used by tests that want to exercise eviction without
    /// pushing 16 MiB through the harness.
    pub fn with_capacity(max_bytes: usize) -> Self {
        Self {
            chunks: HashMap::new(),
            insertion_order: VecDeque::new(),
            total_bytes: 0,
            max_bytes,
            evicted_clusters_total: 0,
            evicted_chunks_total: 0,
            evicted_bytes_total: 0,
        }
    }

    /// Stash a write to `cluster` that we couldn't route yet.
    /// May trigger FIFO eviction of older clusters to honour the cap.
    pub fn push(&mut self, cluster: u32, byte_in_cluster: usize, bytes: &[u8]) {
        let added_bytes = bytes.len();
        let entry = self.chunks.entry(cluster);
        let is_new_cluster = matches!(entry, std::collections::hash_map::Entry::Vacant(_));
        entry.or_default().push(PendingDataChunk {
            byte_in_cluster,
            bytes: bytes.to_vec(),
        });
        if is_new_cluster {
            self.insertion_order.push_back(cluster);
        }
        self.total_bytes = self.total_bytes.saturating_add(added_bytes);
        self.evict_to_fit();
    }

    /// Remove and return every chunk stashed for `cluster`, in
    /// insertion order, for the caller to replay. Returns `None`
    /// if nothing is stashed.
    pub fn take(&mut self, cluster: u32) -> Option<Vec<PendingDataChunk>> {
        let chunks = self.chunks.remove(&cluster)?;
        let removed_bytes: usize = chunks.iter().map(|c| c.bytes.len()).sum();
        self.total_bytes = self.total_bytes.saturating_sub(removed_bytes);
        if let Some(pos) = self.insertion_order.iter().position(|&c| c == cluster) {
            self.insertion_order.remove(pos);
        }
        Some(chunks)
    }

    /// True iff `cluster` has at least one stashed chunk.
    #[cfg(test)]
    pub fn contains(&self, cluster: u32) -> bool {
        self.chunks.contains_key(&cluster)
    }

    /// Number of distinct clusters currently holding stashed chunks.
    #[cfg(test)]
    pub fn cluster_count(&self) -> usize {
        self.chunks.len()
    }

    /// Total bytes currently buffered across all clusters. Surfaced
    /// from the write state machines' flush paths into tracing so
    /// the operator can spot the unresolved-cluster pattern from
    /// the 2026-05-24 OOM incident before it accumulates again.
    pub fn total_bytes(&self) -> usize {
        self.total_bytes
    }

    /// True iff no clusters are stashed. Surfaced from the write
    /// state machines' flush paths for the same reason as
    /// [`Self::total_bytes`].
    pub fn is_empty(&self) -> bool {
        self.chunks.is_empty()
    }

    /// Lifetime count of clusters dropped by eviction. Surfaced so
    /// the operator can correlate cap exhaustion against
    /// hardware-observed write loss.
    pub fn evicted_clusters_total(&self) -> u64 {
        self.evicted_clusters_total
    }

    /// Lifetime count of individual chunks dropped by eviction.
    pub fn evicted_chunks_total(&self) -> u64 {
        self.evicted_chunks_total
    }

    /// Lifetime byte count dropped by eviction.
    pub fn evicted_bytes_total(&self) -> u64 {
        self.evicted_bytes_total
    }

    fn evict_to_fit(&mut self) {
        while self.total_bytes > self.max_bytes {
            let Some(oldest_cluster) = self.insertion_order.pop_front() else {
                // Defensive: cap is 0 or chunks/order desynced.
                // Either way, nothing left to evict.
                break;
            };
            let Some(chunks) = self.chunks.remove(&oldest_cluster) else {
                continue;
            };
            let evicted_bytes: usize = chunks.iter().map(|c| c.bytes.len()).sum();
            self.total_bytes = self.total_bytes.saturating_sub(evicted_bytes);
            self.evicted_clusters_total = self.evicted_clusters_total.saturating_add(1);
            self.evicted_chunks_total = self
                .evicted_chunks_total
                .saturating_add(chunks.len() as u64);
            self.evicted_bytes_total = self
                .evicted_bytes_total
                .saturating_add(evicted_bytes as u64);
            tracing::warn!(
                cluster = oldest_cluster,
                chunks = chunks.len(),
                bytes = evicted_bytes,
                buffer_bytes_after = self.total_bytes,
                buffer_cap_bytes = self.max_bytes,
                evicted_clusters_total = self.evicted_clusters_total,
                "pending-data spill: evicted oldest unresolved cluster to honour cap"
            );
        }
    }
}

#[cfg(test)]
#[allow(clippy::expect_used, clippy::panic, clippy::unwrap_used)]
mod tests {
    use super::{DEFAULT_MAX_SPILL_BYTES, PendingSpill};

    #[test]
    fn push_and_take_round_trips_chunks_in_insertion_order() {
        let mut spill = PendingSpill::new();
        spill.push(7, 0, b"hello");
        spill.push(7, 16, b"world");
        spill.push(9, 0, b"!");
        assert_eq!(spill.cluster_count(), 2);
        assert_eq!(spill.total_bytes(), 11);

        let chunks = spill.take(7).expect("cluster 7 present");
        assert_eq!(chunks.len(), 2);
        assert_eq!(chunks[0].byte_in_cluster, 0);
        assert_eq!(&chunks[0].bytes, b"hello");
        assert_eq!(chunks[1].byte_in_cluster, 16);
        assert_eq!(&chunks[1].bytes, b"world");

        assert!(!spill.contains(7));
        assert!(spill.contains(9));
        assert_eq!(spill.total_bytes(), 1);
    }

    #[test]
    fn take_missing_cluster_returns_none() {
        let mut spill = PendingSpill::new();
        assert!(spill.take(42).is_none());
        spill.push(1, 0, b"x");
        assert!(spill.take(2).is_none());
        assert!(spill.contains(1));
    }

    #[test]
    fn default_cap_matches_documented_value() {
        let spill = PendingSpill::new();
        // We can't read the cap directly, so prove it indirectly:
        // 1 MiB push should not evict.
        assert_eq!(DEFAULT_MAX_SPILL_BYTES, 16 * 1024 * 1024);
        let _ = spill;
    }

    #[test]
    fn eviction_drops_oldest_cluster_when_cap_exceeded() {
        // 32-byte cap so each cluster's 16-byte payload is half the
        // cap. After the third push, total = 48 > 32 → evict oldest.
        let mut spill = PendingSpill::with_capacity(32);
        spill.push(1, 0, &[0u8; 16]);
        spill.push(2, 0, &[0u8; 16]);
        assert_eq!(spill.evicted_clusters_total(), 0);
        assert_eq!(spill.total_bytes(), 32);

        spill.push(3, 0, &[0u8; 16]);
        // Oldest (cluster 1) is gone; total back under cap.
        assert!(!spill.contains(1));
        assert!(spill.contains(2));
        assert!(spill.contains(3));
        assert_eq!(spill.total_bytes(), 32);
        assert_eq!(spill.evicted_clusters_total(), 1);
        assert_eq!(spill.evicted_chunks_total(), 1);
        assert_eq!(spill.evicted_bytes_total(), 16);
    }

    #[test]
    fn eviction_drops_all_chunks_of_oldest_cluster_together() {
        let mut spill = PendingSpill::with_capacity(48);
        spill.push(1, 0, &[0u8; 8]);
        spill.push(1, 8, &[0u8; 8]);
        spill.push(1, 16, &[0u8; 8]); // 24 bytes total on cluster 1
        spill.push(2, 0, &[0u8; 16]); // 24 + 16 = 40 ≤ 48
        assert_eq!(spill.evicted_clusters_total(), 0);

        spill.push(3, 0, &[0u8; 16]); // 40 + 16 = 56 > 48 → evict 1
        assert!(!spill.contains(1));
        assert_eq!(spill.evicted_clusters_total(), 1);
        assert_eq!(spill.evicted_chunks_total(), 3);
        assert_eq!(spill.evicted_bytes_total(), 24);
        assert_eq!(spill.total_bytes(), 32);
    }

    #[test]
    fn repeat_push_to_same_cluster_does_not_re_age_it() {
        let mut spill = PendingSpill::with_capacity(32);
        spill.push(1, 0, &[0u8; 8]); // order: [1]
        spill.push(2, 0, &[0u8; 8]); // order: [1, 2]
        spill.push(1, 8, &[0u8; 8]); // order unchanged: [1, 2]; total = 24
        spill.push(3, 0, &[0u8; 16]); // total 40 > 32 → evict 1
        assert!(!spill.contains(1));
        assert!(spill.contains(2));
        assert!(spill.contains(3));
        // Cluster 1 had two 8-byte chunks → 16 bytes evicted.
        assert_eq!(spill.evicted_chunks_total(), 2);
        assert_eq!(spill.evicted_bytes_total(), 16);
    }

    #[test]
    fn take_removes_cluster_from_insertion_order() {
        let mut spill = PendingSpill::with_capacity(32);
        spill.push(1, 0, &[0u8; 8]);
        spill.push(2, 0, &[0u8; 8]);
        spill.push(3, 0, &[0u8; 8]);
        let _ = spill.take(1); // explicit resolution
        spill.push(4, 0, &[0u8; 16]); // total = 8+8+16 = 32, cap = 32 → no evict
        assert!(!spill.contains(1));
        assert!(spill.contains(2));
        assert!(spill.contains(3));
        assert!(spill.contains(4));
        assert_eq!(spill.evicted_clusters_total(), 0);
        // Now push one more byte to force eviction; cluster 2 must be next.
        spill.push(5, 0, &[0u8; 8]); // 32+8 → evict cluster 2
        assert!(!spill.contains(2));
        assert!(spill.contains(3));
        assert_eq!(spill.evicted_clusters_total(), 1);
    }

    #[test]
    fn empty_spill_reports_no_state() {
        let spill = PendingSpill::new();
        assert!(spill.is_empty());
        assert_eq!(spill.total_bytes(), 0);
        assert_eq!(spill.evicted_clusters_total(), 0);
        assert_eq!(spill.evicted_chunks_total(), 0);
        assert_eq!(spill.evicted_bytes_total(), 0);
    }

    #[test]
    fn regression_2026_05_24_unbounded_growth_does_not_recur() {
        // Simulates the live-device incident: Tesla writes ~10 MB/min
        // of data clusters whose owning dir entries never appear.
        // Pre-fix: ExfatWriteState pending_data grew to 346 MB RSS in
        // ~16 min and triggered OOM. Post-fix: total_bytes must stay
        // at or under the cap regardless of how much we push.
        let cap = 1024 * 1024; // 1 MiB for speed; same arithmetic
        let mut spill = PendingSpill::with_capacity(cap);
        let chunk = vec![0u8; 4096]; // 4 KiB per write
        for cluster in 0u32..(8 * 256) {
            // ~8 MiB of writes, 8× the cap
            spill.push(cluster, 0, &chunk);
            assert!(
                spill.total_bytes() <= cap,
                "total_bytes {} exceeded cap {} at cluster {cluster}",
                spill.total_bytes(),
                cap
            );
        }
        assert!(spill.evicted_clusters_total() > 0);
        assert!(spill.total_bytes() <= cap);
    }
}
