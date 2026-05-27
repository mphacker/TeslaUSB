//! Trip + event materialiser — Phase O (ADR-0019).
//!
//! Turns the raw `clips` + `waypoints` rows the indexer writes
//! into denormalised `trips`, `clip_trip_map`, and
//! `detected_events` rows the web layer reads with small
//! targeted SQL.
//!
//! ## Why this exists
//!
//! v1's mapping page rendered in well under a second because
//! every endpoint ran one small `SELECT … FROM trips …`. B-1
//! initially derived the same data in Python on every request,
//! and the cost was 3–15 s per call × 7 parallel calls per
//! page = 15–35 s first paint. ADR-0019 documents the pivot
//! back to v1's materialised model. This module is the
//! materialisation step that the indexer runs.
//!
//! ## Strategy
//!
//! [`Materializer::rebuild_all`] is the **only** write path.
//! It does a full rebuild from clips + waypoints inside a
//! single SQLite transaction:
//!
//! 1. `DELETE FROM trips` (cascades `clip_trip_map` and
//!    `detected_events` via the schema-v3 ON DELETE CASCADE).
//! 2. Stream non-sentry clips ordered by `clip_started_utc`
//!    and group them into trips by `gap_seconds`.
//! 3. For each trip, INSERT one `trips` row, INSERT one
//!    `clip_trip_map` row per member clip, derive events from
//!    that trip's waypoints, INSERT them.
//! 4. Emit a degenerate per-clip trip for sentry recordings
//!    so the events panel can surface them with `clip_id` set.
//!
//! Why full rebuild instead of incremental:
//!
//! * **Correctness over speed.** A new clip in the middle of
//!   the day can split an existing trip or merge two. An
//!   incremental update would need to find the affected
//!   neighbourhood, undo the old derivation, redo the new one
//!   — three or four code paths each with their own subtle
//!   off-by-one. Full rebuild has one code path.
//! * **Cost.** Rebuild on the operator's 1 369-clip,
//!   95 089-waypoint Pi is bounded by a handful of streaming
//!   SQL scans (no Python derivation). Measured: well under
//!   the supervisor's 5-minute tick budget.
//! * **Recovery.** If anything inside the rebuild transaction
//!   fails, SQLite rolls back the whole thing and the old
//!   derived rows survive — the page never observes a
//!   half-derived state.
//!
//! A `trips_dirty` flag in `meta` lets the supervisor coalesce
//! bursts (Tesla writes 4 cameras per minute → 4
//! `record_clip` calls → 1 rebuild on the next tick) so the
//! amortised cost is one rebuild per supervisor tick, not one
//! per clip insert.
//!
//! ## Event-detection thresholds
//!
//! Verbatim from ADR-0017 §Decision (carried into ADR-0019):
//!
//! | Event             | Condition                              | Severity |
//! |-------------------|----------------------------------------|----------|
//! | speed_limit_exceeded | `speed_mps > 35.76` (80 mph)        | warning  |
//! | hard_acceleration    | `acceleration_x > 3.5`              | warning  |
//! | harsh_braking        | `acceleration_x < -4.0`             | warning  |
//! | emergency_braking    | `acceleration_x < -7.0` (supersedes)| critical |
//! | sharp_turn           | `|acceleration_y| > 4.0`            | warning  |
//! | autopilot_engaged/_disengaged | autopilot transitions       | info     |
//! | sentry               | `bucket='sentry' AND gps_waypoint_count=0` | info |

// "SEI", "GPS", "MP4", "SQLite", "UTC" — domain terms.
#![allow(clippy::doc_markdown)]

use rusqlite::{Connection, OptionalExtension, Transaction, params};
use thiserror::Error;
use tracing::{debug, info};

/// Default trip-grouping gap. Clips in the same non-sentry
/// bucket whose `clip_started_utc` are within this window are
/// merged into one trip. 5 minutes matches v1's
/// `MAPPING_TRIP_GAP_SECONDS` default.
pub const DEFAULT_TRIP_GAP_SECONDS: i64 = 300;

/// Minimum trip distance to materialise (km). Below this the
/// clip cluster is almost certainly a parking-lot blip / SEI
/// noise and would clutter the day card. Matches v1's
/// `MAPPING_TRIP_MIN_DISTANCE_KM`.
///
/// Below-threshold clip clusters are NOT materialised as
/// trips. Their events still surface via the sentry path if
/// they sit in `SentryClips`, otherwise they are silently
/// dropped — same behaviour as v1.
pub const DEFAULT_TRIP_MIN_DISTANCE_KM: f64 = 0.05;

/// Speed threshold (m/s) above which a waypoint emits a
/// `speed-limit` event. 35.76 m/s = 80 mph (the US absolute
/// upper bound for the speed-limit indicator on most signs).
pub const SPEED_LIMIT_MPS: f64 = 35.76;

/// `acceleration_x` threshold for a `hard-accel` event.
pub const HARD_ACCEL_X: f64 = 3.5;

/// `acceleration_x` threshold for a `harsh-brake` event.
/// Stored as a negated value to read naturally in code.
pub const HARSH_BRAKE_X: f64 = -4.0;

/// `acceleration_x` threshold for an `emergency-brake` event.
/// A waypoint that triggers emergency-brake does NOT also
/// trigger harsh-brake — the more severe wins.
pub const EMERGENCY_BRAKE_X: f64 = -7.0;

/// `|acceleration_y|` threshold for a `sharp-turn` event.
pub const SHARP_TURN_ABS_Y: f64 = 4.0;

/// Meta-table key used as the "trips need rebuild" sentinel.
/// The supervisor checks this on every tick and calls
/// [`Materializer::rebuild_all`] when it is `"1"`.
pub const META_KEY_TRIPS_DIRTY: &str = "trips_dirty";

/// Errors emitted by the materialiser. Per-clip data quirks
/// (NULL `clip_started_utc`, single-row trips, sentry-with-GPS)
/// are NOT errors — they are absorbed silently or skipped.
#[derive(Debug, Error)]
pub enum MaterializerError {
    /// Underlying SQLite error.
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
}

