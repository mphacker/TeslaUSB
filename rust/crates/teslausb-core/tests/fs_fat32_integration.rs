//! Phase 2.7 — `fs::fat32` integration test.
//!
//! Exercises the four region synthesizers from Phases 2.2 – 2.5
//! through the [`Fat32Synth`] read dispatcher (Phase 2.6) **from
//! outside the crate**, i.e. via the public API only — anything a
//! downstream consumer (the `teslafat` daemon, in Phase 3) could
//! reach.
//!
//! ## Why an integration test
//!
//! The unit tests in `fs/fat32/synth.rs` cover the same dispatcher
//! but live in the same module — they can touch private fields
//! (`s.fat_table.fat_size_sectors()`) and the test geometry
//! constants. An external integration test catches three classes
//! of breakage that unit tests can miss:
//!
//! 1. **Public-API hygiene** — any missing `pub` (on a struct,
//!    constant, or trait method) will make this file fail to
//!    compile. The unit tests would still pass.
//! 2. **Cross-module composition** — every offset class (boot,
//!    `FsInfo`, reserved-gap, backup-boot, FAT1, FAT2, data) flows
//!    through one [`Fat32Synth::read`] call here. A regression in
//!    the dispatcher's region lookup (e.g. an off-by-one in
//!    [`Fat32Geometry::region_at`]) would land first in this file.
//! 3. **Repeat-read determinism** — every test reads the exact
//!    same byte range twice (or via two different chunk
//!    granularities) and asserts byte-equality. Catches any
//!    accidental mutation introduced by the dispatcher.
//!
//! ## What this file does **not** cover
//!
//! * **Kernel mount + `cmp`.** The plan-stated end-to-end "mount
//!   via `nbd-client` + `loop`, `cmp` against source" requires
//!   Linux, root, and the kernel `nbd` module — none of which is
//!   present on the dev box. That test lives in the **H2 hardware
//!   gate** (`docs/00-PLAN.md` rows H2.1 – H2.8), where it runs
//!   against the real Pi target. Per ADR-0007 the same pattern was
//!   adopted in Phase 1.7: dev-box integration tests speak the
//!   protocol directly from the test process; kernel-tooling
//!   verification is owned by the H-phases.
//! * **Directory entries laid out in the data region.** Phase 2.6
//!   intentionally zero-fills the data region (see the module doc
//!   on `synth.rs`); placing 32-byte SFN/LFN entries on specific
//!   cluster numbers is Phase 2.13 (`lazy_load.rs`) work. This
//!   file asserts the data region is zero-filled, matching the
//!   2.6 contract.

#![allow(
    clippy::cognitive_complexity,
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::panic,
    clippy::unwrap_used
)]

use teslausb_core::fs::fat32::boot_sector::{self, BOOT_SECTOR_SIZE_BYTES, ROOT_DIRECTORY_CLUSTER};
use teslausb_core::fs::fat32::fat_table::{
    END_OF_CHAIN_MARKER, END_OF_CHAIN_MIN, FAT_SECTOR_SIZE_BYTES, FREE_CLUSTER, FatTable,
    InMemoryDirTree,
};
use teslausb_core::fs::fat32::fsinfo::{self, FSINFO_SECTOR_SIZE_BYTES};
use teslausb_core::fs::fat32::geometry::{
    BACKUP_BOOT_SECTOR_INDEX, FSINFO_SECTOR_INDEX, Fat32Geometry, RESERVED_SECTORS,
};
use teslausb_core::fs::fat32::synth::{Fat32Synth, Fat32SynthError};
use teslausb_core::fs::geometry::{Geometry, SECTOR_SIZE_BYTES};

// ── Fixture constants ────────────────────────────────────────────────

const LABEL: &[u8; 11] = b"TESTVOL    ";
const SERIAL: u32 = 0xDEAD_BEEF;
const SMALL_VOLUME_BYTES: u64 = 34 * 1024 * 1024;
const FOUR_GIB: u64 = 4 * 1024 * 1024 * 1024;
const ONE_MIB: usize = 1024 * 1024;

