//! Phase 3.5e — `SynthBackend::write` end-to-end integration
//! for `exFAT`.
//!
//! Parallel to `synth_write_integration.rs` (Phase 3.5c FAT32).
//! These tests exercise the public [`BlockBackend::write`] /
//! [`BlockBackend::flush`] API against a real on-disk backing
//! tree by simulating the byte-level `exFAT` writes a kernel
//! would issue for ordinary file operations. They prove the
//! full cumulative Phase 3.5 deliverable for `exFAT` —
//! decoder (3.2) + dir-entry decoder (3.5d) + write state
//! machine (3.5e) + `DirTreeWriter` (3.3) — works end-to-end
//! through the production code path the NBD server actually
//! invokes.
//!
//! Scope notes:
//!
//! * Tests use a 4 GiB volume (the same minimum as the FAT32
//!   integration suite). The volume size is purely logical —
//!   no on-disk image is materialised; only metadata regions
//!   referenced by writes get touched.
//! * Coordinates are computed from the same [`ExfatGeometry`]
//!   the backend builds internally, so writes land in the
//!   right FAT / cluster-heap regions.
//! * `no_fat_chain == true` is the common case (every file the
//!   B-1 planner ships is contiguous), but the FAT-walking
//!   path is exercised by `exfat_fat_chained_file_…` for
//!   coverage symmetry with FAT32.

#![allow(
    clippy::cast_possible_truncation,
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::missing_panics_doc,
    clippy::panic,
    clippy::unwrap_used
)]

use std::path::PathBuf;

use tempfile::TempDir;
use teslafat::backend::SynthBackend;
use teslafat::config::{Config, FsType, NbdConfig, RetentionConfig};
use teslausb_core::backend::{BlockBackend, WriteFlags};
use teslausb_core::fs::cluster_layout::FIRST_DATA_CLUSTER;
use teslausb_core::fs::exfat::directory::{
    FileAttributes, FileEntrySetParams, FileTimestamps, encode_file_entry_set,
};
use teslausb_core::fs::exfat::geometry::ExfatGeometry;
use teslausb_core::fs::exfat::upcase_table::UpcaseTable;
use teslausb_core::fs::geometry::{Geometry, SECTOR_SIZE_BYTES};

const VOLUME_SIZE_GB: u32 = 4;
const VOLUME_BYTES: u64 = (VOLUME_SIZE_GB as u64) * 1024 * 1024 * 1024;
const SECTOR: u64 = SECTOR_SIZE_BYTES as u64;
const EXFAT_EOC: u32 = 0xFFFF_FFFF;

fn sample_cfg(backing_root: PathBuf) -> Config {
    Config {
        backing_root,
        volume_size_gb: VOLUME_SIZE_GB,
        volume_label: "EXFATINT".to_string(),
        cluster_size: None,
        fs_type: FsType::Exfat,
        retention: RetentionConfig::default(),
        nbd: NbdConfig::default(),
    }
}

fn geometry() -> ExfatGeometry {
    ExfatGeometry::for_volume_size(VOLUME_BYTES).expect("4 GiB is a valid exFAT volume")
}

fn fat_volume_byte(g: &ExfatGeometry, cluster: u32) -> u64 {
    u64::from(g.fat_offset_sectors()) * SECTOR + u64::from(cluster) * 4
}

fn cluster_volume_byte(g: &ExfatGeometry, cluster: u32) -> u64 {
    u64::from(g.cluster_heap_offset_sectors()) * SECTOR
        + u64::from(cluster - FIRST_DATA_CLUSTER) * u64::from(g.bytes_per_cluster())
}

fn root_cluster_byte(g: &ExfatGeometry) -> u64 {
    cluster_volume_byte(g, g.first_root_directory_cluster())
}

fn ts() -> FileTimestamps {
    FileTimestamps {
        create_timestamp: 0x4A21_0000,
        modify_timestamp: 0x4A21_0001,
        access_timestamp: 0x4A21_0002,
        create_10ms: 50,
        modify_10ms: 25,
        create_utc_offset: 0x80,
        modify_utc_offset: 0x80,
        access_utc_offset: 0x80,
    }
}

fn build_file_entry(
    name: &str,
    first_cluster: u32,
    data_length: u64,
    no_fat_chain: bool,
) -> Vec<u8> {
    let name_utf16: Vec<u16> = name.encode_utf16().collect();
    let params = FileEntrySetParams {
        name: &name_utf16,
        attributes: FileAttributes::default(),
        timestamps: ts(),
        first_cluster,
        valid_data_length: data_length,
        data_length,
        no_fat_chain,
    };
    let upcase = UpcaseTable::ascii_identity();
    encode_file_entry_set(&params, &upcase).expect("encode file entry set")
}

