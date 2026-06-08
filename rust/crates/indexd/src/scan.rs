//! Scan orchestrator — the `scannerd → indexd` seam.
//!
//! One [`run_scan_pass`] performs a single derivation cycle over a raw
//! backing image:
//!
//! ```text
//! reader → parse_mbr → (per exFAT partition) parse_boot_sector → Volume
//!        → walk_volume → StabilityTracker.observe
//!        → for each just-stable clip angle:
//!              front  → read bytes → walk_clip_waypoints → waypoint_from_walk
//!                       → upsert_clip + replace_clip_waypoints + upsert_angle
//!              other  → ensure_clip + upsert_angle
//!        → prune vanished clips → rebuild_all_from_db (trips/events)
//! ```
//!
//! All raw byte parsing is reused from [`scannerd`] / `teslausb-core`
//! (never re-implemented here). The derivation itself is pure
//! ([`crate::derive`]); this module only wires the I/O.
//!
//! ## Recording instant
//!
//! The front clip's `mvhd`/GPS instant ([`ClipWaypoints::clip_started_utc`])
//! is authoritative; the Tesla filename timestamp is the documented
//! RTC-less fallback (see [`crate::derive::epoch_from_tesla_timestamp`]).
//! Non-front angles only ever seed the filename fallback and never
//! downgrade a front-resolved instant (they go through
//! [`ensure_clip`](crate::db::ingest::ensure_clip)).
//!
//! ## Daemon loop
//!
//! Stability gating needs ≥2 observations spaced by the quiescence
//! window, so the binary calls [`run_scan_pass`] repeatedly (the tracker
//! and DB persist across passes); a single call is the per-tick unit and
//! is what host tests drive.

use std::collections::HashSet;

use rusqlite::Connection;
use scannerd::boot::parse_boot_sector;
use scannerd::clip::parse_clip_name;
use scannerd::mbr::parse_mbr;
use scannerd::reader::BlockReader;
use scannerd::seiwalk::walk_clip_waypoints;
use scannerd::stability::StabilityTracker;
use scannerd::volume::Volume;
use scannerd::walk::{FileRecord, walk_volume};

use crate::db::DbError;
use crate::db::ingest::{
    AngleFacts, ClipFacts, ensure_clip, load_derive_clips, prune_missing_clips, rebuild_derived,
    replace_clip_waypoints, upsert_angle, upsert_clip,
};
use crate::derive::{DeriveConfig, epoch_from_tesla_timestamp, waypoint_from_walk};
use crate::model::{DeriveWaypoint, FolderClass};

/// The Tesla front camera angle — the only one carrying the SEI
/// telemetry that trips/events derive from.
const FRONT_CAMERA: &str = "front";

/// SEI sample-rate decimation stride. Matches the v1 worker
/// (`worker.toml` `sample_rate = 30`) so the cached waypoint cadence —
/// and therefore the derived events — match production.
pub const DEFAULT_SEI_SAMPLE_RATE: u32 = 30;

/// Hard cap on bytes read for a single clip, to bound memory against a
/// corrupt `valid_data_length`. Tesla clips are tens of MiB; 256 MiB is
/// a generous ceiling.
const MAX_CLIP_BYTES: u64 = 256 * 1024 * 1024;

