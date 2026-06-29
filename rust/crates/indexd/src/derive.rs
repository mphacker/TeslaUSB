//! Pure trip + event derivation over sampled waypoints.
//!
//! This is the **parity-critical** core: every threshold, comparison
//! operator, description string and ordering is ported verbatim from the
//! v1 production worker (`teslausb-worker` materializer, ADR-0019) and the
//! `mapping_*` Python references, cross-checked against each other. Do NOT
//! change any user-visible derivation behavior here without an ASK-FIRST
//! per `indexd.md §7`.
//!
//! ## Ground-truth reconciliation
//!
//! The integrator named the Python (`mapping_event_derivation.py` /
//! `mapping_trip_derivation.py`) as ground truth; the v1 Rust materializer
//! is the *production* derivation. They agree on event types, thresholds,
//! and description strings. Where the on-demand Python and the production
//! materializer differ (distance `(0,0)`-skip; clustering all non-sentry
//! front clips vs `require_gps`), this port follows the **materializer**
//! (what actually runs today) and the choice is recorded in the build
//! notes to the integrator.

use crate::geo::{cap_indices_uniform, haversine_km, simplify_polyline_rdp};
use crate::model::{
    ClipEventInput, Derivation, DeriveClip, DeriveWaypoint, DerivedEvent, DerivedTrip, EventType,
    TripPoint,
};
use scannerd::clip_event::rounded_tz_offset;
use teslausb_core::sei::tesla::AutopilotState;

/// Trip-grouping gap (s): non-sentry clips whose `clip_started_utc` are
/// within this window merge into one trip
/// (`materializer.rs::DEFAULT_TRIP_GAP_SECONDS`).
pub const DEFAULT_TRIP_GAP_SECONDS: i64 = 300;

/// Minimum trip distance to materialize (km); below this the cluster is
/// dropped (`materializer.rs::DEFAULT_TRIP_MIN_DISTANCE_KM`).
pub const DEFAULT_TRIP_MIN_DISTANCE_KM: f64 = 0.05;

/// Speed threshold (m/s) for `speed_limit_exceeded`; strict `>`
/// (`mapping_event_derivation.py::_SPEED_LIMIT_MPS`).
pub const SPEED_LIMIT_MPS: f64 = 35.76;

/// `accel_x` threshold for `hard_acceleration` (`> 3.5`).
pub const HARD_ACCEL_X: f64 = 3.5;

/// `accel_x` threshold for `harsh_braking` (`< -4.0`).
pub const HARSH_BRAKE_X: f64 = -4.0;

/// `accel_x` threshold for `emergency_braking` (`< -7.0`); supersedes
/// harsh braking.
pub const EMERGENCY_BRAKE_X: f64 = -7.0;

/// `|accel_y|` threshold for `sharp_turn` (`> 4.0`).
pub const SHARP_TURN_ABS_Y: f64 = 4.0;

/// Polyline render-segment break: gap in seconds
/// (`mapping_trip_derivation.py::_GAP_MAX_SECONDS_DEFAULT`).
pub const POLYLINE_GAP_MAX_SECONDS: f64 = 60.0;

/// Polyline render-segment break: gap in metres
/// (`mapping_trip_derivation.py::_GAP_MAX_METERS_DEFAULT`).
pub const POLYLINE_GAP_MAX_METERS: f64 = 250.0;

/// RDP simplification tolerance (m). v1 production epsilon.
pub const POLYLINE_RDP_EPSILON_M: f64 = 8.0;

/// Cap on rendered polyline points per trip. v1 production cap.
pub const POLYLINE_MAX_POINTS: usize = 200;

/// v1 frame-rate constant used for event.json front-frame mapping.
const CLIP_EVENT_FRAME_RATE: f64 = 36.0;

/// Tunable derivation parameters (defaults are the v1 production values).
#[derive(Debug, Clone, Copy)]
pub struct DeriveConfig {
    /// Trip-grouping gap (s).
    pub gap_seconds: i64,
    /// Minimum trip distance to materialize (km).
    pub min_distance_km: f64,
    /// Speed-limit threshold (m/s); `<= 0` disables the event.
    pub speed_limit_mps: f64,
}

impl Default for DeriveConfig {
    fn default() -> Self {
        Self {
            gap_seconds: DEFAULT_TRIP_GAP_SECONDS,
            min_distance_km: DEFAULT_TRIP_MIN_DISTANCE_KM,
            speed_limit_mps: SPEED_LIMIT_MPS,
        }
    }
}

/// The autopilot states that count as "actively driving" for event
/// derivation. This is the SINGLE source of truth for the engaged set.
///
/// Ground truth = `mapping_event_derivation.py::is_autopilot_engaged`,
/// whose engaged SET is `{SELF_DRIVING, AUTOSTEER, TACC}`.
///
/// NOTE — intentional bug-divergence: the v1 *materializer*
/// (`teslausb-worker`) instead normalises the AP string via
/// `normalise_ap`, which treats the literal `"NONE"` as engaged
/// (`"engaged (NONE)"`) — a latent bug. We follow the Python's explicit
/// set, which is correct. If the operator ever wants bug-for-bug parity
/// with the materializer, flipping this one const is the entire change.
pub const AUTOPILOT_ENGAGED_STATES: [AutopilotState; 3] = [
    AutopilotState::SelfDriving,
    AutopilotState::Autosteer,
    AutopilotState::Tacc,
];

/// True when an autopilot state means the car is actively driving.
/// Membership test over [`AUTOPILOT_ENGAGED_STATES`].
#[must_use]
pub fn is_autopilot_engaged(state: AutopilotState) -> bool {
    AUTOPILOT_ENGAGED_STATES.contains(&state)
}

