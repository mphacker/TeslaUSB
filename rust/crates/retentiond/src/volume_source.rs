use std::collections::{BTreeMap, HashMap};
use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use scannerd::boot::{ExfatParams, parse_boot_sector};
use scannerd::clip::parse_clip_name;
use scannerd::mbr::parse_mbr;
use scannerd::stability::{StabilityConfig, StabilityTracker};
use scannerd::timestamp::epoch_from_tesla_timestamp;
use scannerd::volume::Volume;
use scannerd::walk::FileRecord;
use teslausb_core::fs::exfat::dir_decode::{DecodedExfatEntry, decode_directory_cluster};

use crate::candidates::{Candidate, CandidateAngle, CandidateSource};
use crate::volume_reader::PreadBlockReader;

const RECENT_PREFIX: &str = "TeslaCam/RecentClips/";
const REQUIRED_STABLE_SCANS: u32 = 2;
const REQUIRED_QUIESCENCE_SECS: u64 = 60;
const MAX_DIR_CLUSTERS: usize = 100_000;
const MAX_DIR_ENTRIES: usize = 500_000;
const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;

#[derive(Debug, Clone)]
struct DirNode {
    first_cluster: u32,
    no_fat_chain: bool,
    contiguous_span: Option<u64>,
}

#[derive(Debug, Clone)]
/// Direct volume-image candidate source for flat `TeslaCam/RecentClips` clips.
pub struct VolumeCandidateSource {
    volume_image: Arc<PathBuf>,
    slot: u8,
    tracker: Arc<Mutex<StabilityTracker>>,
}

impl VolumeCandidateSource {
    /// Open a source over `volume_image`, tracking stability for the selected slot.
    ///
    /// # Errors
    ///
    /// Returns an error when the volume image cannot be opened.
    pub fn open(volume_image: impl Into<PathBuf>, slot: u8) -> io::Result<Self> {
        let volume_image = Arc::new(volume_image.into());
        let _reader = PreadBlockReader::open(&volume_image)?;
        let mut config = StabilityConfig::default();
        config.required_stable_scans = config.required_stable_scans.max(REQUIRED_STABLE_SCANS);
        config.quiescence_secs = config.quiescence_secs.max(REQUIRED_QUIESCENCE_SECS);
        Ok(Self {
            volume_image,
            slot,
            tracker: Arc::new(Mutex::new(StabilityTracker::new(config))),
        })
    }

    fn load_recent_records(&self) -> io::Result<(u32, Vec<FileRecord>, HashMap<String, u64>)> {
        let reader = PreadBlockReader::open(self.volume_image.as_ref())?;
        let params = parse_slot_params(&reader, self.slot)?;
        let volume = Volume::new(&reader, params);
        let records = list_recent_records(&volume, self.slot)?;
        let digests = chain_digests_for_records(&volume, &records)?;
        Ok((params.volume_serial, records, digests))
    }
}

impl CandidateSource for VolumeCandidateSource {
    fn list_candidates(&self) -> io::Result<Vec<Candidate>> {
        let (volume_serial, records, chain_digests) = self.load_recent_records()?;
        let now_secs = now_epoch_secs();
        let eligible = {
            let mut tracker = self
                .tracker
                .lock()
                .map_err(|_| io::Error::other("stability tracker lock poisoned"))?;
            tracker.arm_resync();
            select_stable_records(&mut tracker, &records, now_secs)
        };
        Ok(group_recent_candidates(
            self.slot,
            volume_serial,
            eligible,
            &chain_digests,
        ))
    }
}

fn now_epoch_secs() -> u64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(duration) => duration.as_secs(),
        Err(_) => 0,
    }
}

fn parse_slot_params(reader: &PreadBlockReader, slot: u8) -> io::Result<ExfatParams> {
    let partitions = parse_mbr(reader).map_err(|err| scanner_to_io(&err))?;
    let part = partitions
        .iter()
        .copied()
        .find(|entry| entry.slot == slot && entry.is_exfat())
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "teslacam exfat slot not found"))?;
    parse_boot_sector(reader, part.start_lba).map_err(|err| scanner_to_io(&err))
}

fn scanner_to_io(err: &scannerd::error::ScannerError) -> io::Error {
    io::Error::other(err.to_string())
}

