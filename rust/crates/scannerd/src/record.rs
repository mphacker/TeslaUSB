//! The versioned `scannerd → indexd` fact contract.
//!
//! `scannerd` is the *only* process that touches untrusted, car-written
//! bytes (raw exFAT/MP4/SEI). It parses + stability-gates + extracts SEI
//! and emits the **facts** in this module to `indexd` over a local Unix
//! socket; `indexd` consumes them as **data** (never code), validates the
//! caps below, and writes the `SQLite` catalog. This type IS the trust
//! boundary: everything here is plain, bounded, `serde`-(de)serializable
//! data with no behavior that touches the image.
//!
//! Per [`scannerd.md`](../../../docs/specs/scannerd.md) §2.5 the records
//! carry *file identity, timestamps, partition, clip grouping, and the SEI
//! sample stream* — and nothing about trips/events (that is `indexd`'s
//! derivation, [`indexd.md`](../../../docs/specs/indexd.md) §1/§3).
//!
//! ## Versioning
//!
//! [`PROTOCOL_VERSION`] is stamped into every [`ScanBatch`]. The consumer
//! rejects a mismatched major version rather than guessing. New optional
//! fields can be added behind `#[serde(default)]` without a bump.

use serde::{Deserialize, Serialize};
use teslausb_core::sei::tesla::{AutopilotState, Gear};

/// Wire-format version stamped into every [`ScanBatch`]. Bump on any
/// breaking change to the record shape.
pub const PROTOCOL_VERSION: u32 = 1;

/// Hard cap on the present-key set a single batch may carry. A full
/// `TeslaCam` card is thousands of clips; 200k keys (~tens of MiB of short
/// strings) is a generous ceiling that still bounds consumer memory.
pub const MAX_PRESENT_KEYS: usize = 200_000;

/// Hard cap on emitted records per batch (eligible files). Far above any
/// real per-pass burst, but bounds a malicious producer.
pub const MAX_RECORDS_PER_BATCH: usize = 100_000;

/// Hard cap on waypoints carried for one clip. The indexer decimates SEI
/// to ~1/sec; even an hour-long clip is a few thousand. Bounds a corrupt
/// `mdat` from ballooning a single record.
pub const MAX_WAYPOINTS_PER_RECORD: usize = 100_000;

/// Hard cap on any single wire string (canonical key, path, camera, …).
pub const MAX_STRING_LEN: usize = 4096;

/// Tesla source-folder classification for a clip. Mirrors contract D1
/// `clips.folder_class`; the producer derives it from the directory path,
/// the consumer maps it straight onto its own `FolderClass` via
/// [`Bucket::as_db_str`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "PascalCase")]
pub enum Bucket {
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

impl Bucket {
    /// Classify from a clip's directory path (case-insensitive substring,
    /// most specific first). Mirrors the v1 path classification.
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

    /// The D1 `folder_class` string (the consumer maps this back to its
    /// own enum via `FolderClass::from_db_str`).
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
}

/// Encode an [`AutopilotState`] as its proto integer for the wire
/// (round-trips losslessly through `AutopilotState::from(u32)`, including
/// the forward-compat `Unknown(n)` case).
#[must_use]
pub fn autopilot_to_u32(state: AutopilotState) -> u32 {
    match state {
        AutopilotState::None => 0,
        AutopilotState::SelfDriving => 1,
        AutopilotState::Autosteer => 2,
        AutopilotState::Tacc => 3,
        AutopilotState::Unknown(n) => n,
    }
}

/// Encode a [`Gear`] as its proto integer for the wire (round-trips
/// losslessly through `Gear::from(u32)`).
#[must_use]
pub fn gear_to_u32(gear: Gear) -> u32 {
    match gear {
        Gear::Park => 0,
        Gear::Drive => 1,
        Gear::Reverse => 2,
        Gear::Neutral => 3,
        Gear::Unknown(n) => n,
    }
}

/// One sampled SEI waypoint, normalized for derivation. Field-for-field
/// the producer's image of `indexd`'s internal derive-waypoint; the SEI
/// enums are carried as their proto integers (see [`autopilot_to_u32`] /
/// [`gear_to_u32`]) so `teslausb-core` needs no `serde` derive.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WireWaypoint {
    /// VCL frame index within the clip.
    pub frame_index: i64,
    /// Milliseconds since clip start.
    pub offset_ms: f64,
    /// Absolute UTC epoch seconds, if the clip start was resolvable.
    pub absolute_utc: Option<i64>,
    /// WGS-84 latitude, degrees.
    pub lat: f64,
    /// WGS-84 longitude, degrees.
    pub lon: f64,
    /// Vehicle speed, m/s.
    pub speed: f64,
    /// Compass heading, degrees.
    pub heading: f64,
    /// Longitudinal acceleration, m/s².
    pub accel_x: Option<f64>,
    /// Lateral acceleration, m/s².
    pub accel_y: Option<f64>,
    /// Vertical acceleration, m/s².
    pub accel_z: Option<f64>,
    /// Autopilot state, proto integer.
    pub autopilot_state: u32,
    /// Gear, proto integer.
    pub gear: u32,
    /// Whether this frame carried a usable GPS fix.
    pub has_gps_fix: bool,
}

