//! The **producer** half of the `scannerd → indexd` seam.
//!
//! This is the I/O + parse + normalize pipeline that was previously run
//! *in-process inside `indexd`*. It now lives in `scannerd` — the
//! least-privilege process that holds the read-only image fd — and emits
//! [`ScanBatch`] facts instead of touching a database. It does:
//!
//! ```text
//! reader → parse_mbr → (per exFAT partition) parse_boot_sector → Volume
//!        → walk_volume → StabilityTracker.observe
//!        → for each just-stable clip angle:
//!              front  → read bytes → walk_clip_waypoints → WireWaypoint
//!              other  → filename-epoch only
//!        → ScanBatch { present_keys, records, stats, complete }
//! ```
//!
//! It derives **nothing** about trips/events (that is `indexd`'s job,
//! `indexd.md` §1/§3); it only produces facts (`scannerd.md` §2.5). The
//! consumer (`indexd::apply`) maps these facts onto its DB writes.
//!
//! ## Parity with the legacy in-process pass
//!
//! The per-eligible-file loop mirrors the old `run_scan_pass` exactly,
//! including the corner where a clip has no resolvable recording instant
//! (no `mvhd` GPS time and an out-of-range filename timestamp): nothing is
//! emitted for it, but it is counted in [`ProducerStats::unplaceable_clips`]
//! so the consumer can reconstruct the legacy `clips_upserted` diagnostic.
//! Raw read/parse errors on a single clip are counted
//! ([`ProducerStats::read_errors`]) and skipped, never aborting the batch
//! — a structural error (MBR/boot/volume walk) still aborts the whole pass
//! exactly as before.

use std::collections::HashSet;
use std::time::SystemTime;

use crate::boot::parse_boot_sector;
use crate::clip::parse_clip_name;
use crate::error::ScannerError;
use crate::mbr::parse_mbr;
use crate::reader::BlockReader;
use crate::record::{
    AngleRecord, Bucket, ClipAngleRecord, PROTOCOL_VERSION, ProducerStats, ScanBatch, WireWaypoint,
    autopilot_to_u32, gear_to_u32,
};
use crate::seiwalk::{Waypoint, walk_clip_waypoints};
use crate::stability::StabilityTracker;
use crate::timestamp::epoch_from_tesla_timestamp;
use crate::volume::Volume;
use crate::walk::{FileRecord, walk_volume};

/// The Tesla front camera angle — the only one carrying the SEI telemetry
/// that trips/events derive from.
const FRONT_CAMERA: &str = "front";

/// Default SEI sample-rate decimation stride. Matches the v1 worker
/// (`worker.toml` `sample_rate = 30`) so the cached waypoint cadence — and
/// therefore the derived events — match production.
pub const DEFAULT_SEI_SAMPLE_RATE: u32 = 30;

/// Hard cap on bytes read for a single clip, to bound memory against a
/// corrupt `valid_data_length`. Tesla clips are tens of MiB; 256 MiB is a
/// generous ceiling.
const MAX_CLIP_BYTES: u64 = 256 * 1024 * 1024;

/// A clip angle's identity, parsed from its filename + path.
struct ClipIdent {
    /// Dedup key: `slot:<parent-dir>/<timestamp>` (camera-independent).
    key: String,
    /// The 19-char Tesla timestamp prefix (`YYYY-MM-DD_HH-MM-SS`).
    timestamp: String,
    /// The camera suffix (`front`, `back`, `left_repeater`, ...).
    camera: String,
    /// Source-folder classification from the directory path.
    bucket: Bucket,
}

impl ClipIdent {
    fn is_front(&self) -> bool {
        self.camera.eq_ignore_ascii_case(FRONT_CAMERA)
    }
}

/// Parse a walk record into a clip-angle identity, or `None` if it is not
/// a Tesla clip with a camera suffix.
fn clip_identity(record: &FileRecord) -> Option<ClipIdent> {
    let parsed = parse_clip_name(&record.name)?;
    let camera = parsed.camera?;
    let parent = record
        .path
        .rsplit_once('/')
        .map_or("", |(parent, _)| parent);
    let key = format!("{}:{}/{}", record.partition_slot, parent, parsed.timestamp);
    Some(ClipIdent {
        key,
        timestamp: parsed.timestamp,
        camera,
        bucket: Bucket::from_path(&record.path),
    })
}