/// Result alias for materialiser operations.
pub type Result<T> = std::result::Result<T, MaterializerError>;

/// Summary returned by [`Materializer::rebuild_all`]. Surfaces
/// in supervisor logs and tests.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RebuildStats {
    /// Clips considered (every row in `clips`).
    pub clips_seen: u32,
    /// Trips written to the `trips` table.
    pub trips_written: u32,
    /// Detected events written to `detected_events`.
    pub events_written: u32,
    /// Clip clusters skipped because they were below
    /// [`DEFAULT_TRIP_MIN_DISTANCE_KM`].
    pub trips_skipped_short: u32,
}

/// Trip-and-event materialiser. Owns no state — every call
/// reads the DB. Cheap to construct per call.
pub struct Materializer {
    gap_seconds: i64,
    min_distance_km: f64,
    /// Speed-limit threshold in m/s. `0.0` (or any non-positive
    /// value) **disables** speed-limit event emission entirely.
    /// Live-edited via `/var/lib/teslausb/mapping_settings.json`
    /// — see [`crate::mapping_overrides`].
    speed_limit_mps: f64,
}

impl Default for Materializer {
    fn default() -> Self {
        Self {
            gap_seconds: DEFAULT_TRIP_GAP_SECONDS,
            min_distance_km: DEFAULT_TRIP_MIN_DISTANCE_KM,
            speed_limit_mps: SPEED_LIMIT_MPS,
        }
    }
}

impl Materializer {
    /// Build with custom thresholds. Used by tests; production
    /// uses [`Materializer::from_overrides`].
    #[must_use]
    pub const fn new(gap_seconds: i64, min_distance_km: f64) -> Self {
        Self {
            gap_seconds,
            min_distance_km,
            speed_limit_mps: SPEED_LIMIT_MPS,
        }
    }

    /// Build from the live JSON-backed overrides snapshot. This
    /// is the production constructor — the supervisor calls
    /// [`crate::mapping_overrides::MappingOverridesReader::load`]
    /// once per rebuild and hands the snapshot in here.
    #[must_use]
    pub fn from_overrides(overrides: &crate::mapping_overrides::MappingOverrides) -> Self {
        Self {
            gap_seconds: overrides.trip_gap_seconds,
            min_distance_km: DEFAULT_TRIP_MIN_DISTANCE_KM,
            speed_limit_mps: overrides.speed_limit_mps,
        }
    }

    /// Rebuild `trips`, `clip_trip_map`, and `detected_events`
    /// from scratch using the current `clips` + `waypoints`
    /// rows. Runs inside a single transaction; on error
    /// SQLite rolls back and the previous derived rows remain
    /// intact.
    ///
    /// Clears the `trips_dirty` meta flag on successful
    /// commit.
    ///
    /// # Errors
    ///
    /// Returns `Err` on any SQLite error during the rebuild
    /// transaction.
    pub fn rebuild_all(&self, conn: &mut Connection) -> Result<RebuildStats> {
        let mut stats = RebuildStats::default();
        let tx = conn.transaction()?;

        // Cascade clears everything derived. Schema v3's
        // ON DELETE CASCADE handles clip_trip_map +
        // detected_events for us.
        tx.execute("DELETE FROM trips", [])?;
        // detected_events with trip_id IS NULL (sentry singletons
        // we may emit below) are not reachable through the
        // cascade, so wipe them explicitly here too.
        tx.execute("DELETE FROM detected_events WHERE trip_id IS NULL", [])?;
        tx.execute("DELETE FROM clip_trip_map", [])?;

        let clips = load_clip_rows(&tx)?;
        stats.clips_seen = u32::try_from(clips.len()).unwrap_or(u32::MAX);

        let (driving, sentry): (Vec<ClipRow>, Vec<ClipRow>) =
            clips.into_iter().partition(|c| c.bucket != "sentry");

        for cluster in cluster_into_trips(driving, self.gap_seconds) {
            self.materialize_driving_trip(&tx, &cluster, &mut stats)?;
        }
        for clip in sentry {
            materialize_sentry_clip(&tx, &clip, &mut stats)?;
        }

        // Clear the dirty flag in the same transaction so we
        // never observe "rebuilt but still flagged dirty".
        tx.execute(
            "INSERT INTO meta (key, value) VALUES (?1, '0')
             ON CONFLICT(key) DO UPDATE SET value = '0'",
            params![META_KEY_TRIPS_DIRTY],
        )?;

        tx.commit()?;

        info!(
            clips_seen = stats.clips_seen,
            trips_written = stats.trips_written,
            events_written = stats.events_written,
            trips_skipped_short = stats.trips_skipped_short,
            "materializer rebuild complete",
        );
        Ok(stats)
    }

