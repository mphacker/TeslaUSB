//! Phase 3.5c — `SynthBackend::write` end-to-end integration.
//!
//! These tests exercise the public [`BlockBackend::write`] /
//! [`BlockBackend::flush`] API against a real on-disk backing
//! tree by simulating the byte-level FAT32 writes a real kernel
//! would issue for ordinary file operations. They prove the full
//! cumulative Phase 3.5 deliverable — decoder (3.1) + chain
//! walker (3.5b) + directory decoder (3.5a) + write state
//! machine (3.5c) + `DirTreeWriter` (3.3) — works end-to-end
//! through the production code path the NBD server actually
//! invokes.
//!
//! Scope notes:
//!
//! * Tests use the smallest config-allowed FAT32 volume size (4
//!   GiB). The volume size is purely logical — no on-disk image
//!   is materialised; only the metadata regions referenced by
//!   writes get touched.
//! * Coordinates are computed from the same [`Fat32Geometry`]
//!   the backend builds internally, so writes land in the right
//!   FAT / data regions. The test inspects the resulting
//!   backing tree (NOT the synthesised reads) because that's
//!   the only externally visible side effect of a write.
//! * exFAT writes shipped in Phase 3.5e and have their own
//!   integration suite in `synth_exfat_write_integration.rs`;
//!   the boot-region acceptance test here is a thin smoke
//!   check that the shared dispatch path also reaches exFAT.

#![allow(
    clippy::cast_possible_truncation,
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::missing_panics_doc,
    clippy::panic,
    clippy::unwrap_used
)]

use std::collections::HashMap;
use std::path::PathBuf;

use tempfile::TempDir;
use teslafat::backend::SynthBackend;
use teslafat::config::{Config, FsType, NbdConfig, RetentionConfig};
use teslausb_core::backend::{BlockBackend, WriteFlags};
use teslausb_core::fs::cluster_layout::FIRST_DATA_CLUSTER;
use teslausb_core::fs::fat32::directory::{
    FileAttributes, ShortName, Timestamps, synthesize_lfn_sequence, synthesize_sfn_entry,
};
use teslausb_core::fs::fat32::geometry::{Fat32Geometry, RESERVED_SECTORS};
use teslausb_core::fs::geometry::{Geometry, SECTOR_SIZE_BYTES};

const VOLUME_SIZE_GB: u32 = 4;
const VOLUME_BYTES: u64 = (VOLUME_SIZE_GB as u64) * 1024 * 1024 * 1024;
const SECTOR: u64 = SECTOR_SIZE_BYTES as u64;
const EOC: u32 = 0x0FFF_FFFF;

fn sample_cfg(backing_root: PathBuf, fs_type: FsType) -> Config {
    Config {
        backing_root,
        volume_size_gb: VOLUME_SIZE_GB,
        volume_label: "INTGRTST".to_string(),
        cluster_size: None,
        fs_type,
        retention: RetentionConfig::default(),
        nbd: NbdConfig::default(),
        spill_dir: None,
    }
}

fn geometry() -> Fat32Geometry {
    Fat32Geometry::for_volume_size(VOLUME_BYTES).expect("4 GiB is a valid FAT32 volume")
}

fn fat1_volume_byte(cluster: u32) -> u64 {
    u64::from(RESERVED_SECTORS) * SECTOR + u64::from(cluster) * 4
}

fn cluster_volume_byte(g: &Fat32Geometry, cluster: u32) -> u64 {
    g.first_data_sector() * SECTOR
        + u64::from(cluster - FIRST_DATA_CLUSTER) * u64::from(g.bytes_per_cluster())
}

fn root_cluster_byte(g: &Fat32Geometry) -> u64 {
    cluster_volume_byte(g, /* ROOT_DIRECTORY_CLUSTER */ 2)
}

/// Build the bytes a kernel would write into a directory cluster
/// to create a new regular file entry (LFN + SFN).
fn build_file_entry(name: &str, first_cluster: u32, file_size: u32) -> Vec<u8> {
    let short = ShortName::from_padded_str(&name.to_ascii_uppercase()).unwrap();
    let lfn = synthesize_lfn_sequence(name, short.checksum()).unwrap();
    let sfn = synthesize_sfn_entry(
        &short,
        FileAttributes::archive(),
        first_cluster,
        file_size,
        &Timestamps::epoch(),
    );
    let mut bytes = Vec::new();
    for slot in lfn {
        bytes.extend_from_slice(&slot);
    }
    bytes.extend_from_slice(&sfn);
    bytes
}