/// One camera angle (file) belonging to a clip.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AngleRecord {
    /// Tesla camera label (`front`, `back`, `left_repeater`, …).
    pub camera: String,
    /// Path within the volume the reader can resolve back to the file.
    pub file_ref: String,
    /// `ro_usb` / `archive` provenance.
    pub view_kind: String,
    /// Millisecond offset of this angle relative to the clip start.
    pub offset_ms: i64,
    /// Angle duration in seconds, if known.
    pub duration_s: Option<f64>,
    /// File size in bytes, if known.
    pub size_bytes: Option<i64>,
}

/// One eligible *file* (camera angle) and its clip-level facts — the unit
/// the producer emits and the consumer ingests, mirroring the per-file
/// `process_front` / `process_other` path. `waypoints` is non-empty only
/// for the front angle (the only one carrying SEI telemetry).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClipAngleRecord {
    /// Camera-independent dedup key: `slot:<parent-dir>/<timestamp>`.
    pub canonical_key: String,
    /// Resolved recording instant, UTC epoch seconds.
    pub started_at: i64,
    /// Resolved end instant, if known (front only).
    pub ended_at: Option<i64>,
    /// Source partition label (e.g. `slot0`).
    pub partition: String,
    /// Source-folder classification.
    pub bucket: Bucket,
    /// Clip duration in seconds, if probed (front only).
    pub duration_s: Option<f64>,
    /// Whether this is the front angle (drives SEI ingest in the consumer).
    pub is_front: bool,
    /// This file's angle facts.
    pub angle: AngleRecord,
    /// SEI waypoints (front only; may be empty to clear a stale cache).
    pub waypoints: Vec<WireWaypoint>,
}

/// Diagnostic counts produced scanner-side (logging only; carries no
/// control state).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProducerStats {
    /// exFAT partitions visited.
    pub partitions: usize,
    /// Directory entries (files) walked across partitions.
    pub files_seen: usize,
    /// Records reported just-stable this pass.
    pub eligible: usize,
    /// Front clips whose SEI was walked this pass.
    pub front_clips_walked: usize,
    /// Cached waypoints emitted this pass.
    pub waypoints: usize,
    /// Eligible files that errored during read/parse and were skipped.
    pub errors: usize,
}

/// A single scan pass's worth of facts: the full present-key set (for the
/// consumer's prune step) plus the eligible records. `complete` is the
/// prune-safety gate — it is `true` only when the volume walk fully
/// succeeded, so the consumer never prunes from a torn/partial scan.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ScanBatch {
    /// Wire-format version ([`PROTOCOL_VERSION`]).
    pub version: u32,
    /// Monotonic generation assigned by the consumer's request.
    pub generation: u64,
    /// Whether the volume walk fully succeeded (gates the prune step).
    pub complete: bool,
    /// Scanner-side diagnostic counts.
    pub stats: ProducerStats,
    /// Every clip currently present on the media (canonical keys), for
    /// the consumer's prune step. Trustworthy only when `complete`.
    pub present_keys: Vec<String>,
    /// Eligible files (camera angles) to ingest this pass.
    pub records: Vec<ClipAngleRecord>,
}

/// Why a [`ScanBatch`] failed validation. The consumer treats every
/// inbound batch as untrusted and rejects anything over the caps or with
/// a version it does not understand.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum BatchError {
    /// Protocol version the consumer does not understand.
    #[error("unsupported protocol version: {got} (expected {expected})")]
    Version {
        /// The version on the wire.
        got: u32,
        /// The version this build speaks.
        expected: u32,
    },
    /// A bounded collection exceeded its cap.
    #[error("{what} exceeds cap: {len} > {cap}")]
    TooLarge {
        /// Which collection.
        what: &'static str,
        /// Observed length.
        len: usize,
        /// The cap.
        cap: usize,
    },
    /// A string field exceeded [`MAX_STRING_LEN`].
    #[error("string field `{what}` too long: {len} > {cap}")]
    StringTooLong {
        /// Which field.
        what: &'static str,
        /// Observed length.
        len: usize,
        /// The cap.
        cap: usize,
    },
    /// A coordinate / numeric field was non-finite (NaN/inf).
    #[error("non-finite numeric field: {what}")]
    NonFinite {
        /// Which field.
        what: &'static str,
    },
}

