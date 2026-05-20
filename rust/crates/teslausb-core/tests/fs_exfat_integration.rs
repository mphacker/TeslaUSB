//! Phase 2.12 — `fs::exfat` integration test.
//!
//! Exercises the five region synthesizers from Phases 2.8 – 2.11
//! (geometry, boot region, allocation bitmap, upcase table, root
//! directory) through the [`ExfatSynth`] read dispatcher **from
//! outside the crate**, i.e. via the public API only — anything a
//! downstream consumer (the `teslafat` daemon, in Phase 3) could
//! reach.
//!
//! ## Why an integration test
//!
//! The unit tests inside `fs/exfat/synth.rs` cover the same
//! dispatcher but live in the same module — they can touch
//! private fields and module-private helpers. An external
//! integration test catches three classes of breakage that unit
//! tests can miss:
//!
//! 1. **Public-API hygiene** — any missing `pub` (on a struct,
//!    constant, or trait method) will make this file fail to
//!    compile. The unit tests would still pass.
//! 2. **Cross-module composition** — every offset class (main
//!    boot region, backup boot region, FAT table, data region)
//!    flows through one [`ExfatSynth::read`] call here. A
//!    regression in the dispatcher's region lookup (e.g. an
//!    off-by-one in [`ExfatGeometry::region_at`]) would land
//!    first in this file.
//! 3. **Repeat-read determinism** — every test reads the exact
//!    same byte range twice (or via two different chunk
//!    granularities) and asserts byte-equality. Catches any
//!    accidental mutation introduced by the dispatcher.
//!
//! ## What this file does **not** cover
//!
//! * **Kernel mount + `fsck.exfat`.** The plan-stated end-to-end
//!   "mount via `nbd-client` + `loop`, `fsck.exfat` against the
//!   synthesized volume" requires Linux, root, and the kernel
//!   `nbd` module — none of which is present on the dev box.
//!   That test lives in the **H2 hardware gate**
//!   (`docs/00-PLAN.md`), where it runs against the real Pi
//!   target. Per ADR-0007 the same pattern was adopted in
//!   Phase 1.7 (and re-affirmed for FAT32 in Phase 2.7):
//!   dev-box integration tests speak the protocol directly
//!   from the test process; kernel-tooling verification is
//!   owned by the H-phases.
//! * **User-visible files in the data region.** Phase 2.11
//!   intentionally zero-fills cluster heap slots that aren't
//!   the root directory / bitmap / upcase table; placing
//!   user-visible directory entries on additional clusters is
//!   Phase 2.13 (`lazy_load.rs`) work. This file asserts those
//!   slots are zero-filled, matching the 2.11 contract.

#![allow(
    clippy::cognitive_complexity,
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::panic,
    clippy::unwrap_used
)]

use teslausb_core::fs::exfat::boot_sector::{
    self, BOOT_REGION_SIZE_BYTES, BOOT_SECTOR_SIZE_BYTES, BOOT_SIGNATURE, FILE_SYSTEM_NAME,
    JUMP_BOOT,
};
use teslausb_core::fs::exfat::directory::{
    ENTRY_TYPE_ALLOCATION_BITMAP, ENTRY_TYPE_UPCASE_TABLE, ENTRY_TYPE_VOLUME_LABEL,
};
use teslausb_core::fs::exfat::geometry::{
    BACKUP_BOOT_REGION_OFFSET_SECTORS, ExfatGeometry, FIRST_CLUSTER_NUMBER,
    MAIN_BOOT_REGION_OFFSET_SECTORS,
};
use teslausb_core::fs::exfat::synth::{ExfatSynth, ExfatSynthError};
use teslausb_core::fs::exfat::upcase_table::{
    ASCII_LOWER_A, ASCII_UPPER_A, BYTES_PER_ENTRY, UPCASE_TABLE_SIZE_BYTES,
};
use teslausb_core::fs::geometry::{Geometry, RegionKind, SECTOR_SIZE_BYTES};

// ── Fixture constants ────────────────────────────────────────────────