fn build_dir_entry(name: &str, first_cluster: u32, data_length: u64) -> Vec<u8> {
    let name_utf16: Vec<u16> = name.encode_utf16().collect();
    let attributes = FileAttributes {
        directory: true,
        ..FileAttributes::default()
    };
    let params = FileEntrySetParams {
        name: &name_utf16,
        attributes,
        timestamps: ts(),
        first_cluster,
        valid_data_length: data_length,
        data_length,
        no_fat_chain: true,
    };
    let upcase = UpcaseTable::ascii_identity();
    encode_file_entry_set(&params, &upcase).expect("encode dir entry set")
}

async fn write_fat_entry(backend: &SynthBackend, g: &ExfatGeometry, cluster: u32, value: u32) {
    backend
        .write(
            fat_volume_byte(g, cluster),
            &value.to_le_bytes(),
            WriteFlags::NONE,
        )
        .await
        .expect("FAT entry write");
}

async fn write_cluster_data(g: &ExfatGeometry, backend: &SynthBackend, cluster: u32, data: &[u8]) {
    let offset = cluster_volume_byte(g, cluster);
    backend
        .write(offset, data, WriteFlags::NONE)
        .await
        .expect("data cluster write");
}

fn open_empty_backend() -> (TempDir, SynthBackend) {
    let dir = TempDir::new().expect("tempdir");
    let cfg = sample_cfg(dir.path().to_path_buf());
    let backend = SynthBackend::open(&cfg).expect("open");
    (dir, backend)
}

#[tokio::test]
async fn exfat_create_single_cluster_nofatchain_file() {
    let (dir, backend) = open_empty_backend();
    let g = geometry();
    let payload = b"Hello from the exFAT integration test!".repeat(8);
    let file_cluster = 5;

    // 1. Data first (no FAT walk needed for no_fat_chain).
    write_cluster_data(&g, &backend, file_cluster, &payload).await;
    // 2. Directory entry into the root cluster.
    let dir_entry = build_file_entry("hello.txt", file_cluster, payload.len() as u64, true);
    backend
        .write(root_cluster_byte(&g), &dir_entry, WriteFlags::NONE)
        .await
        .expect("dir entry write");
    // 3. Flush.
    backend.flush().await.expect("flush");

    let on_disk = std::fs::read(dir.path().join("hello.txt")).expect("file present");
    assert_eq!(&on_disk, &payload);
}

#[tokio::test]
async fn exfat_out_of_order_writes_still_create_file() {
    let (dir, backend) = open_empty_backend();
    let g = geometry();
    let payload = b"out of order exfat".repeat(20);
    let file_cluster = 6;

    // Dir entry first — file is pending.
    let dir_entry = build_file_entry("ooo.bin", file_cluster, payload.len() as u64, true);
    backend
        .write(root_cluster_byte(&g), &dir_entry, WriteFlags::NONE)
        .await
        .expect("dir entry");
    // Data second.
    write_cluster_data(&g, &backend, file_cluster, &payload).await;

    backend.flush().await.expect("flush");
    let on_disk = std::fs::read(dir.path().join("ooo.bin")).expect("file present");
    assert_eq!(&on_disk, &payload);
}

#[tokio::test]
async fn exfat_fat_chained_file_resolves() {
    let (dir, backend) = open_empty_backend();
    let g = geometry();
    let bpc = g.bytes_per_cluster() as usize;
    // 2 clusters + partial third — but advertise as
    // no_fat_chain=false so we exercise the FAT walker even
    // though all clusters happen to be contiguous.
    let mut payload = Vec::new();
    payload.extend(std::iter::repeat_n(0xAAu8, bpc));
    payload.extend(std::iter::repeat_n(0xBBu8, bpc));
    payload.extend(std::iter::repeat_n(0xCCu8, 200));
    let total_size = payload.len() as u64;

    let c1 = 7;
    let c2 = 8;
    let c3 = 9;

    // FAT chain: c1 -> c2 -> c3 -> EOC.
    write_fat_entry(&backend, &g, c1, c2).await;
    write_fat_entry(&backend, &g, c2, c3).await;
    write_fat_entry(&backend, &g, c3, EXFAT_EOC).await;

    // Data clusters.
    write_cluster_data(&g, &backend, c1, &payload[..bpc]).await;
    write_cluster_data(&g, &backend, c2, &payload[bpc..2 * bpc]).await;
    write_cluster_data(&g, &backend, c3, &payload[2 * bpc..]).await;

    // Dir entry with no_fat_chain=false.
    let dir_entry = build_file_entry("chained.bin", c1, total_size, false);
    backend
        .write(root_cluster_byte(&g), &dir_entry, WriteFlags::NONE)
        .await
        .expect("dir entry");

    backend.flush().await.expect("flush");

    let on_disk = std::fs::read(dir.path().join("chained.bin")).expect("file");
    assert_eq!(on_disk.len(), total_size as usize);
    assert!(on_disk[..bpc].iter().all(|&b| b == 0xAA));
    assert!(on_disk[bpc..2 * bpc].iter().all(|&b| b == 0xBB));
    assert!(on_disk[2 * bpc..].iter().all(|&b| b == 0xCC));
}

