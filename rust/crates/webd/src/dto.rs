//! Wire DTOs for the read API (contract D2 §4).
//!
//! These are `webd`-owned serde types, not `indexd` types: `indexd`'s
//! `model.rs` holds pre-DB-id derivation types, so the read API binds its own
//! DTOs straight to the as-built columns. Time fields are UTC epoch **seconds**
//! (`*_at`, `t`); `*_ms` fields are milliseconds; speeds are m/s (the SPA
//! converts units client-side).
//!
//! The `Dto` suffix and grouped field names mirror contract D2, so the
//! `module_name_repetitions` / `struct_field_names` pedantic lints are allowed
//! here rather than fighting the contract's naming.
#![allow(clippy::module_name_repetitions, clippy::struct_field_names)]

use serde::Serialize;

use crate::polyline::Polyline;

/// A cursor-paginated page of items (D2 §6 OQ-2: `?after=<id>&limit=`).
///
/// `next_cursor` is the id to pass as `after` for the next page, present when a
/// full page was returned (there may be more). When the total is an exact
/// multiple of `limit`, the final follow-up page is empty — a benign, standard
/// artefact of opaque-cursor pagination.
#[derive(Debug, Serialize)]
pub(crate) struct Page<T> {
    /// The items in this page, ordered by ascending id.
    pub items: Vec<T>,
    /// The id to pass as `after` to fetch the next page, or `null` at the end.
    pub next_cursor: Option<i64>,
    /// The effective page size that was applied.
    pub limit: i64,
}

/// One entry in `GET /api/days`: a civil day that has driving trips, with
/// rolled-up counts. `day` is `trips.day` (UTC civil date — the only stable
/// civil date on the RTC-less Pi). `event_count` counts events linked to trips
/// on that day; trip-less sentry events are not attributed to a day here.
#[derive(Debug, Serialize)]
pub(crate) struct DaySummary {
    /// `trips.day` (`YYYY-MM-DD`).
    pub day: String,
    /// Number of trips on this day (`COUNT(trips)`).
    pub trip_count: i64,
    /// Number of events linked to trips on this day.
    pub event_count: i64,
    /// Total distance on this day (`SUM(trips.distance_m)`), metres.
    pub distance_m: f64,
}

/// A trip's bounding box. Present only when all four `trips.bbox_*` columns are
/// populated (a trip with no valid GPS has no bbox).
#[derive(Debug, Serialize)]
pub(crate) struct Bbox {
    /// `trips.bbox_min_lat`.
    pub min_lat: f64,
    /// `trips.bbox_min_lon`.
    pub min_lon: f64,
    /// `trips.bbox_max_lat`.
    pub max_lat: f64,
    /// `trips.bbox_max_lon`.
    pub max_lon: f64,
}

/// A trip row (`GET /api/trips`), including the decoded cached polyline.
#[derive(Debug, Serialize)]
pub(crate) struct TripDto {
    /// `trips.id`.
    pub id: i64,
    /// `trips.day` (`YYYY-MM-DD`).
    pub day: String,
    /// `trips.started_at`, UTC epoch seconds.
    pub started_at: i64,
    /// `trips.ended_at`, UTC epoch seconds.
    pub ended_at: i64,
    /// Bounding box, or `null` when the trip has no valid-GPS bbox.
    pub bbox: Option<Bbox>,
    /// `trips.distance_m`, metres (nullable).
    pub distance_m: Option<f64>,
    /// `trips.point_count`.
    pub point_count: i64,
    /// Decoded `trips.polyline`: segments of `[lat, lon]` points. Empty when
    /// the blob is `NULL` or could not be decoded.
    pub polyline: Polyline,
}

/// One persisted GPS point of a trip (`trip_points`), in `seq` order.
#[derive(Debug, Serialize)]
pub(crate) struct TripPointDto {
    /// `trip_points.t`, UTC epoch seconds.
    pub t: i64,
    /// `trip_points.lat`, degrees.
    pub lat: f64,
    /// `trip_points.lon`, degrees.
    pub lon: f64,
    /// `trip_points.speed`, m/s (nullable).
    pub speed: Option<f64>,
    /// `trip_points.heading`, degrees (nullable).
    pub heading: Option<f64>,
}

/// A trip plus its persisted points (`GET /api/trips/:id`).
#[derive(Debug, Serialize)]
pub(crate) struct TripDetailDto {
    /// The trip row.
    #[serde(flatten)]
    pub trip: TripDto,
    /// The durable per-row polyline points, in `seq` order.
    pub points: Vec<TripPointDto>,
}

