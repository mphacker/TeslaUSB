//! Bounded spill buffer for data-cluster writes that arrived
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
//! ## History
//!
//! - **Pre-Phase P** — both crates owned a private
//!   `HashMap<u32, Vec<PendingDataChunk>>` with no eviction policy.
//!   Any cluster whose owner never materialized leaked its bytes
//!   forever. On 2026-05-24 this OOM-killed `teslafat` on the Pi
//!   Zero 2 W twice (RSS 346 MB then 357 MB), cascading into a
//!   USB-gadget outage that stopped Tesla recording. See
//!   `docs/01-PROGRESS.md` Phase P entry.
//! - **Phase P** introduced this shared module with a 16 MiB
//!   in-memory FIFO cap. That stopped the OOM but at the cost of
//!   evicting ~1400 clusters/min during sustained Tesla bursts
//!   (Tesla writes a clip's data clusters *before* its dir entry,
//!   and a 1.7 GB clip can never fit in any cap that lives on a
//!   464 MB Pi). Eviction = silent partial data loss.
//! - **Phase Q** (this revision) makes the buffer **disk-backed**
//!   by default. The Pi has hundreds of GB of SD free; cap is now
//!   a few GiB, large enough to hold the in-flight pre-dir-entry
//!   window of every legitimate clip Tesla writes. Memory holds
//!   only the per-cluster index (~32 bytes/chunk). Eviction is now
//!   a true edge case (only when an attacker / fault tries to
//!   overflow the SD) instead of a steady-state regime.
//!
//! ## Storage modes
//!
//! Two modes share the same public API:
//!
//! * [`PendingSpill::new`] — in-memory (legacy). Used by unit
//!   tests and by callers that don't want disk I/O. Cap default
//!   16 MiB.
//! * [`PendingSpill::open_disk`] — disk-backed. Used by the
//!   production write-state machines. Cap default 4 GiB. Each
//!   cluster's chunks live in one append-only file at
//!   `<spill_dir>/<cluster:08x>.bin`. The directory is truncated
//!   at construction time (any leftover from a prior crash is
//!   discarded — the in-memory index that named it is gone too).
//!
//! ## Behaviour (both modes)
//!
//! * Insertion order is tracked per *cluster* (not per *chunk*) so
//!   that a cluster receiving many follow-up writes does not pretend
//!   to be "newer" than its first arrival.
//! * When `total_bytes` exceeds `max_bytes`, the oldest **cluster**
//!   (and all chunks it accumulated) is dropped with a
//!   `tracing::warn!` carrying the cluster number, chunk count, and
//!   bytes evicted. The operator-visible counters
//!   ([`PendingSpill::evicted_clusters_total`] etc.) let
//!   `system_health` surface the condition.
//! * Eviction is FIFO, not LRU, because a cluster that keeps
//!   accumulating new writes without resolution is *more* suspicious
//!   than one that arrived early and sat quietly — promoting it on
//!   each write would hide a runaway write loop forever.
//! * On disk-mode I/O errors (write fails, read fails, file
//!   missing), the affected chunk is logged and dropped. The spill
//!   buffer is a best-effort replay queue, not a crash-safe store:
//!   if the SD card is failing we cannot honour the queue anyway,
//!   and the alternative (returning an error to the caller and
//!   propagating to the kernel) would manifest as an EIO to Tesla
//!   mid-write, which is the catastrophic failure mode the whole
//!   spill is here to prevent.

use std::collections::{HashMap, VecDeque};
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

/// Default cap for the in-memory mode (legacy / tests).
pub(crate) const DEFAULT_MAX_SPILL_BYTES: usize = 16 * 1024 * 1024;

/// Default cap for the disk-backed mode (production). 4 GiB.
///
/// Chosen to comfortably hold several concurrent Tesla
/// pre-dir-entry write bursts (a single sentry clip is ~1.7 GB and
/// Tesla can be writing 4 cameras in parallel) without coming
/// anywhere near the available SD space (the Pi has ~270 GB free).
pub(crate) const DEFAULT_DISK_SPILL_BYTES: u64 = 4 * 1024 * 1024 * 1024;