/// `SystemTime` → positive epoch seconds, rejecting the zero/pre-epoch
/// sentinel (an `mvhd` `creation_time` of 0 means "unset").
fn systemtime_to_epoch(st: SystemTime) -> Option<i64> {
    st.duration_since(SystemTime::UNIX_EPOCH)
        .ok()
        .and_then(|d| i64::try_from(d.as_secs()).ok())
        .filter(|&s| s > 0)
}

/// Human-readable partition label for the clip's `partition` fact. The
/// camera-independent dedup key already encodes the slot.
fn partition_label(slot: u8) -> String {
    format!("slot{slot}")
}

/// Read a file's valid data region in full (bounded by [`MAX_CLIP_BYTES`]).
fn read_full_file<R: BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
    record: &FileRecord,
) -> Result<Vec<u8>, ScannerError> {
    let bpc = volume.params().bytes_per_cluster();
    let span = record.data_length.div_ceil(bpc.max(1)).max(1);
    let clusters = volume.follow_chain(record.first_cluster, record.no_fat_chain, span)?;
    let want = record
        .valid_data_length
        .min(record.data_length)
        .min(MAX_CLIP_BYTES);
    let len = usize::try_from(want).unwrap_or(usize::MAX);
    volume.read_file_range(&clusters, 0, len)
}

/// Build a [`WireWaypoint`] from a `scannerd` walk waypoint and the clip's
/// resolved start instant. `absolute_utc = clip_started_utc +
/// trunc(offset_ms/1000)` (truncation, matching the materializer).
///
/// This is the wire image of `indexd`'s `waypoint_from_walk`: the consumer
/// maps it back to its internal derive-waypoint 1:1, so the SEI telemetry
/// fed into derivation is byte-identical to the legacy in-process path.
#[must_use]
pub fn wire_waypoint_from_walk(walk: &Waypoint, clip_started_utc: i64) -> WireWaypoint {
    let msg = &walk.message;
    #[allow(clippy::cast_possible_truncation)]
    let secs = (walk.timestamp_ms / 1000.0) as i64;
    WireWaypoint {
        frame_index: i64::from(walk.frame_index),
        offset_ms: walk.timestamp_ms,
        absolute_utc: Some(clip_started_utc + secs),
        lat: msg.latitude_deg,
        lon: msg.longitude_deg,
        speed: f64::from(msg.vehicle_speed_mps),
        heading: msg.heading_deg,
        accel_x: Some(msg.linear_acceleration_mps2_x),
        accel_y: Some(msg.linear_acceleration_mps2_y),
        accel_z: Some(msg.linear_acceleration_mps2_z),
        autopilot_state: autopilot_to_u32(msg.autopilot_state),
        gear: gear_to_u32(msg.gear_state),
        has_gps_fix: msg.has_gps_fix(),
    }
}

/// Build the [`AngleRecord`] facts for a record + identity. `view_kind` is
/// intentionally **not** carried — the consumer recomputes it from the
/// bucket so it cannot be forged independently.
fn angle_record(record: &FileRecord, ident: &ClipIdent) -> AngleRecord {
    AngleRecord {
        camera: ident.camera.clone(),
        file_ref: record.path.clone(),
        offset_ms: 0,
        duration_s: None,
        size_bytes: i64::try_from(record.valid_data_length).ok(),
    }
}