impl ScanBatch {
    /// Validate version + every cap + numeric finiteness. The consumer
    /// MUST call this before applying any record — the batch is untrusted
    /// input from the producer.
    ///
    /// # Errors
    ///
    /// Returns the first [`BatchError`] encountered.
    pub fn validate(&self) -> Result<(), BatchError> {
        if self.version != PROTOCOL_VERSION {
            return Err(BatchError::Version {
                got: self.version,
                expected: PROTOCOL_VERSION,
            });
        }
        cap("present_keys", self.present_keys.len(), MAX_PRESENT_KEYS)?;
        cap("records", self.records.len(), MAX_RECORDS_PER_BATCH)?;
        for key in &self.present_keys {
            string_len("present_key", key)?;
        }
        for record in &self.records {
            record.validate()?;
        }
        Ok(())
    }
}

impl ClipAngleRecord {
    /// Validate one record's caps, string lengths, and numeric finiteness.
    ///
    /// # Errors
    ///
    /// Returns the first [`BatchError`] encountered.
    pub fn validate(&self) -> Result<(), BatchError> {
        string_len("canonical_key", &self.canonical_key)?;
        string_len("partition", &self.partition)?;
        string_len("camera", &self.angle.camera)?;
        string_len("file_ref", &self.angle.file_ref)?;
        string_len("view_kind", &self.angle.view_kind)?;
        cap("waypoints", self.waypoints.len(), MAX_WAYPOINTS_PER_RECORD)?;
        finite("duration_s", self.duration_s)?;
        finite("angle.duration_s", self.angle.duration_s)?;
        for wp in &self.waypoints {
            wp.validate()?;
        }
        Ok(())
    }
}

impl WireWaypoint {
    /// Reject non-finite geo/motion fields (a corrupt SEI decode could
    /// surface NaN/inf, which would poison distance/derivation math).
    ///
    /// # Errors
    ///
    /// Returns [`BatchError::NonFinite`] for the first bad field.
    pub fn validate(&self) -> Result<(), BatchError> {
        for (what, v) in [
            ("offset_ms", self.offset_ms),
            ("lat", self.lat),
            ("lon", self.lon),
            ("speed", self.speed),
            ("heading", self.heading),
        ] {
            if !v.is_finite() {
                return Err(BatchError::NonFinite { what });
            }
        }
        finite("accel_x", self.accel_x)?;
        finite("accel_y", self.accel_y)?;
        finite("accel_z", self.accel_z)?;
        Ok(())
    }
}

fn cap(what: &'static str, len: usize, cap: usize) -> Result<(), BatchError> {
    if len > cap {
        Err(BatchError::TooLarge { what, len, cap })
    } else {
        Ok(())
    }
}

fn string_len(what: &'static str, s: &str) -> Result<(), BatchError> {
    if s.len() > MAX_STRING_LEN {
        Err(BatchError::StringTooLong {
            what,
            len: s.len(),
            cap: MAX_STRING_LEN,
        })
    } else {
        Ok(())
    }
}

