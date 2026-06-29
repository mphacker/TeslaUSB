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

/// Hard cap on media-inventory records (p2 MEDIA files) a single batch may
/// carry. Slice 1 emits at most one (the lock chime); the cap leaves
/// headroom for future media listings while bounding a forged peer.
pub const MAX_MEDIA_RECORDS: usize = 10_000;

/// Hard cap on clip-event sidecar records (`event.json`) a single batch may
/// carry. One event directory contributes at most one row.
pub const MAX_CLIP_EVENT_RECORDS: usize = 100_000;

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

/// Encode an [`AutopilotState`] as its proto integer for the wire.
/// Round-trips losslessly through `AutopilotState::from(u32)` for every
/// value a real SEI decode can produce: the decoder only yields
/// `Unknown(n)` for `n` **outside** the known range `0..=3`, so the
/// integer encoding never aliases a known variant in practice. (A
/// hand-built `Unknown(2)` would decode back as `Autosteer`, but such a
/// value is unconstructible from decoding.)
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

/// Encode a [`Gear`] as its proto integer for the wire. Round-trips
/// losslessly through `Gear::from(u32)` for every value a real SEI decode
/// can produce (`Unknown(n)` only ever carries `n` outside the known range
/// `0..=3`).
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
/// for the front angle (the only one carrying SEI telemetry); the consumer
/// derives front-ness from [`AngleRecord::camera`] rather than trusting a
/// forgeable flag, and rejects a non-front record that carries waypoints.
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
    /// This file's angle facts.
    pub angle: AngleRecord,
    /// SEI waypoints (front only; may be empty to clear a stale cache).
    pub waypoints: Vec<WireWaypoint>,
}

impl ClipAngleRecord {
    /// Whether this is the front angle — derived from the camera label
    /// (the consumer never trusts a separate forgeable flag). Front is the
    /// only angle carrying SEI telemetry and the only one that drives the
    /// clip-level `upsert_clip` + waypoint cache in the consumer.
    #[must_use]
    pub fn is_front(&self) -> bool {
        self.angle.camera.eq_ignore_ascii_case("front")
    }
}

/// Diagnostic counts the *producer* can know without a database (logging
/// only; carries no control state). DB-outcome counters
/// (`clips_upserted`, `front_clips_walked`, `waypoints`) live on the
/// consumer's apply report instead, since only the writer knows whether a
/// row was actually committed.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProducerStats {
    /// exFAT partitions visited.
    pub partitions: usize,
    /// Directory entries (files) walked across partitions.
    pub files_seen: usize,
    /// Records reported just-stable this pass.
    pub eligible: usize,
    /// Eligible files that errored during raw read/parse and were skipped.
    pub read_errors: usize,
    /// Eligible files with no resolvable recording instant (no `mvhd` and
    /// an out-of-range filename timestamp): nothing is written, but the
    /// legacy in-process pass still counted them as "upserted".
    pub unplaceable_clips: usize,
    /// Subset of [`Self::unplaceable_clips`] that were the front angle
    /// (the legacy pass also counted these as "front walked").
    pub unplaceable_front: usize,
}