/// Shape a front clip into a record: read its bytes, walk its SEI, resolve
/// the recording instant (`mvhd`/GPS first, Tesla-filename fallback), and
/// emit the clip + front angle + (possibly empty) waypoint stream.
///
/// Returns `Ok(None)` when the clip has no resolvable recording instant at
/// all (cannot be placed in time) — mirroring the legacy `process_front`
/// early return. Read failures surface as `Err` (counted, not fatal).
fn shape_front<R: BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
    record: &FileRecord,
    ident: &ClipIdent,
    sample_rate: u32,
) -> Result<Option<ClipAngleRecord>, ScannerError> {
    let bytes = read_full_file(volume, record)?;
    // A truncated/garbled SEI stream yields `None`; the clip/angle is still
    // recorded (with an empty waypoint stream, which clears a stale cache).
    let walk = walk_clip_waypoints(&bytes, sample_rate).ok();

    let started_at = walk
        .as_ref()
        .and_then(|w| w.clip_started_utc.and_then(systemtime_to_epoch))
        .or_else(|| epoch_from_tesla_timestamp(&ident.timestamp));
    let Some(started_at) = started_at else {
        return Ok(None);
    };

    let waypoints: Vec<WireWaypoint> = walk.as_ref().map_or_else(Vec::new, |w| {
        w.waypoints
            .iter()
            .map(|wp| wire_waypoint_from_walk(wp, started_at))
            .collect()
    });

    let duration_s = walk
        .as_ref()
        .and_then(|w| w.waypoints.last())
        .map(|wp| wp.timestamp_ms / 1000.0);
    #[allow(clippy::cast_possible_truncation)]
    let ended_at = duration_s.map(|secs| started_at + secs.round() as i64);

    Ok(Some(ClipAngleRecord {
        canonical_key: ident.key.clone(),
        started_at,
        ended_at,
        partition: partition_label(record.partition_slot),
        bucket: ident.bucket,
        duration_s,
        angle: angle_record(record, ident),
        waypoints,
    }))
}

/// Shape a non-front angle: anchor it to the filename-derived instant (no
/// SEI walk). Returns `Ok(None)` when the filename timestamp is out of
/// range (mirroring the legacy `process_other` early return).
///
/// Kept fallible (and symmetric with [`shape_front`]) so the producer loop
/// handles both arms uniformly and a future content-hash/probe step can
/// add a real failure mode without reshaping the caller.
#[allow(clippy::unnecessary_wraps)]
fn shape_other(
    record: &FileRecord,
    ident: &ClipIdent,
) -> Result<Option<ClipAngleRecord>, ScannerError> {
    let Some(started_at) = epoch_from_tesla_timestamp(&ident.timestamp) else {
        return Ok(None);
    };
    Ok(Some(ClipAngleRecord {
        canonical_key: ident.key.clone(),
        started_at,
        ended_at: None,
        partition: partition_label(record.partition_slot),
        bucket: ident.bucket,
        duration_s: None,
        angle: angle_record(record, ident),
        waypoints: Vec::new(),
    }))
}