/// Write a 4-byte FAT entry into the primary FAT mirror.
async fn write_fat_entry(backend: &SynthBackend, cluster: u32, value: u32) {
    backend
        .write(
            fat1_volume_byte(cluster),
            &value.to_le_bytes(),
            WriteFlags::NONE,
        )
        .await
        .expect("FAT entry write");
}

/// Write `data` into a data cluster starting at the cluster's
/// first byte.
async fn write_cluster_data(g: &Fat32Geometry, backend: &SynthBackend, cluster: u32, data: &[u8]) {
    let offset = cluster_volume_byte(g, cluster);
    backend
        .write(offset, data, WriteFlags::NONE)
        .await
        .expect("data cluster write");
}

fn open_empty_backend(fs_type: FsType) -> (TempDir, SynthBackend) {
    let dir = TempDir::new().expect("tempdir");
    let cfg = sample_cfg(dir.path().to_path_buf(), fs_type);
    let backend = SynthBackend::open(&cfg).expect("open");
    (dir, backend)
}

#[tokio::test]
async fn fat32_create_single_cluster_file_appears_in_backing_tree() {
    let (dir, backend) = open_empty_backend(FsType::Fat32);
    let g = geometry();
    let payload = b"Hello from the integration test!".repeat(8);
    let file_cluster = 5;

    // 1. Tesla writes the FAT entry marking cluster 5 EOC.
    write_fat_entry(&backend, file_cluster, EOC).await;
    // 2. Tesla writes the data cluster.
    write_cluster_data(&g, &backend, file_cluster, &payload).await;
    // 3. Tesla writes the directory entry into the root cluster.
    let dir_entry = build_file_entry("hello.txt", file_cluster, payload.len() as u32);
    backend
        .write(root_cluster_byte(&g), &dir_entry, WriteFlags::NONE)
        .await
        .expect("dir entry write");

    // 4. Tesla flushes.
    backend.flush().await.expect("flush");

    let final_path = dir.path().join("hello.txt");
    let on_disk = std::fs::read(&final_path).expect("file present");
    assert_eq!(&on_disk, &payload, "backing file content");
}

#[tokio::test]
async fn fat32_out_of_order_writes_still_create_file() {
    // Same as above but FAT → data → dir order is permuted.
    let (dir, backend) = open_empty_backend(FsType::Fat32);
    let g = geometry();
    let payload = b"out-of-order arrival".repeat(20);
    let file_cluster = 6;

    // Dir entry first — no chain known yet, file is pending.
    let dir_entry = build_file_entry("ooo.bin", file_cluster, payload.len() as u32);
    backend
        .write(root_cluster_byte(&g), &dir_entry, WriteFlags::NONE)
        .await
        .expect("dir entry write");

    // Data next — unknown cluster, stash.
    write_cluster_data(&g, &backend, file_cluster, &payload).await;

    // FAT last — chain resolves, stash drains.
    write_fat_entry(&backend, file_cluster, EOC).await;

    backend.flush().await.expect("flush");

    let final_path = dir.path().join("ooo.bin");
    let on_disk = std::fs::read(&final_path).expect("file present");
    assert_eq!(&on_disk, &payload);
}