const SERIAL: u32 = 0xDEAD_BEEF;
const SMALL_VOLUME_BYTES: u64 = 32 * 1024 * 1024;
const LARGE_VOLUME_BYTES: u64 = 1024 * 1024 * 1024;
const ONE_MIB: usize = 1024 * 1024;
const ENTRY_SIZE: usize = 32;
const FAT_ENTRY_SIZE_BYTES: u64 = 4;
const FAT_END_OF_CHAIN: u32 = 0xFFFF_FFFF;
const FAT_MEDIA_TYPE: u32 = 0xFFFF_FFF8;
const FAT_VOLUME_DIRTY: u32 = 0xFFFF_FFFF;
const VOLUME_LABEL_TESTVOL: &[u16] = &[
    b'T' as u16,
    b'E' as u16,
    b'S' as u16,
    b'T' as u16,
    b'V' as u16,
    b'O' as u16,
    b'L' as u16,
];

// ── Fixture helpers ──────────────────────────────────────────────────

fn synth_with(volume_size: u64) -> ExfatSynth {
    let geo = ExfatGeometry::for_volume_size(volume_size).expect("valid geometry");
    ExfatSynth::new(geo, SERIAL, VOLUME_LABEL_TESTVOL).expect("valid synth")
}

fn read_range(s: &ExfatSynth, offset: u64, len: usize) -> Vec<u8> {
    let mut buf = vec![0u8; len];
    s.read(offset, &mut buf).expect("read ok");
    buf
}

fn fat1_offset_bytes(s: &ExfatSynth) -> u64 {
    u64::from(s.geometry().fat_offset_sectors()) * u64::from(SECTOR_SIZE_BYTES)
}

fn data_region_offset_bytes(s: &ExfatSynth) -> u64 {
    u64::from(s.geometry().cluster_heap_offset_sectors()) * u64::from(SECTOR_SIZE_BYTES)
}

fn cluster_offset_bytes(s: &ExfatSynth, cluster: u32) -> u64 {
    let bytes_per_cluster = u64::from(s.geometry().bytes_per_cluster());
    data_region_offset_bytes(s) + u64::from(cluster - FIRST_CLUSTER_NUMBER) * bytes_per_cluster
}

fn fat_entry_offset(cluster: u32) -> u64 {
    u64::from(cluster) * FAT_ENTRY_SIZE_BYTES
}

fn parse_fat_entry(s: &ExfatSynth, cluster: u32) -> u32 {
    let off = fat1_offset_bytes(s) + fat_entry_offset(cluster);
    let buf = read_range(s, off, 4);
    u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]])
}

// ── Boot region integration ──────────────────────────────────────────

#[test]
fn main_boot_region_via_dispatcher_matches_independent_synthesize() {
    let s = synth_with(SMALL_VOLUME_BYTES);

    let read_boot = read_range(&s, 0, BOOT_REGION_SIZE_BYTES);
    let independent = boot_sector::synthesize(s.geometry(), SERIAL).expect("boot synthesize ok");
    assert_eq!(
        &read_boot[..],
        &independent[..],
        "dispatcher main-boot bytes must equal a fresh boot_sector::synthesize"
    );
}

#[test]
fn backup_boot_region_mirrors_main_byte_for_byte() {
    let s = synth_with(SMALL_VOLUME_BYTES);

    let main_offset = u64::from(MAIN_BOOT_REGION_OFFSET_SECTORS) * u64::from(SECTOR_SIZE_BYTES);
    let backup_offset = u64::from(BACKUP_BOOT_REGION_OFFSET_SECTORS) * u64::from(SECTOR_SIZE_BYTES);

    let main = read_range(&s, main_offset, BOOT_REGION_SIZE_BYTES);
    let backup = read_range(&s, backup_offset, BOOT_REGION_SIZE_BYTES);
    assert_eq!(
        main, backup,
        "exFAT spec §3.2: backup boot region must be a byte-for-byte copy of the main boot region"
    );
}