fn list_recent_records<R: scannerd::reader::BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
    slot: u8,
) -> io::Result<Vec<FileRecord>> {
    let root = DirNode {
        first_cluster: volume.params().first_root_cluster,
        no_fat_chain: false,
        contiguous_span: None,
    };
    let Some(tesla_cam) = find_subdir(volume, &root, "TeslaCam")? else {
        return Ok(Vec::new());
    };
    let Some(recent) = find_subdir(volume, &tesla_cam, "RecentClips")? else {
        return Ok(Vec::new());
    };

    let entries = read_dir_entries(volume, &recent)?;
    let mut records = Vec::new();
    for entry in entries {
        let DecodedExfatEntry::File {
            name,
            name_hash,
            attributes,
            timestamps,
            first_cluster,
            valid_data_length,
            data_length,
            no_fat_chain,
            set_checksum_ok,
            ..
        } = entry
        else {
            continue;
        };
        if attributes.directory {
            continue;
        }
        let Some(name) = name else {
            continue;
        };
        records.push(FileRecord {
            partition_slot: slot,
            path: format!("{RECENT_PREFIX}{name}"),
            name,
            name_hash: u32::from(name_hash),
            first_cluster,
            data_length,
            valid_data_length,
            no_fat_chain,
            timestamps,
            set_checksum_ok,
            dir_first_cluster: recent.first_cluster,
        });
    }
    Ok(records)
}

fn chain_digests_for_records<R: scannerd::reader::BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
    records: &[FileRecord],
) -> io::Result<HashMap<String, u64>> {
    let mut digests = HashMap::new();
    for record in records {
        if record.no_fat_chain {
            continue;
        }
        // A 0-byte / in-flux clip can report first_cluster=0 (no data cluster
        // yet). follow_chain(0) would abort the whole cycle, so skip the chain
        // digest for invalid clusters — such a record is not a stable archivable
        // clip and the stability gate filters it out downstream anyway.
        if !volume.params().is_valid_cluster(record.first_cluster) {
            continue;
        }
        let digest = record_chain_digest(volume, record)?;
        digests.insert(record_chain_key(record), digest);
    }
    Ok(digests)
}

fn record_chain_digest<R: scannerd::reader::BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
    record: &FileRecord,
) -> io::Result<u64> {
    let span = record
        .data_length
        .div_ceil(volume.params().bytes_per_cluster())
        .max(1);
    let chain = volume
        .follow_chain(record.first_cluster, false, span)
        .map_err(|err| scanner_to_io(&err))?;
    Ok(fold_chain_digest(&chain))
}

fn fold_chain_digest(chain: &[u32]) -> u64 {
    let mut hash = FNV_OFFSET;
    let fold = |hash: &mut u64, bytes: &[u8]| {
        for byte in bytes {
            *hash ^= u64::from(*byte);
            *hash = hash.wrapping_mul(FNV_PRIME);
        }
    };
    let chain_len = u64::try_from(chain.len()).unwrap_or(u64::MAX);
    fold(&mut hash, &chain_len.to_le_bytes());
    for cluster in chain {
        fold(&mut hash, &cluster.to_le_bytes());
    }
    hash
}

fn record_chain_key(record: &FileRecord) -> String {
    format!(
        "{}:{}:{}:{}",
        record.path, record.first_cluster, record.data_length, record.name_hash
    )
}

fn find_subdir<R: scannerd::reader::BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
    parent: &DirNode,
    name_component: &str,
) -> io::Result<Option<DirNode>> {
    for entry in read_dir_entries(volume, parent)? {
        let DecodedExfatEntry::File {
            name,
            attributes,
            first_cluster,
            data_length,
            no_fat_chain,
            ..
        } = entry
        else {
            continue;
        };
        if !attributes.directory {
            continue;
        }
        let Some(name) = name else {
            continue;
        };
        if !name.eq_ignore_ascii_case(name_component) {
            continue;
        }
        let span = data_length.div_ceil(volume.params().bytes_per_cluster());
        return Ok(Some(DirNode {
            first_cluster,
            no_fat_chain,
            contiguous_span: Some(span),
        }));
    }
    Ok(None)
}