// ── Fixture helpers ──────────────────────────────────────────────────

fn synth_with(volume_size: u64, tree: &InMemoryDirTree) -> Fat32Synth {
    let geo = Fat32Geometry::for_volume_size(volume_size).expect("valid geometry");
    Fat32Synth::new(geo, LABEL, SERIAL, None, None, tree).expect("valid synth")
}

fn read_range(s: &Fat32Synth, offset: u64, len: usize) -> Vec<u8> {
    let mut buf = vec![0u8; len];
    s.read(offset, &mut buf).expect("read ok");
    buf
}

fn fat1_offset_bytes() -> u64 {
    u64::from(RESERVED_SECTORS) * u64::from(SECTOR_SIZE_BYTES)
}

fn fat_size_bytes(tree: &InMemoryDirTree, volume_size: u64) -> u64 {
    let geo = Fat32Geometry::for_volume_size(volume_size).unwrap();
    let table = FatTable::build(&geo, tree).unwrap();
    u64::from(table.fat_size_sectors()) * u64::from(SECTOR_SIZE_BYTES)
}

fn parse_fat_entry(fat_region: &[u8], cluster: u32) -> u32 {
    let off = (cluster as usize) * 4;
    let bytes: [u8; 4] = fat_region[off..off + 4].try_into().unwrap();
    u32::from_le_bytes(bytes) & 0x0FFF_FFFF
}

fn is_eoc(entry: u32) -> bool {
    entry >= END_OF_CHAIN_MIN
}

// ── Boot / FSinfo / backup-boot integration ──────────────────────────

#[test]
fn boot_sector_via_dispatcher_matches_independent_synthesize() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);

    let read_boot = read_range(&s, 0, BOOT_SECTOR_SIZE_BYTES);
    let independent =
        boot_sector::synthesize(s.geometry(), LABEL, SERIAL).expect("boot synthesize ok");
    assert_eq!(
        &read_boot[..],
        &independent[..],
        "dispatcher boot bytes must equal a fresh boot_sector::synthesize"
    );
}

#[test]
fn fsinfo_via_dispatcher_matches_independent_synthesize() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);

    let fsinfo_offset = u64::from(FSINFO_SECTOR_INDEX) * u64::from(SECTOR_SIZE_BYTES);
    let read_fsinfo = read_range(&s, fsinfo_offset, FSINFO_SECTOR_SIZE_BYTES);
    let independent = fsinfo::synthesize(s.geometry(), None, None).expect("fsinfo synthesize ok");
    assert_eq!(
        &read_fsinfo[..],
        &independent[..],
        "dispatcher FSinfo bytes must equal a fresh fsinfo::synthesize"
    );
}

#[test]
fn backup_boot_sector_at_sector_6_mirrors_primary() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);

    let primary = read_range(&s, 0, BOOT_SECTOR_SIZE_BYTES);
    let backup_offset = u64::from(BACKUP_BOOT_SECTOR_INDEX) * u64::from(SECTOR_SIZE_BYTES);
    let backup = read_range(&s, backup_offset, BOOT_SECTOR_SIZE_BYTES);
    assert_eq!(
        primary, backup,
        "fatgen103 §3.4: backup boot sector at sector 6 must be a byte-for-byte copy"
    );
}

#[test]
fn reserved_gap_between_fsinfo_and_backup_is_zero() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);

    // Sectors 2..6 (between FsInfo at 1 and backup boot at 6).
    let gap_start = 2 * u64::from(SECTOR_SIZE_BYTES);
    let gap_len = 4 * (SECTOR_SIZE_BYTES as usize);
    let gap = read_range(&s, gap_start, gap_len);
    assert!(gap.iter().all(|&b| b == 0), "reserved gap must be zero");
}