/// Derive trips + sentry events from a scan's front clips.
///
/// `clips` MUST be sorted by `(clip_started_utc, clip_id)` — the caller
/// (ingest) guarantees this so clustering and waypoint flattening match
/// the materializer's `ORDER BY`.
#[must_use]
pub fn derive(
    clips: &[DeriveClip],
    clip_events: &[ClipEventInput],
    config: DeriveConfig,
) -> Derivation {
    let mut result = Derivation::default();

    // Partition sentry from driving by folder class (materializer
    // partitions on `bucket == "sentry"`).
    let driving: Vec<&DeriveClip> = clips
        .iter()
        .filter(|c| !c.folder_class.is_sentry())
        .collect();
    let sentry: Vec<&DeriveClip> = clips
        .iter()
        .filter(|c| c.folder_class.is_sentry())
        .collect();

    for cluster in cluster_into_trips(&driving, config.gap_seconds) {
        if let Some(trip) = materialize_trip(&cluster, config) {
            result.trips.push(trip);
        }
    }

    let sentry_event_prefixes: Vec<String> = clip_events
        .iter()
        .filter(|event| event.bucket.is_sentry())
        .map(|event| format!("{}/", event.event_dir_key))
        .collect();
    for clip in sentry {
        if sentry_event_prefixes
            .iter()
            .any(|prefix| clip.canonical_key.starts_with(prefix))
        {
            continue;
        }
        if let Some(event) = materialize_sentry_clip(clip) {
            result.sentry_events.push(event);
        }
    }

    let mut clip_event_rows: Vec<(i64, String, DerivedEvent)> = clip_events
        .iter()
        .map(|input| {
            (
                clip_event_timestamp(input),
                input.event_dir_key.clone(),
                materialize_clip_event(input),
            )
        })
        .collect();
    clip_event_rows.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
    for (_, _, event) in clip_event_rows {
        result.sentry_events.push(event);
    }

    result
}

/// Cluster chronologically-ordered driving clips into trips on the
/// `clip_started_utc` gap (`materializer.rs::cluster_into_trips`).
fn cluster_into_trips<'c>(clips: &[&'c DeriveClip], gap_seconds: i64) -> Vec<Vec<&'c DeriveClip>> {
    let mut out: Vec<Vec<&DeriveClip>> = Vec::new();
    let mut current: Vec<&DeriveClip> = Vec::new();
    let mut last_ts: Option<i64> = None;
    for &clip in clips {
        let split = match last_ts {
            Some(prev) => (clip.clip_started_utc - prev) > gap_seconds,
            None => false,
        };
        if split && !current.is_empty() {
            out.push(std::mem::take(&mut current));
        }
        last_ts = Some(clip.clip_started_utc);
        current.push(clip);
    }
    if !current.is_empty() {
        out.push(current);
    }
    out
}

/// Flatten a cluster's waypoints into `(clip_id, waypoint)` pairs in
/// `(clip_started_utc, frame order)` — matching the materializer load
/// order. The cluster is already in `clip_started_utc` order.
fn flatten_waypoints<'c>(cluster: &[&'c DeriveClip]) -> Vec<(i64, &'c DeriveWaypoint)> {
    let mut flat = Vec::new();
    for &clip in cluster {
        for wp in &clip.waypoints {
            flat.push((clip.clip_id, wp));
        }
    }
    flat
}

/// Materialize one driving cluster, or `None` if it is below the
/// min-distance gate (`materializer.rs::materialize_driving_trip`).
fn materialize_trip(cluster: &[&DeriveClip], config: DeriveConfig) -> Option<DerivedTrip> {
    if cluster.is_empty() {
        return None;
    }
    let flat = flatten_waypoints(cluster);
    let distance_km = total_distance_km(&flat);
    if distance_km < config.min_distance_km {
        return None;
    }

    let started_at = cluster
        .iter()
        .map(|c| c.clip_started_utc)
        .min()
        .unwrap_or(0);
    let ended_at = flat
        .iter()
        .rev()
        .find_map(|(_, w)| w.absolute_utc)
        .or_else(|| cluster.iter().map(|c| c.clip_started_utc).max())
        .unwrap_or(started_at);

    let (bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon) = bounding_box(&flat);
    let points = build_trip_points(&flat);
    let polyline = build_polyline(&points);
    #[allow(clippy::cast_precision_loss)]
    let distance_metres = distance_km * 1000.0;
    let events = derive_events(&flat, config.speed_limit_mps);
    let clip_ids = cluster.iter().map(|c| c.clip_id).collect();

    Some(DerivedTrip {
        day: utc_civil_date(started_at),
        started_at,
        ended_at,
        bbox_min_lat,
        bbox_min_lon,
        bbox_max_lat,
        bbox_max_lon,
        distance_m: distance_metres,
        clip_ids,
        points,
        polyline,
        events,
    })
}

/// Sum of haversine over consecutive **valid-geo** waypoints (km). Skips
/// non-finite and `(0,0)` points (`materializer.rs::total_distance_km`).
fn total_distance_km(flat: &[(i64, &DeriveWaypoint)]) -> f64 {
    let mut sum = 0.0;
    let mut prev: Option<&DeriveWaypoint> = None;
    for &(_, w) in flat {
        if !w.has_valid_geo() {
            continue;
        }
        if let Some(p) = prev {
            sum += haversine_km(p.lat, p.lon, w.lat, w.lon);
        }
        prev = Some(w);
    }
    sum
}

/// Bounding box of valid-geo waypoints.
fn bounding_box(
    flat: &[(i64, &DeriveWaypoint)],
) -> (Option<f64>, Option<f64>, Option<f64>, Option<f64>) {
    let mut min_lat: Option<f64> = None;
    let mut min_lon: Option<f64> = None;
    let mut max_lat: Option<f64> = None;
    let mut max_lon: Option<f64> = None;
    for &(_, w) in flat {
        if !w.has_valid_geo() {
            continue;
        }
        min_lat = Some(min_lat.map_or(w.lat, |v: f64| v.min(w.lat)));
        min_lon = Some(min_lon.map_or(w.lon, |v: f64| v.min(w.lon)));
        max_lat = Some(max_lat.map_or(w.lat, |v: f64| v.max(w.lat)));
        max_lon = Some(max_lon.map_or(w.lon, |v: f64| v.max(w.lon)));
    }
    (min_lat, min_lon, max_lat, max_lon)
}