/// One stashed data-cluster write that arrived before the cluster's
/// owning file was known.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PendingDataChunk {
    /// Byte offset within the cluster that this chunk starts at.
    pub byte_in_cluster: usize,
    /// The bytes the kernel asked us to write.
    pub bytes: Vec<u8>,
}

/// Per-chunk metadata held in memory for disk-backed mode.
///
/// Bytes themselves live on disk inside the cluster's spill file;
/// the in-memory footprint of one chunk is just these few fields.
#[derive(Debug, Clone, Copy)]
struct DiskChunkMeta {
    byte_in_cluster: u32,
    /// Byte offset in the spill file where this chunk's payload
    /// starts (after the 8-byte header).
    file_offset: u64,
    len: u32,
}

#[derive(Debug)]
enum Storage {
    /// Chunks held entirely in RAM. Legacy mode, tests.
    Memory {
        chunks: HashMap<u32, Vec<PendingDataChunk>>,
    },
    /// Chunks held in per-cluster files under `dir`. Index in RAM.
    Disk {
        dir: PathBuf,
        index: HashMap<u32, Vec<DiskChunkMeta>>,
    },
}

/// Bounded FIFO spill buffer. See module docs.
#[derive(Debug)]
pub(crate) struct PendingSpill {
    storage: Storage,
    /// First-insertion order for clusters. A cluster appears at
    /// most once; it is added on first push and removed on take or
    /// eviction.
    insertion_order: VecDeque<u32>,
    total_bytes: u64,
    max_bytes: u64,
    evicted_clusters_total: u64,
    evicted_chunks_total: u64,
    evicted_bytes_total: u64,
    io_errors_total: u64,
}

impl Default for PendingSpill {
    fn default() -> Self {
        Self::with_capacity(DEFAULT_MAX_SPILL_BYTES)
    }
}

impl PendingSpill {
    /// Construct an in-memory spill buffer with the default cap.
    /// Prefer [`Self::open_disk`] in production.
    pub fn new() -> Self {
        Self::default()
    }

    /// Construct an in-memory spill buffer with a caller-chosen
    /// byte cap. Tests use small caps to exercise eviction without
    /// pushing 16 MiB through the harness.
    pub fn with_capacity(max_bytes: usize) -> Self {
        Self {
            storage: Storage::Memory {
                chunks: HashMap::new(),
            },
            insertion_order: VecDeque::new(),
            total_bytes: 0,
            max_bytes: max_bytes as u64,
            evicted_clusters_total: 0,
            evicted_chunks_total: 0,
            evicted_bytes_total: 0,
            io_errors_total: 0,
        }
    }

    /// Construct a disk-backed spill buffer. The directory will
    /// be created if it doesn't exist; any files already in it
    /// (stale from a previous crash) are removed — the in-memory
    /// index naming them is gone.
    ///
    /// On directory-prep failure the buffer falls back to the
    /// in-memory mode with the default cap and logs a warning;
    /// this preserves teslafat liveness even if the spill volume
    /// is unavailable.
    pub fn open_disk(spill_dir: PathBuf, max_bytes: u64) -> Self {
        match prepare_spill_dir(&spill_dir) {
            Ok(()) => {
                tracing::info!(
                    spill_dir = %spill_dir.display(),
                    cap_bytes = max_bytes,
                    "pending-data spill: disk-backed mode ready"
                );
                Self {
                    storage: Storage::Disk {
                        dir: spill_dir,
                        index: HashMap::new(),
                    },
                    insertion_order: VecDeque::new(),
                    total_bytes: 0,
                    max_bytes,
                    evicted_clusters_total: 0,
                    evicted_chunks_total: 0,
                    evicted_bytes_total: 0,
                    io_errors_total: 0,
                }
            }
            Err(err) => {
                tracing::warn!(
                    spill_dir = %spill_dir.display(),
                    error = %err,
                    "pending-data spill: could not prepare disk dir; falling back to in-memory mode (writes may be lost under burst load)"
                );
                Self::new()
            }
        }
    }