/// One parsed `event.json` row keyed to an event directory and linked to the
/// event's primary front clip (`canonical_key`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClipEventRecord {
    /// Sidecar primary key: `slot:<event-folder-rel-path>`.
    pub event_dir_key: String,
    /// Source-folder classification.
    pub bucket: Bucket,
    /// Front clip `canonical_key` used as FK link target.
    pub primary_canonical_key: String,
    /// Best-effort UTC from `event.json`.
    pub timestamp_utc: i64,
    /// Raw local wall-clock interpreted as naive seconds.
    pub timestamp_local_naive: i64,
    /// Whether the source timestamp carried an explicit offset.
    pub timestamp_has_offset: bool,
    /// Estimated latitude, or `None`.
    pub est_lat: Option<f64>,
    /// Estimated longitude, or `None`.
    pub est_lon: Option<f64>,
    /// Tesla event reason, if any.
    pub reason: Option<String>,
    /// Tesla city label, if any.
    pub city: Option<String>,
    /// Tesla camera label, if any.
    pub camera: Option<String>,
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
    /// MEDIA-partition (p2) inventory facts for the read-only media screens
    /// (lock chime today; boombox/music/lightshows later). Empty on an
    /// older producer that predates media inventory (`#[serde(default)]`).
    #[serde(default)]
    pub media: Vec<MediaFileRecord>,
    /// Every media `rel_path` currently present on p2 — the consumer's
    /// media prune set. Trustworthy only when `media_inventory` is set.
    #[serde(default)]
    pub media_present_paths: Vec<String>,
    /// Whether this producer populated the media inventory at all. `false`
    /// on an older producer (via `#[serde(default)]`): the consumer then
    /// leaves the media catalog untouched and NEVER prunes it, so a batch
    /// from a media-unaware scannerd can't wipe the `media_entries` table.
    #[serde(default)]
    pub media_inventory: bool,
    /// Parsed `event.json` sidecar facts (SavedClips/SentryClips). Empty on
    /// an older producer that predates clip-event inventory
    /// (`#[serde(default)]`).
    #[serde(default)]
    pub clip_events: Vec<ClipEventRecord>,
    /// Whether this producer populated clip-event inventory at all. `false`
    /// on an older producer (via `#[serde(default)]`): the consumer then
    /// leaves clip-event sidecar rows untouched and NEVER prunes them.
    #[serde(default)]
    pub clip_events_inventory: bool,
}

/// One file inventoried on the MEDIA (p2) partition — the read-only "what is
/// installed" facts the media screens display. Plain data: `scannerd` reads
/// the p2 directory raw + read-only, `indexd` stores it, `webd` serves it,
/// and nothing here mounts or writes the live USB LUN.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MediaFileRecord {
    /// Source partition label (`slot1` for MEDIA), matching the clip
    /// `partition` convention.
    pub partition: String,
    /// Path relative to the partition root (e.g. `LockChime.wav`). Together
    /// with `partition` this is the row's stable identity.
    pub rel_path: String,
    /// File name (last path component).
    pub name: String,
    /// File size in bytes (`DataLength`); never negative.
    pub size_bytes: i64,
    /// Best-effort recorded modification time as a NAIVE local
    /// `YYYY-MM-DDThh:mm:ss` string, or `None` when the packed exFAT
    /// timestamp is out of range. Deliberately not a UTC instant: exFAT
    /// stores local wall-clock plus a separate offset, so claiming an
    /// absolute time would be misleading.
    pub modified_local: Option<String>,
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
    /// A non-front record carried SEI waypoints (only the front angle
    /// has telemetry; a non-front record with waypoints is malformed).
    #[error("non-front record `{key}` carries {count} waypoint(s)")]
    NonFrontWaypoints {
        /// The offending record's canonical key.
        key: String,
        /// How many waypoints it carried.
        count: usize,
    },
    /// A media record failed per-record validation.
    #[error("media record `{rel_path}` invalid: {reason}")]
    MediaInvalid {
        /// The offending media record's `rel_path`.
        rel_path: String,
        /// Why it was rejected.
        reason: &'static str,
    },
}

impl ScanBatch {
    /// Validate **batch-level** invariants: protocol version and the
    /// gross-size caps that bound consumer memory. A failure here is fatal
    /// (the whole batch is rejected) because it signals a protocol
    /// mismatch or a denial-of-service attempt, not one bad clip.
    /// Per-record validation is [`ClipAngleRecord::validate`], applied
    /// individually so a single malformed record cannot starve its
    /// siblings.
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
        cap("media", self.media.len(), MAX_MEDIA_RECORDS)?;
        cap(
            "clip_events",
            self.clip_events.len(),
            MAX_CLIP_EVENT_RECORDS,
        )?;
        cap(
            "media_present_paths",
            self.media_present_paths.len(),
            MAX_MEDIA_RECORDS,
        )?;
        for key in &self.present_keys {
            string_len("present_key", key)?;
        }
        for path in &self.media_present_paths {
            string_len("media_present_path", path)?;
        }
        for event in &self.clip_events {
            string_len("clip_event.event_dir_key", &event.event_dir_key)?;
            string_len(
                "clip_event.primary_canonical_key",
                &event.primary_canonical_key,
            )?;
            if let Some(reason) = &event.reason {
                string_len("clip_event.reason", reason)?;
            }
            if let Some(city) = &event.city {
                string_len("clip_event.city", city)?;
            }
            if let Some(camera) = &event.camera {
                string_len("clip_event.camera", camera)?;
            }
        }
        Ok(())
    }
}