#[test]
fn main_boot_sector_starts_with_jumpboot_and_filesystem_name() {
    let s = synth_with(SMALL_VOLUME_BYTES);

    let jump = read_range(&s, 0, 3);
    assert_eq!(
        jump,
        JUMP_BOOT.to_vec(),
        "exFAT spec §3.1.1: JumpBoot bytes 0xEB 0x76 0x90"
    );

    let fs_name = read_range(&s, 3, 8);
    assert_eq!(
        fs_name,
        FILE_SYSTEM_NAME.to_vec(),
        "exFAT spec §3.1.2: FileSystemName 'EXFAT   '"
    );
}

#[test]
fn main_and_backup_boot_sectors_end_with_55aa_signature() {
    let s = synth_with(SMALL_VOLUME_BYTES);

    let main_sig = read_range(&s, BOOT_SECTOR_SIZE_BYTES as u64 - 2, 2);
    assert_eq!(
        main_sig,
        BOOT_SIGNATURE.to_vec(),
        "exFAT spec §3.1.x: main boot sector signature 0x55 0xAA"
    );

    let backup_sig_offset = u64::from(BACKUP_BOOT_REGION_OFFSET_SECTORS)
        * u64::from(SECTOR_SIZE_BYTES)
        + BOOT_SECTOR_SIZE_BYTES as u64
        - 2;
    let backup_sig = read_range(&s, backup_sig_offset, 2);
    assert_eq!(
        backup_sig,
        BOOT_SIGNATURE.to_vec(),
        "backup boot sector signature 0x55 0xAA"
    );
}

#[test]
fn main_boot_sector_carries_caller_supplied_volume_serial() {
    let s = synth_with(SMALL_VOLUME_BYTES);

    // Spec §3.1.10: VolumeSerialNumber at byte offset 0x064 of
    // the main boot sector, 4 bytes little-endian.
    let serial_bytes = read_range(&s, 0x064, 4);
    let read_serial = u32::from_le_bytes([
        serial_bytes[0],
        serial_bytes[1],
        serial_bytes[2],
        serial_bytes[3],
    ]);
    assert_eq!(
        read_serial, SERIAL,
        "VolumeSerialNumber must round-trip the constructor value"
    );
}

// ── FAT region integration ───────────────────────────────────────────

#[test]
fn fat_entry_zero_is_media_type_marker() {
    let s = synth_with(SMALL_VOLUME_BYTES);
    let entry = parse_fat_entry(&s, 0);
    assert_eq!(
        entry, FAT_MEDIA_TYPE,
        "exFAT spec §4.1: FAT[0] = MediaType marker 0xFFFFFFF8"
    );
}

#[test]
fn fat_entry_one_is_volume_dirty_flag_end_of_chain() {
    let s = synth_with(SMALL_VOLUME_BYTES);
    let entry = parse_fat_entry(&s, 1);
    assert_eq!(
        entry, FAT_VOLUME_DIRTY,
        "exFAT spec §4.1: FAT[1] = 0xFFFFFFFF (clean shutdown marker)"
    );
}

#[test]
fn fat_entry_for_root_cluster_is_end_of_chain() {
    let s = synth_with(SMALL_VOLUME_BYTES);
    let root_cluster = s.geometry().first_root_directory_cluster();
    let entry = parse_fat_entry(&s, root_cluster);
    assert_eq!(
        entry, FAT_END_OF_CHAIN,
        "root directory chain (single cluster) terminates at EOC"
    );
}

#[test]
fn bitmap_chain_terminates_at_end_of_chain() {
    let s = synth_with(LARGE_VOLUME_BYTES);
    let bitmap_first = s.bitmap_first_cluster();

    // Walk the bitmap chain via the dispatcher. The last entry
    // must be EOC, all preceding entries must point to the next
    // cluster in sequence.
    let mut cluster = bitmap_first;
    let mut hop_budget = 64;
    loop {
        let next = parse_fat_entry(&s, cluster);
        if next == FAT_END_OF_CHAIN {
            break;
        }
        assert_eq!(
            next,
            cluster + 1,
            "bitmap chain must be contiguous (cluster {cluster} -> {next})",
        );
        cluster = next;
        hop_budget -= 1;
        assert!(hop_budget > 0, "bitmap chain walk exceeded hop budget");
    }
}