    fn materialize_driving_trip(
        &self,
        tx: &Transaction<'_>,
        cluster: &[ClipRow],
        stats: &mut RebuildStats,
    ) -> Result<()> {
        if cluster.is_empty() {
            return Ok(());
        }
        // Pull all waypoints for the cluster in one query so
        // we can compute distance + derive events without
        // round-tripping per clip.
        let clip_ids: Vec<i64> = cluster.iter().map(|c| c.id).collect();
        let waypoints = load_waypoints_for_clips(tx, &clip_ids)?;

        let distance_km = total_distance_km(&waypoints);
        if distance_km < self.min_distance_km {
            stats.trips_skipped_short += 1;
            debug!(
                clip_count = cluster.len(),
                distance_km, "trip cluster below distance threshold; skipping"
            );
            return Ok(());
        }

        let (start_lat, start_lon, end_lat, end_lon) = endpoint_coords(&waypoints);
        let start_utc = cluster
            .iter()
            .filter_map(|c| c.clip_started_utc)
            .min()
            .unwrap_or(0);
        // end_utc is the *latest* clip start + its frame span.
        // Without a clean clip_duration column we approximate
        // by the last waypoint's absolute timestamp; if absent,
        // fall back to the last clip's start.
        let end_utc = waypoints
            .last()
            .and_then(|w| w.absolute_utc)
            .or_else(|| cluster.iter().filter_map(|c| c.clip_started_utc).max())
            .unwrap_or(start_utc);
        let duration = (end_utc - start_utc).max(0);
        let waypoint_count = u32::try_from(waypoints.len()).unwrap_or(u32::MAX);
        let video_count = u32::try_from(cluster.len()).unwrap_or(u32::MAX);
        let bucket = cluster.first().map_or("recent", |c| c.bucket.as_str());

        let trip_id: i64 = tx.query_row(
            "INSERT INTO trips (
                start_utc, end_utc, start_clip_id, end_clip_id,
                start_lat, start_lon, end_lat, end_lon,
                distance_km, duration_seconds,
                waypoint_count, event_count, video_count, bucket
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, 0, ?12, ?13)
             RETURNING id",
            params![
                start_utc,
                end_utc,
                cluster.first().map(|c| c.id),
                cluster.last().map(|c| c.id),
                start_lat,
                start_lon,
                end_lat,
                end_lon,
                distance_km,
                duration,
                waypoint_count,
                video_count,
                bucket,
            ],
            |r| r.get(0),
        )?;
        stats.trips_written += 1;

        {
            let mut map_stmt =
                tx.prepare("INSERT INTO clip_trip_map (clip_id, trip_id) VALUES (?1, ?2)")?;
            for c in cluster {
                map_stmt.execute(params![c.id, trip_id])?;
            }
        }

        let events = derive_events(trip_id, &waypoints, self.speed_limit_mps);
        let event_count = u32::try_from(events.len()).unwrap_or(u32::MAX);
        {
            let mut ev_stmt = tx.prepare(
                "INSERT INTO detected_events (
                    trip_id, clip_id, event_type, severity, timestamp_utc,
                    latitude_deg, longitude_deg, speed_mps, metadata_json,
                    description, frame_index
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            )?;
            for ev in &events {
                ev_stmt.execute(params![
                    ev.trip_id,
                    ev.clip_id,
                    ev.event_type,
                    ev.severity,
                    ev.timestamp_utc,
                    ev.latitude_deg,
                    ev.longitude_deg,
                    ev.speed_mps,
                    ev.metadata_json,
                    ev.description,
                    ev.frame_index,
                ])?;
            }
        }
        stats.events_written += event_count;

        tx.execute(
            "UPDATE trips SET event_count = ?1 WHERE id = ?2",
            params![event_count, trip_id],
        )?;
        Ok(())
    }
}

/// Mark the worker DB as having pending trip/event work. The
/// supervisor's next tick will call [`Materializer::rebuild_all`].
/// Cheap UPSERT into `meta`.
///
/// # Errors
///
/// Returns `Err` on a SQLite error.
pub fn mark_trips_dirty(conn: &Connection) -> Result<()> {
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?1, '1')
         ON CONFLICT(key) DO UPDATE SET value = '1'",
        params![META_KEY_TRIPS_DIRTY],
    )?;
    Ok(())
}