impl MediaFileRecord {
    /// Validate one media record's string caps and non-negative size. The
    /// consumer calls this **per record** inside its apply loop and skips
    /// (counting) on failure, so a single malformed media row never aborts
    /// the batch.
    ///
    /// # Errors
    ///
    /// Returns the first [`BatchError`] encountered.
    pub fn validate(&self) -> Result<(), BatchError> {
        string_len("media.partition", &self.partition)?;
        string_len("media.rel_path", &self.rel_path)?;
        string_len("media.name", &self.name)?;
        if let Some(modified) = &self.modified_local {
            string_len("media.modified_local", modified)?;
        }
        if self.size_bytes < 0 {
            return Err(BatchError::MediaInvalid {
                rel_path: self.rel_path.clone(),
                reason: "negative size_bytes",
            });
        }
        Ok(())
    }
}

impl ClipEventRecord {
    /// Validate one clip-event sidecar record's string caps (anti-OOM). Numeric
    /// values are deliberately not range-checked here — the geo guard lives in
    /// derivation, shared by both paths (see `clip_event_validate_accepts_non_finite_geo_for_parity`).
    /// The consumer calls this **per record** inside its apply loop and skips
    /// (counting) on failure, so a single malformed sidecar row never aborts
    /// the batch.
    ///
    /// # Errors
    ///
    /// Returns the first [`BatchError`] encountered.
    pub fn validate(&self) -> Result<(), BatchError> {
        string_len("clip_event.event_dir_key", &self.event_dir_key)?;
        string_len(
            "clip_event.primary_canonical_key",
            &self.primary_canonical_key,
        )?;
        if let Some(reason) = &self.reason {
            string_len("clip_event.reason", reason)?;
        }
        if let Some(city) = &self.city {
            string_len("clip_event.city", city)?;
        }
        if let Some(camera) = &self.camera {
            string_len("clip_event.camera", camera)?;
        }
        // Numeric *values* (non-finite est_lat/est_lon) are deliberately NOT
        // range-checked here: like the sibling `ClipAngleRecord` parity rule,
        // value-level geo guards live in derivation (`validated_clip_event_geo`),
        // shared by both paths. Rejecting here would also desync prune (the key
        // stays in the present-set) and leave a stale pin instead of a
        // pin-less event.
        Ok(())
    }
}

