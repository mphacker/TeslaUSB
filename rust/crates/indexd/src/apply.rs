//! The **consumer** half of the `scannerd → indexd` seam.
//!
//! [`apply`] takes a [`ScanBatch`] of facts — produced by `scannerd`'s
//! [`produce`](scannerd::produce::produce) over the untrusted raw image —
//! and runs the one-transaction DB cycle that was previously the tail of
//! `indexd`'s in-process `run_scan_pass`:
//!
//! ```text
//! validate batch → open tx → per record:
//!     front  → upsert_clip + replace_clip_waypoints + upsert_angle
//!     other  → ensure_clip + upsert_angle
//!   → prune vanished clips (only if `complete`) → derive → rebuild → commit
//! ```
//!
//! `indexd` is the **sole DB writer** and does **no raw parsing**
//! (`indexd.md` §1/§3). Every field in the batch is *untrusted data*: the
//! batch is validated against the wire caps, each record is validated
//! individually (a single malformed record is skipped + counted, never
//! aborting the batch), and the forge-prone `is_front` / `view_kind` are
//! **derived** here from the camera label and bucket rather than trusted
//! off the wire.
//!
//! ## Parity with the legacy in-process pass
//!
//! [`crate::scan::run_scan_pass`] is now `produce(...) + apply(...)`, so
//! the in-process path and the future cross-process (socket) path share
//! these exact two halves. The DB-outcome counters live here (only the
//! writer knows whether a row committed); the producer's diagnostic
//! counters (`unplaceable_*`) are merged back in `run_scan_pass` to
//! reproduce the legacy [`ScanReport`](crate::scan::ScanReport) exactly.

use std::collections::HashSet;

use rusqlite::Connection;
use scannerd::record::{ClipAngleRecord, ScanBatch, WireWaypoint};
use teslausb_core::sei::tesla::{AutopilotState, Gear};

use crate::db::DbError;
use crate::db::ingest::{
    AngleFacts, ClipFacts, ensure_clip, load_derive_clips, prune_missing_clips, rebuild_derived,
    replace_clip_waypoints, upsert_angle, upsert_clip,
};
use crate::derive::{DeriveConfig, derive};
use crate::model::{DeriveWaypoint, FolderClass};
use crate::scan::ScanError;

/// DB-outcome counts from one [`apply`] call. These are the counters only
/// the writer can know (a row actually committed); the producer's
/// `unplaceable_*` diagnostics are merged with these in
/// [`run_scan_pass`](crate::scan::run_scan_pass) to form the legacy
/// `ScanReport`.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct ApplyReport {
    /// Clip angles written this pass (front + other) on full DB success.
    pub clips_written: usize,
    /// Front clips whose waypoint cache was replaced this pass.
    pub front_walked: usize,
    /// Cached waypoints written this pass.
    pub waypoints: usize,
    /// Records skipped: failed per-record validation or errored mid-write.
    pub record_errors: usize,
    /// Clips pruned (present only when the batch was `complete`).
    pub pruned: usize,
    /// Trips materialized after the rebuild.
    pub trips: usize,
    /// Events materialized after the rebuild (driving + sentry).
    pub events: usize,
}

/// D1 `view_kind` for a freshly scanned car-volume clip, recomputed from
/// the bucket (never trusted off the wire). `ArchivedClips` are Pi-side
/// archive copies and carry `'archive'` (the durable/playable source);
/// every live car-volume bucket carries `'ro_usb'` (the read-only USB
/// view Tesla may rotate at any time — never retention-leasable / never an
/// upload source per `indexd-schema.md` §3.1 + `uploadd.md` §3).
fn view_kind_for(folder_class: FolderClass) -> &'static str {
    if matches!(folder_class, FolderClass::ArchivedClips) {
        "archive"
    } else {
        "ro_usb"
    }
}

/// Map a wire waypoint back to the internal derive-waypoint 1:1. The SEI
/// enums are decoded from their proto integers via `From<u32>`, which
/// round-trips the forward-compat `Unknown(n)` case losslessly.
fn map_waypoint(w: &WireWaypoint) -> DeriveWaypoint {
    DeriveWaypoint {
        frame_index: w.frame_index,
        offset_ms: w.offset_ms,
        absolute_utc: w.absolute_utc,
        lat: w.lat,
        lon: w.lon,
        speed: w.speed,
        heading: w.heading,
        accel_x: w.accel_x,
        accel_y: w.accel_y,
        accel_z: w.accel_z,
        autopilot_state: AutopilotState::from(w.autopilot_state),
        gear: Gear::from(w.gear),
        has_gps_fix: w.has_gps_fix,
    }
}