/// Returns whether [`META_KEY_TRIPS_DIRTY`] is set. Treats a
/// missing key as `false` so a brand-new DB doesn't trigger a
/// spurious rebuild before any clip lands.
///
/// # Errors
///
/// Returns `Err` on a SQLite error.
pub fn trips_dirty(conn: &Connection) -> Result<bool> {
    let v: Option<String> = conn
        .query_row(
            "SELECT value FROM meta WHERE key = ?1",
            params![META_KEY_TRIPS_DIRTY],
            |r| r.get(0),
        )
        .optional()?;
    Ok(matches!(v.as_deref(), Some("1")))
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct ClipRow {
    id: i64,
    bucket: String,
    clip_started_utc: Option<i64>,
    gps_waypoint_count: i64,
}

#[derive(Debug, Clone)]
struct WaypointRow {
    clip_id: i64,
    /// Frame index within the clip (matches `waypoints.frame_index`).
    /// Carried through derive_events to the detected_events row so
    /// the player can seek straight to the SEI frame.
    frame_index: i64,
    /// Frame-relative milliseconds — used to derive
    /// `absolute_utc` and kept for future per-waypoint
    /// timestamps in the events payload.
    #[allow(dead_code)]
    timestamp_ms: f64,
    /// Approximate absolute UTC seconds for the waypoint
    /// (clip start + timestamp_ms / 1000). Populated when the
    /// clip's `clip_started_utc` was known. Used as the event
    /// timestamp and the trip end_utc.
    absolute_utc: Option<i64>,
    latitude_deg: f64,
    longitude_deg: f64,
    speed_mps: f64,
    acceleration_x: Option<f64>,
    acceleration_y: Option<f64>,
    autopilot_state: Option<String>,
}

#[derive(Debug, Clone)]
struct DerivedEvent {
    trip_id: Option<i64>,
    clip_id: Option<i64>,
    event_type: &'static str,
    severity: &'static str,
    timestamp_utc: i64,
    latitude_deg: Option<f64>,
    longitude_deg: Option<f64>,
    speed_mps: Option<f64>,
    metadata_json: Option<String>,
    /// Human-readable description in the same format the v1 web
    /// layer surfaced. Stored on the row so the Python query
    /// layer never has to re-derive it.
    description: String,
    /// SEI frame index the event was derived from (NULL for
    /// sentry-clip events, which don't have a waypoint).
    frame_index: Option<i64>,
}

fn load_clip_rows(tx: &Transaction<'_>) -> Result<Vec<ClipRow>> {
    // Front-only + non-NULL start guard. Belt-and-braces against
    // any historical or future row that slips past the indexer's
    // front filter: non-front rows replay the same physical path
    // per camera and inflate distance ~6-11×, and NULL
    // start_utc rows collapse into one fake mega-segment at the
    // head of the waypoint sort order. The indexer is the
    // primary gatekeeper (see `pick_canonical_clip` and
    // `walk_and_record`); this WHERE clause is the last line
    // of defense.
    let mut stmt = tx.prepare(
        "SELECT id, bucket, clip_started_utc, gps_waypoint_count
         FROM clips
         WHERE clip_started_utc IS NOT NULL
           AND relative_path NOT LIKE '%-back.mp4'
           AND relative_path NOT LIKE '%-left_repeater.mp4'
           AND relative_path NOT LIKE '%-right_repeater.mp4'
           AND relative_path NOT LIKE '%-left_pillar.mp4'
           AND relative_path NOT LIKE '%-right_pillar.mp4'
         ORDER BY clip_started_utc ASC, id ASC",
    )?;
    let rows = stmt.query_map([], |r| {
        Ok(ClipRow {
            id: r.get(0)?,
            bucket: r.get(1)?,
            clip_started_utc: r.get(2)?,
            gps_waypoint_count: r.get(3)?,
        })
    })?;
    let v = rows.collect::<std::result::Result<Vec<_>, _>>()?;
    Ok(v)
}

fn load_waypoints_for_clips(tx: &Transaction<'_>, clip_ids: &[i64]) -> Result<Vec<WaypointRow>> {
    if clip_ids.is_empty() {
        return Ok(Vec::new());
    }
    // Build a `(?1, ?2, ?3, …)` placeholder list. `clip_ids`
    // is bounded by the number of clips in one trip cluster
    // (low tens in steady state).
    let placeholders = (1..=clip_ids.len())
        .map(|i| format!("?{i}"))
        .collect::<Vec<_>>()
        .join(",");
    let sql = format!(
        "SELECT w.clip_id, w.frame_index, w.timestamp_ms,
                w.latitude_deg, w.longitude_deg, w.speed_mps,
                w.acceleration_x, w.acceleration_y, w.autopilot_state,
                c.clip_started_utc
         FROM waypoints w
         JOIN clips c ON c.id = w.clip_id
         WHERE w.clip_id IN ({placeholders})
         ORDER BY c.clip_started_utc ASC, w.clip_id ASC, w.timestamp_ms ASC"
    );
    let mut stmt = tx.prepare(&sql)?;
    let params: Vec<&dyn rusqlite::ToSql> = clip_ids
        .iter()
        .map(|id| id as &dyn rusqlite::ToSql)
        .collect();
    let rows = stmt.query_map(params.as_slice(), |r| {
        let timestamp_ms: f64 = r.get(2)?;
        let clip_started_utc: Option<i64> = r.get(9)?;
        let absolute_utc = clip_started_utc.map(|s| {
            // Saturating cast — timestamp_ms is bounded by
            // clip length (max ~150 s).
            #[allow(clippy::cast_possible_truncation)]
            let secs = (timestamp_ms / 1000.0) as i64;
            s + secs
        });
        Ok(WaypointRow {
            clip_id: r.get(0)?,
            frame_index: r.get(1)?,
            timestamp_ms,
            absolute_utc,
            latitude_deg: r.get(3)?,
            longitude_deg: r.get(4)?,
            speed_mps: r.get(5)?,
            acceleration_x: r.get(6)?,
            acceleration_y: r.get(7)?,
            autopilot_state: r.get(8)?,
        })
    })?;
    let v = rows.collect::<std::result::Result<Vec<_>, _>>()?;
    Ok(v)
}

/// Group clips into trip clusters by the `gap_seconds`
/// rule. Clips with NULL `clip_started_utc` are kept in
/// their order (they sorted first by COALESCE in the SQL)
/// and treated as one degenerate cluster — better than
/// dropping them.
fn cluster_into_trips(clips: Vec<ClipRow>, gap_seconds: i64) -> Vec<Vec<ClipRow>> {
    let mut out: Vec<Vec<ClipRow>> = Vec::new();
    let mut current: Vec<ClipRow> = Vec::new();
    let mut last_ts: Option<i64> = None;
    for clip in clips {
        let ts = clip.clip_started_utc;
        let split = match (last_ts, ts) {
            (Some(prev), Some(now)) => (now - prev) > gap_seconds,
            // Mixed NULL ↔ Some boundaries split, so NULL-clip
            // clusters don't contaminate timestamped trips.
            (Some(_), None) | (None, Some(_)) => true,
            (None, None) => false,
        };
        if split && !current.is_empty() {
            out.push(std::mem::take(&mut current));
        }
        last_ts = ts;
        current.push(clip);
    }
    if !current.is_empty() {
        out.push(current);
    }
    out
}

/// Haversine distance in km between two lat/lon pairs.
fn haversine_km(a_lat: f64, a_lon: f64, b_lat: f64, b_lon: f64) -> f64 {
    const EARTH_KM: f64 = 6371.0088;
    let dlat = (b_lat - a_lat).to_radians();
    let dlon = (b_lon - a_lon).to_radians();
    let a = (dlat / 2.0).sin().powi(2)
        + a_lat.to_radians().cos() * b_lat.to_radians().cos() * (dlon / 2.0).sin().powi(2);
    let c = 2.0 * a.sqrt().asin();
    EARTH_KM * c
}

fn total_distance_km(waypoints: &[WaypointRow]) -> f64 {
    let mut sum = 0.0;
    let mut prev: Option<&WaypointRow> = None;
    for w in waypoints {
        if !w.latitude_deg.is_finite() || !w.longitude_deg.is_finite() {
            continue;
        }
        if w.latitude_deg == 0.0 && w.longitude_deg == 0.0 {
            continue;
        }
        if let Some(p) = prev {
            sum += haversine_km(
                p.latitude_deg,
                p.longitude_deg,
                w.latitude_deg,
                w.longitude_deg,
            );
        }
        prev = Some(w);
    }
    sum
}

fn endpoint_coords(
    waypoints: &[WaypointRow],
) -> (Option<f64>, Option<f64>, Option<f64>, Option<f64>) {
    let valid = |w: &&WaypointRow| {
        w.latitude_deg.is_finite()
            && w.longitude_deg.is_finite()
            && !(w.latitude_deg == 0.0 && w.longitude_deg == 0.0)
    };
    let first = waypoints.iter().find(valid);
    let last = waypoints.iter().rev().find(valid);
    (
        first.map(|w| w.latitude_deg),
        first.map(|w| w.longitude_deg),
        last.map(|w| w.latitude_deg),
        last.map(|w| w.longitude_deg),
    )
}

/// Derive the per-waypoint event list for one trip. Pure
/// function — no DB access, all decisions made from the
/// `waypoints` slice. Order of returned events matches the
/// input order so timestamps stay monotonic.
///
/// Event types and description format match v1's web layer
/// verbatim (see `mapping_event_derivation.py`) so the
/// /api/events response shape is unchanged when the Python
/// query layer switches to reading these rows.
#[allow(clippy::too_many_lines)]
fn derive_events(
    trip_id: i64,
    waypoints: &[WaypointRow],
    speed_limit_mps: f64,
) -> Vec<DerivedEvent> {
    let mut out = Vec::new();
    let mut prev_ap: Option<String> = None;
    let speed_limit_enabled = speed_limit_mps > 0.0;
    for (idx, w) in waypoints.iter().enumerate() {
        // `idx` is bounded by waypoints-per-trip (well under
        // i64::MAX); a saturating cast is loud-correct here.
        let ts = w
            .absolute_utc
            .unwrap_or_else(|| i64::try_from(idx).unwrap_or(i64::MAX));
        let lat = Some(w.latitude_deg);
        let lon = Some(w.longitude_deg);
        let fi = Some(w.frame_index);

        // Speed-limit. Strict `>` matches ADR-0019 and v1's
        // `> threshold` (NOT `>=`). When the user disables the
        // threshold (mph = 0) we skip this branch entirely so
        // the per-waypoint hot path pays one float compare.
        if speed_limit_enabled && w.speed_mps > speed_limit_mps {
            out.push(DerivedEvent {
                trip_id: Some(trip_id),
                clip_id: Some(w.clip_id),
                event_type: "speed_limit_exceeded",
                severity: "warning",
                timestamp_utc: ts,
                latitude_deg: lat,
                longitude_deg: lon,
                speed_mps: Some(w.speed_mps),
                metadata_json: None,
                description: format!(
                    "Speed {:.1} m/s exceeded limit {:.1} m/s",
                    w.speed_mps, speed_limit_mps
                ),
                frame_index: fi,
            });
        }

        // Acceleration-driven events. Emergency-brake
        // supersedes harsh-brake: a waypoint that crosses
        // BOTH thresholds emits only the critical one.
        if let Some(ax) = w.acceleration_x {
            if ax < EMERGENCY_BRAKE_X {
                out.push(DerivedEvent {
                    trip_id: Some(trip_id),
                    clip_id: Some(w.clip_id),
                    event_type: "emergency_braking",
                    severity: "critical",
                    timestamp_utc: ts,
                    latitude_deg: lat,
                    longitude_deg: lon,
                    speed_mps: Some(w.speed_mps),
                    metadata_json: Some(format!("{{\"acceleration_x\":{ax}}}")),
                    description: format!("Emergency braking detected ({ax:.2} m/s^2)"),
                    frame_index: fi,
                });
            } else if ax < HARSH_BRAKE_X {
                out.push(DerivedEvent {
                    trip_id: Some(trip_id),
                    clip_id: Some(w.clip_id),
                    event_type: "harsh_braking",
                    severity: "warning",
                    timestamp_utc: ts,
                    latitude_deg: lat,
                    longitude_deg: lon,
                    speed_mps: Some(w.speed_mps),
                    metadata_json: Some(format!("{{\"acceleration_x\":{ax}}}")),
                    description: format!("Harsh braking detected ({ax:.2} m/s^2)"),
                    frame_index: fi,
                });
            }
            if ax > HARD_ACCEL_X {
                out.push(DerivedEvent {
                    trip_id: Some(trip_id),
                    clip_id: Some(w.clip_id),
                    event_type: "hard_acceleration",
                    severity: "warning",
                    timestamp_utc: ts,
                    latitude_deg: lat,
                    longitude_deg: lon,
                    speed_mps: Some(w.speed_mps),
                    metadata_json: Some(format!("{{\"acceleration_x\":{ax}}}")),
                    description: format!("Hard acceleration detected ({ax:.2} m/s^2)"),
                    frame_index: fi,
                });
            }
        }
        if let Some(ay) = w.acceleration_y {
            if ay.abs() > SHARP_TURN_ABS_Y {
                out.push(DerivedEvent {
                    trip_id: Some(trip_id),
                    clip_id: Some(w.clip_id),
                    event_type: "sharp_turn",
                    severity: "warning",
                    timestamp_utc: ts,
                    latitude_deg: lat,
                    longitude_deg: lon,
                    speed_mps: Some(w.speed_mps),
                    metadata_json: Some(format!("{{\"acceleration_y\":{ay}}}")),
                    description: format!("Sharp turn detected (lateral {ay:.2} m/s^2)"),
                    frame_index: fi,
                });
            }
        }

        // Autopilot transitions. Compare normalised strings;
        // "off" / "" / None all count as "off" so a NULL on
        // one waypoint doesn't fire a spurious transition.
        let current_ap = normalise_ap(w.autopilot_state.as_deref());
        if let Some(prev) = prev_ap.as_deref() {
            if prev != current_ap {
                let (label, desc) = if current_ap == "off" {
                    ("autopilot_disengaged", "Autopilot disengaged".to_string())
                } else {
                    (
                        "autopilot_engaged",
                        format!(
                            "Autopilot engaged ({})",
                            w.autopilot_state.as_deref().unwrap_or("on")
                        ),
                    )
                };
                out.push(DerivedEvent {
                    trip_id: Some(trip_id),
                    clip_id: Some(w.clip_id),
                    event_type: label,
                    severity: "info",
                    timestamp_utc: ts,
                    latitude_deg: lat,
                    longitude_deg: lon,
                    speed_mps: Some(w.speed_mps),
                    metadata_json: Some(format!("{{\"from\":\"{prev}\",\"to\":\"{current_ap}\"}}")),
                    description: desc,
                    frame_index: fi,
                });
            }
        }
        prev_ap = Some(current_ap.to_string());
    }
    out
}

fn normalise_ap(raw: Option<&str>) -> &str {
    match raw {
        None => "off",
        Some(s) => {
            let trimmed = s.trim();
            if trimmed.is_empty() || trimmed.eq_ignore_ascii_case("off") {
                "off"
            } else {
                trimmed
            }
        }
    }
}

/// Materialise a sentry clip as a single-row `detected_events`
/// entry with `trip_id = NULL`. Sentry recordings never have
/// GPS in steady-state (the car is parked), so they don't
/// roll up into trips, but the events panel still needs to
/// list them.
fn materialize_sentry_clip(
    tx: &Transaction<'_>,
    clip: &ClipRow,
    stats: &mut RebuildStats,
) -> Result<()> {
    // Sentry-with-GPS would be unusual (driving event into
    // the sentry bucket) — skip silently so we don't
    // double-count those waypoints; they belong to driving
    // analysis, not sentry triggers.
    if clip.gps_waypoint_count > 0 {
        return Ok(());
    }
    let ts = clip.clip_started_utc.unwrap_or(0);
    tx.execute(
        "INSERT INTO detected_events (
            trip_id, clip_id, event_type, severity, timestamp_utc,
            latitude_deg, longitude_deg, speed_mps, metadata_json,
            description, frame_index
         ) VALUES (NULL, ?1, 'sentry', 'info', ?2, NULL, NULL, NULL, NULL,
                   'Sentry mode recording', NULL)",
        params![clip.id, ts],
    )?;
    stats.events_written += 1;
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::expect_used,
        clippy::indexing_slicing,
        clippy::panic,
        clippy::unwrap_used,
        clippy::float_cmp,
        clippy::cast_possible_truncation,
        clippy::cast_lossless,
        clippy::doc_markdown
    )]

    use super::*;
    use rusqlite::Connection;

    /// Stand up an in-memory DB with the full schema and the
    /// helper rows the tests share.
    fn fresh_db() -> Connection {
        // Easiest correct path: open a Store (runs the
        // migrations) and take its connection. We can't move
        // the conn out of Store because the field is private,
        // so we instead replay the migrations from a small
        // bootstrap connection. Keeping the SQL inline here
        // would risk drifting from `store::schema::MIGRATIONS`.
        let store = crate::store::Store::open_in_memory().unwrap();
        // Detach: the Store wrapper owns the connection. For
        // tests we want raw rusqlite access (UPDATE/INSERT on
        // tables the Store deliberately doesn't expose). Open
        // a fresh in-memory DB and replay the same migrations.
        drop(store);
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        for sql in crate::store::migrations_for_tests() {
            conn.execute_batch(sql).unwrap();
        }
        conn.execute_batch("INSERT INTO meta (key, value) VALUES ('schema_version', '4');")
            .unwrap();
        conn
    }

    /// Insert one clip row and return its id.
    fn insert_clip(
        conn: &mut Connection,
        path: &str,
        bucket: &str,
        started: Option<i64>,
        gps: i64,
    ) -> i64 {
        conn.execute(
            "INSERT INTO clips
                (relative_path, bucket, clip_started_utc, indexed_at_utc,
                 waypoint_count, gps_waypoint_count)
             VALUES (?1, ?2, ?3, 1000, 0, ?4)",
            params![path, bucket, started, gps],
        )
        .unwrap();
        conn.last_insert_rowid()
    }

    /// Insert one minimal waypoint row.
    #[allow(clippy::too_many_arguments)]
    fn insert_wp(
        conn: &mut Connection,
        clip_id: i64,
        frame_index: i64,
        ts_ms: f64,
        lat: f64,
        lon: f64,
        speed_mps: f64,
        ax: Option<f64>,
        ay: Option<f64>,
        ap: Option<&str>,
    ) {
        conn.execute(
            "INSERT INTO waypoints
                (clip_id, frame_index, timestamp_ms,
                 latitude_deg, longitude_deg, speed_mps, heading_deg,
                 acceleration_x, acceleration_y, acceleration_z,
                 gear, steering_angle,
                 brake_applied, blinker_on_left, blinker_on_right,
                 autopilot_state)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, 90.0,
                     ?7, ?8, NULL,
                     NULL, NULL,
                     0, 0, 0,
                     ?9)",
            params![clip_id, frame_index, ts_ms, lat, lon, speed_mps, ax, ay, ap],
        )
        .unwrap();
    }

    #[test]
    fn empty_db_rebuilds_to_zero() {
        let mut conn = fresh_db();
        let stats = Materializer::default().rebuild_all(&mut conn).unwrap();
        assert_eq!(stats, RebuildStats::default());
        let trips: i64 = conn
            .query_row("SELECT COUNT(*) FROM trips", [], |r| r.get(0))
            .unwrap();
        assert_eq!(trips, 0);
    }

    #[test]
    fn two_close_clips_become_one_trip() {
        let mut conn = fresh_db();
        let a = insert_clip(&mut conn, "a.mp4", "recent", Some(1_000_000), 2);
        let b = insert_clip(&mut conn, "b.mp4", "recent", Some(1_000_060), 2);
        // Drive ~1 km north-ish: ~0.01 deg lat ~ 1.11 km.
        insert_wp(&mut conn, a, 0, 0.0, 40.0, -75.0, 20.0, None, None, None);
        insert_wp(
            &mut conn, a, 30, 30_000.0, 40.005, -75.0, 22.0, None, None, None,
        );
        insert_wp(&mut conn, b, 0, 0.0, 40.005, -75.0, 22.0, None, None, None);
        insert_wp(
            &mut conn, b, 30, 30_000.0, 40.010, -75.0, 25.0, None, None, None,
        );

        let stats = Materializer::default().rebuild_all(&mut conn).unwrap();
        assert_eq!(stats.trips_written, 1);
        assert_eq!(stats.clips_seen, 2);

        let map_rows: i64 = conn
            .query_row("SELECT COUNT(*) FROM clip_trip_map", [], |r| r.get(0))
            .unwrap();
        assert_eq!(map_rows, 2);
    }

    #[test]
    fn far_apart_clips_become_two_trips() {
        let mut conn = fresh_db();
        let a = insert_clip(&mut conn, "a.mp4", "recent", Some(1_000_000), 2);
        let b = insert_clip(&mut conn, "b.mp4", "recent", Some(1_900_000), 2);
        insert_wp(&mut conn, a, 0, 0.0, 40.0, -75.0, 20.0, None, None, None);
        insert_wp(
            &mut conn, a, 30, 30_000.0, 40.005, -75.0, 22.0, None, None, None,
        );
        insert_wp(&mut conn, b, 0, 0.0, 41.0, -76.0, 20.0, None, None, None);
        insert_wp(
            &mut conn, b, 30, 30_000.0, 41.005, -76.0, 22.0, None, None, None,
        );

        let stats = Materializer::default().rebuild_all(&mut conn).unwrap();
        assert_eq!(stats.trips_written, 2);
    }

    #[test]
    fn short_trip_is_skipped() {
        let mut conn = fresh_db();
        let a = insert_clip(&mut conn, "a.mp4", "recent", Some(1_000_000), 2);
        // 0.0001 deg ~ 11 m total — below 50 m threshold.
        insert_wp(&mut conn, a, 0, 0.0, 40.0, -75.0, 1.0, None, None, None);
        insert_wp(
            &mut conn, a, 1, 1_000.0, 40.0001, -75.0, 1.0, None, None, None,
        );
        let stats = Materializer::default().rebuild_all(&mut conn).unwrap();
        assert_eq!(stats.trips_written, 0);
        assert_eq!(stats.trips_skipped_short, 1);
    }

    #[test]
    fn sentry_clip_emits_sentry_event_with_null_trip() {
        let mut conn = fresh_db();
        let s = insert_clip(&mut conn, "s.mp4", "sentry", Some(2_000_000), 0);
        let stats = Materializer::default().rebuild_all(&mut conn).unwrap();
        assert_eq!(stats.trips_written, 0);
        assert_eq!(stats.events_written, 1);
        let (trip_id, etype, clip_id): (Option<i64>, String, i64) = conn
            .query_row(
                "SELECT trip_id, event_type, clip_id FROM detected_events",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .unwrap();
        assert!(trip_id.is_none());
        assert_eq!(etype, "sentry");
        assert_eq!(clip_id, s);
    }

    #[test]
    fn emergency_brake_supersedes_harsh_brake() {
        let mut conn = fresh_db();
        let a = insert_clip(&mut conn, "a.mp4", "recent", Some(3_000_000), 5);
        // Long enough drive to clear the 50 m floor.
        insert_wp(
            &mut conn,
            a,
            0,
            0.0,
            40.0,
            -75.0,
            20.0,
            Some(0.0),
            Some(0.0),
            None,
        );
        insert_wp(
            &mut conn,
            a,
            10,
            1_000.0,
            40.001,
            -75.0,
            22.0,
            Some(-8.0),
            Some(0.0),
            None,
        );
        insert_wp(
            &mut conn,
            a,
            20,
            2_000.0,
            40.002,
            -75.0,
            5.0,
            Some(0.0),
            Some(0.0),
            None,
        );
        Materializer::default().rebuild_all(&mut conn).unwrap();
        let types: Vec<String> = conn
            .prepare("SELECT event_type FROM detected_events ORDER BY id")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .map(std::result::Result::unwrap)
            .collect();
        assert!(types.contains(&"emergency_braking".to_string()));
        assert!(!types.contains(&"harsh_braking".to_string()));
    }

    #[test]
    fn rebuild_is_idempotent() {
        let mut conn = fresh_db();
        let a = insert_clip(&mut conn, "a.mp4", "recent", Some(4_000_000), 2);
        insert_wp(&mut conn, a, 0, 0.0, 40.0, -75.0, 20.0, None, None, None);
        insert_wp(
            &mut conn, a, 1, 1_000.0, 40.005, -75.0, 22.0, None, None, None,
        );
        let s1 = Materializer::default().rebuild_all(&mut conn).unwrap();
        let s2 = Materializer::default().rebuild_all(&mut conn).unwrap();
        assert_eq!(s1, s2);
        let trips: i64 = conn
            .query_row("SELECT COUNT(*) FROM trips", [], |r| r.get(0))
            .unwrap();
        assert_eq!(trips, 1);
    }

    #[test]
    fn rebuild_clears_trips_dirty_flag() {
        let mut conn = fresh_db();
        mark_trips_dirty(&conn).unwrap();
        assert!(trips_dirty(&conn).unwrap());
        Materializer::default().rebuild_all(&mut conn).unwrap();
        assert!(!trips_dirty(&conn).unwrap());
    }

    #[test]
    fn autopilot_transition_emits_event() {
        let mut conn = fresh_db();
        let a = insert_clip(&mut conn, "a.mp4", "recent", Some(5_000_000), 5);
        insert_wp(
            &mut conn,
            a,
            0,
            0.0,
            40.0,
            -75.0,
            20.0,
            None,
            None,
            Some("off"),
        );
        insert_wp(
            &mut conn,
            a,
            1,
            1_000.0,
            40.001,
            -75.0,
            22.0,
            None,
            None,
            Some("autopilot"),
        );
        insert_wp(
            &mut conn,
            a,
            2,
            2_000.0,
            40.002,
            -75.0,
            22.0,
            None,
            None,
            Some("autopilot"),
        );
        insert_wp(
            &mut conn,
            a,
            3,
            3_000.0,
            40.003,
            -75.0,
            22.0,
            None,
            None,
            Some("off"),
        );
        Materializer::default().rebuild_all(&mut conn).unwrap();
        let mut stmt = conn
            .prepare("SELECT event_type FROM detected_events ORDER BY id")
            .unwrap();
        let types: Vec<String> = stmt
            .query_map([], |r| r.get(0))
            .unwrap()
            .map(std::result::Result::unwrap)
            .collect();
        assert_eq!(
            types.iter().filter(|t| t.starts_with("autopilot")).count(),
            2,
            "want one on and one off; got {types:?}"
        );
    }

    #[test]
    fn cascade_on_clip_delete_clears_derived_rows() {
        let mut conn = fresh_db();
        let a = insert_clip(&mut conn, "a.mp4", "recent", Some(6_000_000), 2);
        insert_wp(&mut conn, a, 0, 0.0, 40.0, -75.0, 20.0, None, None, None);
        insert_wp(
            &mut conn, a, 1, 1_000.0, 40.005, -75.0, 22.0, None, None, None,
        );
        Materializer::default().rebuild_all(&mut conn).unwrap();
        conn.execute("DELETE FROM clips WHERE id = ?1", params![a])
            .unwrap();
        let trips: i64 = conn
            .query_row("SELECT COUNT(*) FROM trips", [], |r| r.get(0))
            .unwrap();
        assert_eq!(trips, 0);
        let map: i64 = conn
            .query_row("SELECT COUNT(*) FROM clip_trip_map", [], |r| r.get(0))
            .unwrap();
        assert_eq!(map, 0);
    }

    fn count_speed_events(conn: &Connection) -> i64 {
        conn.query_row(
            "SELECT COUNT(*) FROM detected_events WHERE event_type = 'speed_limit_exceeded'",
            [],
            |r| r.get(0),
        )
        .unwrap()
    }

    /// A trip with one waypoint above 35.76 m/s emits a
    /// speed-limit event under the default threshold.
    #[test]
    fn from_overrides_default_speed_threshold_emits_event() {
        let mut conn = fresh_db();
        // 7,200,000 km/h… er, just need a fast waypoint and enough
        // distance to clear the 0.05 km min trip distance.
        let a = insert_clip(&mut conn, "a.mp4", "recent", Some(8_000_000), 2);
        insert_wp(&mut conn, a, 0, 0.0, 40.0, -75.0, 40.0, None, None, None);
        insert_wp(&mut conn, a, 1, 1_000.0, 40.01, -75.0, 40.0, None, None, None);
        let overrides = crate::mapping_overrides::MappingOverrides {
            trip_gap_seconds: 300,
            speed_limit_mps: SPEED_LIMIT_MPS,
        };
        Materializer::from_overrides(&overrides)
            .rebuild_all(&mut conn)
            .unwrap();
        assert!(count_speed_events(&conn) >= 1);
    }

    /// `speed_limit_mps = 0.0` disables emission even when
    /// waypoints exceed the legacy default threshold.
    #[test]
    fn from_overrides_zero_speed_limit_disables_emission() {
        let mut conn = fresh_db();
        let a = insert_clip(&mut conn, "a.mp4", "recent", Some(9_000_000), 2);
        insert_wp(&mut conn, a, 0, 0.0, 40.0, -75.0, 40.0, None, None, None);
        insert_wp(&mut conn, a, 1, 1_000.0, 40.01, -75.0, 40.0, None, None, None);
        let overrides = crate::mapping_overrides::MappingOverrides {
            trip_gap_seconds: 300,
            speed_limit_mps: 0.0,
        };
        Materializer::from_overrides(&overrides)
            .rebuild_all(&mut conn)
            .unwrap();
        assert_eq!(count_speed_events(&conn), 0);
    }

    /// Custom (low) speed threshold emits a speed event at a
    /// speed well below the legacy default.
    #[test]
    fn from_overrides_custom_speed_limit_emits_at_lower_threshold() {
        let mut conn = fresh_db();
        let a = insert_clip(&mut conn, "a.mp4", "recent", Some(10_000_000), 2);
        // ~13.4 m/s = 30 mph — below the legacy 80-mph default,
        // above the custom 25-mph threshold (11.176 m/s).
        insert_wp(&mut conn, a, 0, 0.0, 40.0, -75.0, 13.4, None, None, None);
        insert_wp(&mut conn, a, 1, 1_000.0, 40.01, -75.0, 13.4, None, None, None);
        let overrides = crate::mapping_overrides::MappingOverrides {
            trip_gap_seconds: 300,
            speed_limit_mps: 25.0 * 0.44704,
        };
        Materializer::from_overrides(&overrides)
            .rebuild_all(&mut conn)
            .unwrap();
        assert!(count_speed_events(&conn) >= 1);
    }

    /// Custom trip-gap propagates: with a 60-second gap, clips
    /// 120 seconds apart end up in two trips rather than one.
    #[test]
    fn from_overrides_custom_trip_gap_splits_trips() {
        let mut conn = fresh_db();
        let a = insert_clip(&mut conn, "a.mp4", "recent", Some(11_000_000), 2);
        insert_wp(&mut conn, a, 0, 0.0, 40.0, -75.0, 10.0, None, None, None);
        insert_wp(&mut conn, a, 1, 500.0, 40.01, -75.0, 10.0, None, None, None);
        // 120 s later (well past the 60-second custom gap).
        let b = insert_clip(&mut conn, "b.mp4", "recent", Some(11_000_120), 2);
        insert_wp(&mut conn, b, 0, 0.0, 40.02, -75.0, 10.0, None, None, None);
        insert_wp(&mut conn, b, 1, 500.0, 40.03, -75.0, 10.0, None, None, None);
        let overrides = crate::mapping_overrides::MappingOverrides {
            trip_gap_seconds: 60,
            speed_limit_mps: 0.0,
        };
        let stats = Materializer::from_overrides(&overrides)
            .rebuild_all(&mut conn)
            .unwrap();
        assert_eq!(stats.trips_written, 2);
    }
}
