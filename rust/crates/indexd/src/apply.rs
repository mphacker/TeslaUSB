//! The **consumer** half of the `scannerd → indexd` seam.
//!
//! [`apply`] takes a [`ScanBatch`] of facts — produced by `scannerd`'s
//! [`produce`](scannerd::produce::produce) over the untrusted raw image —
//! and runs the one-transaction DB cycle that was previously the tail of
//! `indexd`'s in-process `run_scan_pass`:
//!
//! ```text
//! validate batch → open tx → per record:
//!     front  → parse-state-driven clip/waypoint apply + upsert_angle +
//!              upsert_front_parse_attempt
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

use std::collections::{BTreeMap, HashSet};

use rusqlite::Connection;
use scannerd::record::{ClipAngleRecord, FrontUnplaceableRecord, ScanBatch, WireWaypoint};
use teslausb_core::sei::tesla::{AutopilotState, Gear};

use crate::db::ingest::{
    AngleFacts, ClipEventFacts, ClipFacts, FrontParseAttemptRow, MediaFacts, ensure_clip,
    load_clip_events, load_derive_clips, load_front_parse_attempt, prune_missing_clip_events,
    prune_missing_clips, prune_missing_media, prune_orphan_front_parse_attempts, rebuild_derived,
    replace_clip_waypoints, upsert_angle_scan_preserving, upsert_clip, upsert_clip_event,
    upsert_front_parse_attempt, upsert_media,
};
use crate::db::{DbError, now_epoch_s};
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
    /// Front records applied in the `parse_error` state.
    pub front_parse_errors: usize,
    /// Front records applied in the `read_error` state.
    pub front_read_errors: usize,
    /// Front records applied in the `no_waypoints` state.
    pub front_no_waypoints: usize,
    /// Clips pruned (present only when the batch was `complete`).
    pub pruned: usize,
    /// Media rows upserted this pass (p2 inventory).
    pub media_written: usize,
    /// Media rows pruned (present only when the producer inventoried media
    /// AND the batch was `complete`).
    pub media_pruned: usize,
    /// Clip-event sidecar rows upserted this pass.
    pub clip_events_written: usize,
    /// Clip-event sidecar rows pruned (present only when the producer
    /// inventoried clip events AND the batch was `complete`).
    pub clip_events_pruned: usize,
    /// Trips materialized after the rebuild.
    pub trips: usize,
    /// Events materialized after the rebuild (driving + sentry).
    pub events: usize,
    /// Whether this pass observed a derivation-input change.
    pub derived_dirty: bool,
    /// Whether `rebuild_derived` ran this pass.
    pub rebuild_ran: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FrontApplyState {
    ParsedWithWaypoints,
    NoWaypoints,
    ParseError,
    ReadError,
    Encrypted,
}

impl FrontApplyState {
    /// Map the wire `parse_state` to an apply outcome. The known states
    /// map directly. Everything else — `None` (a pre-`parse_state` producer,
    /// though the `PROTOCOL_VERSION` gate already rejects an older batch
    /// wholesale, so this is defensive only) and any unrecognized/`legacy_unknown`
    /// string — collapses to `ParsedWithWaypoints`. That is safe because the
    /// apply path NEVER clears the waypoint cache for this state unless the
    /// record carries a non-empty list (see the data-integrity invariant in
    /// `apply_record`); an empty/unknown record is therefore non-destructive.
    fn from_wire(parse_state: Option<&str>) -> Self {
        match parse_state {
            Some("no_waypoints") => Self::NoWaypoints,
            Some("parse_error") => Self::ParseError,
            Some("read_error") => Self::ReadError,
            Some("encrypted") => Self::Encrypted,
            _ => Self::ParsedWithWaypoints,
        }
    }

    const fn as_wire(self) -> &'static str {
        match self {
            Self::ParsedWithWaypoints => "parsed_with_waypoints",
            Self::NoWaypoints => "no_waypoints",
            Self::ParseError => "parse_error",
            Self::ReadError => "read_error",
            Self::Encrypted => "encrypted",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ApplyRecordOutcome {
    waypoints_written: usize,
    waypoints_deleted: usize,
    front_state: Option<FrontApplyState>,
}

fn parse_fingerprint_to_u64(value: Option<&str>) -> Option<u64> {
    value.and_then(|v| u64::from_str_radix(v, 16).ok())
}

fn front_parse_backoff_secs(attempt_count: i64) -> i64 {
    let mut delay = 60_i64;
    let mut steps = attempt_count.saturating_sub(1);
    while steps > 0 {
        delay = delay.saturating_mul(4);
        if delay >= 3_600 {
            return 3_600;
        }
        steps -= 1;
    }
    delay.min(3_600)
}

fn front_attempt_update(
    prior: Option<FrontParseAttemptRow>,
    front_state: FrontApplyState,
    new_fingerprint: Option<&str>,
    now: i64,
) -> (i64, Option<i64>) {
    match front_state {
        FrontApplyState::ParsedWithWaypoints
        | FrontApplyState::NoWaypoints
        | FrontApplyState::Encrypted => (0, None),
        FrontApplyState::ParseError | FrontApplyState::ReadError => {
            let (prior_fingerprint, prior_count) = match prior {
                Some((_, parse_fingerprint, _, attempt_count, _)) => {
                    (parse_fingerprint, attempt_count)
                }
                None => (None, 0),
            };
            let fingerprint_changed = prior_fingerprint.as_deref().is_some_and(|old| {
                new_fingerprint.is_some_and(|new| {
                    parse_fingerprint_to_u64(Some(old)) != parse_fingerprint_to_u64(Some(new))
                })
            });
            let baseline = if fingerprint_changed { 0 } else { prior_count };
            let next_count = baseline.saturating_add(1);
            let next_retry_at = now.saturating_add(front_parse_backoff_secs(next_count));
            (next_count, Some(next_retry_at))
        }
    }
}

fn count_materialized_rows(conn: &Connection, table: &str) -> Result<usize, DbError> {
    let count: i64 = conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0))?;
    Ok(usize::try_from(count).unwrap_or(usize::MAX))
}

/// Accumulated result of applying one clip's angle records under a savepoint.
#[derive(Default)]
struct GroupOutcome {
    failed: bool,
    errors: usize,
    clips_written: usize,
    front_walked: usize,
    waypoints_written: usize,
    front_parse_errors: usize,
    front_read_errors: usize,
    front_no_waypoints: usize,
    dirty: bool,
    new_sentry: bool,
}