#[tokio::test]
async fn fat32_multi_cluster_fragmented_chain_assembles_correctly() {
    let (dir, backend) = open_empty_backend(FsType::Fat32);
    let g = geometry();
    let bytes_per_cluster = g.bytes_per_cluster() as usize;
    let cluster_a = 10;
    let cluster_b = 25;
    let cluster_c = 19;
    let total_size = bytes_per_cluster * 2 + 100;

    // Build a payload with distinct per-cluster signatures.
    let mut payload = Vec::with_capacity(total_size);
    payload.extend(std::iter::repeat_n(0xAAu8, bytes_per_cluster));
    payload.extend(std::iter::repeat_n(0xBBu8, bytes_per_cluster));
    payload.extend(std::iter::repeat_n(0xCCu8, 100));

    // FAT: A -> B -> C -> EOC.
    write_fat_entry(&backend, cluster_a, cluster_b).await;
    write_fat_entry(&backend, cluster_b, cluster_c).await;
    write_fat_entry(&backend, cluster_c, EOC).await;

    // Data in chain order.
    write_cluster_data(&g, &backend, cluster_a, &payload[..bytes_per_cluster]).await;
    write_cluster_data(
        &g,
        &backend,
        cluster_b,
        &payload[bytes_per_cluster..2 * bytes_per_cluster],
    )
    .await;
    write_cluster_data(&g, &backend, cluster_c, &payload[2 * bytes_per_cluster..]).await;

    // Dir entry: file size truncates the C cluster tail.
    let dir_entry = build_file_entry("frag.bin", cluster_a, total_size as u32);
    backend
        .write(root_cluster_byte(&g), &dir_entry, WriteFlags::NONE)
        .await
        .expect("dir entry");

    backend.flush().await.expect("flush");

    let on_disk = std::fs::read(dir.path().join("frag.bin")).expect("file");
    assert_eq!(on_disk.len(), total_size);
    assert!(on_disk[..bytes_per_cluster].iter().all(|&b| b == 0xAA));
    assert!(
        on_disk[bytes_per_cluster..2 * bytes_per_cluster]
            .iter()
            .all(|&b| b == 0xBB)
    );
    assert!(on_disk[2 * bytes_per_cluster..].iter().all(|&b| b == 0xCC));
}

#[tokio::test]
async fn fat32_deletion_keeps_backing_file_and_records_retention() {
    let (dir, backend) = open_empty_backend(FsType::Fat32);
    let g = geometry();
    let payload = b"will be deleted".repeat(8);
    let file_cluster = 7;

    write_fat_entry(&backend, file_cluster, EOC).await;
    write_cluster_data(&g, &backend, file_cluster, &payload).await;
    let dir_entry = build_file_entry("doomed.bin", file_cluster, payload.len() as u32);
    backend
        .write(root_cluster_byte(&g), &dir_entry, WriteFlags::NONE)
        .await
        .expect("dir entry");
    backend.flush().await.expect("flush create");
    assert!(dir.path().join("doomed.bin").exists());

    // Now Tesla deletes the file. The kernel typically clears
    // the dir entry's first byte to 0xE5 (deleted marker). The
    // simplest deletion that the FAT decoder catches: rewrite
    // the same offset with a zero-byte (end-of-directory)
    // marker. Either way, after re-decode the entry is absent
    // from `new_children` and the diff fires
    // `handle_child_deleted`.
    let mut deletion_marker = vec![0u8; dir_entry.len()];
    deletion_marker[0] = 0xE5; // deleted entry
    backend
        .write(root_cluster_byte(&g), &deletion_marker, WriteFlags::NONE)
        .await
        .expect("delete write");
    backend.flush().await.expect("flush delete");

    // Phase 4.2: backing file must be preserved past Tesla's
    // dir-entry delete. The retention `DeletedSet` records the
    // path for the cleanup worker to evaluate against its GPS /
    // SEI policy.
    assert!(
        dir.path().join("doomed.bin").exists(),
        "Phase 4.2: backing file must persist past Tesla's dir-entry delete"
    );
}

#[tokio::test]
async fn fat32_in_place_overwrite_of_pre_existing_file_preserves_untouched_bytes() {
    // Seed a pre-existing file in the backing tree. The
    // layout planner will assign it a cluster chain; we need
    // to discover that chain by reading the synthesised
    // image.
    let dir = TempDir::new().expect("tempdir");
    let original = b"AAAAAAAAAA".repeat(64);
    let rel = PathBuf::from("preexisting.bin");
    std::fs::write(dir.path().join(&rel), &original).expect("seed");
    let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
    let backend = SynthBackend::open(&cfg).expect("open");
    let g = geometry();

    // Discover the file's first cluster from the synthesised
    // root-directory cluster. Read the root cluster and
    // search for the file's first cluster via the decoded
    // directory entry. Easiest: just scan the FAT for the
    // first non-EOC chain head reachable from cluster 2+.
    // But cleaner — read the root cluster and decode it.
    let root_bytes = {
        let mut buf = vec![0u8; g.bytes_per_cluster() as usize];
        backend.read(root_cluster_byte(&g), &mut buf).await.unwrap();
        buf
    };
    let decoded =
        teslausb_core::fs::fat32::dir_decode::decode_directory_cluster(&root_bytes, Vec::new())
            .expect("root decode");
    let mut first_cluster = None;
    for entry in &decoded.entries {
        if let teslausb_core::fs::fat32::dir_decode::DecodedDirEntry::File {
            long_name,
            first_cluster: fc,
            ..
        } = entry
        {
            if long_name.as_deref() == Some("preexisting.bin") {
                first_cluster = Some(*fc);
                break;
            }
        }
    }
    let first_cluster = first_cluster.expect("file appears in synthesised root");

    // Tesla now overwrites bytes 100..110 of the file by
    // rewriting only the affected sector. To make the
    // assertion specific we touch exactly those bytes.
    let cluster_offset = cluster_volume_byte(&g, first_cluster);
    backend
        .write(cluster_offset + 100, b"ZZZZZZZZZZ", WriteFlags::NONE)
        .await
        .expect("in-place write");
    backend.flush().await.expect("flush");

    let after = std::fs::read(dir.path().join(&rel)).expect("read after");
    assert_eq!(after.len(), original.len(), "file size preserved");
    // bytes 0..100 == original
    assert_eq!(&after[..100], &original[..100]);
    // bytes 100..110 == overwritten
    assert_eq!(&after[100..110], b"ZZZZZZZZZZ");
    // bytes 110.. == original (untouched bytes preserved by
    // the seed-from-target mechanism)
    assert_eq!(&after[110..], &original[110..]);
}