fn read_dir_entries<R: scannerd::reader::BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
    node: &DirNode,
) -> io::Result<Vec<DecodedExfatEntry>> {
    // An empty exFAT directory reports first_cluster=0 (no data cluster
    // allocated). scannerd's proven walk guards every subdirectory descent with
    // is_valid_cluster before follow_chain (walk.rs); a directory-direct
    // traversal like ours must do the same, or follow_chain(0) aborts the whole
    // archive cycle with "invalid cluster 0: chain start out of range". Treat an
    // unallocated/invalid directory cluster as simply empty so an empty (or
    // just-cleared) RecentClips can never take the archiver down.
    if !volume.params().is_valid_cluster(node.first_cluster) {
        return Ok(Vec::new());
    }
    // Bound the contiguous-span request to our directory-cluster cap so a
    // contiguous (no_fat_chain) directory chain can never request more than
    // MAX_DIR_CLUSTERS+1. FAT-chained directories ignore this span inside
    // scannerd, but scannerd's follow_chain is cycle-detected and hard-capped
    // at the volume cluster count, so it always terminates; we then truncate to
    // MAX_DIR_CLUSTERS below and bound decoded entries via MAX_DIR_ENTRIES.
    let cap_span = (MAX_DIR_CLUSTERS as u64).saturating_add(1);
    let span = node
        .contiguous_span
        .unwrap_or_else(|| u64::from(volume.params().cluster_count).saturating_add(1))
        .min(cap_span);
    let mut clusters = volume
        .follow_chain(node.first_cluster, node.no_fat_chain, span)
        .map_err(|err| scanner_to_io(&err))?;
    if cap_directory_clusters(&mut clusters, node.first_cluster) {
        log_directory_cap_warning(
            "directory chain exceeded cap",
            node.first_cluster,
            MAX_DIR_CLUSTERS,
        );
    }

    let mut entries = Vec::new();
    let mut carry = None;
    for cluster in clusters {
        let bytes = volume.read_cluster(cluster).map_err(|err| scanner_to_io(&err))?;
        let decoded = decode_directory_cluster(&bytes, carry)
            .map_err(|err| io::Error::other(err.to_string()))?;
        carry = decoded.trailing_partial_set;
        if extend_entries_with_cap(&mut entries, decoded.entries, node.first_cluster) {
            log_directory_cap_warning(
                "directory entry count exceeded cap",
                node.first_cluster,
                MAX_DIR_ENTRIES,
            );
            break;
        }
        if decoded.end_of_directory_seen {
            break;
        }
    }
    Ok(entries)
}

fn cap_directory_clusters(clusters: &mut Vec<u32>, _first_cluster: u32) -> bool {
    if clusters.len() <= MAX_DIR_CLUSTERS {
        return false;
    }
    clusters.truncate(MAX_DIR_CLUSTERS);
    true
}

fn extend_entries_with_cap(
    entries: &mut Vec<DecodedExfatEntry>,
    decoded_entries: Vec<DecodedExfatEntry>,
    _first_cluster: u32,
) -> bool {
    if entries.len() >= MAX_DIR_ENTRIES {
        return true;
    }
    let remaining = MAX_DIR_ENTRIES.saturating_sub(entries.len());
    if decoded_entries.len() > remaining {
        entries.extend(decoded_entries.into_iter().take(remaining));
        return true;
    }
    entries.extend(decoded_entries);
    false
}

fn log_directory_cap_warning(reason: &str, first_cluster: u32, cap: usize) {
    let mut stderr = io::stderr();
    let _ = writeln!(
        &mut stderr,
        "retentiond volume_source: {reason} first_cluster={first_cluster} cap={cap}"
    );
}

fn select_stable_records(
    tracker: &mut StabilityTracker,
    records: &[FileRecord],
    now_secs: u64,
) -> Vec<FileRecord> {
    tracker
        .observe(records, now_secs)
        .into_iter()
        .filter_map(|idx| records.get(idx))
        .filter(|record| record.valid_data_length == record.data_length && record.set_checksum_ok)
        .cloned()
        .collect()
}