/// Errors from a scan pass.
#[derive(Debug, thiserror::Error)]
pub enum ScanError {
    /// A raw-media read/parse failure (MBR, boot sector, FAT chain, ...).
    #[error("scanner error: {0}")]
    Scanner(#[from] scannerd::error::ScannerError),
    /// A database error from an ingest/derive step.
    #[error("database error: {0}")]
    Db(#[from] DbError),
}

/// Per-pass tuning. The stability window is owned by the caller's
/// [`StabilityTracker`]; this only carries the SEI cadence and the
/// (Copy) derivation parameters.
#[derive(Debug, Clone, Copy)]
pub struct ScanConfig {
    /// SEI sample-rate decimation stride (see [`DEFAULT_SEI_SAMPLE_RATE`]).
    pub sample_rate: u32,
    /// Derivation thresholds (defaults are the v1 production values).
    pub derive: DeriveConfig,
}

impl Default for ScanConfig {
    fn default() -> Self {
        Self {
            sample_rate: DEFAULT_SEI_SAMPLE_RATE,
            derive: DeriveConfig::default(),
        }
    }
}

/// Diagnostic counts from a scan pass (for logging; carries no control
/// state).
#[derive(Debug, Default, Clone, Copy)]
pub struct ScanReport {
    /// exFAT partitions visited.
    pub partitions: usize,
    /// Total directory entries (files) walked across partitions.
    pub files_seen: usize,
    /// Records reported just-stable this pass.
    pub eligible: usize,
    /// Clip angles upserted (front + other).
    pub clips_upserted: usize,
    /// Front clips whose SEI was walked this pass.
    pub front_clips_walked: usize,
    /// Cached waypoints written this pass.
    pub waypoints: usize,
    /// Clips pruned (vanished from the media).
    pub pruned: usize,
    /// Trips materialized after the rebuild.
    pub trips: usize,
    /// Events materialized after the rebuild (driving + sentry).
    pub events: usize,
    /// Eligible clips that errored during ingest and were skipped (the
    /// pass still commits the rest; see [`run_scan_pass`]).
    pub errors: usize,
}

/// A clip angle's identity, parsed from its filename + path.
struct ClipIdent {
    /// Dedup key: `slot:<parent-dir>/<timestamp>` (camera-independent).
    key: String,
    /// The 19-char Tesla timestamp prefix (`YYYY-MM-DD_HH-MM-SS`).
    timestamp: String,
    /// The camera suffix (`front`, `back`, `left_repeater`, ...).
    camera: String,
    /// Bucket classification from the directory path.
    folder_class: FolderClass,
}

impl ClipIdent {
    fn is_front(&self) -> bool {
        self.camera.eq_ignore_ascii_case(FRONT_CAMERA)
    }
}

/// Parse a walk record into a clip-angle identity, or `None` if it is
/// not a Tesla clip with a camera suffix.
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
        folder_class: FolderClass::from_path(&record.path),
    })
}

/// D1 `view_kind` for a freshly scanned car-volume clip. These are the
/// car's own live recordings read read-only through the raw reader;
/// `ArchivedClips` are Pi-side archive copies. (FLAG: provisional
/// mapping — `live`/`ro` distinction may be refined by retentiond/webd.)
fn view_kind_for(folder_class: FolderClass) -> &'static str {
    if matches!(folder_class, FolderClass::ArchivedClips) {
        "archive"
    } else {
        "live"
    }
}

/// `SystemTime` → positive epoch seconds, rejecting the zero/pre-epoch
/// sentinel (an `mvhd` `creation_time` of 0 means "unset").
fn systemtime_to_epoch(st: std::time::SystemTime) -> Option<i64> {
    st.duration_since(std::time::UNIX_EPOCH)
        .ok()
        .and_then(|d| i64::try_from(d.as_secs()).ok())
        .filter(|&s| s > 0)
}