/// Durable polyline rows: the valid-geo waypoints in order (OQ-2).
fn build_trip_points(flat: &[(i64, &DeriveWaypoint)]) -> Vec<TripPoint> {
    flat.iter()
        .filter(|(_, w)| w.has_valid_geo())
        .map(|(_, w)| TripPoint {
            t: w.absolute_utc.unwrap_or(0),
            lat: w.lat,
            lon: w.lon,
            speed: w.speed,
            heading: w.heading,
        })
        .collect()
}

/// Build the cached RDP-simplified polyline blob from durable points
/// (OQ-2). Splits into render segments on the v1 time/distance gap, runs
/// RDP per segment, then caps the total point budget.
fn build_polyline(points: &[TripPoint]) -> Vec<u8> {
    let segments = split_segments(points);
    let mut simplified: Vec<Vec<(f64, f64)>> = Vec::new();
    for seg in &segments {
        let latlons: Vec<(f64, f64)> = seg.iter().map(|p| (p.lat, p.lon)).collect();
        let keep = simplify_polyline_rdp(&latlons, POLYLINE_RDP_EPSILON_M);
        let kept: Vec<(f64, f64)> = keep
            .iter()
            .filter_map(|&i| latlons.get(i).copied())
            .collect();
        if !kept.is_empty() {
            simplified.push(kept);
        }
    }
    cap_segments(&mut simplified, POLYLINE_MAX_POINTS);
    encode_polyline(&simplified)
}

/// Split valid points into render segments on the v1 gap rule
/// (`mapping_trip_derivation.py::is_gap_between` — `> 60 s` OR `> 250 m`).
fn split_segments(points: &[TripPoint]) -> Vec<Vec<TripPoint>> {
    let mut segments: Vec<Vec<TripPoint>> = Vec::new();
    let mut current: Vec<TripPoint> = Vec::new();
    let mut prev: Option<&TripPoint> = None;
    for p in points {
        let gap = match prev {
            Some(q) => is_gap_between(q, p),
            None => false,
        };
        if gap && !current.is_empty() {
            segments.push(std::mem::take(&mut current));
        }
        current.push(*p);
        prev = Some(p);
    }
    if !current.is_empty() {
        segments.push(current);
    }
    segments
}

/// Whether two consecutive points should render as disjoint segments.
fn is_gap_between(prev: &TripPoint, cur: &TripPoint) -> bool {
    #[allow(clippy::cast_precision_loss)]
    let dt = (cur.t - prev.t).abs() as f64;
    if dt > POLYLINE_GAP_MAX_SECONDS {
        return true;
    }
    let distance_m = haversine_km(prev.lat, prev.lon, cur.lat, cur.lon) * 1000.0;
    distance_m > POLYLINE_GAP_MAX_METERS
}

/// Cap the total rendered points across all segments to `max_points`,
/// allocating each segment a proportional budget (always keeping its
/// endpoints). Provisional: the cache format/policy is indexd-internal
/// until webd D2 defines the polyline DTO.
fn cap_segments(segments: &mut [Vec<(f64, f64)>], max_points: usize) {
    let total: usize = segments.iter().map(Vec::len).sum();
    if total <= max_points || segments.is_empty() {
        return;
    }
    for seg in segments.iter_mut() {
        #[allow(
            clippy::cast_precision_loss,
            clippy::cast_possible_truncation,
            clippy::cast_sign_loss
        )]
        let budget = ((seg.len() as f64 / total as f64) * max_points as f64).floor() as usize;
        let budget = budget.max(2);
        if seg.len() > budget {
            let indices: Vec<usize> = (0..seg.len()).collect();
            let keep = cap_indices_uniform(&indices, budget);
            let capped: Vec<(f64, f64)> =
                keep.iter().filter_map(|&i| seg.get(i).copied()).collect();
            *seg = capped;
        }
    }
}

/// Encode simplified segments as a self-describing big-endian blob:
/// `u32 segment_count`, then per segment `u32 point_count` followed by
/// `point_count × (f64 lat, f64 lon)`. Internal/provisional format.
#[must_use]
pub fn encode_polyline(segments: &[Vec<(f64, f64)>]) -> Vec<u8> {
    let mut out = Vec::new();
    #[allow(clippy::cast_possible_truncation)]
    let seg_count = segments.len() as u32;
    out.extend_from_slice(&seg_count.to_be_bytes());
    for seg in segments {
        #[allow(clippy::cast_possible_truncation)]
        let count = seg.len() as u32;
        out.extend_from_slice(&count.to_be_bytes());
        for &(lat, lon) in seg {
            out.extend_from_slice(&lat.to_be_bytes());
            out.extend_from_slice(&lon.to_be_bytes());
        }
    }
    out
}