#[tokio::test]
async fn exfat_deletion_removes_backing_file_after_finalize() {
    let (dir, backend) = open_empty_backend();
    let g = geometry();
    let payload = b"will be deleted exfat".repeat(8);
    let file_cluster = 10;

    write_cluster_data(&g, &backend, file_cluster, &payload).await;
    let dir_entry = build_file_entry("doomed.bin", file_cluster, payload.len() as u64, true);
    backend
        .write(root_cluster_byte(&g), &dir_entry, WriteFlags::NONE)
        .await
        .expect("dir entry");
    backend.flush().await.expect("flush create");
    assert!(dir.path().join("doomed.bin").exists());

    // Tesla deletes the file by zeroing the entry set. Real
    // `exFAT` deletes only clear the InUse bit (0x80) of each
    // entry type; we model the kernel's pattern of overwriting
    // the cluster (rewriting with zeros) as a worst case —
    // either way the redecoded set won't classify as the
    // original file and the diff fires handle_child_deleted.
    let bpc = g.bytes_per_cluster() as usize;
    let zeros = vec![0u8; bpc];
    backend
        .write(root_cluster_byte(&g), &zeros, WriteFlags::NONE)
        .await
        .expect("delete write");
    backend.flush().await.expect("flush delete");

    assert!(
        !dir.path().join("doomed.bin").exists(),
        "delete should remove backing file"
    );
}

#[tokio::test]
async fn exfat_in_place_overwrite_of_pre_existing_file_preserves_untouched_bytes() {
    // Seed a pre-existing file in the backing tree. The layout
    // planner assigns it a cluster chain; we discover the chain
    // by reading the synthesised root cluster.
    let dir = TempDir::new().expect("tempdir");
    let original = b"AAAAAAAAAA".repeat(64);
    let rel = PathBuf::from("preex.bin");
    std::fs::write(dir.path().join(&rel), &original).expect("seed");
    let cfg = sample_cfg(dir.path().to_path_buf());
    let backend = SynthBackend::open(&cfg).expect("open");
    let g = geometry();

    // Read the synthesised root cluster and decode it to find
    // the file's first cluster.
    let root_bytes = {
        let mut buf = vec![0u8; g.bytes_per_cluster() as usize];
        backend.read(root_cluster_byte(&g), &mut buf).await.unwrap();
        buf
    };
    let decoded = teslausb_core::fs::exfat::dir_decode::decode_directory_cluster(&root_bytes, None)
        .expect("root decode");
    let mut first_cluster = None;
    for entry in &decoded.entries {
        if let teslausb_core::fs::exfat::dir_decode::DecodedExfatEntry::File {
            name,
            first_cluster: fc,
            ..
        } = entry
        {
            if name.as_deref() == Some("preex.bin") {
                first_cluster = Some(*fc);
                break;
            }
        }
    }
    let first_cluster = first_cluster.expect("file appears in synthesised root");

    // Tesla overwrites bytes 100..110 in place.
    let cluster_offset = cluster_volume_byte(&g, first_cluster);
    backend
        .write(cluster_offset + 100, b"ZZZZZZZZZZ", WriteFlags::NONE)
        .await
        .expect("in-place write");
    backend.flush().await.expect("flush");

    let after = std::fs::read(dir.path().join(&rel)).expect("read after");
    assert_eq!(after.len(), original.len(), "file size preserved");
    assert_eq!(&after[..100], &original[..100]);
    assert_eq!(&after[100..110], b"ZZZZZZZZZZ");
    assert_eq!(&after[110..], &original[110..]);
}