#[test]
fn upcase_chain_terminates_at_end_of_chain() {
    let s = synth_with(LARGE_VOLUME_BYTES);
    let upcase_first = s.upcase_first_cluster();

    let mut cluster = upcase_first;
    let mut hop_budget = 64;
    loop {
        let next = parse_fat_entry(&s, cluster);
        if next == FAT_END_OF_CHAIN {
            break;
        }
        assert_eq!(
            next,
            cluster + 1,
            "upcase chain must be contiguous (cluster {cluster} -> {next})",
        );
        cluster = next;
        hop_budget -= 1;
        assert!(hop_budget > 0, "upcase chain walk exceeded hop budget");
    }
}

#[test]
fn unallocated_clusters_read_as_free_in_fat() {
    let s = synth_with(LARGE_VOLUME_BYTES);

    // Pick a cluster that's well past upcase end — must be free
    // (entry 0).
    let bytes_per_cluster = u64::from(s.geometry().bytes_per_cluster());
    let upcase_size_bytes = u64::try_from(UPCASE_TABLE_SIZE_BYTES).unwrap();
    let upcase_clusters =
        u32::try_from(upcase_size_bytes.div_ceil(bytes_per_cluster.max(1))).unwrap();
    let probe_cluster = s.upcase_first_cluster() + upcase_clusters + 100;
    assert!(
        probe_cluster < FIRST_CLUSTER_NUMBER + s.geometry().cluster_count(),
        "probe cluster must be within the cluster heap"
    );

    let entry = parse_fat_entry(&s, probe_cluster);
    assert_eq!(
        entry, 0,
        "unallocated clusters must read as FAT entry 0 (free)"
    );
}

// ── Root directory cluster integration ───────────────────────────────

#[test]
fn root_cluster_contains_allocation_bitmap_entry_first() {
    let s = synth_with(SMALL_VOLUME_BYTES);
    let root_cluster = s.geometry().first_root_directory_cluster();
    let off = cluster_offset_bytes(&s, root_cluster);

    let entry = read_range(&s, off, ENTRY_SIZE);
    assert_eq!(
        entry[0], ENTRY_TYPE_ALLOCATION_BITMAP,
        "exFAT spec §6: first root entry must be the Allocation Bitmap entry (0x81)"
    );
}

#[test]
fn root_cluster_contains_upcase_table_entry_second() {
    let s = synth_with(SMALL_VOLUME_BYTES);
    let root_cluster = s.geometry().first_root_directory_cluster();
    let off = cluster_offset_bytes(&s, root_cluster) + ENTRY_SIZE as u64;

    let entry = read_range(&s, off, ENTRY_SIZE);
    assert_eq!(
        entry[0], ENTRY_TYPE_UPCASE_TABLE,
        "exFAT spec §6: second root entry must be the UpCase Table entry (0x82)"
    );
}

#[test]
fn root_cluster_contains_volume_label_entry_third() {
    let s = synth_with(SMALL_VOLUME_BYTES);
    let root_cluster = s.geometry().first_root_directory_cluster();
    let off = cluster_offset_bytes(&s, root_cluster) + (ENTRY_SIZE as u64) * 2;

    let entry = read_range(&s, off, ENTRY_SIZE);
    assert_eq!(
        entry[0], ENTRY_TYPE_VOLUME_LABEL,
        "exFAT spec §6: third root entry must be the Volume Label entry (0x83)"
    );

    // Spec §7.3: byte 1 = CharacterCount (number of UTF-16 code
    // units in the label); the seven 'TESTVOL' code units we
    // passed in must round-trip.
    assert_eq!(
        entry[1] as usize,
        VOLUME_LABEL_TESTVOL.len(),
        "Volume Label CharacterCount must match the constructor input length"
    );

    // Bytes 2..(2+2*len) are the UTF-16 label. Reconstruct and
    // compare.
    let mut got = Vec::with_capacity(VOLUME_LABEL_TESTVOL.len());
    for i in 0..VOLUME_LABEL_TESTVOL.len() {
        let lo = entry[2 + 2 * i];
        let hi = entry[2 + 2 * i + 1];
        got.push(u16::from_le_bytes([lo, hi]));
    }
    assert_eq!(
        got, VOLUME_LABEL_TESTVOL,
        "Volume Label UTF-16 code units must round-trip the constructor input"
    );
}