/// Per-waypoint event detection (`mapping_event_derivation.py`). Order
/// per waypoint: speed, acceleration (emergency | harsh | hard,
/// mutually exclusive), sharp-turn, autopilot transition. Autopilot
/// engaged-state is tracked across the whole trip.
fn derive_events(flat: &[(i64, &DeriveWaypoint)], speed_limit_mps: f64) -> Vec<DerivedEvent> {
    let mut out = Vec::new();
    let mut prev_engaged: Option<bool> = None;
    let speed_enabled = speed_limit_mps > 0.0;
    for (idx, &(clip_id, w)) in flat.iter().enumerate() {
        let t = w
            .absolute_utc
            .unwrap_or_else(|| i64::try_from(idx).unwrap_or(i64::MAX));
        let mk = |event_type: EventType, description: String| DerivedEvent {
            clip_id: Some(clip_id),
            event_type,
            t,
            lat: Some(w.lat),
            lon: Some(w.lon),
            front_frame_index: Some(w.frame_index),
            #[allow(clippy::cast_possible_truncation)]
            front_frame_offset_ms: Some(w.offset_ms.round() as i64),
            description,
        };

        if speed_enabled && w.speed > speed_limit_mps {
            out.push(mk(
                EventType::SpeedLimitExceeded,
                format!(
                    "Speed {:.1} m/s exceeded limit {:.1} m/s",
                    w.speed, speed_limit_mps
                ),
            ));
        }

        if let Some(ax) = w.accel_x {
            if ax < EMERGENCY_BRAKE_X {
                out.push(mk(
                    EventType::EmergencyBraking,
                    format!("Emergency braking detected ({ax:.2} m/s^2)"),
                ));
            } else if ax < HARSH_BRAKE_X {
                out.push(mk(
                    EventType::HarshBraking,
                    format!("Harsh braking detected ({ax:.2} m/s^2)"),
                ));
            } else if ax > HARD_ACCEL_X {
                out.push(mk(
                    EventType::HardAcceleration,
                    format!("Hard acceleration detected ({ax:.2} m/s^2)"),
                ));
            }
        }

        if let Some(ay) = w.accel_y {
            if ay.abs() > SHARP_TURN_ABS_Y {
                out.push(mk(
                    EventType::SharpTurn,
                    format!("Sharp turn detected (lateral {ay:.2} m/s^2)"),
                ));
            }
        }

        let current_engaged = is_autopilot_engaged(w.autopilot_state);
        if let Some(prev) = prev_engaged {
            if prev != current_engaged {
                if current_engaged {
                    out.push(mk(
                        EventType::AutopilotEngaged,
                        format!("Autopilot engaged ({})", w.autopilot_state.as_db_str()),
                    ));
                } else {
                    out.push(mk(
                        EventType::AutopilotDisengaged,
                        "Autopilot disengaged".to_owned(),
                    ));
                }
            }
        }
        prev_engaged = Some(current_engaged);
    }
    out
}

/// Materialize a sentry clip as one `sentry` event with `trip_id` NULL,
/// or `None` if it carries GPS (those belong to driving analysis)
/// (`materializer.rs::materialize_sentry_clip`).
fn materialize_sentry_clip(clip: &DeriveClip) -> Option<DerivedEvent> {
    if clip.gps_waypoint_count > 0 {
        return None;
    }
    Some(DerivedEvent {
        clip_id: Some(clip.clip_id),
        event_type: EventType::Sentry,
        t: clip.clip_started_utc,
        lat: None,
        lon: None,
        front_frame_index: None,
        front_frame_offset_ms: None,
        description: "Sentry mode recording".to_owned(),
    })
}

fn clip_event_timestamp(input: &ClipEventInput) -> i64 {
    if input.timestamp_has_offset {
        return input.timestamp_utc;
    }
    if input.primary_started_trusted {
        if let Some(start) = input.primary_started_utc {
            let offset = rounded_tz_offset(input.timestamp_local_naive, start).unwrap_or(0);
            return input.timestamp_local_naive - offset;
        }
    }
    input.timestamp_local_naive
}

fn materialize_clip_event(input: &ClipEventInput) -> DerivedEvent {
    let t = clip_event_timestamp(input);
    let (front_frame_index, front_frame_offset_ms) = if input.primary_started_trusted {
        if let Some(start) = input.primary_started_utc {
            let delta = t - start;
            #[allow(
                clippy::cast_precision_loss,
                clippy::cast_possible_truncation,
                clippy::cast_sign_loss
            )]
            let frame_index = ((delta as f64) * CLIP_EVENT_FRAME_RATE).round().max(0.0) as i64;
            (Some(frame_index), Some(delta.max(0).saturating_mul(1000)))
        } else {
            (None, None)
        }
    } else {
        (None, None)
    };

    let (lat, lon) = validated_clip_event_geo(input.est_lat, input.est_lon);
    let event_type = if input.bucket.is_sentry() {
        EventType::Sentry
    } else {
        EventType::Saved
    };

    DerivedEvent {
        clip_id: input.primary_clip_id,
        event_type,
        t,
        lat,
        lon,
        front_frame_index,
        front_frame_offset_ms,
        description: clip_event_description(input),
    }
}

fn validated_clip_event_geo(lat: Option<f64>, lon: Option<f64>) -> (Option<f64>, Option<f64>) {
    match (lat, lon) {
        (Some(lat), Some(lon))
            if lat.is_finite()
                && lon.is_finite()
                && (-90.0..=90.0).contains(&lat)
                && (-180.0..=180.0).contains(&lon)
                && !(lat == 0.0 && lon == 0.0) =>
        {
            (Some(lat), Some(lon))
        }
        _ => (None, None),
    }
}

fn clip_event_description(input: &ClipEventInput) -> String {
    let mut parts = Vec::new();
    if let Some(reason) = trim_non_empty(input.reason.as_deref()) {
        parts.push(humanize(reason));
    } else {
        parts.push("Event clip".to_owned());
    }
    if let Some(city) = trim_non_empty(input.city.as_deref()) {
        parts.push(humanize(city));
    }
    if let Some(camera) = trim_non_empty(input.camera.as_deref()) {
        parts.push(humanize(camera));
    }
    parts.join(" | ")
}

fn trim_non_empty(value: Option<&str>) -> Option<&str> {
    value.and_then(|v| {
        let trimmed = v.trim();
        (!trimmed.is_empty()).then_some(trimmed)
    })
}

fn humanize(source: &str) -> String {
    source
        .replace(['_', '-'], " ")
        .split_whitespace()
        .map(title_case_word)
        .collect::<Vec<_>>()
        .join(" ")
}