/// Apply every angle record for one canonical clip key. On the first
/// malformed/absent/failing record it stops with `failed = true` so the
/// caller rolls back the clip's savepoint and leaves its parse marker
/// unadvanced; other clip groups are unaffected.
fn apply_clip_group(
    tx: &Connection,
    group: &[&ClipAngleRecord],
    batch_complete: bool,
    present: &HashSet<&str>,
    prior_keys: &HashSet<String>,
) -> GroupOutcome {
    let mut out = GroupOutcome::default();
    for &record in group {
        if record.validate().is_err()
            || (batch_complete && !present.contains(record.canonical_key.as_str()))
        {
            out.errors += 1;
            out.failed = true;
            return out;
        }
        let is_front = record.is_front();
        let Ok(outcome) = apply_record(tx, record, is_front) else {
            out.errors += 1;
            out.failed = true;
            return out;
        };
        out.clips_written += 1;
        if record.bucket.as_db_str() == "SentryClips"
            && !prior_keys.contains(record.canonical_key.as_str())
        {
            out.new_sentry = true;
        }
        if is_front {
            out.front_walked += 1;
            out.waypoints_written += outcome.waypoints_written;
            if outcome.waypoints_written > 0 || outcome.waypoints_deleted > 0 {
                out.dirty = true;
            }
            match outcome.front_state {
                Some(FrontApplyState::ParseError) => out.front_parse_errors += 1,
                Some(FrontApplyState::ReadError) => out.front_read_errors += 1,
                Some(FrontApplyState::NoWaypoints) => out.front_no_waypoints += 1,
                _ => {}
            }
        }
    }
    out
}