/// An event bubble (`GET /api/events`).
#[derive(Debug, Serialize)]
pub(crate) struct EventDto {
    /// `events.id`.
    pub id: i64,
    /// `events.type` (v1 event-type string, opaque to the wire).
    #[serde(rename = "type")]
    pub event_type: String,
    /// `events.severity` (indexd-derived ordinal, nullable).
    pub severity: Option<i64>,
    /// `events.t`, UTC epoch seconds.
    pub t: i64,
    /// `events.lat`, degrees (nullable).
    pub lat: Option<f64>,
    /// `events.lon`, degrees (nullable).
    pub lon: Option<f64>,
    /// `events.clip_id` (nullable).
    pub clip_id: Option<i64>,
    /// `events.trip_id` (nullable).
    pub trip_id: Option<i64>,
    /// `events.front_frame_index` — v1 VCL frame index (nullable).
    pub front_frame_index: Option<i64>,
    /// `events.front_frame_offset` — milliseconds into the front cam (nullable).
    pub front_frame_offset_ms: Option<i64>,
    /// `events.description` (nullable).
    pub description: Option<String>,
}

/// One camera angle within a clip (`angles`).
#[derive(Debug, Serialize)]
pub(crate) struct AngleDto {
    /// `angles.camera`.
    pub camera: String,
    /// `angles.view_kind` — **opaque passthrough**. A known D1-vs-code mismatch
    /// (code writes `"live"`; D1 says `archive|ro_usb`), so `webd` does not
    /// bind any enum/semantics to its value.
    pub view_kind: String,
    /// `angles.offset_ms` — start offset within the clip, milliseconds.
    pub offset_ms: i64,
    /// `angles.duration_s`, seconds (nullable).
    pub duration_s: Option<f64>,
    /// `angles.size_bytes` (nullable).
    pub size_bytes: Option<i64>,
}

/// A clip with its camera angles (`GET /api/clips`, `GET /api/clips/:id`).
#[derive(Debug, Serialize)]
pub(crate) struct ClipDto {
    /// `clips.id`.
    pub id: i64,
    /// `clips.canonical_key`.
    pub canonical_key: String,
    /// `clips.started_at`, UTC epoch seconds.
    pub started_at: i64,
    /// `clips.ended_at`, UTC epoch seconds (nullable).
    pub ended_at: Option<i64>,
    /// `clips.partition`.
    pub partition: String,
    /// `clips.folder_class`.
    pub folder_class: String,
    /// `clips.is_sentry`.
    pub is_sentry: bool,
    /// `clips.duration_s`, seconds (nullable).
    pub duration_s: Option<f64>,
    /// `clips.availability`.
    pub availability: String,
    /// Camera angles, ordered by `camera`.
    pub angles: Vec<AngleDto>,
}

/// One `{type, count}` row of `events_by_type` in `GET /api/analytics`.
#[derive(Debug, Serialize)]
pub(crate) struct EventTypeCount {
    /// `events.type`.
    #[serde(rename = "type")]
    pub event_type: String,
    /// Number of events of this type.
    pub count: i64,
}

/// One `{day, count, distance_m}` row of `trips_by_day` in `GET /api/analytics`.
#[derive(Debug, Serialize)]
pub(crate) struct DayTripCount {
    /// `trips.day`.
    pub day: String,
    /// Number of trips on this day.
    pub count: i64,
    /// Total distance on this day, metres.
    pub distance_m: f64,
}

/// Basic catalog aggregates (`GET /api/analytics`).
#[derive(Debug, Serialize)]
pub(crate) struct AnalyticsDto {
    /// `COUNT(trips)`.
    pub total_trips: i64,
    /// `SUM(trips.distance_m)`, metres.
    pub total_distance_m: f64,
    /// `COUNT(events)`.
    pub total_events: i64,
    /// Event counts grouped by `events.type`, ordered by type.
    pub events_by_type: Vec<EventTypeCount>,
    /// Trip counts + distance grouped by day, most recent first.
    pub trips_by_day: Vec<DayTripCount>,
}

/// One raw `prefs` row (`GET /api/settings`). Returned verbatim — `webd` does
/// not interpret or reshape settings (those policies are owned by other
/// services and are ASK-FIRST).
#[derive(Debug, Serialize)]
pub(crate) struct PrefDto {
    /// `prefs.key`.
    pub key: String,
    /// `prefs.value` (an opaque string; often JSON).
    pub value: String,
}
