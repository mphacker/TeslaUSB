//! Phase 2.14 — exFAT 10 000-file cold-start synthesis benchmark.
//!
//! The `docs/00-PLAN.md` row 2.14 budget is "≤ 1 s for a 10K-file
//! synthetic tree" — the wall-clock time between `daemon-start`
//! and "USB gadget visible to Tesla". This gates the exFAT
//! synthesis path that the deployed device actually runs:
//! `SynthBackend::open` walks a backing tree, plans the exFAT
//! cluster layout (root directory entry sets, FAT mirror,
//! allocation bitmap, upcase table) and returns a ready-to-serve
//! backend.
//!
//! The complementary metadata-only budgets (empty-volume synth,
//! boot-region read, lazy-cache materialization) live in
//! `teslausb-core/tests/cold_start_bench.rs`.
//!
//! ## What this file does **not** gate
//!
//! On-target performance: the Pi Zero 2 W is ~20× slower than the
//! dev box, so the dev-box 1 s ceiling is the production target,
//! not a hardware measurement. H2.7 owns the on-device budget.

#![allow(
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::panic,
    clippy::print_stderr,
    clippy::unwrap_used
)]

use std::path::PathBuf;
use std::time::{Duration, Instant};

use tempfile::TempDir;
use teslafat::backend::SynthBackend;
use teslafat::config::{Config, FsType, RetentionConfig};
use teslausb_core::backend::BlockBackend;

const COLD_START_BUDGET: Duration = Duration::from_secs(1);
const TEN_THOUSAND: u32 = 10_000;
const VOLUME_SIZE_GB: u32 = 4;

fn sample_cfg(backing_root: PathBuf) -> Config {
    Config {
        backing_root,
        volume_size_gb: VOLUME_SIZE_GB,
        volume_label: "COLDSTART".to_string(),
        cluster_size: None,
        fs_type: FsType::Exfat,
        retention: RetentionConfig::default(),
        spill_dir: None,
        reload_on_sighup: true,
    }
}

/// Create a backing root with 10 000 small files spread across a
/// handful of subdirectories — a realistic Tesla working set
/// (RecentClips/SentryClips full of `*-front.mp4`, `event.json`,
/// GPS logs). Files are 1 byte so the fixture builds quickly; the
/// synthesis cost is dominated by the directory-entry and
/// allocation work, not file payloads.
fn build_ten_thousand_file_tree(root: &TempDir) -> PathBuf {
    const FILES_PER_DIR: u32 = 500;
    let base = root.path().join("RecentClips");
    let mut made = 0u32;
    let mut dir_index = 0u32;
    while made < TEN_THOUSAND {
        let dir = base.join(format!("2026-01-01_{dir_index:04}"));
        std::fs::create_dir_all(&dir).unwrap();
        for f in 0..FILES_PER_DIR {
            if made >= TEN_THOUSAND {
                break;
            }
            std::fs::write(dir.join(format!("clip-{f:04}-front.mp4")), [0u8; 1]).unwrap();
            made += 1;
        }
        dir_index += 1;
    }
    root.path().to_path_buf()
}

fn report(label: &str, elapsed: Duration) {
    let micros = elapsed.as_micros();
    eprintln!("[2.14 exFAT cold-start] {label}: {micros} µs (budget 1 000 000 µs)");
}

#[test]
fn exfat_synth_cold_start_with_10k_file_tree_within_budget() {
    let tmp = TempDir::new().unwrap();
    let backing_root = build_ten_thousand_file_tree(&tmp);
    let cfg = sample_cfg(backing_root);

    let start = Instant::now();
    let backend = SynthBackend::open(&cfg).expect("synth must build");
    let elapsed = start.elapsed();

    report("SynthBackend::open on 10k-file tree", elapsed);
    // Reach through the API to defeat any compiler optimization
    // that would elide the construction if it were never read.
    assert!(backend.file_count() >= TEN_THOUSAND as usize);

    assert!(
        elapsed < COLD_START_BUDGET,
        "exFAT cold-start with 10k files took {elapsed:?}; budget is {COLD_START_BUDGET:?}",
    );
}

#[tokio::test]
async fn exfat_synth_cold_start_reads_boot_region_within_budget() {
    // Tesla's first reads against a freshly-mounted exFAT LUN cover
    // the main + backup boot regions; measure that read path on a
    // backend carrying a full 10k-file directory.
    let tmp = TempDir::new().unwrap();
    let backing_root = build_ten_thousand_file_tree(&tmp);
    let cfg = sample_cfg(backing_root);
    let backend = SynthBackend::open(&cfg).expect("synth must build");

    // 24 sectors = 12 KiB covers both the main and backup boot
    // regions — what fsck.exfat reads on mount.
    let mut buf = [0_u8; 12 * 1024];
    let start = Instant::now();
    backend.read(0, &mut buf).await.expect("read must succeed");
    let elapsed = start.elapsed();

    report("exFAT boot-region read (10k-file backend)", elapsed);
    assert!(
        elapsed < COLD_START_BUDGET,
        "boot-region read took {elapsed:?}; budget is {COLD_START_BUDGET:?}",
    );

    // Sanity: 0x55 0xAA at byte 510 of the main boot sector.
    assert_eq!(buf[510], 0x55);
    assert_eq!(buf[511], 0xAA);
}