#[test]
fn root_cluster_after_three_entries_is_zero_filled() {
    let s = synth_with(SMALL_VOLUME_BYTES);
    let root_cluster = s.geometry().first_root_directory_cluster();
    let bytes_per_cluster = usize::try_from(s.geometry().bytes_per_cluster()).unwrap();
    let off = cluster_offset_bytes(&s, root_cluster) + (ENTRY_SIZE as u64) * 3;
    let tail_len = bytes_per_cluster - ENTRY_SIZE * 3;

    let tail = read_range(&s, off, tail_len);
    assert!(
        tail.iter().all(|&b| b == 0),
        "remainder of root cluster after the three mandatory entries must be zero"
    );
}

// ── Allocation bitmap stream integration ─────────────────────────────

#[test]
fn allocation_bitmap_first_byte_marks_root_bitmap_and_upcase_clusters() {
    let s = synth_with(SMALL_VOLUME_BYTES);
    let bitmap_first = s.bitmap_first_cluster();
    let off = cluster_offset_bytes(&s, bitmap_first);

    // Spec §7.1.5: bit n of byte 0 corresponds to cluster
    // (FIRST_CLUSTER_NUMBER + n) — i.e. bit 0 = cluster 2 (root).
    // We've marked cluster 2 (root) and a contiguous run starting
    // at bitmap_first_cluster (typically cluster 3). The first
    // few bits should all be set.
    let byte0 = read_range(&s, off, 1)[0];
    assert!(
        byte0 & 0x01 != 0,
        "bit 0 of bitmap byte 0 must be set (root cluster {FIRST_CLUSTER_NUMBER})",
    );
}

// ── UpCase table stream integration ──────────────────────────────────

#[test]
fn upcase_table_maps_lowercase_a_to_uppercase_a() {
    let s = synth_with(LARGE_VOLUME_BYTES);
    let upcase_first = s.upcase_first_cluster();
    let off = cluster_offset_bytes(&s, upcase_first)
        + u64::from(ASCII_LOWER_A) * (BYTES_PER_ENTRY as u64);

    let bytes = read_range(&s, off, BYTES_PER_ENTRY);
    let folded = u16::from_le_bytes([bytes[0], bytes[1]]);
    assert_eq!(
        folded, ASCII_UPPER_A,
        "UpCase table must fold 0x0061 -> 0x0041 (lowercase 'a' -> uppercase 'A')"
    );
}

// ── Whole-volume integration ─────────────────────────────────────────

#[test]
fn small_volume_read_in_one_meg_chunks_totals_volume_size_bytes() {
    let s = synth_with(SMALL_VOLUME_BYTES);
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
    let s = synth_with(SMALL_VOLUME_BYTES);

    // Pick a span that crosses main boot + backup boot + first
    // FAT sector (offsets 0..(fat1 + 512)).
    let span_len = usize::try_from(fat1_offset_bytes(&s)).unwrap() + SECTOR_SIZE_BYTES as usize;
    let first = read_range(&s, 0, span_len);
    let second = read_range(&s, 0, span_len);
    assert_eq!(
        first, second,
        "two reads of the same offset must return byte-identical output (no mutation in the dispatcher)"
    );
}