fn finite(what: &'static str, v: Option<f64>) -> Result<(), BatchError> {
    match v {
        Some(x) if !x.is_finite() => Err(BatchError::NonFinite { what }),
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::float_cmp, clippy::indexing_slicing)]

    use super::{
        AngleRecord, Bucket, ClipAngleRecord, MAX_STRING_LEN, PROTOCOL_VERSION, ProducerStats,
        ScanBatch, WireWaypoint, autopilot_to_u32, gear_to_u32,
    };
    use teslausb_core::sei::tesla::{AutopilotState, Gear};

    fn sample_waypoint() -> WireWaypoint {
        WireWaypoint {
            frame_index: 30,
            offset_ms: 1000.0,
            absolute_utc: Some(1_700_000_001),
            lat: 47.6,
            lon: -122.3,
            speed: 12.5,
            heading: 90.0,
            accel_x: Some(0.1),
            accel_y: Some(-0.2),
            accel_z: Some(9.8),
            autopilot_state: autopilot_to_u32(AutopilotState::Autosteer),
            gear: gear_to_u32(Gear::Drive),
            has_gps_fix: true,
        }
    }

    fn sample_batch() -> ScanBatch {
        ScanBatch {
            version: PROTOCOL_VERSION,
            generation: 7,
            complete: true,
            stats: ProducerStats {
                partitions: 1,
                files_seen: 4,
                eligible: 1,
                front_clips_walked: 1,
                waypoints: 1,
                errors: 0,
            },
            present_keys: vec!["0:TeslaCam/SavedClips/2026-06-01_20-10-04".to_owned()],
            records: vec![ClipAngleRecord {
                canonical_key: "0:TeslaCam/SavedClips/2026-06-01_20-10-04".to_owned(),
                started_at: 1_700_000_000,
                ended_at: Some(1_700_000_060),
                partition: "slot0".to_owned(),
                bucket: Bucket::SavedClips,
                duration_s: Some(60.0),
                is_front: true,
                angle: AngleRecord {
                    camera: "front".to_owned(),
                    file_ref: "TeslaCam/SavedClips/2026-06-01_20-10-04/x-front.mp4".to_owned(),
                    view_kind: "ro_usb".to_owned(),
                    offset_ms: 0,
                    duration_s: None,
                    size_bytes: Some(1024),
                },
                waypoints: vec![sample_waypoint()],
            }],
        }
    }

    #[test]
    fn batch_roundtrips_through_json() {
        let batch = sample_batch();
        let json = serde_json::to_string(&batch).unwrap();
        let back: ScanBatch = serde_json::from_str(&json).unwrap();
        assert_eq!(batch, back);
    }

    #[test]
    fn bucket_roundtrips_and_maps_to_db_str() {
        for (b, s) in [
            (Bucket::RecentClips, "RecentClips"),
            (Bucket::SavedClips, "SavedClips"),
            (Bucket::SentryClips, "SentryClips"),
            (Bucket::TeslaTrackMode, "TeslaTrackMode"),
            (Bucket::ArchivedClips, "ArchivedClips"),
        ] {
            assert_eq!(b.as_db_str(), s);
            let json = serde_json::to_string(&b).unwrap();
            assert_eq!(json, format!("\"{s}\""));
            let back: Bucket = serde_json::from_str(&json).unwrap();
            assert_eq!(b, back);
        }
    }

    #[test]
    fn bucket_from_path_matches_classification() {
        assert_eq!(
            Bucket::from_path("TeslaCam/SentryClips/2026/x-front.mp4"),
            Bucket::SentryClips
        );
        assert_eq!(
            Bucket::from_path("TeslaCam/SavedClips/2026/x-front.mp4"),
            Bucket::SavedClips
        );
        assert_eq!(
            Bucket::from_path("TeslaCam/RecentClips/x-front.mp4"),
            Bucket::RecentClips
        );
        assert_eq!(
            Bucket::from_path("archive/ArchivedClips/x.mp4"),
            Bucket::ArchivedClips
        );
    }

    #[test]
    fn autopilot_and_gear_encode_round_trip_including_unknown() {
        for s in [
            AutopilotState::None,
            AutopilotState::SelfDriving,
            AutopilotState::Autosteer,
            AutopilotState::Tacc,
            AutopilotState::Unknown(42),
        ] {
            assert_eq!(AutopilotState::from(autopilot_to_u32(s)), s);
        }
        for g in [
            Gear::Park,
            Gear::Drive,
            Gear::Reverse,
            Gear::Neutral,
            Gear::Unknown(99),
        ] {
            assert_eq!(Gear::from(gear_to_u32(g)), g);
        }
    }

    #[test]
    fn validate_accepts_good_batch() {
        assert!(sample_batch().validate().is_ok());
    }

    #[test]
    fn validate_rejects_wrong_version() {
        let mut batch = sample_batch();
        batch.version = PROTOCOL_VERSION + 1;
        assert!(matches!(
            batch.validate(),
            Err(super::BatchError::Version { .. })
        ));
    }

    #[test]
    fn validate_rejects_oversize_string() {
        let mut batch = sample_batch();
        batch.records[0].canonical_key = "x".repeat(MAX_STRING_LEN + 1);
        assert!(matches!(
            batch.validate(),
            Err(super::BatchError::StringTooLong { .. })
        ));
    }

    #[test]
    fn validate_rejects_non_finite_waypoint() {
        let mut batch = sample_batch();
        batch.records[0].waypoints[0].lat = f64::NAN;
        assert!(matches!(
            batch.validate(),
            Err(super::BatchError::NonFinite { .. })
        ));
    }
}