fn group_recent_candidates(
    slot: u8,
    volume_serial: u32,
    records: Vec<FileRecord>,
    chain_digests: &HashMap<String, u64>,
) -> Vec<Candidate> {
    let mut grouped: BTreeMap<String, Vec<FileRecord>> = BTreeMap::new();
    for record in records {
        if !record.path.starts_with(RECENT_PREFIX) {
            continue;
        }
        let rel = record.path.trim_start_matches(RECENT_PREFIX);
        if rel.contains('/') {
            continue;
        }
        let Some(parsed) = parse_clip_name(&record.name) else {
            continue;
        };
        if parsed.camera.is_none() {
            continue;
        }
        grouped.entry(parsed.timestamp).or_default().push(record);
    }

    let mut out = Vec::with_capacity(grouped.len());
    for (timestamp, mut group) in grouped {
        group.sort_by(|a, b| a.path.cmp(&b.path));
        let started_at = epoch_from_tesla_timestamp(&timestamp).unwrap_or(0);
        let canonical_key = format!("{slot}:{RECENT_PREFIX}{timestamp}");
        let angles = group
            .iter()
            .filter_map(|record| {
                parse_clip_name(&record.name).and_then(|parsed| {
                    parsed.camera.map(|camera| CandidateAngle {
                        camera,
                        file_ref: record.path.clone(),
                        offset_ms: 0,
                        duration_s: None,
                        size_bytes: record.valid_data_length,
                    })
                })
            })
            .collect::<Vec<_>>();
        if angles.is_empty() {
            continue;
        }

        out.push(Candidate {
            clip_id: clip_id_for_key(&canonical_key),
            canonical_key: canonical_key.clone(),
            partition: format!("slot{slot}"),
            started_at,
            ended_at: started_at,
            duration_s: None,
            source_volume_serial: volume_serial,
            source_fingerprint: clip_source_fingerprint(
                slot,
                volume_serial,
                &timestamp,
                &group,
                chain_digests,
            ),
            angles,
        });
    }
    out.sort_by_key(|candidate| (candidate.started_at, candidate.canonical_key.clone()));
    out
}

fn clip_id_for_key(key: &str) -> i64 {
    let mut hash = 0xcbf2_9ce4_8422_2325_u64;
    for byte in key.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    let value = hash & (i64::MAX as u64);
    i64::try_from(value).unwrap_or(i64::MAX)
}

fn clip_source_fingerprint(
    slot: u8,
    volume_serial: u32,
    timestamp: &str,
    records: &[FileRecord],
    chain_digests: &HashMap<String, u64>,
) -> String {
    let mut hash = FNV_OFFSET;
    let fold = |hash: &mut u64, bytes: &[u8]| {
        for byte in bytes {
            *hash ^= u64::from(*byte);
            *hash = hash.wrapping_mul(FNV_PRIME);
        }
    };
    fold(&mut hash, &[slot]);
    fold(&mut hash, &volume_serial.to_le_bytes());
    fold(&mut hash, timestamp.as_bytes());
    for record in records {
        fold(&mut hash, record.path.as_bytes());
        fold(&mut hash, &record.name_hash.to_le_bytes());
        fold(&mut hash, &record.first_cluster.to_le_bytes());
        fold(&mut hash, &record.data_length.to_le_bytes());
        fold(&mut hash, &record.valid_data_length.to_le_bytes());
        fold(&mut hash, &[u8::from(record.set_checksum_ok)]);
        fold(&mut hash, &[u8::from(record.no_fat_chain)]);
        if !record.no_fat_chain {
            let digest = chain_digests
                .get(&record_chain_key(record))
                .copied()
                .unwrap_or_default();
            fold(&mut hash, &digest.to_le_bytes());
        }
        fold(&mut hash, &record.dir_first_cluster.to_le_bytes());
        fold(&mut hash, &record.timestamps.create_timestamp.to_le_bytes());
        fold(&mut hash, &record.timestamps.modify_timestamp.to_le_bytes());
        fold(&mut hash, &[record.timestamps.modify_10ms]);
    }
    format!("{hash:016x}")
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic, clippy::indexing_slicing)]
mod tests {
    use std::collections::HashMap;