/// Run one **produce** pass: parse + walk + stability-gate the raw image
/// and build a [`ScanBatch`] of facts for the consumer.
///
/// `tracker` persists across passes (the stability gate needs repeated
/// observations on the same tracker). `now_secs` is the wall-clock time
/// used for the quiescence window. `generation` is left `0`; the serving
/// daemon stamps it from the consumer's request.
///
/// # Errors
///
/// Returns [`ScannerError`] if a structural raw-media step fails
/// (`parse_mbr`, a boot sector, or a volume walk) — exactly the cases that
/// aborted the legacy in-process pass. Individual unreadable clips are
/// skipped (counted in [`ProducerStats::read_errors`]), never fatal.
pub fn produce<R: BlockReader + ?Sized>(
    reader: &R,
    tracker: &mut StabilityTracker,
    now_secs: u64,
    sample_rate: u32,
) -> Result<ScanBatch, ScannerError> {
    let mut stats = ProducerStats::default();

    let mbr = parse_mbr(reader)?;
    let mut volumes: Vec<(u8, Volume<'_, R>)> = Vec::new();
    let mut all_records: Vec<FileRecord> = Vec::new();
    for entry in &mbr {
        if !entry.is_exfat() {
            continue;
        }
        let params = parse_boot_sector(reader, entry.start_lba)?;
        let volume = Volume::new(reader, params);
        let records = walk_volume(&volume, entry.slot)?;
        all_records.extend(records);
        volumes.push((entry.slot, volume));
    }
    stats.partitions = volumes.len();
    stats.files_seen = all_records.len();

    let eligible = tracker.observe(&all_records, now_secs);
    stats.eligible = eligible.len();

    // Present set = every clip currently on the media, regardless of
    // stability (an in-flux clip must NOT be pruned by the consumer).
    let mut present: HashSet<String> = HashSet::new();
    for record in &all_records {
        if let Some(ident) = clip_identity(record) {
            present.insert(ident.key);
        }
    }

    let mut records: Vec<ClipAngleRecord> = Vec::new();
    for &idx in &eligible {
        let Some(record) = all_records.get(idx) else {
            continue;
        };
        let Some(ident) = clip_identity(record) else {
            continue;
        };
        if ident.is_front() {
            let Some(volume) = volumes
                .iter()
                .find(|(slot, _)| *slot == record.partition_slot)
                .map(|(_, volume)| volume)
            else {
                continue;
            };
            match shape_front(volume, record, &ident, sample_rate) {
                Ok(Some(rec)) => records.push(rec),
                Ok(None) => {
                    stats.unplaceable_clips += 1;
                    stats.unplaceable_front += 1;
                }
                Err(_) => stats.read_errors += 1,
            }
        } else {
            match shape_other(record, &ident) {
                Ok(Some(rec)) => records.push(rec),
                Ok(None) => stats.unplaceable_clips += 1,
                Err(_) => stats.read_errors += 1,
            }
        }
    }

    Ok(ScanBatch {
        version: PROTOCOL_VERSION,
        generation: 0,
        // A structural failure already returned `Err` above, so a returned
        // batch always has a trustworthy present set. The flag is the
        // consumer's prune-safety gate (and the hook for a future
        // partial-walk mode).
        complete: true,
        stats,
        present_keys: present.into_iter().collect(),
        records,
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::{ClipIdent, clip_identity, partition_label};
    use crate::record::Bucket;
    use crate::walk::FileRecord;
    use teslausb_core::fs::exfat::directory::FileTimestamps;

    fn record(path: &str, name: &str, slot: u8) -> FileRecord {
        FileRecord {
            partition_slot: slot,
            path: path.to_owned(),
            name: name.to_owned(),
            first_cluster: 5,
            data_length: 1024,
            valid_data_length: 1024,
            no_fat_chain: true,
            timestamps: FileTimestamps::default(),
            set_checksum_ok: true,
            dir_first_cluster: 4,
        }
    }

    fn ident(rec: &FileRecord) -> ClipIdent {
        clip_identity(rec).expect("clip parses")
    }

    #[test]
    fn identity_parses_front_clip() {
        let rec = record(
            "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-front.mp4",
            "2026-06-01_20-10-04-front.mp4",
            0,
        );
        let id = ident(&rec);
        assert!(id.is_front());
        assert_eq!(id.timestamp, "2026-06-01_20-10-04");
        assert_eq!(id.bucket, Bucket::SavedClips);
        assert_eq!(
            id.key,
            "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04"
        );
    }

    #[test]
    fn all_angles_share_one_canonical_key() {
        let dir = "TeslaCam/SentryClips/2026-06-01_20-10-04";
        let front = record(
            &format!("{dir}/2026-06-01_20-10-04-front.mp4"),
            "2026-06-01_20-10-04-front.mp4",
            1,
        );
        let back = record(
            &format!("{dir}/2026-06-01_20-10-04-back.mp4"),
            "2026-06-01_20-10-04-back.mp4",
            1,
        );
        let left = record(
            &format!("{dir}/2026-06-01_20-10-04-left_repeater.mp4"),
            "2026-06-01_20-10-04-left_repeater.mp4",
            1,
        );
        let fk = ident(&front);
        let bk = ident(&back);
        let lk = ident(&left);
        assert_eq!(fk.key, bk.key);
        assert_eq!(fk.key, lk.key);
        assert!(fk.is_front());
        assert!(!bk.is_front());
        assert!(!lk.is_front());
        assert_eq!(fk.bucket, Bucket::SentryClips);
    }

    #[test]
    fn non_clip_files_are_ignored() {
        let rec = record("TeslaCam/event.json", "event.json", 0);
        assert!(clip_identity(&rec).is_none());
    }

    #[test]
    fn partition_label_encodes_slot() {
        assert_eq!(partition_label(3), "slot3");
    }
}