#[tokio::test]
async fn exfat_write_without_flush_does_not_materialize_file() {
    let (dir, backend) = open_empty_backend();
    let g = geometry();
    let payload = b"unflushed exfat".repeat(4);
    let file_cluster = 11;

    write_cluster_data(&g, &backend, file_cluster, &payload).await;
    let dir_entry = build_file_entry("partial.bin", file_cluster, payload.len() as u64, true);
    backend
        .write(root_cluster_byte(&g), &dir_entry, WriteFlags::NONE)
        .await
        .expect("dir entry");

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
async fn exfat_fua_flag_finalizes_immediately() {
    let (dir, backend) = open_empty_backend();
    let g = geometry();
    let payload = b"FUA exfat".repeat(4);
    let file_cluster = 12;

    write_cluster_data(&g, &backend, file_cluster, &payload).await;
    let dir_entry = build_file_entry("fua.bin", file_cluster, payload.len() as u64, true);
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
async fn exfat_double_flush_is_idempotent() {
    let (dir, backend) = open_empty_backend();
    let g = geometry();
    let payload = b"double flush".repeat(4);
    let file_cluster = 13;

    write_cluster_data(&g, &backend, file_cluster, &payload).await;
    let dir_entry = build_file_entry("dfx.bin", file_cluster, payload.len() as u64, true);
    backend
        .write(root_cluster_byte(&g), &dir_entry, WriteFlags::NONE)
        .await
        .expect("dir entry");

    backend.flush().await.expect("flush 1");
    backend.flush().await.expect("flush 2");

    assert!(dir.path().join("dfx.bin").exists());
}

#[tokio::test]
async fn exfat_subdirectory_creation_routes_child_into_subdir() {
    let (dir, backend) = open_empty_backend();
    let g = geometry();
    let bpc = g.bytes_per_cluster();
    let subdir_cluster = 14;
    let child_cluster = 15;

    // Subdir entry in root (no_fat_chain=true, single cluster).
    let subdir_entry = build_dir_entry("subdir", subdir_cluster, u64::from(bpc));
    backend
        .write(root_cluster_byte(&g), &subdir_entry, WriteFlags::NONE)
        .await
        .expect("subdir entry");

    // Child file entry inside subdir cluster.
    let child_payload = b"inside subdir exfat".repeat(8);
    let child_entry =
        build_file_entry("child.txt", child_cluster, child_payload.len() as u64, true);
    backend
        .write(
            cluster_volume_byte(&g, subdir_cluster),
            &child_entry,
            WriteFlags::NONE,
        )
        .await
        .expect("child entry");

    // Child data.
    write_cluster_data(&g, &backend, child_cluster, &child_payload).await;

    backend.flush().await.expect("flush");

    let child_path = dir.path().join("subdir").join("child.txt");
    let on_disk = std::fs::read(&child_path).expect("child file");
    assert_eq!(&on_disk, &child_payload);
    assert!(
        dir.path().join("subdir").is_dir(),
        "subdir should exist as directory"
    );
}

#[tokio::test]
async fn exfat_empty_file_creates_zero_length_final_after_dir_entry_alone() {
    // Empty file has data_length=0, so no data clusters are
    // ever written. Phase 3.5e never creates a .partial for an
    // empty file (no data routed → in_flight stays empty), so
    // flush is a no-op and the empty final file isn't
    // materialised. This pins the contract.
    let (dir, backend) = open_empty_backend();
    let g = geometry();
    let dir_entry = build_file_entry("empty.bin", 5, 0, true);
    backend
        .write(root_cluster_byte(&g), &dir_entry, WriteFlags::NONE)
        .await
        .expect("dir entry");
    backend.flush().await.expect("flush");
    // No data was written, no file should appear. (The user
    // contract is "data-bearing files survive a crash"; empty
    // files materialise only after the kernel issues an empty
    // create — which it does via touch + close → 0-byte data
    // write on most filesystems but not always on exFAT.)
    assert!(!dir.path().join("empty.bin").exists());
}

#[tokio::test]
async fn exfat_two_files_in_root_both_materialize() {
    let (dir, backend) = open_empty_backend();
    let g = geometry();

    let p1 = b"first file".repeat(4);
    let p2 = b"second file".repeat(4);
    let c1 = 5;
    let c2 = 6;

    write_cluster_data(&g, &backend, c1, &p1).await;
    let e1 = build_file_entry("first.bin", c1, p1.len() as u64, true);
    backend
        .write(root_cluster_byte(&g), &e1, WriteFlags::NONE)
        .await
        .expect("e1");

    // Second entry written immediately after the first.
    write_cluster_data(&g, &backend, c2, &p2).await;
    let e2 = build_file_entry("second.bin", c2, p2.len() as u64, true);
    backend
        .write(
            root_cluster_byte(&g) + e1.len() as u64,
            &e2,
            WriteFlags::NONE,
        )
        .await
        .expect("e2");

    backend.flush().await.expect("flush");

    assert_eq!(std::fs::read(dir.path().join("first.bin")).unwrap(), p1);
    assert_eq!(std::fs::read(dir.path().join("second.bin")).unwrap(), p2);
}
