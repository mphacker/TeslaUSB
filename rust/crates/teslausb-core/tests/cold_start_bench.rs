//! Phase 2.14 — cold-start synthesis benchmark (CI gate).
//!
//! The `docs/00-PLAN.md` row 2.14 budget is "≤ 1 s for a 10K-file
//! synthetic tree" — the wall-clock time between `daemon-start`
//! and "USB gadget visible to Tesla". Tesla expects to see the
//! USB drive within ~3 s of boot; teslafat's share of that is
//! 1 s for FS synthesis with another 2 s for everything else
//! (kernel modprobe, NBD connect, `g_mass_storage` bind). See
//! plan §"Non-negotiables" item 1.
//!
//! ## What this file gates
//!
//! Three measurements that compose the cold-start path:
//!
//! 1. **Fat32 synthesis with 10 000 file chains.** The synthesizer
//!    walks the in-memory dir tree, builds the FAT table,
//!    composes boot/`FsInfo`, and returns a ready-to-serve
//!    `Fat32Synth`. Budget: 1 s. This is the closest current
//!    proxy for the row-2.14 deliverable since Phase 3 hasn't
//!    yet added user-file entries to the exFAT side.
//! 2. **exFAT synthesis on a 64 GiB volume.** Worst-case
//!    metadata size (largest cluster heap + bitmap +
//!    backup-boot). Budget: 1 s.
//! 3. **Lazy cluster materialization of 10 000 distinct
//!    clusters** through a no-op materializer. Validates that
//!    the cache's lock + `BTreeMap` path doesn't itself blow
//!    the budget under realistic working-set sizes.
//!    Budget: 1 s.
//!
//! On a modern dev box every measurement here completes in
//! single-digit-to-tens-of-milliseconds; the 1 s ceilings are
//! deliberately the production target so a regression that
//! adds 100 ms to a hot path still surfaces here long before
//! anyone deploys it.
//!
//! ## What this file does **not** gate
//!
//! * **On-target performance.** The Pi Zero 2 W is ~20×
//!   slower than the dev box for CPU-bound work and
//!   has additional contention from SDIO + `WiFi`. The
//!   on-target cold-start budget is verified by H2.7 (see
//!   `docs/00-PLAN.md` row H2.7 — "Cold-start time captured:
//!   synth start → mount succeeds. Target ≤ 1 s.").
//! * **End-to-end mount time.** That includes NBD,
//!   `g_mass_storage`, kernel modprobe — none of which is
//!   reachable from the dev box. H2.7 owns that.

#![allow(
    clippy::cognitive_complexity,
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::panic,
    clippy::print_stderr,
    clippy::unwrap_used
)]

use std::time::{Duration, Instant};

use teslausb_core::fs::exfat::geometry::ExfatGeometry;
use teslausb_core::fs::exfat::lazy_load::{
    ClusterMaterializer, LazyClusterCache, MaterializeError,
};
use teslausb_core::fs::exfat::synth::ExfatSynth;
use teslausb_core::fs::fat32::boot_sector::ROOT_DIRECTORY_CLUSTER;
use teslausb_core::fs::fat32::fat_table::InMemoryDirTree;
use teslausb_core::fs::fat32::geometry::Fat32Geometry;
use teslausb_core::fs::fat32::synth::Fat32Synth;
use teslausb_core::fs::geometry::Geometry;

const COLD_START_BUDGET: Duration = Duration::from_secs(1);
const TEN_THOUSAND: u32 = 10_000;
const TESTVOL_FAT_LABEL: &[u8; 11] = b"TESTVOL    ";
const TESTVOL_EXFAT_UTF16: &[u16] = &[
    b'T' as u16,
    b'E' as u16,
    b'S' as u16,
    b'T' as u16,
    b'V' as u16,
    b'O' as u16,
    b'L' as u16,
];
const SERIAL: u32 = 0xDEAD_BEEF;
const FAT32_VOLUME_BYTES: u64 = 4 * 1024 * 1024 * 1024;
const EXFAT_VOLUME_BYTES: u64 = 64 * 1024 * 1024 * 1024;
const CLUSTER_SIZE_BYTES: usize = 32 * 1024;