/// Build the [`ClipFacts`] for a record. Front clips carry the probed
/// `ended_at` / `duration_s`; non-front records always carry `None` for
/// both (the producer never fills them), so a single uniform construction
/// reproduces both legacy `process_front` and `process_other`.
fn clip_facts(record: &ClipAngleRecord, folder_class: FolderClass) -> ClipFacts {
    ClipFacts {
        canonical_key: record.canonical_key.clone(),
        started_at: record.started_at,
        ended_at: record.ended_at,
        partition: record.partition.clone(),
        folder_class,
        duration_s: record.duration_s,
    }
}

/// Build the [`AngleFacts`] for a record, recomputing `view_kind` from the
/// bucket so it cannot be forged independently off the wire.
fn angle_facts(record: &ClipAngleRecord, folder_class: FolderClass) -> AngleFacts {
    AngleFacts {
        camera: record.angle.camera.clone(),
        file_ref: record.angle.file_ref.clone(),
        view_kind: view_kind_for(folder_class).to_owned(),
        offset_ms: record.angle.offset_ms,
        duration_s: record.angle.duration_s,
        size_bytes: record.angle.size_bytes,
    }
}

/// Ingest one validated record. Front angles upsert the clip, replace the
/// waypoint cache (even when empty, to clear a stale version), and upsert
/// the front angle; non-front angles only ensure the clip exists (never
/// downgrading a front-resolved instant) and upsert the angle. Returns the
/// number of waypoints written (front only; `0` otherwise).
fn apply_record(
    conn: &Connection,
    record: &ClipAngleRecord,
    is_front: bool,
) -> Result<usize, DbError> {
    let folder_class = FolderClass::from_db_str(record.bucket.as_db_str());
    let facts = clip_facts(record, folder_class);
    let angle = angle_facts(record, folder_class);
    if is_front {
        let clip_id = upsert_clip(conn, &facts)?;
        let derived: Vec<DeriveWaypoint> = record.waypoints.iter().map(map_waypoint).collect();
        replace_clip_waypoints(conn, clip_id, &derived)?;
        upsert_angle(conn, clip_id, &angle)?;
        Ok(derived.len())
    } else {
        let clip_id = ensure_clip(conn, &facts)?;
        upsert_angle(conn, clip_id, &angle)?;
        Ok(0)
    }
}