fn title_case_word(word: &str) -> String {
    let mut chars = word.chars();
    let Some(first) = chars.next() else {
        return String::new();
    };
    let mut out = String::new();
    out.extend(first.to_uppercase());
    for ch in chars {
        out.extend(ch.to_lowercase());
    }
    out
}

/// Build a `DeriveWaypoint` from a scannerd walk waypoint and the clip's
/// resolved start instant. `absolute_utc = clip_started_utc +
/// trunc(offset_ms/1000)` (truncation, matching the materializer).
#[must_use]
pub fn waypoint_from_walk(
    walk: &scannerd::seiwalk::Waypoint,
    clip_started_utc: i64,
) -> DeriveWaypoint {
    let msg = &walk.message;
    #[allow(clippy::cast_possible_truncation)]
    let secs = (walk.timestamp_ms / 1000.0) as i64;
    DeriveWaypoint {
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
        autopilot_state: msg.autopilot_state,
        gear: msg.gear_state,
        has_gps_fix: msg.has_gps_fix(),
    }
}

/// UTC civil date `YYYY-MM-DD` from epoch seconds. Pure integer
/// algorithm (Howard Hinnant's `civil_from_days`); avoids a chrono dep.
/// UTC is used because the RTC-less Pi has no stable local timezone
/// (flagged: D1 says "local civil date").
#[must_use]
pub fn utc_civil_date(epoch_s: i64) -> String {
    let days = epoch_s.div_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    format!("{year:04}-{month:02}-{day:02}")
}