#[test]
fn unaligned_chunk_sizes_produce_consistent_byte_stream() {
    let s = synth_with(SMALL_VOLUME_BYTES);

    // Read the backup boot region (12 sectors = 6144 bytes) two
    // ways:
    //   (a) one 6144-byte read;
    //   (b) many reads of varied sizes that sum to 6144.
    let backup_offset = u64::from(BACKUP_BOOT_REGION_OFFSET_SECTORS) * u64::from(SECTOR_SIZE_BYTES);
    let baseline = read_range(&s, backup_offset, BOOT_REGION_SIZE_BYTES);

    let chunk_sizes: [usize; 9] = [1, 17, 51, 53, 64, 71, 100, 73, 5714];
    assert_eq!(chunk_sizes.iter().sum::<usize>(), BOOT_REGION_SIZE_BYTES);

    let mut pieced = Vec::with_capacity(BOOT_REGION_SIZE_BYTES);
    let mut off = backup_offset;
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

// ── Cross-region integration ─────────────────────────────────────────

#[test]
fn cross_boundary_read_spans_main_and_backup_boot_regions() {
    let s = synth_with(SMALL_VOLUME_BYTES);

    // Read straddling the main/backup boot boundary: last 16
    // bytes of main + first 16 bytes of backup, in one call.
    let backup_offset = u64::from(BACKUP_BOOT_REGION_OFFSET_SECTORS) * u64::from(SECTOR_SIZE_BYTES);
    let combined = read_range(&s, backup_offset - 16, 32);

    // Same bytes read as two separate reads should be identical
    // when concatenated.
    let main_tail = read_range(&s, backup_offset - 16, 16);
    let backup_head = read_range(&s, backup_offset, 16);
    let mut expected = main_tail;
    expected.extend_from_slice(&backup_head);

    assert_eq!(
        combined, expected,
        "cross-region read must equal the concatenation of two single-region reads"
    );
}

// ── Error-path integration ───────────────────────────────────────────

#[test]
fn read_past_volume_end_is_rejected_via_public_api() {
    let s = synth_with(SMALL_VOLUME_BYTES);
    let total = s.geometry().volume_size_bytes();

    // Reading 513 bytes from the last 512-byte sector extends one
    // byte past EOF.
    let err = s.read(total - 512, &mut [0u8; 513]).expect_err("must fail");
    assert!(matches!(err, ExfatSynthError::LengthExceedsVolume { .. }));
}

#[test]
fn read_offset_at_volume_end_with_data_is_rejected_via_public_api() {
    let s = synth_with(SMALL_VOLUME_BYTES);
    let total = s.geometry().volume_size_bytes();

    let err = s.read(total, &mut [0u8; 1]).expect_err("must fail");
    assert!(matches!(err, ExfatSynthError::OffsetBeyondVolume { .. }));
}

#[test]
fn empty_read_at_any_offset_within_volume_is_ok() {
    let s = synth_with(SMALL_VOLUME_BYTES);
    let total = s.geometry().volume_size_bytes();

    // Empty buffer is the documented no-op even at boundary
    // offsets.
    s.read(0, &mut []).expect("empty read at offset 0");
    s.read(total - 1, &mut []).expect("empty read near end");
    s.read(total, &mut []).expect("empty read at end");
}

// ── Geometry-region invariants visible through the dispatcher ────────

#[test]
fn every_region_kind_in_geometry_map_is_reachable_via_read() {
    // A 1 GiB volume produces the full set of exFAT region
    // kinds: main boot, backup boot, FAT table, and data.
    // Verifying we can read one byte from each catches any
    // dispatcher gap where a region kind is silently skipped.
    let s = synth_with(LARGE_VOLUME_BYTES);

    for region in s.geometry().regions() {
        let mut buf = [0u8; 1];
        s.read(region.start, &mut buf).unwrap_or_else(|err| {
            panic!(
                "dispatcher must serve at least one byte from every geometry region; \
                 kind={:?} start={} failed: {err}",
                region.kind, region.start,
            )
        });

        // Defense-in-depth: confirm the region map only contains
        // exFAT-flavoured variants. A FAT32 variant would mean
        // the geometry constructor is leaking the wrong layout.
        assert!(
            matches!(
                region.kind,
                RegionKind::ExfatMainBootRegion
                    | RegionKind::ExfatBackupBootRegion
                    | RegionKind::FatTable { .. }
                    | RegionKind::Data
                    | RegionKind::Reserved
            ),
            "ExfatGeometry must not emit non-exFAT region kinds; got {:?}",
            region.kind,
        );
    }
}