/// No-op materializer used to isolate the cache's overhead from
/// any backend cost. Fills the cluster with a deterministic
/// pattern so the benchmark proves the bytes round-trip end to
/// end (a benchmark that doesn't read the bytes back risks the
/// compiler optimizing the entire call away).
struct NoOpMaterializer;

impl ClusterMaterializer for NoOpMaterializer {
    fn materialize(&self, cluster: u32, out: &mut [u8]) -> Result<(), MaterializeError> {
        let high = u8::try_from(cluster & 0xFF).unwrap_or(0);
        for (i, slot) in out.iter_mut().enumerate() {
            let low = u8::try_from(i & 0xFF).unwrap_or(0);
            *slot = high ^ low;
        }
        Ok(())
    }
}

fn build_ten_thousand_chain_tree() -> InMemoryDirTree {
    // First chain is the root directory at cluster 2. Each of
    // the remaining 10 000 chains is a single-cluster file at
    // clusters 3..10_003 — a realistic small-file workload
    // (~4 KiB Tesla event.json files, GPS NMEA logs, etc.).
    let mut chains = Vec::with_capacity(TEN_THOUSAND as usize + 1);
    chains.push(vec![ROOT_DIRECTORY_CLUSTER]);
    for cluster in (ROOT_DIRECTORY_CLUSTER + 1)..=(ROOT_DIRECTORY_CLUSTER + TEN_THOUSAND) {
        chains.push(vec![cluster]);
    }
    InMemoryDirTree::from_chains(chains)
}

fn report(label: &str, elapsed: Duration) {
    let micros = elapsed.as_micros();
    eprintln!("[2.14 cold-start bench] {label}: {micros} µs (budget 1 000 000 µs)");
}

// ── Fat32 cold-start ─────────────────────────────────────────────────

#[test]
fn fat32_synth_cold_start_with_10k_file_tree_within_budget() {
    let tree = build_ten_thousand_chain_tree();
    let geo = Fat32Geometry::for_volume_size(FAT32_VOLUME_BYTES).expect("valid geometry");

    let start = Instant::now();
    let synth = Fat32Synth::new(geo, TESTVOL_FAT_LABEL, SERIAL, None, None, &tree)
        .expect("synth must build");
    let elapsed = start.elapsed();

    report("Fat32Synth::new on 10k-file tree", elapsed);
    // Reach through the API to defeat any compiler optimization
    // that would constant-fold the synth construction away if
    // the value were never read.
    let _ = synth.geometry().volume_size_bytes();

    assert!(
        elapsed < COLD_START_BUDGET,
        "Fat32 cold-start with 10k file chains took {elapsed:?}; budget is {COLD_START_BUDGET:?}",
    );
}

#[test]
fn fat32_synth_cold_start_reads_full_fat1_region_within_budget() {
    // Tesla's first SCSI read against a newly-mounted FAT32 LUN
    // is typically the entire reserved + first-FAT region.
    // Measure that read path end-to-end.
    let tree = build_ten_thousand_chain_tree();
    let geo = Fat32Geometry::for_volume_size(FAT32_VOLUME_BYTES).expect("valid geometry");
    let synth = Fat32Synth::new(geo, TESTVOL_FAT_LABEL, SERIAL, None, None, &tree)
        .expect("synth must build");

    // Read the first 16 MiB — covers boot, FsInfo, both FATs at
    // 4 GiB scale.
    let mut buf = vec![0_u8; 16 * 1024 * 1024];
    let start = Instant::now();
    synth.read(0, &mut buf).expect("read must succeed");
    let elapsed = start.elapsed();

    report("Fat32 first-16-MiB sweep read", elapsed);
    assert!(
        elapsed < COLD_START_BUDGET,
        "first 16 MiB sweep took {elapsed:?}; budget is {COLD_START_BUDGET:?}",
    );

    // Sanity: the first 11 bytes are the BPB jump + OEM name
    // (`MSWIN4.1` etc.) — at minimum non-zero, proving the read
    // path actually wrote bytes.
    assert!(
        buf[..11].iter().any(|&b| b != 0),
        "first 11 bytes of boot sector must contain JumpBoot + OEM name",
    );
}

// ── exFAT cold-start ─────────────────────────────────────────────────