#[test]
fn reserved_sectors_7_through_31_are_zero() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);

    // Sectors 7..32 are the rest of the reserved region (the FAT
    // starts at sector RESERVED_SECTORS = 32).
    let start = 7 * u64::from(SECTOR_SIZE_BYTES);
    let len = ((RESERVED_SECTORS - 7) as usize) * (SECTOR_SIZE_BYTES as usize);
    let region = read_range(&s, start, len);
    assert!(
        region.iter().all(|&b| b == 0),
        "post-backup-boot reserved region must be zero"
    );
}

// ── FAT region integration ───────────────────────────────────────────

#[test]
fn fat1_first_sector_via_dispatcher_matches_synthesize_sector_zero() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);

    let dispatcher = read_range(&s, fat1_offset_bytes(), FAT_SECTOR_SIZE_BYTES);
    let geo = Fat32Geometry::for_volume_size(SMALL_VOLUME_BYTES).unwrap();
    let table = FatTable::build(&geo, &tree).unwrap();
    let independent = table.synthesize_sector(0).expect("FAT sector 0 ok");

    assert_eq!(
        dispatcher,
        independent.to_vec(),
        "FAT1 sector 0 via dispatcher must match FatTable::synthesize_sector(0)"
    );
}

#[test]
fn fat2_mirrors_fat1_via_dispatcher() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);
    let fat_size = fat_size_bytes(&tree, SMALL_VOLUME_BYTES);

    // Compare FAT1 and FAT2 sector-by-sector for the first
    // four sectors (covers both the reserved entries 0/1 and the
    // root-cluster EOC at entry 2).
    let compare_sectors: u32 = 4;
    for sec in 0..compare_sectors {
        let off = u64::from(sec) * u64::from(SECTOR_SIZE_BYTES);
        let fat1 = read_range(&s, fat1_offset_bytes() + off, FAT_SECTOR_SIZE_BYTES);
        let fat2 = read_range(
            &s,
            fat1_offset_bytes() + fat_size + off,
            FAT_SECTOR_SIZE_BYTES,
        );
        assert_eq!(fat1, fat2, "FAT2 must mirror FAT1 at sector {sec}");
    }
}

#[test]
fn fragmented_chain_walks_correctly_via_dispatcher_reads() {
    // [3 → 100 → 200] is fragmented enough that the chain entries
    // span multiple FAT sectors at FAT_ENTRIES_PER_SECTOR = 128 each.
    let chain_a = vec![ROOT_DIRECTORY_CLUSTER];
    let chain_b = vec![3, 100, 200];
    let chain_c = vec![4, 5, 6];
    let tree = InMemoryDirTree::from_chains(vec![chain_a, chain_b.clone(), chain_c]);
    let s = synth_with(FOUR_GIB, &tree);

    // Read the entire FAT1 region in one go so we can index into
    // it by cluster number.
    let fat_size = fat_size_bytes(&tree, FOUR_GIB);
    let fat_region = read_range(&s, fat1_offset_bytes(), usize::try_from(fat_size).unwrap());

    // Walk chain_b: start at cluster 3, follow next-pointers until
    // we hit EOC.
    let mut walked = Vec::new();
    let mut cluster = chain_b[0];
    let mut hop_budget = 16;
    loop {
        walked.push(cluster);
        let next = parse_fat_entry(&fat_region, cluster);
        if is_eoc(next) {
            break;
        }
        cluster = next;
        hop_budget -= 1;
        assert!(hop_budget > 0, "chain walk exceeded hop budget");
    }
    assert_eq!(
        walked, chain_b,
        "chain walk via dispatcher reads must reproduce the original chain"
    );

    // The cluster between 6 (end of chain_c) and 100 (chain_b[1])
    // must be FREE — confirms the FAT only marks the allocated
    // clusters.
    assert_eq!(parse_fat_entry(&fat_region, 50), FREE_CLUSTER);
    assert_eq!(parse_fat_entry(&fat_region, 99), FREE_CLUSTER);
}

// ── Data region integration ──────────────────────────────────────────

