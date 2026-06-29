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
use crate::clip_event::parse_event_json;
use crate::error::ScannerError;
use crate::mbr::parse_mbr;
use crate::reader::BlockReader;
use crate::record::{
    autopilot_to_u32, gear_to_u32, AngleRecord, Bucket, ClipAngleRecord, ClipEventRecord,
    MediaFileRecord, ProducerStats, ScanBatch, WireWaypoint, MAX_CLIP_EVENT_RECORDS,
    MAX_MEDIA_RECORDS, PROTOCOL_VERSION,
};
use crate::seiwalk::{walk_clip_waypoints, Waypoint};
use crate::stability::StabilityTracker;
use crate::timestamp::epoch_from_tesla_timestamp;
use crate::volume::Volume;
use crate::walk::{walk_volume, FileRecord};

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

/// Maximum expensive front-clip SEI walks performed in a single produce
/// pass. Front clips are the only records that read+parse clip bytes (tens
/// of MiB each); capping them per batch bounds the per-request work so a
/// single response always returns well under the consumer's read timeout,
/// even when a large backlog (e.g. a long drive just recorded) becomes
/// eligible all at once. The remaining eligible clips are deferred to the
/// next pass (`complete=false`), and the consumer drains the backlog over
/// several fast batches. Non-front angles are cheap (filename only) and are
/// not capped.
const MAX_FRONT_SHAPES_PER_BATCH: usize = 8;

/// Tesla event sidecar filename.
const EVENT_JSON_NAME: &str = "event.json";

/// Hard cap on `event.json` bytes read per sidecar.
const MAX_EVENT_JSON_BYTES: u64 = 64 * 1024;

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

/// MBR slot of the MEDIA (p2) partition — the second exFAT partition, which
/// holds the operator-installed lock chime (and, later, boombox/music/
/// lightshows). Slot 0 is the dashcam (p1) partition.
const MEDIA_PARTITION_SLOT: u8 = 1;

/// The lock chime at the p2 root (exact path, no directory prefix).
const LOCK_CHIME_REL_PATH: &str = "LockChime.wav";

/// Decode an exFAT packed DOS date-time (`modify_timestamp`) into a NAIVE
/// local `YYYY-MM-DDThh:mm:ss` string. Returns `None` when any field is out
/// of range (e.g. the all-zero sentinel), so a corrupt entry degrades to
/// "no timestamp" rather than a bogus date.
///
/// Layout (mirrors [`FileTimestamps::from_system_time`] packing):
/// `packed = (date << 16) | time`, with
/// `date = ((year-1980) << 9) | (month << 5) | day` and
/// `time = (hour << 11) | (minute << 5) | (second/2)`.
fn decode_exfat_timestamp(packed: u32) -> Option<String> {
    let date = packed >> 16;
    let time = packed & 0xFFFF;
    let year = ((date >> 9) & 0x7F) + 1980;
    let month = (date >> 5) & 0x0F;
    let day = date & 0x1F;
    let hour = (time >> 11) & 0x1F;
    let minute = (time >> 5) & 0x3F;
    let second = (time & 0x1F) * 2;
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 59
    {
        return None;
    }
    Some(format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}"
    ))
}

/// Return `true` when a p2 `rel_path` belongs to one of the toybox media
/// categories the producer inventories. Evaluated on the walk-level path
/// (partition-root-relative), never on a filesystem path.
///
/// Categories:
/// * Lock chime — root-level `LockChime.wav` (exact match).
/// * Chimes — any file under `Chimes/` (library uploads, visible on the media drive).
/// * Boombox — any file under `Boombox/` (Tesla loads the first 5
///   alphabetically; subdirectories are allowed by the producer but Tesla
///   only plays root-level names in practice).
/// * Music — any file under `Music/` (supports artist/album subdirectories).
/// * `LightShow` — any file under `LightShow/`.
/// * `LicensePlate` — any file under `LicensePlate/`.
/// * Wraps — any file under the root-level `Wraps/` folder.
///
/// Wraps live in their own root-level `Wraps/` folder (the layout Tesla's
/// Paint Shop reads), so they never overlap the `LightShow/` subtree; the
/// LightShow/Wraps disambiguation in `webd`'s query layer is a simple
/// `rel_path LIKE 'Wraps/%'` vs `rel_path LIKE 'LightShow/%'` split.
fn is_toybox_path(path: &str) -> bool {
    path == LOCK_CHIME_REL_PATH
        || path.starts_with("Chimes/")
        || path.starts_with("Boombox/")
        || path.starts_with("Music/")
        || path.starts_with("LightShow/")
        || path.starts_with("LicensePlate/")
        || path.starts_with("Wraps/")
}