#[test]
fn exfat_synth_cold_start_64gib_volume_within_budget() {
    let geo = ExfatGeometry::for_volume_size(EXFAT_VOLUME_BYTES).expect("valid geometry");

    let start = Instant::now();
    let synth = ExfatSynth::new(geo, SERIAL, TESTVOL_EXFAT_UTF16).expect("synth must build");
    let elapsed = start.elapsed();

    report("ExfatSynth::new on 64 GiB volume", elapsed);
    let _ = synth.geometry().volume_size_bytes();

    assert!(
        elapsed < COLD_START_BUDGET,
        "exFAT cold-start on 64 GiB took {elapsed:?}; budget is {COLD_START_BUDGET:?}",
    );
}

#[test]
fn exfat_synth_cold_start_reads_main_and_backup_boot_within_budget() {
    let geo = ExfatGeometry::for_volume_size(EXFAT_VOLUME_BYTES).expect("valid geometry");
    let synth = ExfatSynth::new(geo, SERIAL, TESTVOL_EXFAT_UTF16).expect("synth must build");

    // 24 sectors = 12 KiB covers both the main and backup boot
    // regions — what fsck.exfat reads on mount.
    let mut buf = [0_u8; 12 * 1024];
    let start = Instant::now();
    synth.read(0, &mut buf).expect("read must succeed");
    let elapsed = start.elapsed();

    report("exFAT boot-region (24-sector) read", elapsed);
    assert!(
        elapsed < COLD_START_BUDGET,
        "boot+backup boot read took {elapsed:?}; budget is {COLD_START_BUDGET:?}",
    );

    // Sanity: 0x55 0xAA at byte 510 of the main boot sector.
    assert_eq!(buf[510], 0x55);
    assert_eq!(buf[511], 0xAA);
}

// ── Lazy load cold-start ─────────────────────────────────────────────

#[test]
fn lazy_cache_materializes_10k_distinct_clusters_within_budget() {
    let cache = LazyClusterCache::new(NoOpMaterializer, CLUSTER_SIZE_BYTES, TEN_THOUSAND as usize)
        .expect("cache must build");

    let mut buf = vec![0_u8; CLUSTER_SIZE_BYTES];
    let start = Instant::now();
    for cluster in 0..TEN_THOUSAND {
        cache
            .read_cluster_chunk(cluster, 0, &mut buf)
            .expect("read must succeed");
    }
    let elapsed = start.elapsed();

    report(
        "LazyClusterCache: 10k distinct cluster materializations",
        elapsed,
    );
    assert!(
        elapsed < COLD_START_BUDGET,
        "10k materializations took {elapsed:?}; budget is {COLD_START_BUDGET:?}",
    );

    assert_eq!(cache.cache_len().unwrap(), TEN_THOUSAND as usize);
}

#[test]
fn lazy_cache_serves_10k_warm_reads_within_budget() {
    // After the materialization pass, reading every cluster
    // again should hit cache only (no materializer calls) and
    // therefore complete much faster.
    let cache = LazyClusterCache::new(NoOpMaterializer, CLUSTER_SIZE_BYTES, TEN_THOUSAND as usize)
        .expect("cache must build");

    let mut buf = vec![0_u8; CLUSTER_SIZE_BYTES];
    for cluster in 0..TEN_THOUSAND {
        cache
            .read_cluster_chunk(cluster, 0, &mut buf)
            .expect("warm-up read");
    }

    let start = Instant::now();
    for cluster in 0..TEN_THOUSAND {
        cache
            .read_cluster_chunk(cluster, 0, &mut buf)
            .expect("warm read must succeed");
    }
    let elapsed = start.elapsed();

    report("LazyClusterCache: 10k warm reads", elapsed);
    assert!(
        elapsed < COLD_START_BUDGET,
        "10k warm reads took {elapsed:?}; budget is {COLD_START_BUDGET:?}",
    );

    // Sanity: warm read of cluster 0 still returns the
    // materializer's pattern (no cache poisoning).
    cache.read_cluster_chunk(0, 0, &mut buf).expect("read");
    assert_eq!(buf[0], 0, "byte 0 of cluster 0 is 0 ^ 0 = 0");
    assert_eq!(buf[1], 1, "byte 1 of cluster 0 is 0 ^ 1 = 1");
}