    use super::{
        DirNode, MAX_DIR_CLUSTERS, MAX_DIR_ENTRIES, cap_directory_clusters, chain_digests_for_records,
        clip_source_fingerprint, extend_entries_with_cap, group_recent_candidates, read_dir_entries,
        record_chain_key, select_stable_records,
    };
    use scannerd::boot::ExfatParams;
    use scannerd::reader::{BlockReader, ReaderError};
    use scannerd::stability::{StabilityConfig, StabilityTracker};
    use scannerd::volume::Volume;
    use scannerd::walk::FileRecord;
    use teslausb_core::fs::exfat::dir_decode::DecodedExfatEntry;
    use teslausb_core::fs::exfat::directory::FileTimestamps;

    struct ZeroReader {
        size: u64,
    }

    impl BlockReader for ZeroReader {
        fn size_bytes(&self) -> u64 {
            self.size
        }

        fn read_exact_at(&self, _offset: u64, buf: &mut [u8]) -> Result<(), ReaderError> {
            buf.fill(0);
            Ok(())
        }
    }

    fn test_params() -> ExfatParams {
        ExfatParams {
            partition_offset_sectors: 2048,
            volume_length_sectors: 1 << 20,
            fat_offset_sectors: 128,
            fat_length_sectors: 64,
            cluster_heap_offset_sectors: 4096,
            cluster_count: 1000,
            first_root_cluster: 4,
            volume_serial: 0xdead_beef,
            bytes_per_sector_shift: 9,
            sectors_per_cluster_shift: 3,
            number_of_fats: 1,
        }
    }

    fn rec(path: &str, name: &str, vdl: u64, dl: u64, set_checksum_ok: bool) -> FileRecord {
        FileRecord {
            partition_slot: 0,
            path: path.to_owned(),
            name: name.to_owned(),
            name_hash: 1,
            first_cluster: 10,
            data_length: dl,
            valid_data_length: vdl,
            no_fat_chain: false,
            timestamps: FileTimestamps::default(),
            set_checksum_ok,
            dir_first_cluster: 4,
        }
    }

    #[test]
    fn flat_recentclips_grouping_ignores_subfolders() {
        let records = vec![
            rec(
                "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4",
                "2026-06-19_10-00-00-front.mp4",
                1024,
                1024,
                true,
            ),
            rec(
                "TeslaCam/RecentClips/2026-06-19_10-00-00-back.mp4",
                "2026-06-19_10-00-00-back.mp4",
                512,
                512,
                true,
            ),
            rec(
                "TeslaCam/RecentClips/subdir/2026-06-19_10-00-00-left.mp4",
                "2026-06-19_10-00-00-left.mp4",
                999,
                999,
                true,
            ),
        ];

        let grouped = group_recent_candidates(0, 0x1234_abcd, records, &HashMap::new());
        assert_eq!(grouped.len(), 1);
        assert_eq!(
            grouped[0].canonical_key,
            "0:TeslaCam/RecentClips/2026-06-19_10-00-00"
        );
        assert_eq!(grouped[0].angles.len(), 2);
    }

    #[test]
    fn growing_clip_is_ineligible_until_full_checksum_ok_and_stable() {
        let mut tracker = StabilityTracker::new(StabilityConfig {
            required_stable_scans: 2,
            quiescence_secs: 60,
        });

        let growing = vec![rec(
            "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4",
            "2026-06-19_10-00-00-front.mp4",
            500,
            1000,
            true,
        )];
        assert!(select_stable_records(&mut tracker, &growing, 0).is_empty());
        assert!(select_stable_records(&mut tracker, &growing, 120).is_empty());

        let bad_checksum = vec![rec(
            "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4",
            "2026-06-19_10-00-00-front.mp4",
            1000,
            1000,
            false,
        )];
        assert!(select_stable_records(&mut tracker, &bad_checksum, 240).is_empty());
        assert!(select_stable_records(&mut tracker, &bad_checksum, 360).is_empty());

        let complete = vec![rec(
            "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4",
            "2026-06-19_10-00-00-front.mp4",
            1000,
            1000,
            true,
        )];
        assert!(select_stable_records(&mut tracker, &complete, 480).is_empty());
        let stable = select_stable_records(&mut tracker, &complete, 541);
        assert_eq!(stable.len(), 1);
        assert_eq!(stable[0].valid_data_length, stable[0].data_length);
        assert!(stable[0].set_checksum_ok);
    }