impl ClipAngleRecord {
    /// Validate one record's caps, string lengths, and the front/waypoint
    /// invariant. The consumer calls this **per record** inside its apply
    /// loop and skips (counting an error) on failure, so a single malformed
    /// record never aborts the batch.
    ///
    /// These are the **structural** bounds that are no-ops on the producer's
    /// own output (so the in-process path keeps exact parity) but defend the
    /// DB-owning consumer against a forged wire peer: total/per-field size
    /// caps (anti-OOM) and the rule that only the front angle carries SEI
    /// telemetry. Numeric *values* are deliberately **not** range-checked —
    /// the legacy in-process pass ingested whatever the SEI decoded, so the
    /// consumer must too (any value-level guard lives in derivation, shared
    /// by both paths).
    ///
    /// # Errors
    ///
    /// Returns the first [`BatchError`] encountered.
    pub fn validate(&self) -> Result<(), BatchError> {
        string_len("canonical_key", &self.canonical_key)?;
        string_len("partition", &self.partition)?;
        string_len("camera", &self.angle.camera)?;
        string_len("file_ref", &self.angle.file_ref)?;
        cap("waypoints", self.waypoints.len(), MAX_WAYPOINTS_PER_RECORD)?;
        if !self.is_front() && !self.waypoints.is_empty() {
            return Err(BatchError::NonFrontWaypoints {
                key: self.canonical_key.clone(),
                count: self.waypoints.len(),
            });
        }
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

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::float_cmp, clippy::indexing_slicing)]

    use super::{
        AngleRecord, Bucket, ClipAngleRecord, ClipEventRecord, MAX_CLIP_EVENT_RECORDS,
        MAX_MEDIA_RECORDS, MAX_STRING_LEN, MediaFileRecord, PROTOCOL_VERSION, ProducerStats,
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
                read_errors: 0,
                unplaceable_clips: 0,
                unplaceable_front: 0,
            },
            present_keys: vec!["0:TeslaCam/SavedClips/2026-06-01_20-10-04".to_owned()],
            records: vec![ClipAngleRecord {
                canonical_key: "0:TeslaCam/SavedClips/2026-06-01_20-10-04".to_owned(),
                started_at: 1_700_000_000,
                ended_at: Some(1_700_000_060),
                partition: "slot0".to_owned(),
                bucket: Bucket::SavedClips,
                duration_s: Some(60.0),
                angle: AngleRecord {
                    camera: "front".to_owned(),
                    file_ref: "TeslaCam/SavedClips/2026-06-01_20-10-04/x-front.mp4".to_owned(),
                    offset_ms: 0,
                    duration_s: None,
                    size_bytes: Some(1024),
                },
                waypoints: vec![sample_waypoint()],
            }],
            media: Vec::new(),
            media_present_paths: Vec::new(),
            media_inventory: false,
            clip_events: Vec::new(),
            clip_events_inventory: false,
        }
    }

    #[test]
    fn batch_roundtrips_through_json() {
        let batch = sample_batch();
        let json = serde_json::to_string(&batch).unwrap();
        let back: ScanBatch = serde_json::from_str(&json).unwrap();
        assert_eq!(batch, back);
    }

    fn sample_media() -> MediaFileRecord {
        MediaFileRecord {
            partition: "slot1".to_owned(),
            rel_path: "LockChime.wav".to_owned(),
            name: "LockChime.wav".to_owned(),
            size_bytes: 219_770,
            modified_local: Some("2026-06-01T20:10:04".to_owned()),
        }
    }

    #[test]
    fn batch_with_media_roundtrips_through_json() {
        let mut batch = sample_batch();
        batch.media = vec![sample_media()];
        batch.media_present_paths = vec!["LockChime.wav".to_owned()];
        batch.media_inventory = true;
        let json = serde_json::to_string(&batch).unwrap();
        let back: ScanBatch = serde_json::from_str(&json).unwrap();
        assert_eq!(batch, back);
    }

    #[test]
    fn old_batch_without_media_fields_deserializes_as_media_unaware() {
        // A pre-media producer omits the three media fields entirely; the
        // serde defaults must read back as an empty, NON-inventory batch so
        // the consumer never prunes the media catalog from it.
        let json = r#"{
            "version": 1, "generation": 3, "complete": true,
            "stats": {"partitions":1,"files_seen":0,"eligible":0,
                      "read_errors":0,"unplaceable_clips":0,"unplaceable_front":0},
            "present_keys": [], "records": []
        }"#;
        let batch: ScanBatch = serde_json::from_str(json).unwrap();
        assert!(batch.media.is_empty());
        assert!(batch.media_present_paths.is_empty());
        assert!(!batch.media_inventory);
        assert!(batch.clip_events.is_empty());
        assert!(!batch.clip_events_inventory);
        batch.validate().unwrap();
    }

    #[test]
    fn media_record_validates_and_rejects_negative_size() {
        sample_media().validate().unwrap();
        let mut bad = sample_media();
        bad.size_bytes = -1;
        assert!(bad.validate().is_err());
    }

    #[test]
    fn media_record_rejects_overlong_strings() {
        let mut bad = sample_media();
        bad.rel_path = "x".repeat(MAX_STRING_LEN + 1);
        assert!(bad.validate().is_err());
    }

    #[test]
    fn batch_rejects_media_over_cap() {
        let mut batch = sample_batch();
        batch.media_present_paths = (0..=MAX_MEDIA_RECORDS).map(|i| i.to_string()).collect();
        assert!(batch.validate().is_err());
    }

    #[test]
    fn batch_rejects_clip_events_over_cap() {
        let mut batch = sample_batch();
        batch.clip_events = (0..=MAX_CLIP_EVENT_RECORDS)
            .map(|i| ClipEventRecord {
                event_dir_key: format!("slot:TeslaCam/SavedClips/event-{i}"),
                bucket: Bucket::SavedClips,
                primary_canonical_key: format!("slot:TeslaCam/SavedClips/clip-{i}"),
                timestamp_utc: 1_700_000_000,
                timestamp_local_naive: 1_700_000_000,
                timestamp_has_offset: false,
                est_lat: None,
                est_lon: None,
                reason: None,
                city: None,
                camera: None,
            })
            .collect();
        assert!(batch.validate().is_err());
    }

    #[test]
    fn batch_rejects_clip_event_overlong_string() {
        let mut batch = sample_batch();
        batch.clip_events.push(ClipEventRecord {
            event_dir_key: "x".repeat(MAX_STRING_LEN + 1),
            bucket: Bucket::SavedClips,
            primary_canonical_key: "slot:TeslaCam/SavedClips/clip".to_owned(),
            timestamp_utc: 1_700_000_000,
            timestamp_local_naive: 1_700_000_000,
            timestamp_has_offset: false,
            est_lat: None,
            est_lon: None,
            reason: None,
            city: None,
            camera: None,
        });
        assert!(batch.validate().is_err());
    }

    #[test]
    fn clip_event_validate_accepts_non_finite_geo_for_parity() {
        // Non-finite est_lat/est_lon must NOT abort the record: the geo guard
        // lives in derivation (which nulls it into a pin-less event), and
        // rejecting here would desync prune and strand a stale pin. Only the
        // string caps are enforced at this layer.
        let mut rec = ClipEventRecord {
            event_dir_key: "slot0:TeslaCam/SavedClips/2026-06-01_20-10-04".to_owned(),
            bucket: Bucket::SavedClips,
            primary_canonical_key: "slot0:TeslaCam/SavedClips/clip".to_owned(),
            timestamp_utc: 1_700_000_000,
            timestamp_local_naive: 1_700_000_000,
            timestamp_has_offset: false,
            est_lat: Some(f64::NAN),
            est_lon: Some(f64::INFINITY),
            reason: Some("sentry".to_owned()),
            city: Some("Seattle".to_owned()),
            camera: Some("front".to_owned()),
        };
        assert!(rec.validate().is_ok());
        rec.est_lat = Some(47.6);
        rec.est_lon = Some(-122.3);
        assert!(rec.validate().is_ok());
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
        let batch = sample_batch();
        assert!(batch.validate().is_ok());
        for record in &batch.records {
            assert!(record.validate().is_ok());
        }
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
    fn record_validate_rejects_oversize_string() {
        let mut batch = sample_batch();
        batch.records[0].canonical_key = "x".repeat(MAX_STRING_LEN + 1);
        assert!(matches!(
            batch.records[0].validate(),
            Err(super::BatchError::StringTooLong { .. })
        ));
    }

    #[test]
    fn record_validate_accepts_non_finite_waypoint_for_parity() {
        // The legacy in-process pass ingested whatever the SEI decoded,
        // including a NaN/inf coordinate from a corrupt-but-parseable frame.
        // The consumer must NOT drop such a record (that would diverge from
        // the golden in-process behavior); value-level guards live in
        // derivation, shared by both paths.
        let mut batch = sample_batch();
        batch.records[0].waypoints[0].lat = f64::NAN;
        batch.records[0].waypoints[0].speed = f64::INFINITY;
        assert!(batch.records[0].validate().is_ok());
    }

    #[test]
    fn record_validate_rejects_non_front_with_waypoints() {
        let mut batch = sample_batch();
        batch.records[0].angle.camera = "back".to_owned();
        assert!(matches!(
            batch.records[0].validate(),
            Err(super::BatchError::NonFrontWaypoints { .. })
        ));
    }

    #[test]
    fn is_front_is_derived_from_camera() {
        let mut batch = sample_batch();
        assert!(batch.records[0].is_front());
        batch.records[0].angle.camera = "left_repeater".to_owned();
        assert!(!batch.records[0].is_front());
    }
}