/// Collect the MEDIA-partition (p2) inventory facts from the full walk.
///
/// Includes:
/// * `LockChime.wav` at the partition root (exact match).
/// * All files under `Chimes/`, `Boombox/`, `Music/`, `LightShow/`,
///   `LicensePlate/`, and the root-level `Wraps/` folder.
///
/// Only "complete" entries are collected: both the exFAT set-checksum must
/// pass AND `valid_data_length == data_length` must hold — a mid-install
/// torn entry (which fails the checksum the `gadgetd` temp+atomic-rename
/// install guards against) is excluded so the consumer never records a
/// half-written file.
fn collect_media(all_records: &[FileRecord]) -> Vec<MediaFileRecord> {
    let mut media: Vec<MediaFileRecord> = Vec::new();
    for record in all_records {
        if record.partition_slot != MEDIA_PARTITION_SLOT {
            continue;
        }
        if !is_toybox_path(&record.path) {
            continue;
        }
        if !record.set_checksum_ok || record.valid_data_length != record.data_length {
            continue;
        }
        media.push(MediaFileRecord {
            partition: partition_label(record.partition_slot),
            rel_path: record.path.clone(),
            name: record.name.clone(),
            size_bytes: i64::try_from(record.data_length).unwrap_or(i64::MAX),
            modified_local: decode_exfat_timestamp(record.timestamps.modify_timestamp),
        });
        if media.len() >= MAX_MEDIA_RECORDS {
            break;
        }
    }
    media
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

/// Read a file's valid data region bounded by `max_bytes` — used for small
/// sidecars like `event.json`, never the 256 MiB clip path.
fn read_bounded_file<R: BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
    record: &FileRecord,
    max_bytes: u64,
) -> Result<Vec<u8>, ScannerError> {
    let bpc = volume.params().bytes_per_cluster();
    let span = record.data_length.div_ceil(bpc.max(1)).max(1);
    let clusters = volume.follow_chain(record.first_cluster, record.no_fat_chain, span)?;
    let want = record
        .valid_data_length
        .min(record.data_length)
        .min(max_bytes);
    let len = usize::try_from(want).unwrap_or(usize::MAX);
    volume.read_file_range(&clusters, 0, len)
}