/// Read a file's valid data region in full (bounded by [`MAX_CLIP_BYTES`]).
fn read_full_file<R: BlockReader + ?Sized>(
    volume: &Volume<'_, R>,
    record: &FileRecord,
) -> Result<Vec<u8>, scannerd::error::ScannerError> {
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

/// Ingest a front clip. Walks its SEI, resolves the recording instant
/// (`mvhd`/GPS first, Tesla-filename fallback), and **unconditionally**
/// replaces the cached waypoints + upserts the clip and front angle.
///
/// Replacing the waypoint cache even when the SEI walk yields nothing is
/// deliberate: a clip is only re-processed when its content version
/// changed (the stability gate emits once per version), so any prior
/// cache is stale and MUST be cleared — otherwise the rebuild would
/// derive phantom trips/events from an obsolete telemetry version. A
/// front clip that walks to zero waypoints therefore produces no trip
/// (correct parity: no telemetry → no trip).
///
/// Returns the number of waypoints cached.
fn process_front<R: BlockReader + ?Sized>(
    conn: &Connection,
    volume: &Volume<'_, R>,
    record: &FileRecord,
    ident: &ClipIdent,
    config: ScanConfig,
) -> Result<usize, ScanError> {
    let bytes = read_full_file(volume, record)?;
    // A truncated/garbled SEI stream yields `None`; we still clear the
    // stale cache and record the clip/angle below.
    let walk = walk_clip_waypoints(&bytes, config.sample_rate).ok();

    let started_at = walk
        .as_ref()
        .and_then(|w| w.clip_started_utc.and_then(systemtime_to_epoch))
        .or_else(|| epoch_from_tesla_timestamp(&ident.timestamp));
    let Some(started_at) = started_at else {
        // No usable recording instant at all — cannot place the clip.
        return Ok(0);
    };

    let derived: Vec<DeriveWaypoint> = walk.as_ref().map_or_else(Vec::new, |w| {
        w.waypoints
            .iter()
            .map(|wp| waypoint_from_walk(wp, started_at))
            .collect()
    });

    let duration_s = walk
        .as_ref()
        .and_then(|w| w.waypoints.last())
        .map(|wp| wp.timestamp_ms / 1000.0);
    #[allow(clippy::cast_possible_truncation)]
    let ended_at = duration_s.map(|secs| started_at + secs.round() as i64);

    let facts = ClipFacts {
        canonical_key: ident.key.clone(),
        started_at,
        ended_at,
        partition: partition_label(record.partition_slot),
        folder_class: ident.folder_class,
        duration_s,
    };
    let clip_id = upsert_clip(conn, &facts)?;
    replace_clip_waypoints(conn, clip_id, &derived)?;
    upsert_angle(conn, clip_id, &angle_facts(record, ident))?;
    Ok(derived.len())
}

/// Ingest a non-front angle: ensure the clip row exists (without
/// downgrading a front-resolved instant) and upsert the angle.
fn process_other(
    conn: &Connection,
    record: &FileRecord,
    ident: &ClipIdent,
) -> Result<(), ScanError> {
    let Some(started_at) = epoch_from_tesla_timestamp(&ident.timestamp) else {
        // Unparseable filename and no front clip to anchor it — skip.
        return Ok(());
    };
    let facts = ClipFacts {
        canonical_key: ident.key.clone(),
        started_at,
        ended_at: None,
        partition: partition_label(record.partition_slot),
        folder_class: ident.folder_class,
        duration_s: None,
    };
    let clip_id = ensure_clip(conn, &facts)?;
    upsert_angle(conn, clip_id, &angle_facts(record, ident))?;
    Ok(())
}

/// Build the [`AngleFacts`] for a record + identity.
fn angle_facts(record: &FileRecord, ident: &ClipIdent) -> AngleFacts {
    AngleFacts {
        camera: ident.camera.clone(),
        file_ref: record.path.clone(),
        view_kind: view_kind_for(ident.folder_class).to_owned(),
        offset_ms: 0,
        duration_s: None,
        size_bytes: i64::try_from(record.valid_data_length).ok(),
    }
}

/// Human-readable partition label for the `clips.partition` column. The
/// camera-independent dedup key already encodes the slot.
fn partition_label(slot: u8) -> String {
    format!("slot{slot}")
}

/// Run one scan + derivation pass.
///
/// `tracker` and `conn` persist across passes (the stability gate needs
/// repeated observations). `now_secs` is the wall-clock time used for
/// the quiescence window.
///
/// # Errors
///
/// Returns [`ScanError`] if a raw-media read or a database step fails.
/// Individual unreadable clips are skipped (not fatal); a malformed
/// partition table or a DB write failure aborts the pass.
pub fn run_scan_pass<R: BlockReader + ?Sized>(
    reader: &R,
    conn: &mut Connection,
    tracker: &mut StabilityTracker,
    now_secs: u64,
    config: ScanConfig,
) -> Result<ScanReport, ScanError> {
    let mut report = ScanReport::default();

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
    report.partitions = volumes.len();
    report.files_seen = all_records.len();

    let eligible = tracker.observe(&all_records, now_secs);
    report.eligible = eligible.len();

    // Present set = every clip currently on the media, regardless of
    // stability (an in-flux clip must NOT be pruned).
    let mut present: HashSet<String> = HashSet::new();
    for record in &all_records {
        if let Some(ident) = clip_identity(record) {
            present.insert(ident.key);
        }
    }

    // All DB mutations for this pass commit atomically: if the process
    // dies mid-pass the cache is never left torn, and the next pass
    // re-derives. Per-clip read/ingest errors are caught and counted so
    // one bad clip cannot starve its siblings or abort the prune/rebuild
    // (the disposable DB is fully rebuilt on a clean restart, which
    // recovers any clip the stability gate permanently suppressed after
    // an error).
    let tx = conn.transaction().map_err(DbError::from)?;
    for &idx in &eligible {
        let Some(record) = all_records.get(idx) else {
            continue;
        };
        let Some(ident) = clip_identity(record) else {
            continue;
        };
        let outcome = if ident.is_front() {
            match volumes
                .iter()
                .find(|(slot, _)| *slot == record.partition_slot)
                .map(|(_, volume)| volume)
            {
                Some(volume) => process_front(&tx, volume, record, &ident, config),
                None => continue,
            }
        } else {
            process_other(&tx, record, &ident).map(|()| 0)
        };
        match outcome {
            Ok(waypoints) => {
                if ident.is_front() {
                    report.front_clips_walked += 1;
                    report.waypoints += waypoints;
                }
                report.clips_upserted += 1;
            }
            Err(_) => report.errors += 1,
        }
    }

    report.pruned = prune_missing_clips(&tx, &present)?;

    let clips = load_derive_clips(&tx)?;
    let derivation = crate::derive::derive(&clips, config.derive);
    rebuild_derived(&tx, &derivation)?;
    tx.commit().map_err(DbError::from)?;

    report.trips = derivation.trips.len();
    let trip_events: usize = derivation.trips.iter().map(|t| t.events.len()).sum();
    report.events = trip_events + derivation.sentry_events.len();

    Ok(report)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::indexing_slicing)]

    use super::{clip_identity, partition_label, view_kind_for};
    use crate::model::FolderClass;
    use scannerd::walk::FileRecord;
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

    #[test]
    fn identity_parses_front_clip() {
        let rec = record(
            "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-front.mp4",
            "2026-06-01_20-10-04-front.mp4",
            0,
        );
        let ident = clip_identity(&rec).expect("front clip parses");
        assert!(ident.is_front());
        assert_eq!(ident.timestamp, "2026-06-01_20-10-04");
        assert_eq!(ident.folder_class, FolderClass::SavedClips);
        assert_eq!(
            ident.key,
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
        let fk = clip_identity(&front).unwrap();
        let bk = clip_identity(&back).unwrap();
        let lk = clip_identity(&left).unwrap();
        assert_eq!(fk.key, bk.key);
        assert_eq!(fk.key, lk.key);
        assert!(fk.is_front());
        assert!(!bk.is_front());
        assert!(!lk.is_front());
        assert_eq!(fk.folder_class, FolderClass::SentryClips);
    }

    #[test]
    fn non_clip_files_are_ignored() {
        let rec = record("TeslaCam/event.json", "event.json", 0);
        assert!(clip_identity(&rec).is_none());
    }

    #[test]
    fn view_kind_maps_archive_vs_live() {
        assert_eq!(view_kind_for(FolderClass::ArchivedClips), "archive");
        assert_eq!(view_kind_for(FolderClass::SavedClips), "live");
        assert_eq!(view_kind_for(FolderClass::RecentClips), "live");
    }

    #[test]
    fn partition_label_encodes_slot() {
        assert_eq!(partition_label(3), "slot3");
    }
}