    /// Stash a write to `cluster` that we couldn't route yet.
    /// May trigger FIFO eviction of older clusters to honour the cap.
    pub fn push(&mut self, cluster: u32, byte_in_cluster: usize, bytes: &[u8]) {
        let added_bytes = bytes.len() as u64;
        let is_new_cluster = match &mut self.storage {
            Storage::Memory { chunks } => {
                let entry = chunks.entry(cluster);
                let is_new = matches!(entry, std::collections::hash_map::Entry::Vacant(_));
                entry.or_default().push(PendingDataChunk {
                    byte_in_cluster,
                    bytes: bytes.to_vec(),
                });
                is_new
            }
            Storage::Disk { dir, index } => {
                // u32 is sufficient: byte_in_cluster is bounded by
                // the cluster size (≤ 128 KiB for any sane FAT/exFAT
                // geometry), and NBD writes are ≤ 4 MiB. Guard
                // defensively anyway — a corrupt chunk is silently
                // dropped (logged via the io_errors counter so an
                // operator can still see it).
                let Ok(byte_in_cluster_u32) = u32::try_from(byte_in_cluster) else {
                    self.io_errors_total = self.io_errors_total.saturating_add(1);
                    tracing::warn!(
                        cluster,
                        byte_in_cluster,
                        "pending-data spill: byte_in_cluster exceeds u32; dropping chunk"
                    );
                    return;
                };
                let Ok(len_u32) = u32::try_from(bytes.len()) else {
                    self.io_errors_total = self.io_errors_total.saturating_add(1);
                    tracing::warn!(
                        cluster,
                        len = bytes.len(),
                        "pending-data spill: chunk length exceeds u32; dropping chunk"
                    );
                    return;
                };
                let path = cluster_file_path(dir, cluster);
                match append_chunk_to_disk(&path, byte_in_cluster, bytes) {
                    Ok(file_offset) => {
                        let entry = index.entry(cluster);
                        let is_new =
                            matches!(entry, std::collections::hash_map::Entry::Vacant(_));
                        entry.or_default().push(DiskChunkMeta {
                            byte_in_cluster: byte_in_cluster_u32,
                            file_offset,
                            len: len_u32,
                        });
                        is_new
                    }
                    Err(err) => {
                        self.io_errors_total = self.io_errors_total.saturating_add(1);
                        tracing::warn!(
                            cluster,
                            byte_in_cluster,
                            len = bytes.len(),
                            error = %err,
                            "pending-data spill: disk write failed; dropping chunk"
                        );
                        return;
                    }
                }
            }
        };
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
        let chunks = match &mut self.storage {
            Storage::Memory { chunks } => {
                let v = chunks.remove(&cluster)?;
                let removed_bytes: u64 = v.iter().map(|c| c.bytes.len() as u64).sum();
                self.total_bytes = self.total_bytes.saturating_sub(removed_bytes);
                v
            }
            Storage::Disk { dir, index } => {
                let metas = index.remove(&cluster)?;
                let removed_bytes: u64 = metas.iter().map(|m| u64::from(m.len)).sum();
                self.total_bytes = self.total_bytes.saturating_sub(removed_bytes);
                let path = cluster_file_path(dir, cluster);
                let loaded = match load_chunks_from_disk(&path, &metas) {
                    Ok(v) => v,
                    Err(err) => {
                        self.io_errors_total = self.io_errors_total.saturating_add(1);
                        tracing::warn!(
                            cluster,
                            path = %path.display(),
                            error = %err,
                            "pending-data spill: disk read failed; chunks for this cluster are lost"
                        );
                        let _ = std::fs::remove_file(&path);
                        Vec::new()
                    }
                };
                let _ = std::fs::remove_file(&path);
                loaded
            }
        };
        if let Some(pos) = self.insertion_order.iter().position(|&c| c == cluster) {
            self.insertion_order.remove(pos);
        }
        Some(chunks)
    }