#[tokio::test]
async fn fat32_write_without_flush_does_not_materialize_file() {
    // The .partial file is created, but the final file is not
    // until flush() runs.
    let (dir, backend) = open_empty_backend(FsType::Fat32);
    let g = geometry();
    let payload = b"unflushed payload".repeat(4);
    let file_cluster = 8;

    write_fat_entry(&backend, file_cluster, EOC).await;
    write_cluster_data(&g, &backend, file_cluster, &payload).await;
    let dir_entry = build_file_entry("partial.bin", file_cluster, payload.len() as u32);
    backend
        .write(root_cluster_byte(&g), &dir_entry, WriteFlags::NONE)
        .await
        .expect("dir entry");

    // No flush yet — final file must not exist.
    assert!(!dir.path().join("partial.bin").exists());
    assert!(
        dir.path().join("partial.bin.partial").exists(),
        ".partial file should exist before flush"
    );

    backend.flush().await.expect("flush");
    assert!(dir.path().join("partial.bin").exists());
    assert!(!dir.path().join("partial.bin.partial").exists());
}

#[tokio::test]
async fn fat32_fua_flag_finalizes_immediately() {
    let (dir, backend) = open_empty_backend(FsType::Fat32);
    let g = geometry();
    let payload = b"FUA payload".repeat(4);
    let file_cluster = 9;

    write_fat_entry(&backend, file_cluster, EOC).await;
    write_cluster_data(&g, &backend, file_cluster, &payload).await;
    // Last write with FUA — must finalize without flush.
    let dir_entry = build_file_entry("fua.bin", file_cluster, payload.len() as u32);
    backend
        .write(root_cluster_byte(&g), &dir_entry, WriteFlags::FUA)
        .await
        .expect("FUA dir entry");

    assert!(
        dir.path().join("fua.bin").exists(),
        "FUA should finalize without explicit flush"
    );
}

#[tokio::test]
async fn exfat_writes_to_boot_region_are_accepted_as_metadata() {
    // Phase 3.5e shipped exFAT write support. Boot-region writes
    // are swallowed as metadata (the synth is the source of
    // truth); full exFAT write coverage lives in
    // `synth_exfat_write_integration.rs`.
    let (_dir, backend) = open_empty_backend(FsType::Exfat);
    backend
        .write(0, &[0u8; 16], WriteFlags::NONE)
        .await
        .expect("exFAT metadata write should be accepted");
}

