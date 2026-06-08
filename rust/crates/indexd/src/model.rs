//! Domain types shared by the derivation and the store.
//!
//! These mirror the v1 production worker's derivation model
//! (`teslausb-worker` materializer + the `mapping_*` Python references)
//! so the derived trips/events are byte-for-byte the ones a user sees
//! today. Field-level parity notes cite the source.

use teslausb_core::sei::tesla::{AutopilotState, Gear};

/// Tesla source-folder classification for a clip. Names match contract
/// D1 (`clips.folder_class`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FolderClass {
    /// `RecentClips` — the rolling dashcam buffer.
    RecentClips,
    /// `SavedClips` — user-saved dashcam events.
    SavedClips,
    /// `SentryClips` — Sentry-mode triggered recordings.
    SentryClips,
    /// `TeslaTrackMode` — track-mode recordings.
    TeslaTrackMode,
    /// `ArchivedClips` — Pi-side archived copies.
    ArchivedClips,
}

impl FolderClass {
    /// The D1 `folder_class` string.
    #[must_use]
    pub fn as_db_str(self) -> &'static str {
        match self {
            Self::RecentClips => "RecentClips",
            Self::SavedClips => "SavedClips",
            Self::SentryClips => "SentryClips",
            Self::TeslaTrackMode => "TeslaTrackMode",
            Self::ArchivedClips => "ArchivedClips",
        }
    }

    /// Whether this is the Sentry bucket. Sentry clips are routed to the
    /// single-event sentry path, never clustered into driving trips
    /// (`materializer.rs::rebuild_all` partitions on `bucket == "sentry"`).
    #[must_use]
    pub fn is_sentry(self) -> bool {
        matches!(self, Self::SentryClips)
    }

    /// Classify from a clip's directory path (case-insensitive substring,
    /// most specific first).
    #[must_use]
    pub fn from_path(path: &str) -> Self {
        let lower = path.to_ascii_lowercase();
        if lower.contains("sentryclips") {
            Self::SentryClips
        } else if lower.contains("savedclips") {
            Self::SavedClips
        } else if lower.contains("teslatrackmode") || lower.contains("trackclips") {
            Self::TeslaTrackMode
        } else if lower.contains("archivedclips") {
            Self::ArchivedClips
        } else {
            Self::RecentClips
        }
    }

    /// Parse from the D1 `folder_class` string; unknown values fall back
    /// to [`Self::RecentClips`].
    #[must_use]
    pub fn from_db_str(s: &str) -> Self {
        match s {
            "SavedClips" => Self::SavedClips,
            "SentryClips" => Self::SentryClips,
            "TeslaTrackMode" => Self::TeslaTrackMode,
            "ArchivedClips" => Self::ArchivedClips,
            _ => Self::RecentClips,
        }
    }
}

/// Severity ordinal stored in `events.severity` (D1 says "indexd-derived
/// ordinal"). v1 used the strings `info`/`warning`/`critical`; this is the
/// ordinal mapping flagged in the build notes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    /// `info` — autopilot transitions, sentry.
    Info = 1,
    /// `warning` — speed / hard-accel / harsh-brake / sharp-turn.
    Warning = 2,
    /// `critical` — emergency braking.
    Critical = 3,
}

impl Severity {
    /// The v1 string form (used for golden-file parity assertions).
    #[must_use]
    pub fn as_v1_str(self) -> &'static str {
        match self {
            Self::Info => "info",
            Self::Warning => "warning",
            Self::Critical => "critical",
        }
    }

    /// The D1 ordinal.
    #[must_use]
    pub fn ordinal(self) -> i64 {
        self as i64
    }
}

/// A derived event type. Strings match v1's `mapping_event_derivation.py`
/// exactly (NOT D1's indicative short names — those are reconciled at
/// freeze).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventType {
    /// Speed exceeded the configured limit (`speed_limit_exceeded`).
    SpeedLimitExceeded,
    /// Hard acceleration (`hard_acceleration`).
    HardAcceleration,
    /// Harsh braking (`harsh_braking`).
    HarshBraking,
    /// Emergency braking (`emergency_braking`) — supersedes harsh.
    EmergencyBraking,
    /// Sharp turn (`sharp_turn`).
    SharpTurn,
    /// Autopilot engaged transition (`autopilot_engaged`).
    AutopilotEngaged,
    /// Autopilot disengaged transition (`autopilot_disengaged`).
    AutopilotDisengaged,
    /// Sentry-mode recording (`sentry`).
    Sentry,
}

impl EventType {
    /// The v1 `event_type` string.
    #[must_use]
    pub fn as_db_str(self) -> &'static str {
        match self {
            Self::SpeedLimitExceeded => "speed_limit_exceeded",
            Self::HardAcceleration => "hard_acceleration",
            Self::HarshBraking => "harsh_braking",
            Self::EmergencyBraking => "emergency_braking",
            Self::SharpTurn => "sharp_turn",
            Self::AutopilotEngaged => "autopilot_engaged",
            Self::AutopilotDisengaged => "autopilot_disengaged",
            Self::Sentry => "sentry",
        }
    }

    /// Severity bucket (`mapping_event_derivation.py::_EVENT_SEVERITIES`).
    #[must_use]
    pub fn severity(self) -> Severity {
        match self {
            Self::EmergencyBraking => Severity::Critical,
            Self::SpeedLimitExceeded
            | Self::HardAcceleration
            | Self::HarshBraking
            | Self::SharpTurn => Severity::Warning,
            Self::AutopilotEngaged | Self::AutopilotDisengaged | Self::Sentry => Severity::Info,
        }
    }
}