    #[test]
    fn stable_clip_is_reoffered_each_cycle_when_resync_is_armed() {
        let mut tracker = StabilityTracker::new(StabilityConfig {
            required_stable_scans: 2,
            quiescence_secs: 60,
        });
        let complete = vec![rec(
            "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4",
            "2026-06-19_10-00-00-front.mp4",
            1000,
            1000,
            true,
        )];

        tracker.arm_resync();
        assert!(select_stable_records(&mut tracker, &complete, 0).is_empty());
        tracker.arm_resync();
        let first = select_stable_records(&mut tracker, &complete, 120);
        assert_eq!(first.len(), 1);
        tracker.arm_resync();
        let second = select_stable_records(&mut tracker, &complete, 180);
        assert_eq!(second.len(), 1);
    }

    #[test]
    fn source_fingerprint_changes_when_fragment_chain_digest_changes() {
        let record = rec(
            "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4",
            "2026-06-19_10-00-00-front.mp4",
            1000,
            1000,
            true,
        );
        let mut digest_a = HashMap::new();
        digest_a.insert(record_chain_key(&record), 0x1111_2222_3333_4444);
        let mut digest_b = HashMap::new();
        digest_b.insert(record_chain_key(&record), 0xaaaa_bbbb_cccc_dddd);

        let fingerprint_a =
            clip_source_fingerprint(0, 0x1234_abcd, "2026-06-19_10-00-00", &[record.clone()], &digest_a);
        let fingerprint_b =
            clip_source_fingerprint(0, 0x1234_abcd, "2026-06-19_10-00-00", &[record], &digest_b);
        assert_ne!(fingerprint_a, fingerprint_b);
    }

    #[test]
    fn read_dir_entries_treats_empty_directory_first_cluster_zero_as_empty() {
        // Regression: an empty exFAT directory reports first_cluster=0. Without
        // the is_valid_cluster guard, read_dir_entries called follow_chain(0)
        // and aborted the whole archive cycle ("invalid cluster 0: chain start
        // out of range") — taking the always-on archiver down whenever
        // RecentClips was momentarily empty. It must return an empty listing,
        // never an error.
        let reader = ZeroReader { size: 1 << 24 };
        let volume = Volume::new(&reader, test_params());
        let empty_dir = DirNode {
            first_cluster: 0,
            no_fat_chain: false,
            contiguous_span: Some(0),
        };
        let entries = read_dir_entries(&volume, &empty_dir).expect("empty directory must not error");
        assert!(entries.is_empty());
    }

    #[test]
    fn chain_digests_skip_records_with_invalid_first_cluster() {
        // A 0-byte / in-flux clip can report first_cluster=0; computing its
        // chain digest would follow_chain(0) and abort the cycle. Such records
        // must be skipped, not error.
        let reader = ZeroReader { size: 1 << 24 };
        let volume = Volume::new(&reader, test_params());
        let mut zero = rec(
            "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4",
            "2026-06-19_10-00-00-front.mp4",
            0,
            0,
            true,
        );
        zero.first_cluster = 0;
        let digests = chain_digests_for_records(&volume, &[zero]).expect("must not error");
        assert!(digests.is_empty());
    }

    #[test]
    fn directory_cap_helpers_bound_clusters_and_entries() {
        let mut clusters: Vec<u32> = (2..=u32::try_from(MAX_DIR_CLUSTERS + 10).expect("u32 cap"))
            .collect();
        assert!(cap_directory_clusters(&mut clusters, 2));
        assert_eq!(clusters.len(), MAX_DIR_CLUSTERS);

        let mut entries = Vec::with_capacity(MAX_DIR_ENTRIES);
        let one_entry = || DecodedExfatEntry::VolumeLabel {
            label_utf16: Vec::new(),
            label_utf8: None,
            offset: 0,
        };
        entries.extend((0..MAX_DIR_ENTRIES.saturating_sub(1)).map(|_| one_entry()));
        let overflow = vec![one_entry(), one_entry()];
        let reached_cap = extend_entries_with_cap(&mut entries, overflow, 2);
        assert!(reached_cap);
        assert_eq!(entries.len(), MAX_DIR_ENTRIES);
    }
}