#[test]
fn data_region_first_cluster_is_zero_filled_in_phase_2_6() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(FOUR_GIB, &tree);
    let geo = s.geometry();

    let data_region = geo
        .regions()
        .iter()
        .find(|r| matches!(r.kind, teslausb_core::fs::geometry::RegionKind::Data))
        .expect("4 GiB volume has a Data region");
    let len = usize::try_from(u64::from(geo.bytes_per_cluster())).unwrap();
    let first_cluster_bytes = read_range(&s, data_region.start, len);
    assert!(
        first_cluster_bytes.iter().all(|&b| b == 0),
        "Phase 2.6 leaves the data region zero-filled (see synth.rs module doc)"
    );
}

// ── Whole-volume integration ─────────────────────────────────────────

#[test]
fn small_volume_read_in_one_meg_chunks_totals_volume_size_bytes() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);
    let total = s.geometry().volume_size_bytes();

    let mut cursor = 0_u64;
    let mut total_read = 0_u64;
    while cursor < total {
        let chunk = u64::try_from(ONE_MIB).unwrap().min(total - cursor);
        let chunk_usize = usize::try_from(chunk).unwrap();
        let mut buf = vec![0u8; chunk_usize];
        s.read(cursor, &mut buf).expect("chunked read ok");
        cursor += chunk;
        total_read += chunk;
    }
    assert_eq!(
        total_read, total,
        "full volume read total must equal volume_size_bytes()"
    );
}

#[test]
fn read_repeated_at_same_offset_returns_identical_bytes() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);

    // Pick a span that crosses boot → FSinfo → reserved → backup
    // → FAT1 (offsets 0..16896, the first 33 sectors).
    let span_len = 33 * (SECTOR_SIZE_BYTES as usize);
    let first = read_range(&s, 0, span_len);
    let second = read_range(&s, 0, span_len);
    assert_eq!(
        first, second,
        "two reads of the same offset must return byte-identical output (no mutation in the dispatcher)"
    );
}

#[test]
fn unaligned_chunk_sizes_produce_consistent_byte_stream() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);

    // Read sector 6 (the backup boot) two ways:
    // (a) one 512-byte read; (b) nine reads of varied sizes that sum to 512.
    let baseline = read_range(&s, 3072, 512);

    let chunk_sizes = [1_usize, 17, 51, 53, 64, 71, 100, 73, 82];
    assert_eq!(chunk_sizes.iter().sum::<usize>(), 512);

    let mut pieced = Vec::with_capacity(512);
    let mut off = 3072_u64;
    for size in chunk_sizes {
        let mut buf = vec![0u8; size];
        s.read(off, &mut buf).expect("partial read ok");
        pieced.extend_from_slice(&buf);
        off += u64::try_from(size).unwrap();
    }
    assert_eq!(
        baseline, pieced,
        "chunk granularity must not change the resulting byte stream"
    );
}

// ── Phase 2.17: BackingTree → Fat32Layout → Fat32Synth round-trip ────

mod phase_2_17 {
    use std::path::PathBuf;
    use std::time::SystemTime;

    use teslausb_core::fs::backing_tree::{BackingDir, BackingFile, BackingTree};
    use teslausb_core::fs::data_cluster_source::DataClusterSource;
    use teslausb_core::fs::fat32::boot_sector::ROOT_DIRECTORY_CLUSTER;
    use teslausb_core::fs::fat32::directory::{ATTR_DIRECTORY, FileAttributes};
    use teslausb_core::fs::fat32::geometry::Fat32Geometry;
    use teslausb_core::fs::fat32::layout::Fat32Layout;
    use teslausb_core::fs::fat32::synth::Fat32Synth;

    const LABEL: &[u8; 11] = b"TESTVOL    ";
    const SERIAL: u32 = 0xDEAD_BEEF;
    const SMALL: u64 = 34 * 1024 * 1024;

    fn empty_dir(name: &str) -> BackingDir {
        BackingDir {
            name: name.to_string(),
            backing_path: PathBuf::from("/").join(name),
            mtime: SystemTime::UNIX_EPOCH,
            subdirs: Vec::new(),
            files: Vec::new(),
        }
    }