/// Collect parsed clip-event sidecars (`event.json`) from SavedClips/SentryClips.
fn collect_clip_events<R: BlockReader + ?Sized>(
    all_records: &[FileRecord],
    volumes: &[(u8, Volume<'_, R>)],
) -> Vec<ClipEventRecord> {
    let mut result: Vec<ClipEventRecord> = Vec::new();

    for record in all_records {
        if !record.name.eq_ignore_ascii_case(EVENT_JSON_NAME) {
            continue;
        }

        let bucket = Bucket::from_path(&record.path);
        if !matches!(bucket, Bucket::SavedClips | Bucket::SentryClips) {
            continue;
        }

        let parent = record.path.rsplit_once('/').map_or("", |(p, _)| p);
        let event_dir_key = format!("{}:{parent}", record.partition_slot);

        let mut primary: Option<ClipIdent> = None;
        for candidate in all_records {
            if candidate.partition_slot != record.partition_slot {
                continue;
            }
            let candidate_parent = candidate.path.rsplit_once('/').map_or("", |(p, _)| p);
            if candidate_parent != parent {
                continue;
            }
            let Some(ident) = clip_identity(candidate) else {
                continue;
            };
            let should_replace = match primary.as_ref() {
                Some(current) => ident.timestamp > current.timestamp,
                None => true,
            };
            if should_replace {
                primary = Some(ident);
            }
        }
        let primary_canonical_key = primary.map_or_else(String::new, |ident| ident.key);

        let Some(volume) = volumes
            .iter()
            .find(|(slot, _)| *slot == record.partition_slot)
            .map(|(_, volume)| volume)
        else {
            continue;
        };

        let Ok(bytes) = read_bounded_file(volume, record, MAX_EVENT_JSON_BYTES) else {
            continue;
        };
        let Ok(meta) = parse_event_json(&bytes) else {
            continue;
        };

        result.push(ClipEventRecord {
            event_dir_key,
            bucket,
            primary_canonical_key,
            timestamp_utc: meta.timestamp_utc,
            timestamp_local_naive: meta.timestamp_local_naive,
            timestamp_has_offset: meta.timestamp_has_offset,
            est_lat: meta.est_lat,
            est_lon: meta.est_lon,
            reason: meta.reason,
            city: meta.city,
            camera: meta.camera,
        });

        if result.len() >= MAX_CLIP_EVENT_RECORDS {
            break;
        }
    }

    result
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

/// One read-only backing image fed to [`produce`], with an optional
/// logical-slot override.
///
/// In the two-image layout each image is **single-partition** (one exFAT
/// volume at MBR slot 0): `teslacam.img` holds the dashcam partition and
/// `media.img` holds the MEDIA partition. The downstream classification
/// keys on the partition slot ([`MEDIA_PARTITION_SLOT`] = 1 marks media vs
/// dashcam), so the caller stamps a **logical** slot per image — the
/// `TeslaCam` image overrides to `0`, the media image to `1` — making the
/// two single-partition images behave exactly like the legacy combined
/// `disk.img` (MBR p1 dashcam + p2 MEDIA) for every consumer downstream.
///
/// When `slot_override` is `None` the partition's own MBR slot is used,
/// preserving the legacy single combined-image path (diagnostic modes and
/// any pre-migration `disk.img`).
///
/// Each image must contribute a **unique** logical slot (the volume lookup
/// in [`produce`] resolves front-clip reads by slot); a single image is
/// expected to carry exactly one exFAT partition when `slot_override` is
/// set.
pub struct ImageSource<'a, R: BlockReader + ?Sized> {
    /// The read-only reader for this image.
    pub reader: &'a R,
    /// Logical partition slot stamped on every record from this image, or
    /// `None` to use the partition's own MBR slot.
    pub slot_override: Option<u8>,
}

impl<'a, R: BlockReader + ?Sized> ImageSource<'a, R> {
    /// A source that keeps each partition's native MBR slot (legacy
    /// single combined `disk.img`).
    #[must_use]
    pub fn native(reader: &'a R) -> Self {
        Self {
            reader,
            slot_override: None,
        }
    }

    /// A source whose single partition is stamped with `slot` (one image
    /// per LUN in the two-image layout).
    #[must_use]
    pub fn with_slot(reader: &'a R, slot: u8) -> Self {
        Self {
            reader,
            slot_override: Some(slot),
        }
    }
}

/// Run one **produce** pass: parse + walk + stability-gate every backing
/// image and build a single [`ScanBatch`] of facts for the consumer.
///
/// Each [`ImageSource`] is walked in order and its records merged into one
/// batch, so the two single-partition images (`teslacam.img` + `media.img`)
/// produce the same combined catalog the legacy single `disk.img` did.
///
/// `tracker` persists across passes (the stability gate needs repeated
/// observations on the same tracker). `now_secs` is the wall-clock time
/// used for the quiescence window. `generation` is left `0`; the serving
/// daemon stamps it from the consumer's request.
///
/// # Errors
///
/// Returns [`ScannerError`] if a structural raw-media step fails
/// (`parse_mbr`, a boot sector, or a volume walk) on **any** source —
/// exactly the cases that aborted the legacy in-process pass. Individual
/// unreadable clips are skipped (counted in
/// [`ProducerStats::read_errors`]), never fatal.
pub fn produce<R: BlockReader + ?Sized>(
    sources: &[ImageSource<'_, R>],
    tracker: &mut StabilityTracker,
    now_secs: u64,
    sample_rate: u32,
) -> Result<ScanBatch, ScannerError> {
    let mut stats = ProducerStats::default();

    let mut volumes: Vec<(u8, Volume<'_, R>)> = Vec::new();
    let mut all_records: Vec<FileRecord> = Vec::new();
    for source in sources {
        let mbr = parse_mbr(source.reader)?;
        for entry in &mbr {
            if !entry.is_exfat() {
                continue;
            }
            let params = parse_boot_sector(source.reader, entry.start_lba)?;
            let volume = Volume::new(source.reader, params);
            let slot = source.slot_override.unwrap_or(entry.slot);
            let records = walk_volume(&volume, slot)?;
            all_records.extend(records);
            volumes.push((slot, volume));
        }
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
    let mut front_shaped: usize = 0;
    let mut deferred = false;
    for &idx in &eligible {
        let Some(record) = all_records.get(idx) else {
            continue;
        };
        let Some(ident) = clip_identity(record) else {
            continue;
        };
        if ident.is_front() {
            // Front clips are the only expensive records (they read + parse
            // tens of MiB of clip bytes). Once the per-pass front budget is
            // spent, defer the remaining front clips to the next pass so this
            // response stays small and returns well under the consumer's read
            // timeout; unmark_emitted re-offers them. Cheap non-front angles
            // and non-clip sidecars are never deferred, so `complete=false`
            // strictly means expensive front work still remains.
            if front_shaped >= MAX_FRONT_SHAPES_PER_BATCH {
                tracker.unmark_emitted(record);
                deferred = true;
                continue;
            }
            let Some(volume) = volumes
                .iter()
                .find(|(slot, _)| *slot == record.partition_slot)
                .map(|(_, volume)| volume)
            else {
                continue;
            };
            front_shaped += 1;
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

    let media = collect_media(&all_records);
    let clip_events = collect_clip_events(&all_records, &volumes);

    Ok(ScanBatch {
        version: PROTOCOL_VERSION,
        generation: 0,
        // A structural failure already returned `Err` above, so a returned
        // batch always has a trustworthy present set. The flag is the
        // consumer's prune-safety gate; `complete=false` means this pass
        // deferred additional eligible clips to keep per-request work bounded.
        complete: !deferred,
        stats,
        present_keys: present.into_iter().collect(),
        records,
        // This producer DID inventory p2, so the consumer may prune stale
        // media rows against `media_present_paths` (gated on `complete`).
        media_present_paths: media.iter().map(|m| m.rel_path.clone()).collect(),
        media,
        media_inventory: true,
        // This producer DID inventory `event.json`, so the consumer may prune
        // stale sidecar rows against `clip_events` (gated on `complete`).
        clip_events,
        clip_events_inventory: true,
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::indexing_slicing)]

    use std::collections::BTreeSet;

    use super::{
        clip_identity, collect_media, decode_exfat_timestamp, is_toybox_path, partition_label,
        ClipIdent, MEDIA_PARTITION_SLOT,
    };
    use crate::record::Bucket;
    use crate::walk::FileRecord;
    use teslausb_core::fs::exfat::directory::{
        encode_file_entry_set, FileAttributes, FileEntrySetParams, FileTimestamps,
    };
    use teslausb_core::fs::exfat::upcase_table::UpcaseTable;

    fn record(path: &str, name: &str, slot: u8) -> FileRecord {
        FileRecord {
            partition_slot: slot,
            path: path.to_owned(),
            name: name.to_owned(),
            name_hash: 0,
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

    /// Pack a date-time the way `FileTimestamps::from_system_time` does, so
    /// the decoder is tested against the real on-disk layout.
    fn pack(year: u32, month: u32, day: u32, hour: u32, minute: u32, second: u32) -> u32 {
        let date = ((year - 1980) << 9) | (month << 5) | day;
        let time = (hour << 11) | (minute << 5) | (second / 2);
        (date << 16) | time
    }

    #[test]
    fn decode_timestamp_roundtrips_packed_value() {
        let packed = pack(2026, 6, 1, 20, 10, 4);
        assert_eq!(
            decode_exfat_timestamp(packed).as_deref(),
            Some("2026-06-01T20:10:04")
        );
    }

    #[test]
    fn decode_timestamp_rejects_zero_sentinel() {
        // All-zero packs month=0/day=0 → out of range → None, not a bogus date.
        assert_eq!(decode_exfat_timestamp(0), None);
    }

    fn media_record(path: &str, complete: bool, slot: u8) -> FileRecord {
        let mut rec = record(path, path, slot);
        rec.timestamps = FileTimestamps {
            modify_timestamp: pack(2026, 6, 1, 20, 10, 4),
            ..FileTimestamps::default()
        };
        if !complete {
            rec.valid_data_length = rec.data_length - 1;
        }
        rec
    }

    #[test]
    fn collect_media_finds_complete_lock_chime_on_p2() {
        let recs = vec![media_record("LockChime.wav", true, MEDIA_PARTITION_SLOT)];
        let media = collect_media(&recs);
        assert_eq!(media.len(), 1);
        assert_eq!(media[0].partition, "slot1");
        assert_eq!(media[0].rel_path, "LockChime.wav");
        assert_eq!(media[0].size_bytes, 1024);
        assert_eq!(
            media[0].modified_local.as_deref(),
            Some("2026-06-01T20:10:04")
        );
    }

    #[test]
    fn collect_media_skips_torn_chime() {
        // valid_data_length < data_length ⇒ mid-install ⇒ excluded.
        let recs = vec![media_record("LockChime.wav", false, MEDIA_PARTITION_SLOT)];
        assert!(collect_media(&recs).is_empty());
    }

    #[test]
    fn collect_media_skips_bad_checksum() {
        let mut rec = media_record("LockChime.wav", true, MEDIA_PARTITION_SLOT);
        rec.set_checksum_ok = false;
        assert!(collect_media(&[rec]).is_empty());
    }

    #[test]
    fn collect_media_ignores_p1_and_unknown_paths() {
        let recs = vec![
            // Right name but dashcam partition (slot 0) → ignored.
            media_record("LockChime.wav", true, 0),
            // Media partition but no known category prefix → ignored.
            media_record("Other.wav", true, MEDIA_PARTITION_SLOT),
            media_record("UnknownDir/file.wav", true, MEDIA_PARTITION_SLOT),
        ];
        assert!(collect_media(&recs).is_empty());
    }

    #[test]
    fn collect_media_finds_boombox_file_on_p2() {
        let recs = vec![media_record("Boombox/horn.wav", true, MEDIA_PARTITION_SLOT)];
        let media = collect_media(&recs);
        assert_eq!(media.len(), 1);
        assert_eq!(media[0].rel_path, "Boombox/horn.wav");
        assert_eq!(media[0].name, "Boombox/horn.wav");
        assert_eq!(media[0].partition, "slot1");
    }

    #[test]
    fn collect_media_finds_music_file_on_p2() {
        let recs = vec![
            media_record("Music/song.mp3", true, MEDIA_PARTITION_SLOT),
            media_record("Music/Artist/Album/track.flac", true, MEDIA_PARTITION_SLOT),
        ];
        let media = collect_media(&recs);
        assert_eq!(media.len(), 2);
    }

    #[test]
    fn collect_media_finds_lightshow_and_root_wraps() {
        // LightShow files and root-level Wraps files are both emitted; the
        // LightShow/Wraps disambiguation is done in webd's query layer.
        let recs = vec![
            media_record("LightShow/show.fseq", true, MEDIA_PARTITION_SLOT),
            media_record("Wraps/mywrap.png", true, MEDIA_PARTITION_SLOT),
        ];
        let media = collect_media(&recs);
        assert_eq!(media.len(), 2);
        assert!(media.iter().any(|m| m.rel_path == "LightShow/show.fseq"));
        assert!(media.iter().any(|m| m.rel_path == "Wraps/mywrap.png"));
    }

    #[test]
    fn collect_media_finds_license_plate_on_p2() {
        let recs = vec![media_record(
            "LicensePlate/myplate.png",
            true,
            MEDIA_PARTITION_SLOT,
        )];
        let media = collect_media(&recs);
        assert_eq!(media.len(), 1);
        assert_eq!(media[0].rel_path, "LicensePlate/myplate.png");
    }

    #[test]
    fn collect_media_skips_torn_category_file() {
        let recs = vec![media_record(
            "Boombox/horn.wav",
            false,
            MEDIA_PARTITION_SLOT,
        )];
        assert!(collect_media(&recs).is_empty());
    }

    // ── is_toybox_path unit tests ──────────────────────────────────────────

    #[test]
    fn is_toybox_path_accepts_lock_chime() {
        assert!(is_toybox_path("LockChime.wav"));
    }

    #[test]
    fn is_toybox_path_accepts_all_categories() {
        assert!(is_toybox_path("Boombox/foo.wav"));
        assert!(is_toybox_path("Music/song.mp3"));
        assert!(is_toybox_path("Music/Artist/Album/track.flac"));
        assert!(is_toybox_path("LightShow/show.fseq"));
        assert!(is_toybox_path("Wraps/mywrap.png"));
        assert!(is_toybox_path("LicensePlate/myplate.png"));
    }

    #[test]
    fn is_toybox_path_rejects_unknown_paths() {
        assert!(!is_toybox_path("Other.wav"));
        assert!(!is_toybox_path("UnknownDir/file.wav"));
        assert!(!is_toybox_path(""));
        assert!(!is_toybox_path("BoomboxExtra/file.wav")); // prefix-but-not-dir
    }

    #[test]
    fn is_toybox_path_rejects_p1_files() {
        // Slot filtering is done in collect_media, not here — but path-level
        // the function should not accept TeslaCam paths.
        assert!(!is_toybox_path("TeslaCam/SavedClips/2026-01-01/clip.mp4"));
    }

    // ---- two-image / two-LUN produce path -------------------------------

    use super::{produce, ImageSource, DEFAULT_SEI_SAMPLE_RATE, MAX_FRONT_SHAPES_PER_BATCH};
    use crate::reader::SliceReader;
    use crate::stability::{StabilityConfig, StabilityTracker};

    const START_LBA: u32 = 1;
    const CLUSTER_SIZE: usize = 512;
    const ROOT_CLUSTER: u32 = 2;
    const TESLACAM_CLUSTER: u32 = 3;
    const BUCKET_CLUSTER: u32 = 4;
    const EVENT_DIR_CLUSTER: u32 = 5;
    const FRONT_FILE_CLUSTER: u32 = 6;
    const EVENT_JSON_CLUSTER: u32 = 7;
    const EVENT_TIMESTAMP: &str = "2026-06-01_20-10-35";
    const FRONT_FILE_NAME: &str = "2026-06-01_20-10-35-front.mp4";

    /// Build a minimal but structurally valid single-partition exFAT image
    /// whose root directory is empty (the walk yields zero files). This is
    /// the in-memory analogue of one of the two-LUN backing images
    /// (`teslacam.img` / `media.img`): MBR with one `0x07` partition + a
    /// valid boot sector + a one-cluster, end-of-directory root.
    fn empty_single_partition_image() -> Vec<u8> {
        // 512-byte sectors, 512-byte clusters, one FAT.
        let mut img = vec![0_u8; 4096];

        // --- MBR (sector 0): one exFAT partition at START_LBA. ---
        let e1 = 446;
        img[e1 + 4] = 0x07;
        img[e1 + 8..e1 + 12].copy_from_slice(&START_LBA.to_le_bytes());
        img[e1 + 12..e1 + 16].copy_from_slice(&15_u32.to_le_bytes());
        img[510] = 0x55;
        img[511] = 0xAA;

        // --- Boot sector at START_LBA * 512 = 512. ---
        let bs = (START_LBA as usize) * 512;
        img[bs..bs + 3].copy_from_slice(&[0xEB, 0x76, 0x90]);
        img[bs + 3..bs + 11].copy_from_slice(b"EXFAT   ");
        img[bs + 64..bs + 72].copy_from_slice(&u64::from(START_LBA).to_le_bytes()); // partition_offset
        img[bs + 72..bs + 80].copy_from_slice(&16_u64.to_le_bytes()); // volume_length
        img[bs + 80..bs + 84].copy_from_slice(&1_u32.to_le_bytes()); // fat_offset (rel)
        img[bs + 84..bs + 88].copy_from_slice(&1_u32.to_le_bytes()); // fat_length
        img[bs + 88..bs + 92].copy_from_slice(&2_u32.to_le_bytes()); // cluster_heap_offset (rel)
        img[bs + 92..bs + 96].copy_from_slice(&4_u32.to_le_bytes()); // cluster_count
        img[bs + 96..bs + 100].copy_from_slice(&2_u32.to_le_bytes()); // first_root_cluster
        img[bs + 100..bs + 104].copy_from_slice(&0xDEAD_BEEF_u32.to_le_bytes()); // serial
        img[bs + 108] = 9; // bytes_per_sector_shift (512)
        img[bs + 109] = 0; // sectors_per_cluster_shift (512-byte clusters)
        img[bs + 110] = 1; // number_of_fats
        img[bs + 510] = 0x55;
        img[bs + 511] = 0xAA;

        // --- FAT (abs (fat_offset + partition_offset) * 512 = 1024): root
        // cluster 2 is end-of-chain. ---
        let fat_base = (1 + START_LBA as usize) * 512;
        let entry2 = fat_base + (2 * 4);
        img[entry2..entry2 + 4].copy_from_slice(&0xFFFF_FFFF_u32.to_le_bytes());

        // Heap cluster 2 (abs (cluster_heap_offset + partition_offset) * 512
        // = 1536) is left all-zero ⇒ immediate end-of-directory ⇒ no files.
        img
    }

    fn event_dir_name() -> String {
        EVENT_TIMESTAMP.to_owned()
    }

    fn clip_canonical_key(bucket_dir: &str) -> String {
        format!("0:TeslaCam/{bucket_dir}/{EVENT_TIMESTAMP}/{EVENT_TIMESTAMP}")
    }

    fn event_dir_key(bucket_dir: &str) -> String {
        format!("0:TeslaCam/{bucket_dir}/{EVENT_TIMESTAMP}")
    }

    fn front_clip_path(bucket_dir: &str) -> String {
        format!("TeslaCam/{bucket_dir}/{EVENT_TIMESTAMP}/{FRONT_FILE_NAME}")
    }

    fn encode_entry_set(
        name: &str,
        is_directory: bool,
        first_cluster: u32,
        valid_data_length: u64,
        data_length: u64,
        no_fat_chain: bool,
        upcase: &UpcaseTable,
    ) -> Vec<u8> {
        let name_utf16: Vec<u16> = name.encode_utf16().collect();
        let attributes = FileAttributes {
            directory: is_directory,
            archive: !is_directory,
            ..FileAttributes::default()
        };
        encode_file_entry_set(
            &FileEntrySetParams {
                name: &name_utf16,
                attributes,
                timestamps: FileTimestamps::default(),
                first_cluster,
                valid_data_length,
                data_length,
                no_fat_chain,
            },
            upcase,
        )
        .expect("encode entry set")
    }

    fn directory_cluster(entries: &[Vec<u8>], cluster_size: usize) -> Vec<u8> {
        let mut cluster = vec![0_u8; cluster_size];
        let mut offset = 0;
        for entry in entries {
            cluster[offset..offset + entry.len()].copy_from_slice(entry);
            offset += entry.len();
        }
        cluster
    }

    fn write_cluster(img: &mut [u8], start_lba: u32, cluster: u32, payload: &[u8]) {
        let base =
            usize::try_from(cluster_offset(start_lba, cluster)).expect("cluster offset usize");
        img[base..base + payload.len()].copy_from_slice(payload);
    }

    fn cluster_offset(start_lba: u32, cluster: u32) -> u64 {
        let cluster_index = u64::from(cluster.saturating_sub(2));
        (u64::from(start_lba + 2) * CLUSTER_SIZE as u64) + (cluster_index * CLUSTER_SIZE as u64)
    }

    fn event_fixture_image(bucket_dir: &str, event_json: &[u8]) -> Vec<u8> {
        let mut img = vec![0_u8; 8192];

        let mbr = 446;
        img[mbr + 4] = 0x07;
        img[mbr + 8..mbr + 12].copy_from_slice(&START_LBA.to_le_bytes());
        img[mbr + 12..mbr + 16].copy_from_slice(&31_u32.to_le_bytes());
        img[510] = 0x55;
        img[511] = 0xAA;

        let bs = (START_LBA as usize) * CLUSTER_SIZE;
        img[bs..bs + 3].copy_from_slice(&[0xEB, 0x76, 0x90]);
        img[bs + 3..bs + 11].copy_from_slice(b"EXFAT   ");
        img[bs + 64..bs + 72].copy_from_slice(&u64::from(START_LBA).to_le_bytes());
        img[bs + 72..bs + 80].copy_from_slice(&32_u64.to_le_bytes());
        img[bs + 80..bs + 84].copy_from_slice(&1_u32.to_le_bytes());
        img[bs + 84..bs + 88].copy_from_slice(&1_u32.to_le_bytes());
        img[bs + 88..bs + 92].copy_from_slice(&2_u32.to_le_bytes());
        img[bs + 92..bs + 96].copy_from_slice(&16_u32.to_le_bytes());
        img[bs + 96..bs + 100].copy_from_slice(&ROOT_CLUSTER.to_le_bytes());
        img[bs + 100..bs + 104].copy_from_slice(&0xC0FF_EE11_u32.to_le_bytes());
        img[bs + 108] = 9;
        img[bs + 109] = 0;
        img[bs + 110] = 1;
        img[bs + 510] = 0x55;
        img[bs + 511] = 0xAA;

        let fat_base = (1 + START_LBA as usize) * CLUSTER_SIZE;
        let root_entry = fat_base + (ROOT_CLUSTER as usize * 4);
        img[root_entry..root_entry + 4].copy_from_slice(&0xFFFF_FFFF_u32.to_le_bytes());

        let upcase = UpcaseTable::ascii_identity();
        let teslacam_dir = encode_entry_set(
            "TeslaCam",
            true,
            TESLACAM_CLUSTER,
            CLUSTER_SIZE as u64,
            CLUSTER_SIZE as u64,
            true,
            &upcase,
        );
        let bucket_entry = encode_entry_set(
            bucket_dir,
            true,
            BUCKET_CLUSTER,
            CLUSTER_SIZE as u64,
            CLUSTER_SIZE as u64,
            true,
            &upcase,
        );
        let event_dir_entry = encode_entry_set(
            &event_dir_name(),
            true,
            EVENT_DIR_CLUSTER,
            CLUSTER_SIZE as u64,
            CLUSTER_SIZE as u64,
            true,
            &upcase,
        );
        let front_entry = encode_entry_set(
            FRONT_FILE_NAME,
            false,
            FRONT_FILE_CLUSTER,
            8,
            8,
            true,
            &upcase,
        );
        let event_entry = encode_entry_set(
            "event.json",
            false,
            EVENT_JSON_CLUSTER,
            event_json.len() as u64,
            event_json.len() as u64,
            true,
            &upcase,
        );

        write_cluster(
            &mut img,
            START_LBA,
            ROOT_CLUSTER,
            &directory_cluster(&[teslacam_dir], CLUSTER_SIZE),
        );
        write_cluster(
            &mut img,
            START_LBA,
            TESLACAM_CLUSTER,
            &directory_cluster(&[bucket_entry], CLUSTER_SIZE),
        );
        write_cluster(
            &mut img,
            START_LBA,
            BUCKET_CLUSTER,
            &directory_cluster(&[event_dir_entry], CLUSTER_SIZE),
        );
        write_cluster(
            &mut img,
            START_LBA,
            EVENT_DIR_CLUSTER,
            &directory_cluster(&[front_entry, event_entry], CLUSTER_SIZE),
        );
        write_cluster(&mut img, START_LBA, FRONT_FILE_CLUSTER, b"not-anmp");
        write_cluster(&mut img, START_LBA, EVENT_JSON_CLUSTER, event_json);
        img
    }

    fn produce_stable_batch(reader: &SliceReader) -> crate::record::ScanBatch {
        let sources = [ImageSource::with_slot(reader, 0)];
        let mut tracker = StabilityTracker::new(StabilityConfig::default());
        let _ =
            produce(&sources, &mut tracker, 1_000, DEFAULT_SEI_SAMPLE_RATE).expect("first pass");
        produce(&sources, &mut tracker, 1_060, DEFAULT_SEI_SAMPLE_RATE).expect("second pass")
    }

    fn sources_with_slot_overrides(readers: &[SliceReader]) -> Vec<ImageSource<'_, SliceReader>> {
        readers
            .iter()
            .enumerate()
            .map(|(slot, reader)| {
                ImageSource::with_slot(reader, u8::try_from(slot).expect("slot fits into u8"))
            })
            .collect()
    }

    fn expected_savedclip_keys(source_count: usize) -> BTreeSet<String> {
        (0..source_count)
            .map(|slot| format!("{slot}:TeslaCam/SavedClips/{EVENT_TIMESTAMP}/{EVENT_TIMESTAMP}"))
            .collect()
    }

    #[test]
    fn image_source_constructors_carry_slot() {
        let img = empty_single_partition_image();
        let reader = SliceReader::new(img);
        assert_eq!(ImageSource::native(&reader).slot_override, None);
        assert_eq!(ImageSource::with_slot(&reader, 1).slot_override, Some(1));
    }

    #[test]
    fn produce_walks_two_images_into_one_complete_batch() {
        let teslacam = SliceReader::new(empty_single_partition_image());
        let media = SliceReader::new(empty_single_partition_image());
        let sources = [
            ImageSource::with_slot(&teslacam, 0),
            ImageSource::with_slot(&media, 1),
        ];
        let mut tracker = StabilityTracker::new(StabilityConfig::default());

        let batch = produce(&sources, &mut tracker, 1_000, 30).expect("two-image produce");

        // Both single-partition images were walked and merged.
        assert_eq!(batch.stats.partitions, 2);
        assert!(batch.complete);
        assert!(batch.media_inventory);
        // Empty roots ⇒ no clips, no media, nothing to prune.
        assert!(batch.records.is_empty());
        assert!(batch.present_keys.is_empty());
        assert!(batch.media.is_empty());
        assert!(batch.media_present_paths.is_empty());
    }

    #[test]
    fn produce_single_native_image_walks_one_partition() {
        let reader = SliceReader::new(empty_single_partition_image());
        let sources = [ImageSource::native(&reader)];
        let mut tracker = StabilityTracker::new(StabilityConfig::default());

        let batch = produce(&sources, &mut tracker, 1_000, 30).expect("single-image produce");
        assert_eq!(batch.stats.partitions, 1);
        assert!(batch.complete);
    }

    #[test]
    fn produce_chunks_front_backlog_across_batches() {
        let source_count = MAX_FRONT_SHAPES_PER_BATCH + 1;
        let event_json = br#"{"timestamp":"2026-06-01T20:10:35-07:00","est_lat":"37.7749","est_lon":"-122.4194"}"#;
        let readers: Vec<SliceReader> = (0..source_count)
            .map(|_| SliceReader::new(event_fixture_image("SavedClips", event_json)))
            .collect();
        let sources = sources_with_slot_overrides(&readers);
        let expected_keys = expected_savedclip_keys(source_count);
        let mut tracker = StabilityTracker::new(StabilityConfig::default());

        let warmup =
            produce(&sources, &mut tracker, 1_000, DEFAULT_SEI_SAMPLE_RATE).expect("warmup pass");
        assert!(warmup.records.is_empty());

        let first_backlog = produce(&sources, &mut tracker, 1_060, DEFAULT_SEI_SAMPLE_RATE)
            .expect("first eligible pass");
        assert!(!first_backlog.complete);
        assert!(first_backlog
            .records
            .iter()
            .filter(|record| record.is_front())
            .count()
            <= MAX_FRONT_SHAPES_PER_BATCH);

        let mut emitted_front = first_backlog
            .records
            .iter()
            .filter(|record| record.is_front())
            .count();
        let mut seen_record_keys: BTreeSet<String> = first_backlog
            .records
            .iter()
            .map(|record| record.canonical_key.clone())
            .collect();
        let mut seen_present_keys: BTreeSet<String> =
            first_backlog.present_keys.iter().cloned().collect();

        let mut now_secs = 1_120;
        let mut passes = 0_u8;
        loop {
            let batch =
                produce(&sources, &mut tracker, now_secs, DEFAULT_SEI_SAMPLE_RATE).expect("pass");
            emitted_front += batch
                .records
                .iter()
                .filter(|record| record.is_front())
                .count();
            seen_record_keys.extend(batch.records.iter().map(|record| record.canonical_key.clone()));
            seen_present_keys.extend(batch.present_keys.iter().cloned());
            if batch.complete {
                break;
            }
            assert!(batch
                .records
                .iter()
                .filter(|record| record.is_front())
                .count()
                <= MAX_FRONT_SHAPES_PER_BATCH);
            now_secs += 60;
            passes = passes.saturating_add(1);
            assert!(passes < 8, "backlog did not drain in expected passes");
        }

        assert_eq!(emitted_front, source_count);
        assert_eq!(seen_record_keys, expected_keys);
        assert_eq!(seen_present_keys, expected_keys);
    }

    #[test]
    fn produce_keeps_complete_true_when_front_backlog_is_within_cap() {
        let source_count = MAX_FRONT_SHAPES_PER_BATCH - 1;
        let event_json = br#"{"timestamp":"2026-06-01T20:10:35-07:00","est_lat":"37.7749","est_lon":"-122.4194"}"#;
        let readers: Vec<SliceReader> = (0..source_count)
            .map(|_| SliceReader::new(event_fixture_image("SavedClips", event_json)))
            .collect();
        let sources = sources_with_slot_overrides(&readers);
        let mut tracker = StabilityTracker::new(StabilityConfig::default());

        let _ = produce(&sources, &mut tracker, 1_000, DEFAULT_SEI_SAMPLE_RATE)
            .expect("warmup pass");
        let steady = produce(&sources, &mut tracker, 1_060, DEFAULT_SEI_SAMPLE_RATE)
            .expect("eligible pass");

        assert!(steady.complete);
        assert_eq!(
            steady
                .records
                .iter()
                .filter(|record| record.is_front())
                .count(),
            source_count
        );
        let drained = produce(&sources, &mut tracker, 1_120, DEFAULT_SEI_SAMPLE_RATE)
            .expect("post-drain pass");
        assert!(drained.complete);
        assert!(drained.records.is_empty());
    }

    #[test]
    fn produce_completes_in_one_pass_at_exactly_the_front_cap() {
        // Exactly the cap's worth of front clips (each carrying an event.json
        // sidecar). The cheap sidecars trailing the final shaped front must NOT
        // force a spurious `complete=false`: with exactly `cap` fronts none is
        // deferred, so the batch is complete in a single eligible pass.
        let source_count = MAX_FRONT_SHAPES_PER_BATCH;
        let event_json = br#"{"timestamp":"2026-06-01T20:10:35-07:00","est_lat":"37.7749","est_lon":"-122.4194"}"#;
        let readers: Vec<SliceReader> = (0..source_count)
            .map(|_| SliceReader::new(event_fixture_image("SavedClips", event_json)))
            .collect();
        let sources = sources_with_slot_overrides(&readers);
        let mut tracker = StabilityTracker::new(StabilityConfig::default());

        let _ = produce(&sources, &mut tracker, 1_000, DEFAULT_SEI_SAMPLE_RATE)
            .expect("warmup pass");
        let batch = produce(&sources, &mut tracker, 1_060, DEFAULT_SEI_SAMPLE_RATE)
            .expect("eligible pass");

        assert!(
            batch.complete,
            "exactly the front cap must not defer (no spurious partial batch)"
        );
        assert_eq!(
            batch
                .records
                .iter()
                .filter(|record| record.is_front())
                .count(),
            source_count
        );
    }

    #[test]
    fn produce_populates_savedclips_clip_event_inventory() {
        let event_json = br#"{"timestamp":"2026-06-01T20:10:35-07:00","est_lat":"37.7749","est_lon":"-122.4194","reason":"sentry","city":"San Francisco","camera":"front"}"#;
        let reader = SliceReader::new(event_fixture_image("SavedClips", event_json));
        let batch = produce_stable_batch(&reader);
        let canonical_key = clip_canonical_key("SavedClips");

        assert!(batch.clip_events_inventory);
        assert_eq!(batch.clip_events.len(), 1);
        assert_eq!(batch.records.len(), 1);
        assert!(batch.present_keys.iter().any(|k| k == &canonical_key));
        let event = &batch.clip_events[0];
        assert_eq!(event.event_dir_key, event_dir_key("SavedClips"));
        assert_eq!(event.bucket, Bucket::SavedClips);
        assert_eq!(event.primary_canonical_key, canonical_key);
        assert_eq!(event.est_lat, Some(37.7749));
        assert_eq!(event.est_lon, Some(-122.4194));
    }

    #[test]
    fn produce_skips_malformed_event_json_without_dropping_clip() {
        let reader = SliceReader::new(event_fixture_image(
            "SavedClips",
            br#"{"timestamp":"2026-06-01T20:10:35""#,
        ));
        let batch = produce_stable_batch(&reader);
        let canonical_key = clip_canonical_key("SavedClips");

        assert!(batch.clip_events_inventory);
        assert!(batch.clip_events.is_empty());
        assert!(batch
            .records
            .iter()
            .any(|r| r.canonical_key == canonical_key));
        assert!(batch.present_keys.iter().any(|k| k == &canonical_key));
    }

    #[test]
    fn produce_ignores_recentclips_event_json() {
        let event_json =
            br#"{"timestamp":"2026-06-01T20:10:35Z","est_lat":"37.7749","est_lon":"-122.4194"}"#;
        let reader = SliceReader::new(event_fixture_image("RecentClips", event_json));
        let batch = produce_stable_batch(&reader);
        let canonical_key = clip_canonical_key("RecentClips");

        assert!(batch.clip_events_inventory);
        assert!(batch.clip_events.is_empty());
        assert!(batch
            .records
            .iter()
            .any(|r| r.canonical_key == canonical_key));
        assert!(batch.present_keys.iter().any(|k| k == &canonical_key));
        assert!(batch
            .records
            .iter()
            .any(|r| r.angle.file_ref == front_clip_path("RecentClips")));
    }

    #[test]
    fn produce_aborts_when_any_source_is_structurally_corrupt() {
        let good = SliceReader::new(empty_single_partition_image());
        // A 512-byte buffer with no 0x55AA signature fails parse_mbr.
        let bad = SliceReader::new(vec![0_u8; 512]);
        let sources = [
            ImageSource::with_slot(&good, 0),
            ImageSource::with_slot(&bad, 1),
        ];
        let mut tracker = StabilityTracker::new(StabilityConfig::default());

        // A structural failure on the SECOND source aborts the whole pass —
        // the consumer must never see a half-walked batch marked complete.
        assert!(produce(&sources, &mut tracker, 1_000, 30).is_err());
    }
}