    /// True iff `cluster` has at least one stashed chunk.
    #[cfg(test)]
    pub fn contains(&self, cluster: u32) -> bool {
        match &self.storage {
            Storage::Memory { chunks } => chunks.contains_key(&cluster),
            Storage::Disk { index, .. } => index.contains_key(&cluster),
        }
    }

    /// Number of distinct clusters currently holding stashed chunks.
    #[cfg(test)]
    #[allow(dead_code)]
    pub fn cluster_count(&self) -> usize {
        match &self.storage {
            Storage::Memory { chunks } => chunks.len(),
            Storage::Disk { index, .. } => index.len(),
        }
    }

    /// Total bytes currently buffered across all clusters.
    pub fn total_bytes(&self) -> u64 {
        self.total_bytes
    }

    /// True iff no clusters are stashed.
    pub fn is_empty(&self) -> bool {
        match &self.storage {
            Storage::Memory { chunks } => chunks.is_empty(),
            Storage::Disk { index, .. } => index.is_empty(),
        }
    }

    /// Lifetime count of clusters dropped by eviction.
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

    /// Lifetime count of disk I/O errors (push failed, take read
    /// failed). Surfaced for operator diagnosis of a failing SD.
    pub fn io_errors_total(&self) -> u64 {
        self.io_errors_total
    }

    fn evict_to_fit(&mut self) {
        while self.total_bytes > self.max_bytes {
            let Some(oldest_cluster) = self.insertion_order.pop_front() else {
                break;
            };
            let (evicted_chunks, evicted_bytes) = match &mut self.storage {
                Storage::Memory { chunks } => {
                    let Some(v) = chunks.remove(&oldest_cluster) else {
                        continue;
                    };
                    let bytes: u64 = v.iter().map(|c| c.bytes.len() as u64).sum();
                    (v.len() as u64, bytes)
                }
                Storage::Disk { dir, index } => {
                    let Some(metas) = index.remove(&oldest_cluster) else {
                        continue;
                    };
                    let bytes: u64 = metas.iter().map(|m| u64::from(m.len)).sum();
                    let path = cluster_file_path(dir, oldest_cluster);
                    if let Err(err) = std::fs::remove_file(&path) {
                        if err.kind() != std::io::ErrorKind::NotFound {
                            tracing::debug!(
                                cluster = oldest_cluster,
                                path = %path.display(),
                                error = %err,
                                "pending-data spill: failed to remove evicted cluster file"
                            );
                        }
                    }
                    (metas.len() as u64, bytes)
                }
            };
            self.total_bytes = self.total_bytes.saturating_sub(evicted_bytes);
            self.evicted_clusters_total = self.evicted_clusters_total.saturating_add(1);
            self.evicted_chunks_total = self
                .evicted_chunks_total
                .saturating_add(evicted_chunks);
            self.evicted_bytes_total = self.evicted_bytes_total.saturating_add(evicted_bytes);
            tracing::warn!(
                cluster = oldest_cluster,
                chunks = evicted_chunks,
                bytes = evicted_bytes,
                buffer_bytes_after = self.total_bytes,
                buffer_cap_bytes = self.max_bytes,
                evicted_clusters_total = self.evicted_clusters_total,
                "pending-data spill: evicted oldest unresolved cluster to honour cap"
            );
        }
    }
}

/// `<spill_dir>/<cluster:08x>.bin`. Hex keeps file names short and
/// sortable, lower-case is ext4-friendly.
fn cluster_file_path(spill_dir: &Path, cluster: u32) -> PathBuf {
    spill_dir.join(format!("{cluster:08x}.bin"))
}