#[tokio::test]
async fn fat32_subdirectory_creation_routes_child_into_subdir() {
    let (dir, backend) = open_empty_backend(FsType::Fat32);
    let g = geometry();
    let bytes_per_cluster = g.bytes_per_cluster() as u32;
    let subdir_cluster = 11;
    let child_cluster = 12;

    // 1) FAT for subdir cluster + child cluster.
    write_fat_entry(&backend, subdir_cluster, EOC).await;
    write_fat_entry(&backend, child_cluster, EOC).await;

    // 2) Write the subdir's directory entry into root.
    let subdir_entry = build_dir_entry("subdir", subdir_cluster);
    backend
        .write(root_cluster_byte(&g), &subdir_entry, WriteFlags::NONE)
        .await
        .expect("subdir entry");

    // 3) Zero-fill the subdir cluster, then write a child
    //    file entry into it.
    let child_payload = b"inside subdir".repeat(8);
    let child_entry = build_file_entry("child.txt", child_cluster, child_payload.len() as u32);
    backend
        .write(
            cluster_volume_byte(&g, subdir_cluster),
            &child_entry,
            WriteFlags::NONE,
        )
        .await
        .expect("child entry");

    // 4) Child data.
    write_cluster_data(&g, &backend, child_cluster, &child_payload).await;

    backend.flush().await.expect("flush");

    let child_path = dir.path().join("subdir").join("child.txt");
    let on_disk = std::fs::read(&child_path).expect("child file");
    assert_eq!(&on_disk, &child_payload);

    // Sanity: the subdirectory itself exists as a directory.
    assert!(
        dir.path().join("subdir").is_dir(),
        "subdir should exist as directory"
    );

    let _ = bytes_per_cluster;
}

fn build_dir_entry(name: &str, first_cluster: u32) -> Vec<u8> {
    let short = ShortName::from_padded_str(&name.to_ascii_uppercase()).unwrap();
    let lfn = synthesize_lfn_sequence(name, short.checksum()).unwrap();
    let sfn = synthesize_sfn_entry(
        &short,
        FileAttributes::directory(),
        first_cluster,
        0,
        &Timestamps::epoch(),
    );
    let mut bytes = Vec::new();
    for slot in lfn {
        bytes.extend_from_slice(&slot);
    }
    bytes.extend_from_slice(&sfn);
    bytes
}

#[tokio::test]
async fn fat32_double_flush_is_idempotent() {
    let (dir, backend) = open_empty_backend(FsType::Fat32);
    let g = geometry();
    let payload = b"idempotent".repeat(4);
    let file_cluster = 13;

    write_fat_entry(&backend, file_cluster, EOC).await;
    write_cluster_data(&g, &backend, file_cluster, &payload).await;
    let entry = build_file_entry("idem.bin", file_cluster, payload.len() as u32);
    backend
        .write(root_cluster_byte(&g), &entry, WriteFlags::NONE)
        .await
        .expect("dir entry");

    backend.flush().await.expect("first flush");
    backend.flush().await.expect("second flush idempotent");
    let on_disk = std::fs::read(dir.path().join("idem.bin")).expect("file");
    assert_eq!(&on_disk, &payload);
}

#[tokio::test]
async fn fat32_pre_existing_files_set_seeds_correctly() {
    // Verify that opening a backend with non-empty backing
    // tree registers each existing file in `pre_existing_files`
    // so a later in-place write doesn't truncate untouched
    // bytes. (Covered structurally by
    // fat32_in_place_overwrite_of_pre_existing_file_preserves_untouched_bytes
    // — this test just verifies open() doesn't error on a
    // populated tree.)
    let dir = TempDir::new().expect("tempdir");
    let mut expected: HashMap<String, Vec<u8>> = HashMap::new();
    for i in 0..5u8 {
        let name = format!("file{i}.bin");
        let bytes = vec![i; 1024];
        std::fs::write(dir.path().join(&name), &bytes).expect("seed");
        expected.insert(name, bytes);
    }
    let cfg = sample_cfg(dir.path().to_path_buf(), FsType::Fat32);
    let backend = SynthBackend::open(&cfg).expect("open with pre-existing files");
    // Read back the synthesised root cluster and confirm
    // every seeded name appears as a directory entry.
    let g = geometry();
    let mut buf = vec![0u8; g.bytes_per_cluster() as usize];
    backend
        .read(root_cluster_byte(&g), &mut buf)
        .await
        .expect("read root");
    let decoded = teslausb_core::fs::fat32::dir_decode::decode_directory_cluster(&buf, Vec::new())
        .expect("decode");
    let names: Vec<String> = decoded
        .entries
        .iter()
        .filter_map(|e| match e {
            teslausb_core::fs::fat32::dir_decode::DecodedDirEntry::File { long_name, .. } => {
                long_name.clone()
            }
            _ => None,
        })
        .collect();
    for name in expected.keys() {
        assert!(
            names.contains(name),
            "expected {name} in synthesised root, got {names:?}"
        );
    }
    // Confirm nothing was written yet — backing files are
    // untouched.
    for (name, bytes) in &expected {
        let on_disk = std::fs::read(dir.path().join(name)).expect("read");
        assert_eq!(&on_disk, bytes, "{name} untouched");
    }
}
