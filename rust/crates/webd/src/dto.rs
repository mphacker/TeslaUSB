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

/// A cursor-paginated page of items (`?cursor=<opaque>&limit=`).
///
/// `next_cursor` is an opaque string the client must echo back verbatim to fetch
/// the next page; `null` means the end has been reached.
#[derive(Debug, Serialize)]
pub(crate) struct Page<T> {
    /// The items in this page, ordered newest-first by `(date DESC, id DESC)`.
    pub items: Vec<T>,
    /// Opaque next-page cursor, or `null` at the end.
    pub next_cursor: Option<String>,
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

/// One `{severity, count}` row of `events_by_severity` in `GET /api/analytics`.
/// `severity` is the indexd ordinal (1=info, 2=warning, 3=critical).
#[derive(Debug, Serialize)]
pub(crate) struct SeverityCount {
    /// `events.severity` ordinal.
    pub severity: i64,
    /// Number of events at this severity.
    pub count: i64,
}

/// Per-folder-class footage aggregate (`video_stats.by_folder_class`).
#[derive(Debug, Serialize)]
pub(crate) struct FolderClassStat {
    /// `clips.folder_class` (e.g. `RecentClips`, `SavedClips`, `SentryClips`).
    pub folder_class: String,
    /// Number of distinct clips in this folder class.
    pub clip_count: i64,
    /// Number of camera-angle files across those clips.
    pub file_count: i64,
    /// Total bytes across those angle files (`SUM(angles.size_bytes)`).
    pub size_bytes: i64,
}

/// Footage aggregates over `clips` ⋈ `angles` (`GET /api/analytics`).
/// The catalog-derived parity of v1's storage-analytics video/folder section
/// (computed from indexed `size_bytes`, not a live filesystem walk).
#[derive(Debug, Serialize)]
pub(crate) struct VideoStats {
    /// Total distinct clips.
    pub total_clips: i64,
    /// Total camera-angle files.
    pub total_files: i64,
    /// Total bytes across all angle files.
    pub total_bytes: i64,
    /// Breakdown by folder class, ordered by class name.
    pub by_folder_class: Vec<FolderClassStat>,
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
    /// `SUM(trips.ended_at - trips.started_at)`, seconds.
    pub total_drive_time_s: i64,
    /// `COUNT(events WHERE severity >= 2)` — warnings + critical.
    pub warning_event_count: i64,
    /// `AVG(trip_points.speed)`, m/s (`None` when no speed samples exist).
    pub avg_speed_mps: Option<f64>,
    /// `MAX(trip_points.speed)`, m/s (`None` when no speed samples exist).
    pub max_speed_mps: Option<f64>,
    /// Event counts grouped by severity ordinal, ascending.
    pub events_by_severity: Vec<SeverityCount>,
    /// Footage aggregates over `clips` ⋈ `angles`.
    pub video_stats: VideoStats,
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

/// The installed lock chime (`GET /api/chimes` → `installed`), read from the
/// `media_entries` catalog (`indexd` v2). `None` at the wire level (serialized
/// as `"installed": null`) when no chime is installed OR the catalog predates
/// the media inventory.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub(crate) struct InstalledChimeDto {
    /// `media_entries.name` (e.g. `LockChime.wav`).
    pub name: String,
    /// `media_entries.rel_path` (the fixed `LockChime.wav` at the p2 root).
    pub rel_path: String,
    /// `media_entries.size_bytes`.
    pub size_bytes: i64,
    /// `media_entries.modified` — best-effort naive-local
    /// `YYYY-MM-DDThh:mm:ss`, or `null` when the on-disk timestamp was
    /// unreadable.
    pub modified: Option<String>,
}

/// The `GET /api/chimes` response envelope: the single installed lock chime,
/// or `null` when none is present / the catalog has no media inventory.
#[derive(Debug, Serialize)]
pub(crate) struct ChimesDto {
    /// The installed lock chime, or `null`.
    pub installed: Option<InstalledChimeDto>,
}

/// One file inventoried on the MEDIA (p2) partition for the toybox categories
/// (Boombox, Music, `LightShow`, `LicensePlate`, Wraps). Mirrors the catalog
/// `media_entries` row shape without a `category` column — callers derive
/// the category from the `rel_path` prefix.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub(crate) struct MediaItemDto {
    /// `media_entries.name` (file name component, e.g. `horn.wav`).
    pub name: String,
    /// `media_entries.rel_path` (partition-root-relative, e.g.
    /// `Boombox/horn.wav`). Also serves as the delete `id`.
    pub rel_path: String,
    /// `media_entries.size_bytes`.
    pub size_bytes: i64,
    /// `media_entries.modified` — best-effort naive-local
    /// `YYYY-MM-DDThh:mm:ss`, or `null` when the on-disk timestamp was
    /// unreadable.
    pub modified: Option<String>,
}

/// Generic response envelope for a toybox media category list
/// (`GET /api/boombox`, `/api/music`, etc.).
#[derive(Debug, Serialize)]
pub(crate) struct MediaListDto {
    /// All installed items for this category (may be empty).
    pub items: Vec<MediaItemDto>,
}