/// Apply one batch of scanner facts to the catalog in a single transaction.
///
/// The batch is validated at the batch level first (protocol version +
/// gross-size caps); a failure there is fatal (rejects the whole batch).
/// Each record is then validated and applied individually: a record that
/// fails validation, references a key absent from a `complete` batch's
/// present set, or errors mid-write is skipped and counted in
/// [`ApplyReport::record_errors`] — one bad record never aborts the batch
/// (matching the legacy tolerate-bad-clip behavior). The prune step runs
/// **only** when the batch is `complete` (the present set is trustworthy),
/// then the derivation is rebuilt and the transaction commits atomically.
///
/// # Errors
///
/// Returns [`ScanError::Batch`] if the batch fails batch-level validation,
/// or [`ScanError::Db`] if a transaction/prune/derive/commit step fails.
pub fn apply(
    conn: &mut Connection,
    batch: &ScanBatch,
    derive_cfg: DeriveConfig,
) -> Result<ApplyReport, ScanError> {
    batch.validate()?;

    let present: HashSet<&str> = batch.present_keys.iter().map(String::as_str).collect();
    let mut report = ApplyReport::default();

    let tx = conn.transaction().map_err(DbError::from)?;
    for record in &batch.records {
        if record.validate().is_err() {
            report.record_errors += 1;
            continue;
        }
        // A `complete` batch's present set is trustworthy and must contain
        // every emitted record; a record outside it is inconsistent (only
        // reachable over a forged wire — the in-process producer always
        // satisfies it) and is skipped rather than ingested.
        if batch.complete && !present.contains(record.canonical_key.as_str()) {
            report.record_errors += 1;
            continue;
        }
        let is_front = record.is_front();
        match apply_record(&tx, record, is_front) {
            Ok(waypoints) => {
                report.clips_written += 1;
                if is_front {
                    report.front_walked += 1;
                    report.waypoints += waypoints;
                }
            }
            Err(_) => report.record_errors += 1,
        }
    }

    if batch.complete {
        let present_keys: HashSet<String> = batch.present_keys.iter().cloned().collect();
        report.pruned = prune_missing_clips(&tx, &present_keys)?;
    }

    let clips = load_derive_clips(&tx)?;
    let derivation = derive(&clips, derive_cfg);
    rebuild_derived(&tx, &derivation)?;
    tx.commit().map_err(DbError::from)?;

    report.trips = derivation.trips.len();
    let trip_events: usize = derivation.trips.iter().map(|t| t.events.len()).sum();
    report.events = trip_events + derivation.sentry_events.len();

    Ok(report)
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        clippy::float_cmp,
        clippy::indexing_slicing
    )]

    use super::{apply, map_waypoint, view_kind_for};
    use crate::db::open_in_memory;
    use crate::derive::{DeriveConfig, waypoint_from_walk};
    use crate::model::FolderClass;
    use rusqlite::Connection;
    use scannerd::produce::wire_waypoint_from_walk;
    use scannerd::record::{
        AngleRecord, Bucket, ClipAngleRecord, PROTOCOL_VERSION, ProducerStats, ScanBatch,
    };
    use scannerd::seiwalk::Waypoint;
    use teslausb_core::sei::tesla::{AutopilotState, Gear, SeiMessage};

    fn count(conn: &Connection, table: &str) -> i64 {
        conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0))
            .unwrap()
    }

    fn front_angle(camera: &str, dir: &str) -> AngleRecord {
        AngleRecord {
            camera: camera.to_owned(),
            file_ref: format!("{dir}/2026-06-01_20-10-04-{camera}.mp4"),
            offset_ms: 0,
            duration_s: None,
            size_bytes: Some(1024),
        }
    }

    fn waypoint(frame: u32, offset_ms: f64, lat: f64, lon: f64) -> Waypoint {
        Waypoint {
            frame_index: frame,
            timestamp_ms: offset_ms,
            message: SeiMessage {
                latitude_deg: lat,
                longitude_deg: lon,
                vehicle_speed_mps: 12.0,
                heading_deg: 90.0,
                linear_acceleration_mps2_x: 0.5,
                linear_acceleration_mps2_y: -0.25,
                linear_acceleration_mps2_z: 9.8,
                autopilot_state: AutopilotState::Autosteer,
                gear_state: Gear::Drive,
                ..SeiMessage::default()
            },
        }
    }

    /// Load-bearing parity check: the wire round-trip
    /// (`wire_waypoint_from_walk` → `map_waypoint`) must produce the exact
    /// `DeriveWaypoint` the legacy in-process `waypoint_from_walk` produced
    /// — that value is what feeds `replace_clip_waypoints` and therefore
    /// the entire derivation. Covers GPS / no-GPS and the forward-compat
    /// `Unknown(n)` enum cases.
    #[test]
    fn wire_waypoint_maps_back_to_legacy_derive_waypoint() {
        let started_at = 1_700_000_000;
        let mut cases = vec![
            waypoint(0, 0.0, 37.5, -122.3),
            waypoint(7, 1500.0, 0.0, 0.0), // no GPS fix
        ];
        // Forward-compat unknown enum codes must survive the int encoding.
        let mut unknown = waypoint(9, 3000.0, 1.0, 2.0);
        unknown.message.autopilot_state = AutopilotState::Unknown(42);
        unknown.message.gear_state = Gear::Unknown(7);
        cases.push(unknown);

        for w in &cases {
            let via_wire = map_waypoint(&wire_waypoint_from_walk(w, started_at));
            let legacy = waypoint_from_walk(w, started_at);
            assert_eq!(
                via_wire, legacy,
                "wire round-trip diverged from legacy derive-waypoint"
            );
        }
    }

    #[test]
    fn view_kind_maps_archive_vs_ro_usb() {
        assert_eq!(view_kind_for(FolderClass::ArchivedClips), "archive");
        assert_eq!(view_kind_for(FolderClass::SavedClips), "ro_usb");
        assert_eq!(view_kind_for(FolderClass::RecentClips), "ro_usb");
    }

    fn front_record(key: &str, dir: &str, started_at: i64) -> ClipAngleRecord {
        let started = started_at;
        let waypoints = vec![
            wire_waypoint_from_walk(&waypoint(0, 0.0, 37.5, -122.3), started),
            wire_waypoint_from_walk(&waypoint(1, 1000.0, 37.5005, -122.3005), started),
        ];
        ClipAngleRecord {
            canonical_key: key.to_owned(),
            started_at: started,
            ended_at: Some(started + 1),
            partition: "slot0".to_owned(),
            bucket: Bucket::SavedClips,
            duration_s: Some(1.0),
            angle: front_angle("front", dir),
            waypoints,
        }
    }

    fn other_record(key: &str, dir: &str, camera: &str, started_at: i64) -> ClipAngleRecord {
        ClipAngleRecord {
            canonical_key: key.to_owned(),
            started_at,
            ended_at: None,
            partition: "slot0".to_owned(),
            bucket: Bucket::SavedClips,
            duration_s: None,
            angle: front_angle(camera, dir),
            waypoints: Vec::new(),
        }
    }

    fn batch(records: Vec<ClipAngleRecord>, complete: bool) -> ScanBatch {
        let present_keys: Vec<String> = records.iter().map(|r| r.canonical_key.clone()).collect();
        ScanBatch {
            version: PROTOCOL_VERSION,
            generation: 1,
            complete,
            stats: ProducerStats::default(),
            present_keys,
            records,
        }
    }

    #[test]
    fn apply_ingests_clip_angle_and_waypoints() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let b = batch(
            vec![
                front_record(key, dir, 1_700_000_000),
                other_record(key, dir, "back", 1_700_000_000),
            ],
            true,
        );

        let report = apply(&mut conn, &b, DeriveConfig::default()).unwrap();
        assert_eq!(report.clips_written, 2);
        assert_eq!(report.front_walked, 1);
        assert_eq!(report.waypoints, 2);
        assert_eq!(report.record_errors, 0);

        assert_eq!(count(&conn, "clips"), 1);
        assert_eq!(count(&conn, "angles"), 2);
        assert_eq!(count(&conn, "clip_waypoints"), 2);
    }

    #[test]
    fn apply_is_idempotent_across_a_serde_round_trip() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let b = batch(vec![front_record(key, dir, 1_700_000_000)], true);

        // The cross-process path serializes the batch over the socket; a
        // serde round-trip before apply must not change the outcome.
        let json = serde_json::to_string(&b).unwrap();
        let decoded: ScanBatch = serde_json::from_str(&json).unwrap();
        assert_eq!(b, decoded);

        let first = apply(&mut conn, &decoded, DeriveConfig::default()).unwrap();
        let second = apply(&mut conn, &decoded, DeriveConfig::default()).unwrap();
        assert_eq!(first.clips_written, second.clips_written);
        assert_eq!(first.waypoints, second.waypoints);

        // Re-applying the same facts must not duplicate rows.
        assert_eq!(count(&conn, "clips"), 1);
        assert_eq!(count(&conn, "angles"), 1);
        assert_eq!(count(&conn, "clip_waypoints"), 2);
    }

    #[test]
    fn incomplete_batch_does_not_prune() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key_a = "0:TeslaCam/SavedClips/a/2026-06-01_20-10-04";
        let key_b = "0:TeslaCam/SavedClips/b/2026-06-01_20-11-04";

        // Seed two clips via a complete batch.
        let seed = batch(
            vec![
                front_record(key_a, dir, 1_700_000_000),
                front_record(key_b, dir, 1_700_000_100),
            ],
            true,
        );
        apply(&mut conn, &seed, DeriveConfig::default()).unwrap();
        assert_eq!(count(&conn, "clips"), 2);

        // An INCOMPLETE batch listing only key_a must NOT prune key_b.
        let partial = batch(vec![front_record(key_a, dir, 1_700_000_000)], false);
        let report = apply(&mut conn, &partial, DeriveConfig::default()).unwrap();
        assert_eq!(report.pruned, 0);
        assert_eq!(count(&conn, "clips"), 2);

        // A COMPLETE batch listing only key_a prunes the vanished key_b.
        let full = batch(vec![front_record(key_a, dir, 1_700_000_000)], true);
        let report = apply(&mut conn, &full, DeriveConfig::default()).unwrap();
        assert_eq!(report.pruned, 1);
        assert_eq!(count(&conn, "clips"), 1);
    }

    #[test]
    fn malformed_record_is_skipped_not_fatal() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let good = "0:TeslaCam/SavedClips/good/2026-06-01_20-10-04";
        let bad = "0:TeslaCam/SavedClips/bad/2026-06-01_20-11-04";

        // A non-front record carrying waypoints fails per-record validation.
        let mut bad_record = other_record(bad, dir, "back", 1_700_000_100);
        bad_record.waypoints = vec![wire_waypoint_from_walk(
            &waypoint(0, 0.0, 1.0, 2.0),
            1_700_000_100,
        )];

        let b = batch(
            vec![front_record(good, dir, 1_700_000_000), bad_record],
            true,
        );
        let report = apply(&mut conn, &b, DeriveConfig::default()).unwrap();
        assert_eq!(report.record_errors, 1);
        assert_eq!(report.clips_written, 1);
        // The good clip still landed.
        assert_eq!(count(&conn, "clips"), 1);
    }

    #[test]
    fn version_mismatch_is_fatal() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let mut b = batch(vec![front_record(key, dir, 1_700_000_000)], true);
        b.version = PROTOCOL_VERSION + 1;
        assert!(apply(&mut conn, &b, DeriveConfig::default()).is_err());
    }
}