    fn file(name: &str, size: u64) -> BackingFile {
        BackingFile {
            name: name.to_string(),
            backing_path: PathBuf::from("/").join(name),
            size,
            mtime: SystemTime::UNIX_EPOCH,
        }
    }

    fn build_synth(tree: &BackingTree, volume_bytes: u64) -> (Fat32Synth, Fat32Layout, u64, u32) {
        let geo = Fat32Geometry::for_volume_size(volume_bytes).expect("valid geometry");
        let layout = Fat32Layout::plan(&geo, LABEL, tree).expect("layout plans");
        let first_data_byte = layout.first_data_byte();
        let bytes_per_cluster = layout.bytes_per_cluster();
        // Snapshot the chains so we can build the synth (which
        // consumes `geo`) and still move the layout into
        // `with_data_source`.
        let chains = layout.chains().clone();
        let free = layout.free_cluster_count();
        let next_free = layout.next_free_cluster_hint();
        let synth = Fat32Synth::new(geo, LABEL, SERIAL, Some(free), next_free, &chains)
            .expect("synth builds");
        // Plan once more to obtain an owned `Fat32Layout` to
        // hand to `with_data_source`. Planning is deterministic
        // for a given (geometry, tree), so the data-source
        // layout matches the chain layout byte-for-byte.
        let layout_for_source = Fat32Layout::plan(
            &Fat32Geometry::for_volume_size(volume_bytes).unwrap(),
            LABEL,
            tree,
        )
        .expect("data-source layout plans");
        let synth = synth.with_data_source(Box::new(layout_for_source));
        (synth, layout, first_data_byte, bytes_per_cluster)
    }

    fn cluster_offset(first_data_byte: u64, bytes_per_cluster: u32, cluster: u32) -> u64 {
        first_data_byte + u64::from(cluster - 2) * u64::from(bytes_per_cluster)
    }

    #[test]
    fn dispatcher_serves_root_dir_entries_for_one_file_tree() {
        let mut root = empty_dir("");
        root.files.push(file("hello.txt", 0));
        let tree = BackingTree { root };
        let (synth, _layout, first_data_byte, bytes_per_cluster) = build_synth(&tree, SMALL);

        let root_offset =
            cluster_offset(first_data_byte, bytes_per_cluster, ROOT_DIRECTORY_CLUSTER);
        let mut buf = vec![0u8; 96];
        synth
            .read(root_offset, &mut buf)
            .expect("read root cluster");
        // Layout after D1:
        //   [0..32]   volume label entry → "TESTVOL    " + ATTR_VOLUME_ID
        //   [32..64]  LFN ordinal 1+LAST at 32, attribute 0x0F at 43
        //   [64..96]  SFN entry "F000001    " + ATTR_ARCHIVE
        assert_eq!(&buf[0..11], LABEL);
        assert_eq!(buf[11], 0x08);
        assert_eq!(buf[32], 0x41);
        assert_eq!(buf[32 + 11], 0x0F);
        assert_eq!(&buf[64..75], b"F000001    ");
        assert_eq!(buf[75], FileAttributes::archive().raw());
    }

    #[test]
    fn dispatcher_serves_subdir_dot_entries() {
        let mut root = empty_dir("");
        root.subdirs.push(empty_dir("sub"));
        let tree = BackingTree { root };
        let (synth, _layout, first_data_byte, bytes_per_cluster) = build_synth(&tree, SMALL);

        // sub = cluster 3.
        let sub_offset = cluster_offset(first_data_byte, bytes_per_cluster, 3);
        let mut buf = vec![0u8; 64];
        synth.read(sub_offset, &mut buf).expect("read sub cluster");
        assert_eq!(buf[0], b'.');
        assert_eq!(buf[11], ATTR_DIRECTORY);
        assert_eq!(&buf[32..34], b"..");
    }