/// Create `spill_dir` if missing and remove any files in it.
/// Returns an `io::Error` if the directory could not be made
/// usable.
fn prepare_spill_dir(spill_dir: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(spill_dir)?;
    for entry in std::fs::read_dir(spill_dir)? {
        let entry = entry?;
        let path = entry.path();
        if path.is_file() {
            let _ = std::fs::remove_file(&path);
        }
    }
    Ok(())
}

/// Append one chunk to its cluster file. File format per chunk:
/// `[byte_in_cluster: u64 LE][len: u64 LE][bytes…]`. Returns the
/// file offset where the payload (not the header) starts.
fn append_chunk_to_disk(
    path: &Path,
    byte_in_cluster: usize,
    bytes: &[u8],
) -> std::io::Result<u64> {
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    let header_start = file.seek(SeekFrom::End(0))?;
    let mut header = [0u8; 16];
    header[..8].copy_from_slice(&(byte_in_cluster as u64).to_le_bytes());
    header[8..].copy_from_slice(&(bytes.len() as u64).to_le_bytes());
    file.write_all(&header)?;
    file.write_all(bytes)?;
    Ok(header_start + 16)
}

/// Read every chunk listed in `metas` from `path`. Chunks are
/// returned in metas order (= insertion order).
fn load_chunks_from_disk(
    path: &Path,
    metas: &[DiskChunkMeta],
) -> std::io::Result<Vec<PendingDataChunk>> {
    let mut file = File::open(path)?;
    let mut out = Vec::with_capacity(metas.len());
    for meta in metas {
        file.seek(SeekFrom::Start(meta.file_offset))?;
        let mut buf = vec![0u8; meta.len as usize];
        file.read_exact(&mut buf)?;
        out.push(PendingDataChunk {
            byte_in_cluster: meta.byte_in_cluster as usize,
            bytes: buf,
        });
    }
    Ok(out)
}

#[cfg(test)]
#[allow(clippy::expect_used, clippy::panic, clippy::unwrap_used)]
mod tests {
    use super::{DEFAULT_MAX_SPILL_BYTES, PendingSpill};

    #[test]
    fn push_and_take_round_trips_chunks_in_insertion_order() {
        let mut spill = PendingSpill::new();
        spill.push(7, 0, b"first");
        spill.push(7, 16, b"second");
        spill.push(8, 0, b"other");
        let chunks = spill.take(7).expect("cluster 7 present");
        assert_eq!(chunks.len(), 2);
        assert_eq!(chunks[0].byte_in_cluster, 0);
        assert_eq!(chunks[0].bytes, b"first");
        assert_eq!(chunks[1].byte_in_cluster, 16);
        assert_eq!(chunks[1].bytes, b"second");
        assert!(spill.contains(8));
        assert!(!spill.contains(7));
    }

    #[test]
    fn take_missing_returns_none() {
        let mut spill = PendingSpill::new();
        assert!(spill.take(42).is_none());
    }

    #[test]
    fn total_bytes_and_is_empty_track_state() {
        let mut spill = PendingSpill::new();
        assert!(spill.is_empty());
        assert_eq!(spill.total_bytes(), 0);
        spill.push(1, 0, &[0u8; 100]);
        assert!(!spill.is_empty());
        assert_eq!(spill.total_bytes(), 100);
        spill.take(1);
        assert!(spill.is_empty());
        assert_eq!(spill.total_bytes(), 0);
    }

    #[test]
    fn fifo_eviction_drops_oldest_cluster_first() {
        let mut spill = PendingSpill::with_capacity(10);
        spill.push(1, 0, &[1u8; 4]);
        spill.push(2, 0, &[2u8; 4]);
        spill.push(3, 0, &[3u8; 4]);
        // 12 > 10 → cluster 1 should evict.
        assert!(!spill.contains(1));
        assert!(spill.contains(2));
        assert!(spill.contains(3));
        assert_eq!(spill.evicted_clusters_total(), 1);
        assert_eq!(spill.evicted_chunks_total(), 1);
        assert_eq!(spill.evicted_bytes_total(), 4);
    }