fn apply_unplaceable_fronts(
    conn: &Connection,
    front_unplaceable: &[FrontUnplaceableRecord],
) -> Result<(), DbError> {
    for unplaceable in front_unplaceable {
        let front_state = match unplaceable.reason.as_str() {
            "read_error" => FrontApplyState::ReadError,
            "encrypted" => FrontApplyState::Encrypted,
            _ => FrontApplyState::ParseError,
        };
        let parse_fingerprint = format!("{:x}", unplaceable.front_fingerprint);
        let prior = load_front_parse_attempt(conn, &unplaceable.canonical_key)?;
        let now = now_epoch_s();
        let (attempt_count, next_retry_at) =
            front_attempt_update(prior, front_state, Some(parse_fingerprint.as_str()), now);
        upsert_front_parse_attempt(
            conn,
            &unplaceable.canonical_key,
            front_state.as_wire(),
            Some(parse_fingerprint.as_str()),
            None,
            attempt_count,
            next_retry_at,
        )?;
    }
    Ok(())
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

/// Ingest one validated record. Front angles are parse-state driven:
/// `parsed_with_waypoints` / `no_waypoints` upsert+replace waypoints,
/// `encrypted` preserves the existing waypoint cache and clip timing fields
/// (`ensure_clip` path), `parse_error` / `read_error` are ensure-only
/// (non-destructive, no waypoint replacement), and all front paths upsert
/// the parse-attempt row and angle. Non-front angles only ensure the clip
/// exists (never downgrading a front-resolved instant) and upsert the
/// angle.
///
/// Errors are surfaced to the caller; the caller owns per-clip savepoint
/// semantics and decides whether to roll back the whole clip group.
fn apply_record(
    conn: &Connection,
    record: &ClipAngleRecord,
    is_front: bool,
) -> Result<ApplyRecordOutcome, DbError> {
    let folder_class = FolderClass::from_db_str(record.bucket.as_db_str());
    let facts = clip_facts(record, folder_class);
    let angle = angle_facts(record, folder_class);
    if is_front {
        let front_state = FrontApplyState::from_wire(record.parse_state.as_deref());
        let derived: Vec<DeriveWaypoint> = record.waypoints.iter().map(map_waypoint).collect();
        let clip_id = match front_state {
            FrontApplyState::ParseError
            | FrontApplyState::ReadError
            | FrontApplyState::Encrypted => ensure_clip(conn, &facts)?,
            FrontApplyState::ParsedWithWaypoints | FrontApplyState::NoWaypoints => {
                upsert_clip(conn, &facts)?
            }
        };
        // Data-integrity invariant: `replace_clip_waypoints` is a
        // DELETE-then-insert, so a destructive clear of prior good GPS may
        // ONLY happen for the explicit `no_waypoints` state. A
        // `parsed_with_waypoints` (or unknown/legacy state that `from_wire`
        // maps here) carrying an EMPTY list is incoherent — never let it
        // wipe the cache; leave the prior waypoints intact.
        let mut waypoints_deleted = 0;
        let waypoints_written = match front_state {
            FrontApplyState::ParsedWithWaypoints => {
                if derived.is_empty() {
                    0
                } else {
                    waypoints_deleted = replace_clip_waypoints(conn, clip_id, &derived)?;
                    derived.len()
                }
            }
            FrontApplyState::NoWaypoints => {
                waypoints_deleted = replace_clip_waypoints(conn, clip_id, &[])?;
                0
            }
            FrontApplyState::Encrypted => 0,
            FrontApplyState::ParseError | FrontApplyState::ReadError => 0,
        };
        let prior_attempt = load_front_parse_attempt(conn, &record.canonical_key)?;
        let now = now_epoch_s();
        let (attempt_count, next_retry_at) = front_attempt_update(
            prior_attempt,
            front_state,
            record.parse_fingerprint.as_deref(),
            now,
        );
        upsert_angle_scan_preserving(conn, clip_id, &angle)?;
        upsert_front_parse_attempt(
            conn,
            &record.canonical_key,
            front_state.as_wire(),
            record.parse_fingerprint.as_deref(),
            record.parser_version,
            attempt_count,
            next_retry_at,
        )?;
        Ok(ApplyRecordOutcome {
            waypoints_written,
            waypoints_deleted,
            front_state: Some(front_state),
        })
    } else {
        let clip_id = ensure_clip(conn, &facts)?;
        upsert_angle_scan_preserving(conn, clip_id, &angle)?;
        Ok(ApplyRecordOutcome {
            waypoints_written: 0,
            waypoints_deleted: 0,
            front_state: None,
        })
    }
}

/// Apply one batch of scanner facts to the catalog in a single transaction.
///
/// The batch is validated at the batch level first (protocol version +
/// gross-size caps); a failure there is fatal (rejects the whole batch).
/// Records are grouped by `canonical_key` and applied under per-clip
/// savepoints; a malformed/failing record rolls back that clip group and
/// leaves its parse marker unadvanced while other groups continue. Media and
/// clip-event loops run outside those savepoints. The prune step runs when
/// the batch is `complete`; derivation rebuild is gated by semantic dirtiness.
///
/// # Errors
///
/// Returns [`ScanError::Batch`] if the batch fails batch-level validation,
/// or [`ScanError::Db`] if a transaction/prune/derive/commit step fails.
#[allow(clippy::too_many_lines)]
pub fn apply(
    conn: &mut Connection,
    batch: &ScanBatch,
    derive_cfg: DeriveConfig,
) -> Result<ApplyReport, ScanError> {
    batch.validate()?;

    let present: HashSet<&str> = batch.present_keys.iter().map(String::as_str).collect();
    let mut report = ApplyReport::default();
    let mut derived_dirty = false;

    let tx = conn.transaction().map_err(DbError::from)?;
    let prior_keys: HashSet<String> = {
        let mut stmt = tx
            .prepare("SELECT canonical_key FROM clips")
            .map_err(DbError::from)?;
        let rows = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(DbError::from)?;
        let mut keys = HashSet::new();
        for row in rows {
            keys.insert(row.map_err(DbError::from)?);
        }
        keys
    };

    let mut grouped: BTreeMap<String, Vec<&ClipAngleRecord>> = BTreeMap::new();
    for record in &batch.records {
        grouped
            .entry(record.canonical_key.clone())
            .or_default()
            .push(record);
    }

    for (_canonical_key, group) in grouped {
        tx.execute("SAVEPOINT clip_group", [])
            .map_err(DbError::from)?;
        let outcome = apply_clip_group(&tx, &group, batch.complete, &present, &prior_keys);
        if outcome.failed {
            tx.execute("ROLLBACK TO clip_group", [])
                .map_err(DbError::from)?;
            tx.execute("RELEASE SAVEPOINT clip_group", [])
                .map_err(DbError::from)?;
            report.record_errors += outcome.errors.max(1);
            continue;
        }
        tx.execute("RELEASE SAVEPOINT clip_group", [])
            .map_err(DbError::from)?;
        report.clips_written += outcome.clips_written;
        report.front_walked += outcome.front_walked;
        report.waypoints += outcome.waypoints_written;
        report.front_parse_errors += outcome.front_parse_errors;
        report.front_read_errors += outcome.front_read_errors;
        report.front_no_waypoints += outcome.front_no_waypoints;
        if outcome.new_sentry || outcome.dirty {
            derived_dirty = true;
        }
    }

    apply_unplaceable_fronts(&tx, &batch.front_unplaceable)?;

    if batch.complete {
        let present_keys: HashSet<String> = batch.present_keys.iter().cloned().collect();
        report.pruned = prune_missing_clips(&tx, &present_keys)?;
        if report.pruned > 0 {
            derived_dirty = true;
        }
        let _ = prune_orphan_front_parse_attempts(&tx, &present_keys)?;
    }

    // MEDIA (p2) inventory. Only a media-aware producer touches the catalog:
    // a batch from an older scannerd has `media_inventory == false`, so we
    // neither upsert nor prune and the existing rows are preserved.
    if batch.media_inventory {
        for media in &batch.media {
            if media.validate().is_err() {
                report.record_errors += 1;
                continue;
            }
            // A complete inventory's present set must contain every emitted
            // media path; an inconsistent record (only reachable over a
            // forged wire) is skipped rather than written.
            if batch.complete && !batch.media_present_paths.contains(&media.rel_path) {
                report.record_errors += 1;
                continue;
            }
            if upsert_media(
                &tx,
                &MediaFacts {
                    partition: media.partition.clone(),
                    rel_path: media.rel_path.clone(),
                    name: media.name.clone(),
                    size_bytes: media.size_bytes,
                    modified: media.modified_local.clone(),
                },
            )
            .is_err()
            {
                report.record_errors += 1;
                continue;
            }
            report.media_written += 1;
        }
        // Prune only when the inventory is also a complete scan: a torn pass
        // could omit a present file and wrongly delete its row.
        if batch.complete {
            let present_paths: HashSet<String> =
                batch.media_present_paths.iter().cloned().collect();
            report.media_pruned = prune_missing_media(&tx, &present_paths)?;
        }
    }

    if batch.clip_events_inventory {
        for ev in &batch.clip_events {
            if ev.validate().is_err() {
                report.record_errors += 1;
                continue;
            }
            let Ok(changed) = upsert_clip_event(
                &tx,
                &ClipEventFacts {
                    event_dir_key: ev.event_dir_key.clone(),
                    bucket: ev.bucket.as_db_str().to_owned(),
                    primary_canonical_key: ev.primary_canonical_key.clone(),
                    timestamp_utc: ev.timestamp_utc,
                    timestamp_local_naive: ev.timestamp_local_naive,
                    timestamp_has_offset: ev.timestamp_has_offset,
                    est_lat: ev.est_lat,
                    est_lon: ev.est_lon,
                    reason: ev.reason.clone(),
                    city: ev.city.clone(),
                    camera: ev.camera.clone(),
                },
            ) else {
                report.record_errors += 1;
                continue;
            };
            report.clip_events_written += changed;
            if changed > 0 {
                derived_dirty = true;
            }
        }
        if batch.complete {
            let present_event_keys: HashSet<String> = batch
                .clip_events
                .iter()
                .map(|e| e.event_dir_key.clone())
                .collect();
            report.clip_events_pruned = prune_missing_clip_events(&tx, &present_event_keys)?;
            if report.clip_events_pruned > 0 {
                derived_dirty = true;
            }
        }
    }

    if derived_dirty {
        let clips = load_derive_clips(&tx)?;
        let clip_events = load_clip_events(&tx)?;
        let derivation = derive(&clips, &clip_events, derive_cfg);
        rebuild_derived(&tx, &derivation)?;
        report.trips = derivation.trips.len();
        let trip_events: usize = derivation.trips.iter().map(|t| t.events.len()).sum();
        report.events = trip_events + derivation.sentry_events.len();
        report.rebuild_ran = true;
    } else {
        report.trips = count_materialized_rows(&tx, "trips")?;
        report.events = count_materialized_rows(&tx, "events")?;
        report.rebuild_ran = false;
    }
    report.derived_dirty = derived_dirty;

    let has_lifecycle_material = count_materialized_rows(&tx, "clips")? > 0
        || report.trips > 0
        || report.events > 0
        || count_materialized_rows(&tx, "front_parse_attempts")? > 0;
    if has_lifecycle_material {
        tx.execute(
            "INSERT INTO index_lifecycle_meta (id, last_derived_at)
                 VALUES (1, ?1)
             ON CONFLICT(id) DO UPDATE
                 SET last_derived_at = MAX(index_lifecycle_meta.last_derived_at, excluded.last_derived_at)",
            [now_epoch_s()],
        )
        .map_err(DbError::from)?;
    } else {
        tx.execute("DELETE FROM index_lifecycle_meta WHERE id = 1", [])
            .map_err(DbError::from)?;
    }
    tx.commit().map_err(DbError::from)?;

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
        AngleRecord, Bucket, ClipAngleRecord, ClipEventRecord, FrontUnplaceableRecord,
        MediaFileRecord, PARSER_VERSION, PROTOCOL_VERSION, ProducerStats, ScanBatch,
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
        front_record_with_state(
            key,
            dir,
            started_at,
            "parsed_with_waypoints",
            waypoints,
            Some(started + 1),
            Some(1.0),
        )
    }

    fn front_record_with_state(
        key: &str,
        dir: &str,
        started_at: i64,
        parse_state: &str,
        waypoints: Vec<scannerd::record::WireWaypoint>,
        ended_at: Option<i64>,
        duration_s: Option<f64>,
    ) -> ClipAngleRecord {
        ClipAngleRecord {
            canonical_key: key.to_owned(),
            started_at,
            ended_at,
            partition: "slot0".to_owned(),
            bucket: Bucket::SavedClips,
            duration_s,
            angle: front_angle("front", dir),
            parse_state: Some(parse_state.to_owned()),
            parse_fingerprint: Some("f00d".to_owned()),
            parser_version: Some(PARSER_VERSION),
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
            parse_state: None,
            parse_fingerprint: None,
            parser_version: None,
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
            front_census: Vec::new(),
            front_unplaceable: Vec::new(),
            records,
            media: Vec::new(),
            media_present_paths: Vec::new(),
            media_inventory: false,
            clip_events: Vec::new(),
            clip_events_inventory: false,
        }
    }

    fn waypoint_count_for_key(conn: &Connection, key: &str) -> i64 {
        conn.query_row(
            "SELECT COUNT(*)
               FROM clip_waypoints w
               JOIN clips c ON c.id = w.clip_id
              WHERE c.canonical_key = ?1",
            [key],
            |r| r.get(0),
        )
        .unwrap()
    }

    fn front_attempt_state(conn: &Connection, key: &str) -> Option<String> {
        conn.query_row(
            "SELECT parse_state FROM front_parse_attempts WHERE canonical_key = ?1",
            [key],
            |r| r.get(0),
        )
        .ok()
    }

    fn front_attempt_count(conn: &Connection, key: &str) -> Option<(i64, Option<i64>)> {
        conn.query_row(
            "SELECT attempt_count, next_retry_at FROM front_parse_attempts WHERE canonical_key = ?1",
            [key],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .ok()
    }

    fn front_attempt_row(
        conn: &Connection,
        key: &str,
    ) -> Option<(String, Option<String>, i64, Option<i64>)> {
        conn.query_row(
            "SELECT parse_state, parse_fingerprint, attempt_count, next_retry_at
               FROM front_parse_attempts
              WHERE canonical_key = ?1",
            [key],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .ok()
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
    fn apply_indexes_non_front_only_clip_without_front_attempt_marker() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let b = batch(
            vec![
                other_record(key, dir, "back", 1_700_000_000),
                other_record(key, dir, "left_repeater", 1_700_000_000),
                other_record(key, dir, "right_repeater", 1_700_000_000),
            ],
            true,
        );
        let report = apply(&mut conn, &b, DeriveConfig::default()).unwrap();
        assert_eq!(report.clips_written, 3);
        assert_eq!(count(&conn, "clips"), 1);
        assert_eq!(count(&conn, "angles"), 3);
        assert_eq!(front_attempt_state(&conn, key), None);
    }

    #[test]
    fn legacy_front_record_without_parse_state_uses_replace_path() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let mut legacy = front_record(key, dir, 1_700_000_000);
        legacy.parse_state = None;
        legacy.parse_fingerprint = None;
        legacy.parser_version = None;
        apply(
            &mut conn,
            &batch(vec![legacy], true),
            DeriveConfig::default(),
        )
        .unwrap();
        assert_eq!(waypoint_count_for_key(&conn, key), 2);
        assert_eq!(
            front_attempt_state(&conn, key).as_deref(),
            Some("parsed_with_waypoints")
        );
    }

    #[test]
    fn parse_error_preserves_existing_waypoints() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        apply(
            &mut conn,
            &batch(vec![front_record(key, dir, 1_700_000_000)], true),
            DeriveConfig::default(),
        )
        .unwrap();
        assert_eq!(waypoint_count_for_key(&conn, key), 2);

        let parse_error = front_record_with_state(
            key,
            dir,
            1_700_000_000,
            "parse_error",
            Vec::new(),
            None,
            None,
        );
        let report = apply(
            &mut conn,
            &batch(vec![parse_error], true),
            DeriveConfig::default(),
        )
        .unwrap();
        assert_eq!(report.front_parse_errors, 1);
        assert_eq!(waypoint_count_for_key(&conn, key), 2);
        assert_eq!(
            front_attempt_state(&conn, key).as_deref(),
            Some("parse_error")
        );
    }

    #[test]
    fn read_error_preserves_existing_waypoints() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        apply(
            &mut conn,
            &batch(vec![front_record(key, dir, 1_700_000_000)], true),
            DeriveConfig::default(),
        )
        .unwrap();
        assert_eq!(waypoint_count_for_key(&conn, key), 2);

        let read_error = front_record_with_state(
            key,
            dir,
            1_700_000_000,
            "read_error",
            Vec::new(),
            None,
            None,
        );
        let report = apply(
            &mut conn,
            &batch(vec![read_error], true),
            DeriveConfig::default(),
        )
        .unwrap();
        assert_eq!(report.front_read_errors, 1);
        assert_eq!(waypoint_count_for_key(&conn, key), 2);
        assert_eq!(
            front_attempt_state(&conn, key).as_deref(),
            Some("read_error")
        );
        let attempt = front_attempt_count(&conn, key).unwrap();
        assert_eq!(attempt.0, 1);
        assert!(attempt.1.is_some());
    }

    #[test]
    fn read_error_attempts_back_off_and_fingerprint_change_resets_counter() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let mut read_error = front_record_with_state(
            key,
            dir,
            1_700_000_000,
            "read_error",
            Vec::new(),
            None,
            None,
        );
        read_error.parse_fingerprint = Some("a".to_owned());
        apply(
            &mut conn,
            &batch(vec![read_error.clone()], true),
            DeriveConfig::default(),
        )
        .unwrap();
        let first = front_attempt_count(&conn, key).unwrap();
        assert_eq!(first.0, 1);
        assert!(first.1.is_some());

        apply(
            &mut conn,
            &batch(vec![read_error.clone()], true),
            DeriveConfig::default(),
        )
        .unwrap();
        let second = front_attempt_count(&conn, key).unwrap();
        assert_eq!(second.0, 2);
        assert!(second.1.is_some());
        assert!(second.1.unwrap_or_default() >= first.1.unwrap_or_default());

        read_error.parse_fingerprint = Some("b".to_owned());
        apply(
            &mut conn,
            &batch(vec![read_error], true),
            DeriveConfig::default(),
        )
        .unwrap();
        let reset = front_attempt_count(&conn, key).unwrap();
        assert_eq!(reset.0, 1);
    }

    #[test]
    fn encrypted_state_is_terminal_and_not_counted_as_parse_or_read_error() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/EncryptedClips/RecentClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/EncryptedClips/RecentClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        apply(
            &mut conn,
            &batch(vec![front_record(key, dir, 1_700_000_000)], true),
            DeriveConfig::default(),
        )
        .unwrap();
        assert_eq!(waypoint_count_for_key(&conn, key), 2);

        let encrypted =
            front_record_with_state(key, dir, 1_700_000_000, "encrypted", Vec::new(), None, None);
        let report = apply(
            &mut conn,
            &batch(vec![encrypted], true),
            DeriveConfig::default(),
        )
        .unwrap();
        assert_eq!(report.front_walked, 1);
        assert_eq!(report.front_parse_errors, 0);
        assert_eq!(report.front_read_errors, 0);
        assert_eq!(report.front_no_waypoints, 0);
        assert_eq!(
            waypoint_count_for_key(&conn, key),
            2,
            "encrypted state must not delete previously decoded waypoints"
        );
        assert_eq!(
            front_attempt_state(&conn, key).as_deref(),
            Some("encrypted")
        );
        assert_eq!(front_attempt_count(&conn, key), Some((0, None)));
    }

    #[test]
    fn unplaceable_parse_error_writes_attempt_without_dirtying_derive() {
        let mut conn = open_in_memory().unwrap();
        let key = "0:TeslaCam/SavedClips/unplaceable/2026-13-99_00-00-00";
        let fingerprint = 0xabc_u64;
        let mut unplaceable_batch = batch(Vec::new(), true);
        unplaceable_batch.present_keys = vec![key.to_owned()];
        unplaceable_batch.front_unplaceable = vec![FrontUnplaceableRecord {
            canonical_key: key.to_owned(),
            front_fingerprint: fingerprint,
            reason: "parse_error".to_owned(),
        }];
        let before = crate::db::now_epoch_s();
        let report = apply(&mut conn, &unplaceable_batch, DeriveConfig::default()).unwrap();
        let after = crate::db::now_epoch_s();

        let row = front_attempt_row(&conn, key).expect("front attempt row");
        assert_eq!(row.0, "parse_error");
        assert_eq!(row.1.as_deref(), Some("abc"));
        assert_eq!(row.2, 1);
        let retry = row.3.expect("next_retry_at");
        assert!(retry >= before + 60);
        assert!(retry <= after + 60);
        assert!(!report.derived_dirty);
    }

    #[test]
    fn repeated_unplaceable_front_advances_backoff() {
        let mut conn = open_in_memory().unwrap();
        let key = "0:TeslaCam/SavedClips/unplaceable/2026-13-99_00-00-00";
        let mut unplaceable_batch = batch(Vec::new(), true);
        unplaceable_batch.present_keys = vec![key.to_owned()];
        unplaceable_batch.front_unplaceable = vec![FrontUnplaceableRecord {
            canonical_key: key.to_owned(),
            front_fingerprint: 0xabc_u64,
            reason: "parse_error".to_owned(),
        }];

        apply(&mut conn, &unplaceable_batch, DeriveConfig::default()).unwrap();
        let before = crate::db::now_epoch_s();
        apply(&mut conn, &unplaceable_batch, DeriveConfig::default()).unwrap();
        let after = crate::db::now_epoch_s();

        let row = front_attempt_row(&conn, key).expect("front attempt row");
        assert_eq!(row.2, 2);
        let retry = row.3.expect("next_retry_at");
        assert!(retry >= before + 240);
        assert!(retry <= after + 240);
    }

    #[test]
    fn unplaceable_front_fingerprint_change_resets_attempt_count() {
        let mut conn = open_in_memory().unwrap();
        let key = "0:TeslaCam/SavedClips/unplaceable/2026-13-99_00-00-00";
        let mut first = batch(Vec::new(), true);
        first.present_keys = vec![key.to_owned()];
        first.front_unplaceable = vec![FrontUnplaceableRecord {
            canonical_key: key.to_owned(),
            front_fingerprint: 0xabc_u64,
            reason: "parse_error".to_owned(),
        }];
        apply(&mut conn, &first, DeriveConfig::default()).unwrap();
        apply(&mut conn, &first, DeriveConfig::default()).unwrap();
        assert_eq!(front_attempt_count(&conn, key).map(|row| row.0), Some(2));

        let mut changed = batch(Vec::new(), true);
        changed.present_keys = vec![key.to_owned()];
        changed.front_unplaceable = vec![FrontUnplaceableRecord {
            canonical_key: key.to_owned(),
            front_fingerprint: 0xdef_u64,
            reason: "parse_error".to_owned(),
        }];
        apply(&mut conn, &changed, DeriveConfig::default()).unwrap();
        let row = front_attempt_row(&conn, key).expect("front attempt row");
        assert_eq!(row.1.as_deref(), Some("def"));
        assert_eq!(row.2, 1);
    }

    #[test]
    fn unplaceable_encrypted_writes_terminal_attempt_without_retry() {
        let mut conn = open_in_memory().unwrap();
        let key = "0:TeslaCam/EncryptedClips/RecentClips/2026-06-01_20-10-35/2026-06-01_20-10-35";
        let mut unplaceable_batch = batch(Vec::new(), true);
        unplaceable_batch.present_keys = vec![key.to_owned()];
        unplaceable_batch.front_unplaceable = vec![FrontUnplaceableRecord {
            canonical_key: key.to_owned(),
            front_fingerprint: 0xabc_u64,
            reason: "encrypted".to_owned(),
        }];
        apply(&mut conn, &unplaceable_batch, DeriveConfig::default()).unwrap();
        let row = front_attempt_row(&conn, key).expect("front attempt row");
        assert_eq!(row.0, "encrypted");
        assert_eq!(row.1.as_deref(), Some("abc"));
        assert_eq!(row.2, 0);
        assert_eq!(row.3, None);
    }

    #[test]
    fn unknown_parse_state_with_empty_waypoints_is_non_destructive() {
        // Finding-1 regression: an unknown/`legacy_unknown` parse_state (which
        // `from_wire` collapses to ParsedWithWaypoints) carrying an EMPTY
        // waypoint list must NEVER delete prior good GPS. Only the explicit
        // `no_waypoints` state may clear the cache.
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        apply(
            &mut conn,
            &batch(vec![front_record(key, dir, 1_700_000_000)], true),
            DeriveConfig::default(),
        )
        .unwrap();
        assert_eq!(waypoint_count_for_key(&conn, key), 2);

        let unknown = front_record_with_state(
            key,
            dir,
            1_700_000_000,
            "legacy_unknown",
            Vec::new(),
            None,
            None,
        );
        apply(
            &mut conn,
            &batch(vec![unknown], true),
            DeriveConfig::default(),
        )
        .unwrap();
        // Prior GPS preserved; not wiped by the incoherent empty record.
        assert_eq!(waypoint_count_for_key(&conn, key), 2);
    }

    #[test]
    fn no_waypoints_clears_existing_waypoints_and_records_attempt() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        apply(
            &mut conn,
            &batch(vec![front_record(key, dir, 1_700_000_000)], true),
            DeriveConfig::default(),
        )
        .unwrap();
        assert_eq!(waypoint_count_for_key(&conn, key), 2);

        let no_waypoints = front_record_with_state(
            key,
            dir,
            1_700_000_000,
            "no_waypoints",
            Vec::new(),
            Some(1_700_000_000),
            Some(0.0),
        );
        let report = apply(
            &mut conn,
            &batch(vec![no_waypoints], true),
            DeriveConfig::default(),
        )
        .unwrap();
        assert_eq!(report.front_no_waypoints, 1);
        assert!(report.derived_dirty);
        assert!(report.rebuild_ran);
        assert_eq!(waypoint_count_for_key(&conn, key), 0);
        assert_eq!(
            front_attempt_state(&conn, key).as_deref(),
            Some("no_waypoints")
        );
    }

    #[test]
    fn no_waypoints_on_new_clip_records_attempt_with_empty_cache() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/new/2026-06-01_20-10-04";
        let no_waypoints = front_record_with_state(
            key,
            dir,
            1_700_000_000,
            "no_waypoints",
            Vec::new(),
            Some(1_700_000_000),
            Some(0.0),
        );
        apply(
            &mut conn,
            &batch(vec![no_waypoints], true),
            DeriveConfig::default(),
        )
        .unwrap();
        assert_eq!(waypoint_count_for_key(&conn, key), 0);
        assert_eq!(
            front_attempt_state(&conn, key).as_deref(),
            Some("no_waypoints")
        );
    }

    #[test]
    fn no_waypoints_removes_stale_trip_when_cached_waypoints_existed() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/trip/2026-06-01_20-10-04";
        let started = 1_700_000_000;
        let moving: Vec<scannerd::record::WireWaypoint> = (0..6_u32)
            .map(|i| {
                wire_waypoint_from_walk(
                    &waypoint(
                        i,
                        f64::from(i) * 1000.0,
                        37.5 + f64::from(i) * 0.001,
                        -122.3,
                    ),
                    started,
                )
            })
            .collect();
        let seeded = front_record_with_state(
            key,
            dir,
            started,
            "parsed_with_waypoints",
            moving,
            Some(started + 5),
            Some(5.0),
        );
        apply(
            &mut conn,
            &batch(vec![seeded], true),
            DeriveConfig::default(),
        )
        .unwrap();
        let trips_before = count(&conn, "trips");
        assert!(trips_before > 0);

        let no_waypoints = front_record_with_state(
            key,
            dir,
            started,
            "no_waypoints",
            Vec::new(),
            Some(started),
            Some(0.0),
        );
        let report = apply(
            &mut conn,
            &batch(vec![no_waypoints], true),
            DeriveConfig::default(),
        )
        .unwrap();
        assert!(report.derived_dirty);
        assert!(report.rebuild_ran);
        assert_eq!(count(&conn, "trip_points"), 0);
        assert_eq!(count(&conn, "trips"), 0);
    }

    #[test]
    fn front_error_states_are_distinctly_counted_in_report() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let parse_key = "0:TeslaCam/SavedClips/a/2026-06-01_20-10-04";
        let read_key = "0:TeslaCam/SavedClips/b/2026-06-01_20-11-04";
        let b = batch(
            vec![
                front_record_with_state(
                    parse_key,
                    dir,
                    1_700_000_000,
                    "parse_error",
                    Vec::new(),
                    None,
                    None,
                ),
                front_record_with_state(
                    read_key,
                    dir,
                    1_700_000_100,
                    "read_error",
                    Vec::new(),
                    None,
                    None,
                ),
            ],
            true,
        );
        let report = apply(&mut conn, &b, DeriveConfig::default()).unwrap();
        assert_eq!(report.front_walked, 2);
        assert_eq!(report.front_parse_errors, 1);
        assert_eq!(report.front_read_errors, 1);
        assert_eq!(report.front_no_waypoints, 0);
    }

    #[test]
    fn parse_error_does_not_downgrade_clip_ended_at_or_duration() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        apply(
            &mut conn,
            &batch(vec![front_record(key, dir, 1_700_000_000)], true),
            DeriveConfig::default(),
        )
        .unwrap();
        let before: (Option<i64>, Option<f64>) = conn
            .query_row(
                "SELECT ended_at, duration_s FROM clips WHERE canonical_key = ?1",
                [key],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();

        let parse_error = front_record_with_state(
            key,
            dir,
            1_700_000_000,
            "parse_error",
            Vec::new(),
            None,
            None,
        );
        apply(
            &mut conn,
            &batch(vec![parse_error], true),
            DeriveConfig::default(),
        )
        .unwrap();
        let after: (Option<i64>, Option<f64>) = conn
            .query_row(
                "SELECT ended_at, duration_s FROM clips WHERE canonical_key = ?1",
                [key],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(after, before);
    }

    fn media_rec(rel_path: &str) -> MediaFileRecord {
        MediaFileRecord {
            partition: "slot1".to_owned(),
            rel_path: rel_path.to_owned(),
            name: rel_path.to_owned(),
            size_bytes: 219_770,
            modified_local: Some("2026-06-01T20:10:04".to_owned()),
        }
    }

    fn media_batch(media: Vec<MediaFileRecord>, inventory: bool, complete: bool) -> ScanBatch {
        let mut b = batch(Vec::new(), complete);
        b.media_present_paths = media.iter().map(|m| m.rel_path.clone()).collect();
        b.media = media;
        b.media_inventory = inventory;
        b
    }

    fn media_row(conn: &Connection, rel_path: &str) -> Option<(String, i64, Option<String>)> {
        conn.query_row(
            "SELECT name, size_bytes, modified FROM media_entries WHERE rel_path = ?1",
            [rel_path],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .ok()
    }

    fn clip_event_rec(
        event_dir_key: &str,
        est_lat: Option<f64>,
        est_lon: Option<f64>,
    ) -> ClipEventRecord {
        ClipEventRecord {
            event_dir_key: event_dir_key.to_owned(),
            bucket: Bucket::SavedClips,
            primary_canonical_key: "slot0:TeslaCam/SavedClips/clip".to_owned(),
            timestamp_utc: 1_700_000_000,
            timestamp_local_naive: 1_700_000_000,
            timestamp_has_offset: false,
            est_lat,
            est_lon,
            reason: Some("sentry".to_owned()),
            city: Some("Seattle".to_owned()),
            camera: Some("front".to_owned()),
        }
    }

    fn clip_event_batch(
        events: Vec<ClipEventRecord>,
        inventory: bool,
        complete: bool,
    ) -> ScanBatch {
        let mut b = batch(Vec::new(), complete);
        b.clip_events = events;
        b.clip_events_inventory = inventory;
        b
    }

    #[test]
    fn apply_upserts_and_prunes_media_when_inventoried() {
        let mut conn = open_in_memory().unwrap();
        let b = media_batch(vec![media_rec("LockChime.wav")], true, true);
        let report = apply(&mut conn, &b, DeriveConfig::default()).unwrap();
        assert_eq!(report.media_written, 1);
        assert_eq!(report.media_pruned, 0);
        let row = media_row(&conn, "LockChime.wav").unwrap();
        assert_eq!(row.0, "LockChime.wav");
        assert_eq!(row.1, 219_770);
        assert_eq!(row.2.as_deref(), Some("2026-06-01T20:10:04"));

        // A later complete inventory with the chime GONE prunes the row.
        let empty = media_batch(Vec::new(), true, true);
        let report2 = apply(&mut conn, &empty, DeriveConfig::default()).unwrap();
        assert_eq!(report2.media_pruned, 1);
        assert_eq!(count(&conn, "media_entries"), 0);
    }

    #[test]
    fn apply_idempotent_media_no_duplicate_rows() {
        let mut conn = open_in_memory().unwrap();
        let b = media_batch(vec![media_rec("LockChime.wav")], true, true);
        apply(&mut conn, &b, DeriveConfig::default()).unwrap();
        apply(&mut conn, &b, DeriveConfig::default()).unwrap();
        assert_eq!(count(&conn, "media_entries"), 1);
    }

    #[test]
    fn media_unaware_batch_never_touches_catalog() {
        let mut conn = open_in_memory().unwrap();
        // First, a media-aware pass installs a row.
        let installed = media_batch(vec![media_rec("LockChime.wav")], true, true);
        apply(&mut conn, &installed, DeriveConfig::default()).unwrap();
        assert_eq!(count(&conn, "media_entries"), 1);

        // Then a pass from an OLD (media-unaware) scannerd: empty media,
        // inventory=false, complete=true. It must NOT prune the row.
        let old = media_batch(Vec::new(), false, true);
        let report = apply(&mut conn, &old, DeriveConfig::default()).unwrap();
        assert_eq!(report.media_written, 0);
        assert_eq!(report.media_pruned, 0);
        assert_eq!(count(&conn, "media_entries"), 1);
    }

    #[test]
    fn incomplete_media_inventory_upserts_but_does_not_prune() {
        let mut conn = open_in_memory().unwrap();
        let installed = media_batch(vec![media_rec("LockChime.wav")], true, true);
        apply(&mut conn, &installed, DeriveConfig::default()).unwrap();

        // A torn (incomplete) pass that no longer sees the chime must keep
        // the row — prune is gated on `complete`.
        let torn = media_batch(Vec::new(), true, false);
        let report = apply(&mut conn, &torn, DeriveConfig::default()).unwrap();
        assert_eq!(report.media_pruned, 0);
        assert_eq!(count(&conn, "media_entries"), 1);
    }

    #[test]
    fn apply_persists_clip_events() {
        let mut conn = open_in_memory().unwrap();
        let event_key = "slot0:TeslaCam/SavedClips/2026-06-01_20-10-04";
        let batch = clip_event_batch(
            vec![clip_event_rec(event_key, Some(47.6), Some(-122.3))],
            true,
            true,
        );
        let report = apply(&mut conn, &batch, DeriveConfig::default()).unwrap();
        assert_eq!(report.clip_events_written, 1);
        assert!(report.derived_dirty);
        assert!(report.rebuild_ran);
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM clip_events", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);
        let est_lat: Option<f64> = conn
            .query_row(
                "SELECT est_lat FROM clip_events WHERE event_dir_key = ?1",
                [event_key],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(est_lat, Some(47.6));
    }

    #[test]
    fn apply_prunes_vanished_clip_events() {
        let mut conn = open_in_memory().unwrap();
        let event_key = "slot0:TeslaCam/SavedClips/2026-06-01_20-10-04";
        let seed = clip_event_batch(vec![clip_event_rec(event_key, None, None)], true, true);
        apply(&mut conn, &seed, DeriveConfig::default()).unwrap();
        assert_eq!(count(&conn, "clip_events"), 1);

        let next = clip_event_batch(Vec::new(), true, true);
        let report = apply(&mut conn, &next, DeriveConfig::default()).unwrap();
        assert_eq!(report.clip_events_pruned, 1);
        assert_eq!(count(&conn, "clip_events"), 0);
    }

    #[test]
    fn apply_without_clip_events_inventory_preserves_rows() {
        let mut conn = open_in_memory().unwrap();
        let event_key = "slot0:TeslaCam/SavedClips/2026-06-01_20-10-04";
        let seed = clip_event_batch(vec![clip_event_rec(event_key, None, None)], true, true);
        apply(&mut conn, &seed, DeriveConfig::default()).unwrap();
        assert_eq!(count(&conn, "clip_events"), 1);

        let unaware = clip_event_batch(Vec::new(), false, true);
        let report = apply(&mut conn, &unaware, DeriveConfig::default()).unwrap();
        assert_eq!(report.clip_events_written, 0);
        assert_eq!(report.clip_events_pruned, 0);
        assert_eq!(count(&conn, "clip_events"), 1);
    }

    #[test]
    fn unchanged_clip_event_batch_skips_rebuild_but_semantic_change_rebuilds() {
        let mut conn = open_in_memory().unwrap();
        let event_key = "slot0:TeslaCam/SavedClips/2026-06-01_20-10-04";
        let base = clip_event_batch(
            vec![clip_event_rec(event_key, Some(47.6), Some(-122.3))],
            true,
            true,
        );
        let first = apply(&mut conn, &base, DeriveConfig::default()).unwrap();
        assert!(first.rebuild_ran);
        assert!(first.derived_dirty);
        let second = apply(&mut conn, &base, DeriveConfig::default()).unwrap();
        assert_eq!(second.clip_events_written, 0);
        assert!(!second.derived_dirty);
        assert!(!second.rebuild_ran);

        let mut changed_event = clip_event_rec(event_key, Some(47.6), Some(-122.3));
        changed_event.city = Some("Tacoma".to_owned());
        let changed = clip_event_batch(vec![changed_event], true, true);
        let third = apply(&mut conn, &changed, DeriveConfig::default()).unwrap();
        assert_eq!(third.clip_events_written, 1);
        assert!(third.derived_dirty);
        assert!(third.rebuild_ran);
    }

    #[test]
    fn no_change_pass_advances_lifecycle_freshness_marker() {
        let mut conn = open_in_memory().unwrap();
        let event_key = "slot0:TeslaCam/SavedClips/2026-06-01_20-10-04";
        let base = clip_event_batch(
            vec![clip_event_rec(event_key, Some(47.6), Some(-122.3))],
            true,
            true,
        );
        let first = apply(&mut conn, &base, DeriveConfig::default()).unwrap();
        assert!(first.rebuild_ran);
        conn.execute(
            "INSERT OR REPLACE INTO index_lifecycle_meta (id, last_derived_at) VALUES (1, 0)",
            [],
        )
        .unwrap();

        let second = apply(&mut conn, &base, DeriveConfig::default()).unwrap();
        assert!(!second.derived_dirty);
        assert!(!second.rebuild_ran);
        let marker: i64 = conn
            .query_row(
                "SELECT last_derived_at FROM index_lifecycle_meta WHERE id = 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(marker > 0);
    }

    #[test]
    fn empty_complete_pass_clears_lifecycle_freshness_marker() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key = "0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let seeded = batch(vec![front_record(key, dir, 1_700_000_000)], true);
        apply(&mut conn, &seeded, DeriveConfig::default()).unwrap();

        let marker_before: i64 = conn
            .query_row("SELECT COUNT(*) FROM index_lifecycle_meta WHERE id = 1", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(marker_before, 1);

        let empty = batch(Vec::new(), true);
        apply(&mut conn, &empty, DeriveConfig::default()).unwrap();
        let marker_after: i64 = conn
            .query_row("SELECT COUNT(*) FROM index_lifecycle_meta WHERE id = 1", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(marker_after, 0);
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
    fn front_parse_attempts_prune_only_on_complete_batches() {
        let mut conn = open_in_memory().unwrap();
        let dir = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        let key_a = "0:TeslaCam/SavedClips/a/2026-06-01_20-10-04";
        let key_b = "0:TeslaCam/SavedClips/b/2026-06-01_20-11-04";

        let seed = batch(
            vec![
                front_record(key_a, dir, 1_700_000_000),
                front_record(key_b, dir, 1_700_000_100),
            ],
            true,
        );
        apply(&mut conn, &seed, DeriveConfig::default()).unwrap();
        assert_eq!(count(&conn, "front_parse_attempts"), 2);

        let incomplete = batch(vec![front_record(key_a, dir, 1_700_000_000)], false);
        apply(&mut conn, &incomplete, DeriveConfig::default()).unwrap();
        assert_eq!(count(&conn, "front_parse_attempts"), 2);

        let complete = batch(vec![front_record(key_a, dir, 1_700_000_000)], true);
        apply(&mut conn, &complete, DeriveConfig::default()).unwrap();
        assert_eq!(count(&conn, "front_parse_attempts"), 1);
        assert_eq!(
            front_attempt_state(&conn, key_a).as_deref(),
            Some("parsed_with_waypoints")
        );
        assert_eq!(front_attempt_state(&conn, key_b), None);
    }

    #[test]
    fn malformed_record_rolls_back_clip_group_but_other_groups_and_sidecars_commit() {
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

        let mut b = batch(
            vec![
                front_record(good, dir, 1_700_000_000),
                front_record(bad, dir, 1_700_000_100),
                bad_record,
            ],
            true,
        );
        b.media_inventory = true;
        b.media = vec![media_rec("LockChime.wav")];
        b.media_present_paths = vec!["LockChime.wav".to_owned()];
        b.clip_events_inventory = true;
        b.clip_events = vec![clip_event_rec(
            "slot0:TeslaCam/SavedClips/2026-06-01_20-10-04",
            Some(47.6),
            Some(-122.3),
        )];
        let report = apply(&mut conn, &b, DeriveConfig::default()).unwrap();
        assert_eq!(report.record_errors, 1);
        assert_eq!(report.clips_written, 1);
        // Good group committed; bad group rolled back in full.
        assert_eq!(count(&conn, "clips"), 1);
        assert_eq!(front_attempt_state(&conn, bad), None);
        // Batch-level sidecars still apply outside per-clip savepoints.
        assert_eq!(count(&conn, "media_entries"), 1);
        assert_eq!(count(&conn, "clip_events"), 1);
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