    #[test]
    fn dispatcher_without_data_source_still_zero_fills() {
        // The Phase-2.6 contract: with no DataClusterSource
        // installed, the data region zero-fills. The existing
        // tests above already cover this, but this test pins it
        // down in the new BackingTree-aware module to catch any
        // future regression where the wiring code accidentally
        // installs a source by default.
        use teslausb_core::fs::fat32::fat_table::InMemoryDirTree;
        let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
        let geo = Fat32Geometry::for_volume_size(SMALL).unwrap();
        let synth = Fat32Synth::new(geo, LABEL, SERIAL, None, None, &tree).unwrap();
        // Cluster 2's offset.
        let off = synth.geometry().first_data_sector()
            * u64::from(teslausb_core::fs::geometry::SECTOR_SIZE_BYTES);
        let mut buf = vec![0u8; 512];
        synth.read(off, &mut buf).unwrap();
        assert!(buf.iter().all(|&b| b == 0));
    }

    #[test]
    fn dispatcher_partial_offset_within_cluster_matches_layout_source() {
        let mut root = empty_dir("");
        root.files.push(file("a.bin", 0));
        let tree = BackingTree { root };
        let (synth, layout, first_data_byte, bytes_per_cluster) = build_synth(&tree, SMALL);

        let root_offset =
            cluster_offset(first_data_byte, bytes_per_cluster, ROOT_DIRECTORY_CLUSTER);
        // Read 11 bytes starting at offset 64 within the root
        // cluster — should be the SFN field of the first child
        // entry (label at 0, LFN at 32, SFN at 64).
        let mut via_synth = vec![0u8; 11];
        synth
            .read(root_offset + 64, &mut via_synth)
            .expect("partial read");
        let mut via_layout = vec![0u8; 11];
        layout.read_cluster_bytes(ROOT_DIRECTORY_CLUSTER, 64, &mut via_layout);
        assert_eq!(via_synth, via_layout);
        assert_eq!(&via_synth[..], b"F000001    ");
    }
}

// ── Error-path integration ───────────────────────────────────────────

#[test]
fn read_past_volume_end_is_rejected_via_public_api() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);
    let total = s.geometry().volume_size_bytes();

    // Reading 513 bytes from the last 512-byte sector extends one
    // byte past EOF.
    let err = s.read(total - 512, &mut [0u8; 513]).expect_err("must fail");
    assert!(matches!(err, Fat32SynthError::LengthExceedsVolume { .. }));
}

#[test]
fn read_offset_at_volume_end_with_data_is_rejected_via_public_api() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);
    let total = s.geometry().volume_size_bytes();

    let err = s.read(total, &mut [0u8; 1]).expect_err("must fail");
    assert!(matches!(err, Fat32SynthError::OffsetBeyondVolume { .. }));
}

#[test]
fn boot_sector_ends_with_55aa_signature_via_dispatcher() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);

    let sig = read_range(&s, 510, 2);
    assert_eq!(sig, vec![0x55, 0xAA], "fatgen103 §3.4: boot signature");

    // Same for the backup boot sector at sector 6.
    let backup_sig = read_range(&s, 3072 + 510, 2);
    assert_eq!(backup_sig, vec![0x55, 0xAA], "backup boot signature");
}

#[test]
fn end_of_chain_marker_for_root_cluster_present_in_dispatcher_view_of_fat() {
    let tree = InMemoryDirTree::from_chains(vec![vec![ROOT_DIRECTORY_CLUSTER]]);
    let s = synth_with(SMALL_VOLUME_BYTES, &tree);

    // Cluster 2 lives at offset 2*4=8 inside FAT sector 0.
    let buf = read_range(&s, fat1_offset_bytes() + 8, 4);
    let entry = u32::from_le_bytes(buf.try_into().unwrap()) & 0x0FFF_FFFF;
    assert!(
        is_eoc(entry),
        "FAT[2] must be an EOC marker for the root directory; got {entry:#010X}"
    );
    assert_eq!(
        entry, END_OF_CHAIN_MARKER,
        "FatTable::build emits the canonical END_OF_CHAIN_MARKER"
    );
}