    #[test]
    fn extra_chunks_on_existing_cluster_dont_promote_it() {
        let mut spill = PendingSpill::with_capacity(20);
        spill.push(1, 0, &[1u8; 8]);
        spill.push(2, 0, &[2u8; 8]);
        // Now cluster 1 receives a second chunk — it must still
        // count as the OLDEST, not be reordered to youngest.
        spill.push(1, 8, &[1u8; 8]);
        // total = 24 > 20 → cluster 1 evicts (both its chunks).
        assert!(!spill.contains(1));
        assert!(spill.contains(2));
        assert_eq!(spill.evicted_chunks_total(), 2);
    }

    #[test]
    fn default_cap_matches_const() {
        let spill = PendingSpill::new();
        // Indirectly: pushing 16 MiB - 1 byte stays, pushing more evicts.
        assert!(spill.total_bytes() < DEFAULT_MAX_SPILL_BYTES as u64);
    }

    #[test]
    fn regression_2026_05_24_unbounded_growth_does_not_recur() {
        // Simulates a Tesla burst where 1000 clusters each receive
        // a chunk but their owners never arrive. With the old
        // (unbounded) HashMap this allocated > 100 MB. With the
        // bounded spill, total_bytes must stay <= cap.
        let cap = 1024 * 1024;
        let mut spill = PendingSpill::with_capacity(cap);
        for c in 0..1000 {
            spill.push(c, 0, &[0u8; 4096]);
        }
        assert!(spill.total_bytes() <= cap as u64);
        assert!(spill.evicted_clusters_total() > 0);
    }

    #[test]
    fn disk_mode_round_trips_chunks_through_filesystem() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut spill =
            PendingSpill::open_disk(tmp.path().to_path_buf(), 1024 * 1024);
        spill.push(7, 0, b"first");
        spill.push(7, 16, b"second");
        spill.push(8, 0, b"other");
        // File should exist for cluster 7.
        let p7 = super::cluster_file_path(tmp.path(), 7);
        assert!(p7.exists(), "spill file for cluster 7 should exist");
        let chunks = spill.take(7).expect("cluster 7 present");
        assert_eq!(chunks.len(), 2);
        assert_eq!(chunks[0].bytes, b"first");
        assert_eq!(chunks[1].bytes, b"second");
        // File should be removed after take.
        assert!(!p7.exists(), "spill file for cluster 7 should be removed");
        // cluster 8 still present.
        assert!(spill.contains(8));
    }

    #[test]
    fn disk_mode_eviction_removes_files_and_updates_counters() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut spill = PendingSpill::open_disk(tmp.path().to_path_buf(), 10);
        spill.push(1, 0, &[1u8; 4]);
        spill.push(2, 0, &[2u8; 4]);
        spill.push(3, 0, &[3u8; 4]);
        assert_eq!(spill.evicted_clusters_total(), 1);
        let p1 = super::cluster_file_path(tmp.path(), 1);
        assert!(!p1.exists(), "evicted cluster file should be removed");
    }

    #[test]
    fn disk_mode_prepare_clears_stale_files() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let stale = super::cluster_file_path(tmp.path(), 42);
        std::fs::write(&stale, b"stale").expect("write stale");
        assert!(stale.exists());
        let _spill = PendingSpill::open_disk(tmp.path().to_path_buf(), 1024);
        assert!(!stale.exists(), "stale spill file should have been removed");
    }

    #[test]
    fn disk_mode_take_missing_returns_none() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut spill = PendingSpill::open_disk(tmp.path().to_path_buf(), 1024);
        assert!(spill.take(99).is_none());
    }
}