/// Epoch seconds (UTC) for a `YYYY-MM-DD_HH-MM-SS` Tesla clip filename
/// timestamp. Used ONLY as the recording-instant fallback when the MP4
/// `mvhd`/GPS instant is unavailable (the Pi has no RTC). NOTE the Tesla
/// filename is in the car's LOCAL timezone, so this fallback carries the
/// documented clock-skew (flagged: `indexd.md` clock-skew rule); mvhd-GPS
/// is always preferred when present.
///
/// Delegates to [`scannerd::timestamp::epoch_from_tesla_timestamp`] — the
/// single implementation, re-homed in the raw-parsing crate so the
/// `scannerd` producer can resolve `started_at` without a dependency cycle
/// back into `indexd`.
///
/// Returns `None` if the string is not a parseable Tesla timestamp.
#[must_use]
pub fn epoch_from_tesla_timestamp(ts: &str) -> Option<i64> {
    scannerd::timestamp::epoch_from_tesla_timestamp(ts)
}
/// (Howard Hinnant, <http://howardhinnant.github.io/date_algorithms.html>).
// `doe`/`doy`/`yoe` are the canonical names from the published algorithm;
// renaming them for clippy would obscure the citation.
#[allow(clippy::similar_names)]
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if m <= 2 { y + 1 } else { y };
    #[allow(clippy::cast_sign_loss, clippy::cast_possible_truncation)]
    (year, m as u32, d as u32)
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::float_cmp,
        clippy::unwrap_used,
        clippy::too_many_lines,
        clippy::indexing_slicing
    )]

    use super::{DeriveConfig, derive, encode_polyline, is_autopilot_engaged, utc_civil_date};
    use crate::model::{ClipEventInput, DeriveClip, DeriveWaypoint, EventType, FolderClass};
    use scannerd::clip_event::rounded_tz_offset;
    use teslausb_core::sei::tesla::{AutopilotState, Gear};

    fn wp(frame: i64, offset_ms: f64, lat: f64, lon: f64, speed: f64) -> DeriveWaypoint {
        DeriveWaypoint {
            frame_index: frame,
            offset_ms,
            absolute_utc: None,
            lat,
            lon,
            speed,
            heading: 0.0,
            accel_x: None,
            accel_y: None,
            accel_z: None,
            autopilot_state: AutopilotState::None,
            gear: Gear::Drive,
            has_gps_fix: lat != 0.0 || lon != 0.0,
        }
    }

    fn clip(id: i64, started: i64, folder: FolderClass, wps: Vec<DeriveWaypoint>) -> DeriveClip {
        let gps = wps.iter().filter(|w| w.has_gps_fix).count();
        // attach absolute_utc the way ingest does (trunc offset)
        let wps = wps
            .into_iter()
            .map(|mut w| {
                #[allow(clippy::cast_possible_truncation)]
                let secs = (w.offset_ms / 1000.0) as i64;
                w.absolute_utc = Some(started + secs);
                w
            })
            .collect();
        DeriveClip {
            clip_id: id,
            canonical_key: format!("0:TeslaCam/{}/{started}/{started}", folder.as_db_str()),
            clip_started_utc: started,
            folder_class: folder,
            gps_waypoint_count: i64::try_from(gps).unwrap_or(0),
            waypoints: wps,
        }
    }

    fn clip_event(
        event_dir_key: &str,
        bucket: FolderClass,
        timestamp_utc: i64,
        timestamp_local_naive: i64,
    ) -> ClipEventInput {
        ClipEventInput {
            event_dir_key: event_dir_key.to_owned(),
            bucket,
            primary_clip_id: Some(1),
            primary_started_utc: Some(1_700_000_000),
            primary_started_trusted: true,
            est_lat: Some(42.0),
            est_lon: Some(-83.0),
            reason: Some("user_interaction_honk".to_owned()),
            city: Some("grand_blanc".to_owned()),
            camera: Some("front".to_owned()),
            timestamp_utc,
            timestamp_local_naive,
            timestamp_has_offset: true,
        }
    }

    #[test]
    fn autopilot_engaged_set() {
        assert!(is_autopilot_engaged(AutopilotState::SelfDriving));
        assert!(is_autopilot_engaged(AutopilotState::Autosteer));
        assert!(is_autopilot_engaged(AutopilotState::Tacc));
        assert!(!is_autopilot_engaged(AutopilotState::None));
        assert!(!is_autopilot_engaged(AutopilotState::Unknown(7)));
    }

    #[test]
    fn utc_civil_date_known_values() {
        assert_eq!(utc_civil_date(0), "1970-01-01");
        // 2021-01-01T00:00:00Z = 1609459200
        assert_eq!(utc_civil_date(1_609_459_200), "2021-01-01");
        // 2026-06-01T20:10:04Z = 1780344604
        assert_eq!(utc_civil_date(1_780_344_604), "2026-06-01");
    }

    #[test]
    fn epoch_from_tesla_timestamp_round_trips() {
        // Known UTC instants the civil-date test already pins.
        assert_eq!(
            super::epoch_from_tesla_timestamp("1970-01-01_00-00-00"),
            Some(0)
        );
        assert_eq!(
            super::epoch_from_tesla_timestamp("2021-01-01_00-00-00"),
            Some(1_609_459_200)
        );
        assert_eq!(
            super::epoch_from_tesla_timestamp("2026-06-01_20-10-04"),
            Some(1_780_344_604)
        );
    }

    #[test]
    fn epoch_from_tesla_timestamp_rejects_malformed() {
        assert_eq!(super::epoch_from_tesla_timestamp(""), None);
        assert_eq!(super::epoch_from_tesla_timestamp("2026-06-01"), None);
        assert_eq!(
            super::epoch_from_tesla_timestamp("2026-13-01_00-00-00"),
            None
        );
        assert_eq!(
            super::epoch_from_tesla_timestamp("2026-06-32_00-00-00"),
            None
        );
        assert_eq!(
            super::epoch_from_tesla_timestamp("2026-06-01_24-00-00"),
            None
        );
        assert_eq!(
            super::epoch_from_tesla_timestamp("xxxx-06-01_00-00-00"),
            None
        );
    }

    #[test]
    fn two_clips_split_on_gap() {
        // Two clips 10 minutes apart, each moving ~1 km, must form 2 trips.
        let a = clip(
            1,
            1_000,
            FolderClass::RecentClips,
            vec![
                wp(0, 0.0, 40.000, -75.000, 20.0),
                wp(1, 1000.0, 40.010, -75.000, 20.0),
            ],
        );
        let b = clip(
            2,
            1_000 + 600,
            FolderClass::RecentClips,
            vec![
                wp(0, 0.0, 41.000, -75.000, 20.0),
                wp(1, 1000.0, 41.010, -75.000, 20.0),
            ],
        );
        let d = derive(&[a, b], &[], DeriveConfig::default());
        assert_eq!(d.trips.len(), 2);
    }

    #[test]
    fn two_clips_merge_within_gap() {
        let a = clip(
            1,
            1_000,
            FolderClass::RecentClips,
            vec![
                wp(0, 0.0, 40.000, -75.000, 20.0),
                wp(1, 1000.0, 40.010, -75.000, 20.0),
            ],
        );
        let b = clip(
            2,
            1_060,
            FolderClass::RecentClips,
            vec![
                wp(0, 0.0, 40.020, -75.000, 20.0),
                wp(1, 1000.0, 40.030, -75.000, 20.0),
            ],
        );
        let d = derive(&[a, b], &[], DeriveConfig::default());
        assert_eq!(d.trips.len(), 1);
        assert_eq!(d.trips[0].clip_ids, vec![1, 2]);
    }

    #[test]
    fn short_trip_is_dropped() {
        // ~5 m of movement is below the 50 m gate.
        let a = clip(
            1,
            1_000,
            FolderClass::RecentClips,
            vec![
                wp(0, 0.0, 40.000_00, -75.0, 1.0),
                wp(1, 1000.0, 40.000_05, -75.0, 1.0),
            ],
        );
        let d = derive(&[a], &[], DeriveConfig::default());
        assert!(d.trips.is_empty());
    }

    #[test]
    fn sentry_clip_emits_single_event() {
        let mut w = wp(0, 0.0, 0.0, 0.0, 0.0);
        w.has_gps_fix = false;
        let s = clip(9, 5_000, FolderClass::SentryClips, vec![w]);
        let d = derive(&[s], &[], DeriveConfig::default());
        assert!(d.trips.is_empty());
        assert_eq!(d.sentry_events.len(), 1);
        assert_eq!(d.sentry_events[0].event_type, EventType::Sentry);
        assert_eq!(d.sentry_events[0].t, 5_000);
    }

    #[test]
    fn event_thresholds_and_descriptions() {
        let mut hard_brake = wp(0, 0.0, 40.0, -75.0, 10.0);
        hard_brake.accel_x = Some(-5.0); // harsh (< -4, not < -7)
        let mut emerg = wp(1, 100.0, 40.001, -75.0, 10.0);
        emerg.accel_x = Some(-8.0); // emergency (< -7)
        let mut accel = wp(2, 200.0, 40.002, -75.0, 10.0);
        accel.accel_x = Some(4.0); // hard accel (> 3.5)
        let mut turn = wp(3, 300.0, 40.003, -75.0, 10.0);
        turn.accel_y = Some(-4.5); // |ay| > 4
        let mut fast = wp(4, 400.0, 40.004, -75.0, 40.0); // > 35.76
        fast.accel_x = Some(0.0);

        let c = clip(
            1,
            1_000,
            FolderClass::RecentClips,
            vec![hard_brake, emerg, accel, turn, fast],
        );
        let d = derive(&[c], &[], DeriveConfig::default());
        assert_eq!(d.trips.len(), 1);
        let types: Vec<EventType> = d.trips[0].events.iter().map(|e| e.event_type).collect();
        assert!(types.contains(&EventType::HarshBraking));
        assert!(types.contains(&EventType::EmergencyBraking));
        assert!(types.contains(&EventType::HardAcceleration));
        assert!(types.contains(&EventType::SharpTurn));
        assert!(types.contains(&EventType::SpeedLimitExceeded));
        // Emergency supersedes harsh: emerg waypoint emits exactly one accel event.
        let emerg_event = d.trips[0]
            .events
            .iter()
            .find(|e| e.event_type == EventType::EmergencyBraking)
            .unwrap();
        assert_eq!(
            emerg_event.description,
            "Emergency braking detected (-8.00 m/s^2)"
        );
    }

    #[test]
    fn autopilot_transition_events() {
        let mut off1 = wp(0, 0.0, 40.0, -75.0, 20.0);
        off1.autopilot_state = AutopilotState::None;
        let mut on = wp(1, 100.0, 40.01, -75.0, 20.0);
        on.autopilot_state = AutopilotState::Autosteer;
        let mut off2 = wp(2, 200.0, 40.02, -75.0, 20.0);
        off2.autopilot_state = AutopilotState::None;
        let c = clip(1, 1_000, FolderClass::RecentClips, vec![off1, on, off2]);
        let d = derive(&[c], &[], DeriveConfig::default());
        let ap: Vec<EventType> = d.trips[0]
            .events
            .iter()
            .filter(|e| {
                matches!(
                    e.event_type,
                    EventType::AutopilotEngaged | EventType::AutopilotDisengaged
                )
            })
            .map(|e| e.event_type)
            .collect();
        assert_eq!(
            ap,
            vec![EventType::AutopilotEngaged, EventType::AutopilotDisengaged]
        );
        let engaged = d.trips[0]
            .events
            .iter()
            .find(|e| e.event_type == EventType::AutopilotEngaged)
            .unwrap();
        assert_eq!(engaged.description, "Autopilot engaged (AUTOSTEER)");
    }

    #[test]
    fn speed_limit_disabled_at_zero() {
        let fast = wp(0, 0.0, 40.0, -75.0, 99.0);
        let fast2 = wp(1, 1000.0, 40.02, -75.0, 99.0);
        let c = clip(1, 1_000, FolderClass::RecentClips, vec![fast, fast2]);
        let cfg = DeriveConfig {
            speed_limit_mps: 0.0,
            ..DeriveConfig::default()
        };
        let d = derive(&[c], &[], cfg);
        assert!(
            d.trips[0]
                .events
                .iter()
                .all(|e| e.event_type != EventType::SpeedLimitExceeded)
        );
    }

    #[test]
    fn polyline_blob_is_nonempty_for_moving_trip() {
        let c = clip(
            1,
            1_000,
            FolderClass::RecentClips,
            vec![
                wp(0, 0.0, 40.000, -75.0, 20.0),
                wp(1, 1000.0, 40.005, -75.0, 20.0),
                wp(2, 2000.0, 40.010, -75.0, 20.0),
            ],
        );
        let d = derive(&[c], &[], DeriveConfig::default());
        assert!(!d.trips[0].polyline.is_empty());
        // 1 segment, ≥2 points → header (4) + seg-count (4) + ≥2×16.
        assert!(d.trips[0].polyline.len() >= 4 + 4 + 32);
    }

    #[test]
    fn clip_event_saved_pin_and_description_humanized() {
        let event = clip_event(
            "0:TeslaCam/SavedClips/2026-06-01_20-10-04",
            FolderClass::SavedClips,
            1_780_344_604,
            1_780_344_604,
        );
        let d = derive(&[], &[event], DeriveConfig::default());
        assert_eq!(d.sentry_events.len(), 1);
        let got = &d.sentry_events[0];
        assert_eq!(got.event_type, EventType::Saved);
        assert_eq!(got.lat, Some(42.0));
        assert_eq!(got.lon, Some(-83.0));
        assert_eq!(
            got.description,
            "User Interaction Honk | Grand Blanc | Front"
        );
    }

    #[test]
    fn clip_event_description_defaults_event_clip_for_missing_or_blank_reason() {
        let mut missing_reason = clip_event(
            "0:TeslaCam/SavedClips/2026-06-01_20-10-04",
            FolderClass::SavedClips,
            2_000,
            2_000,
        );
        missing_reason.reason = None;
        missing_reason.city = None;
        missing_reason.camera = None;

        let mut blank_reason = clip_event(
            "0:TeslaCam/SentryClips/2026-06-01_20-11-04",
            FolderClass::SentryClips,
            2_100,
            2_100,
        );
        blank_reason.reason = Some("   ".to_owned());
        blank_reason.city = None;
        blank_reason.camera = None;

        let d = derive(
            &[],
            &[missing_reason, blank_reason],
            DeriveConfig::default(),
        );
        assert_eq!(d.sentry_events.len(), 2);
        assert!(
            d.sentry_events
                .iter()
                .all(|e| e.description == "Event clip")
        );
    }

    #[test]
    fn clip_event_timestamp_anchoring_and_frame_rules() {
        let mut has_offset = clip_event(
            "0:TeslaCam/SavedClips/has-offset",
            FolderClass::SavedClips,
            5_000,
            4_000,
        );
        has_offset.timestamp_has_offset = true;
        has_offset.primary_started_utc = Some(4_000);

        let mut trusted_anchor = clip_event(
            "0:TeslaCam/SavedClips/trusted-anchor",
            FolderClass::SavedClips,
            0,
            1_700_012_345,
        );
        trusted_anchor.timestamp_has_offset = false;
        trusted_anchor.primary_started_utc = Some(1_700_000_000);
        trusted_anchor.primary_started_trusted = true;

        let mut untrusted_anchor = clip_event(
            "0:TeslaCam/SentryClips/untrusted-anchor",
            FolderClass::SentryClips,
            9_999,
            8_888,
        );
        untrusted_anchor.timestamp_has_offset = false;
        untrusted_anchor.primary_started_trusted = false;

        let d = derive(
            &[],
            &[has_offset, trusted_anchor.clone(), untrusted_anchor.clone()],
            DeriveConfig::default(),
        );
        assert_eq!(d.sentry_events.len(), 3);

        let has_offset_event = d
            .sentry_events
            .iter()
            .find(|e| e.clip_id == Some(1) && e.t == 5_000)
            .unwrap();
        assert_eq!(has_offset_event.t, 5_000);

        let expected_offset =
            rounded_tz_offset(trusted_anchor.timestamp_local_naive, 1_700_000_000).unwrap_or(0);
        let expected_t = trusted_anchor.timestamp_local_naive - expected_offset;
        let trusted_event = d.sentry_events.iter().find(|e| e.t == expected_t).unwrap();
        assert_eq!(trusted_event.t, expected_t);

        let untrusted_event = d
            .sentry_events
            .iter()
            .find(|e| e.t == untrusted_anchor.timestamp_local_naive)
            .unwrap();
        assert_eq!(untrusted_event.t, untrusted_anchor.timestamp_local_naive);
        assert_eq!(untrusted_event.front_frame_index, None);
        assert_eq!(untrusted_event.front_frame_offset_ms, None);
    }

    #[test]
    fn clip_event_frame_fields_and_clipless_pin_behavior() {
        let mut with_frame = clip_event(
            "0:TeslaCam/SavedClips/frame",
            FolderClass::SavedClips,
            1_002,
            1_002,
        );
        with_frame.primary_started_utc = Some(1_000);
        with_frame.timestamp_has_offset = true;
        with_frame.primary_started_trusted = true;

        let mut clipless = clip_event(
            "0:TeslaCam/SavedClips/clipless",
            FolderClass::SavedClips,
            2_000,
            2_000,
        );
        clipless.primary_clip_id = None;
        clipless.primary_started_utc = None;
        clipless.primary_started_trusted = false;

        let d = derive(&[], &[with_frame, clipless], DeriveConfig::default());
        assert_eq!(d.sentry_events.len(), 2);
        let framed = d.sentry_events.iter().find(|e| e.t == 1_002).unwrap();
        assert_eq!(framed.front_frame_index, Some(72));
        assert_eq!(framed.front_frame_offset_ms, Some(2_000));

        let clipless_event = d.sentry_events.iter().find(|e| e.t == 2_000).unwrap();
        assert_eq!(clipless_event.clip_id, None);
        assert_eq!(clipless_event.lat, Some(42.0));
        assert_eq!(clipless_event.lon, Some(-83.0));
        assert_eq!(clipless_event.front_frame_index, None);
        assert_eq!(clipless_event.front_frame_offset_ms, None);
    }

    #[test]
    fn clip_event_sentry_folder_collapse_suppresses_segment_events() {
        let sentry_clips = [
            DeriveClip {
                clip_id: 10,
                canonical_key: "0:TeslaCam/SentryClips/2026-06-01_20-10-04/2026-06-01_20-10-04"
                    .to_owned(),
                clip_started_utc: 10_000,
                folder_class: FolderClass::SentryClips,
                gps_waypoint_count: 0,
                waypoints: Vec::new(),
            },
            DeriveClip {
                clip_id: 11,
                canonical_key: "0:TeslaCam/SentryClips/2026-06-01_20-10-04/2026-06-01_20-10-34"
                    .to_owned(),
                clip_started_utc: 10_030,
                folder_class: FolderClass::SentryClips,
                gps_waypoint_count: 0,
                waypoints: Vec::new(),
            },
            DeriveClip {
                clip_id: 12,
                canonical_key: "0:TeslaCam/SentryClips/2026-06-01_20-10-04/2026-06-01_20-11-04"
                    .to_owned(),
                clip_started_utc: 10_060,
                folder_class: FolderClass::SentryClips,
                gps_waypoint_count: 0,
                waypoints: Vec::new(),
            },
        ];
        let event = clip_event(
            "0:TeslaCam/SentryClips/2026-06-01_20-10-04",
            FolderClass::SentryClips,
            10_010,
            10_010,
        );
        let d = derive(&sentry_clips, &[event], DeriveConfig::default());
        assert_eq!(d.sentry_events.len(), 1);
        assert_eq!(d.sentry_events[0].event_type, EventType::Sentry);
        assert_eq!(
            d.sentry_events[0].description,
            "User Interaction Honk | Grand Blanc | Front"
        );
    }

    #[test]
    fn clip_event_invalid_geo_becomes_none_but_event_is_kept() {
        let mut zero_geo = clip_event(
            "0:TeslaCam/SavedClips/zero-geo",
            FolderClass::SavedClips,
            2_500,
            2_500,
        );
        zero_geo.est_lat = Some(0.0);
        zero_geo.est_lon = Some(0.0);

        let mut out_of_range_geo = clip_event(
            "0:TeslaCam/SentryClips/out-of-range",
            FolderClass::SentryClips,
            2_600,
            2_600,
        );
        out_of_range_geo.est_lat = Some(123.0);
        out_of_range_geo.est_lon = Some(-181.0);

        let mut none_geo = clip_event(
            "0:TeslaCam/SavedClips/none-geo",
            FolderClass::SavedClips,
            2_700,
            2_700,
        );
        none_geo.est_lat = None;
        none_geo.est_lon = Some(-83.0);

        let d = derive(
            &[],
            &[zero_geo, out_of_range_geo, none_geo],
            DeriveConfig::default(),
        );
        assert_eq!(d.sentry_events.len(), 3);
        assert!(
            d.sentry_events
                .iter()
                .all(|event| event.lat.is_none() && event.lon.is_none())
        );
    }

    #[test]
    fn encode_polyline_roundtrip_header() {
        let blob = encode_polyline(&[vec![(40.0, -75.0), (41.0, -76.0)]]);
        // segment_count = 1
        assert_eq!(&blob[0..4], &1u32.to_be_bytes());
        // point_count = 2
        assert_eq!(&blob[4..8], &2u32.to_be_bytes());
    }
}
