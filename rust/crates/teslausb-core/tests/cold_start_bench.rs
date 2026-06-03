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
//! Measurements that compose the cold-start path:
//!
//! 1. **exFAT synthesis on a 64 GiB volume.** Worst-case
//!    metadata size (largest cluster heap + bitmap +
//!    backup-boot). Budget: 1 s.
//! 2. **Lazy cluster materialization of 10 000 distinct
//!    clusters** through a no-op materializer. Validates that
//!    the cache's lock + `BTreeMap` path doesn't itself blow
//!    the budget under realistic working-set sizes.
//!    Budget: 1 s.
//!
//! The 10 000-file synthesis budget (the row-2.14 deliverable) is
//! gated against the real `SynthBackend::open` cold-start path in
//! `teslafat/tests/exfat_cold_start_bench.rs`.
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
use teslausb_core::fs::geometry::Geometry;

const COLD_START_BUDGET: Duration = Duration::from_secs(1);
const TEN_THOUSAND: u32 = 10_000;
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

fn report(label: &str, elapsed: Duration) {
    let micros = elapsed.as_micros();
    eprintln!("[2.14 cold-start bench] {label}: {micros} µs (budget 1 000 000 µs)");
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