/// One sampled waypoint normalized for derivation. Built from a
/// [`scannerd::seiwalk::Waypoint`] plus the clip's resolved start time.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DeriveWaypoint {
    /// VCL frame index within the clip (v1 `frame_index`).
    pub frame_index: i64,
    /// Milliseconds since clip start (v1 `timestamp_ms`).
    pub offset_ms: f64,
    /// Absolute UTC epoch seconds = `clip_started_utc + trunc(offset_ms/1000)`.
    pub absolute_utc: Option<i64>,
    /// WGS-84 latitude, degrees.
    pub lat: f64,
    /// WGS-84 longitude, degrees.
    pub lon: f64,
    /// Vehicle speed, m/s.
    pub speed: f64,
    /// Compass heading, degrees.
    pub heading: f64,
    /// Linear acceleration X, m/s² (longitudinal; +accel / −brake).
    pub accel_x: Option<f64>,
    /// Linear acceleration Y, m/s² (lateral).
    pub accel_y: Option<f64>,
    /// Linear acceleration Z, m/s².
    pub accel_z: Option<f64>,
    /// Autopilot state at this frame.
    pub autopilot_state: AutopilotState,
    /// Gear at this frame.
    pub gear: Gear,
    /// Whether this frame carried a usable GPS fix
    /// (`lat != 0 || lon != 0`).
    pub has_gps_fix: bool,
}

impl DeriveWaypoint {
    /// True when the GPS coordinate is usable for distance / polyline:
    /// finite and not the `(0,0)` "not yet locked" sentinel
    /// (`materializer.rs::total_distance_km`).
    #[must_use]
    pub fn has_valid_geo(&self) -> bool {
        self.lat.is_finite() && self.lon.is_finite() && !(self.lat == 0.0 && self.lon == 0.0)
    }
}

/// A front-camera clip with its sampled waypoints, ready for derivation.
/// `clip_id` is the DB id assigned at ingest; derivation keys events and
/// trip membership off it.
#[derive(Debug, Clone)]
pub struct DeriveClip {
    /// DB id of the clip row.
    pub clip_id: i64,
    /// Resolved start instant, UTC epoch seconds (mvhd-first).
    pub clip_started_utc: i64,
    /// Source-folder classification.
    pub folder_class: FolderClass,
    /// Count of waypoints carrying a GPS fix (v1 `gps_waypoint_count`).
    pub gps_waypoint_count: i64,
    /// Sampled waypoints, in frame order.
    pub waypoints: Vec<DeriveWaypoint>,
}

/// One persisted GPS polyline point of a trip (`trip_points` row).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TripPoint {
    /// UTC epoch seconds.
    pub t: i64,
    /// Latitude, degrees.
    pub lat: f64,
    /// Longitude, degrees.
    pub lon: f64,
    /// Speed, m/s.
    pub speed: f64,
    /// Heading, degrees.
    pub heading: f64,
}

/// A derived event, before a DB trip id is assigned. `clip_id` is the
/// waypoint's clip (v1 `detected_events.clip_id`).
#[derive(Debug, Clone, PartialEq)]
pub struct DerivedEvent {
    /// Clip the event came from (`None` only for clip-less synthetics).
    pub clip_id: Option<i64>,
    /// Event type.
    pub event_type: EventType,
    /// UTC epoch seconds.
    pub t: i64,
    /// Latitude, degrees (nullable: stationary sentry lacks geo).
    pub lat: Option<f64>,
    /// Longitude, degrees.
    pub lon: Option<f64>,
    /// v1 VCL frame index into the front cam (parity field).
    pub front_frame_index: Option<i64>,
    /// Milliseconds into the front cam to jump to (D1 `front_frame_offset`).
    pub front_frame_offset_ms: Option<i64>,
    /// Human description string (verbatim v1 format).
    pub description: String,
}

impl DerivedEvent {
    /// The severity ordinal for the DB row.
    #[must_use]
    pub fn severity(&self) -> Severity {
        self.event_type.severity()
    }
}

/// A fully derived driving trip.
#[derive(Debug, Clone, PartialEq)]
pub struct DerivedTrip {
    /// UTC civil date `YYYY-MM-DD` from `started_at` (flagged: UTC, the
    /// only stable civil date on an RTC-less Pi).
    pub day: String,
    /// Trip start, UTC epoch seconds.
    pub started_at: i64,
    /// Trip end, UTC epoch seconds.
    pub ended_at: i64,
    /// Bounding box of valid GPS points.
    pub bbox_min_lat: Option<f64>,
    /// Bounding box of valid GPS points.
    pub bbox_min_lon: Option<f64>,
    /// Bounding box of valid GPS points.
    pub bbox_max_lat: Option<f64>,
    /// Bounding box of valid GPS points.
    pub bbox_max_lon: Option<f64>,
    /// Total distance, metres (haversine over valid points).
    pub distance_m: f64,
    /// Member clip DB ids, in trip order.
    pub clip_ids: Vec<i64>,
    /// Durable per-point polyline rows (OQ-2).
    pub points: Vec<TripPoint>,
    /// Cached RDP-simplified polyline blob (OQ-2). See
    /// [`crate::derive::encode_polyline`] for the format.
    pub polyline: Vec<u8>,
    /// Events derived within this trip.
    pub events: Vec<DerivedEvent>,
}

/// The full result of deriving over a scan's front clips.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Derivation {
    /// Materialized driving trips (passed the min-distance gate).
    pub trips: Vec<DerivedTrip>,
    /// Sentry singleton events (`trip_id` NULL at ingest).
    pub sentry_events: Vec<DerivedEvent>,
}
