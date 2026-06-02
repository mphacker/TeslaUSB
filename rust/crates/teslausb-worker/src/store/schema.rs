//! Schema constants — the version + ordered migration list.
//!
//! Append-only. Bump [`CURRENT_SCHEMA_VERSION`] each time a
//! migration is appended; NEVER edit an existing entry.

/// Schema-version key in the `meta` table. Crate-private —
/// only the store-impl layer reads/writes it.
pub(super) const META_KEY_SCHEMA_VERSION: &str = "schema_version";

/// Ordered DB migrations. Each is one DDL transaction that
/// brings the schema from version `index` to version
/// `index + 1`. NEVER mutate an existing entry — append a
/// new one and bump [`CURRENT_SCHEMA_VERSION`].
pub(super) const MIGRATIONS: &[&str] = &[
    // v0 -> v1: initial schema.
    "\
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path TEXT NOT NULL UNIQUE,
    bucket TEXT NOT NULL,
    clip_started_utc INTEGER,
    indexed_at_utc INTEGER NOT NULL,
    waypoint_count INTEGER NOT NULL DEFAULT 0,
    gps_waypoint_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX clips_by_bucket_started ON clips(bucket, clip_started_utc);
CREATE TABLE waypoints (
    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    frame_index INTEGER NOT NULL,
    timestamp_ms REAL NOT NULL,
    latitude_deg REAL NOT NULL,
    longitude_deg REAL NOT NULL,
    speed_mps REAL NOT NULL,
    heading_deg REAL NOT NULL,
    PRIMARY KEY (clip_id, frame_index)
);
CREATE INDEX waypoints_by_clip ON waypoints(clip_id);
",
    // v1 -> v2: telemetry expansion + PK rebuild.
    //
    // Adds 8 SEI-derived columns (acceleration_x/y/z, gear,
    // steering_angle, brake_applied, blinker_on_left/right,
    // autopilot_state) so the mapping layer can derive harsh-
    // brake / sharp-turn / blinker analytics directly from
    // this DB — no second database, no second SEI parser.
    //
    // Also replaces the `(clip_id, frame_index)` primary key
    // with a synthetic `id` so the walker can persist multiple
    // SEI NAL units that share a frame_index (Tesla emits 2+
    // consecutive SEI NALs without an intervening slice; the
    // old PK collision caused the entire clip's waypoints to
    // be rolled back). Existing rows are migrated verbatim.
    "\
CREATE TABLE waypoints_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    frame_index INTEGER NOT NULL,
    timestamp_ms REAL NOT NULL,
    latitude_deg REAL NOT NULL,
    longitude_deg REAL NOT NULL,
    speed_mps REAL NOT NULL,
    heading_deg REAL NOT NULL,
    acceleration_x REAL,
    acceleration_y REAL,
    acceleration_z REAL,
    gear TEXT,
    steering_angle REAL,
    brake_applied INTEGER NOT NULL DEFAULT 0,
    blinker_on_left INTEGER NOT NULL DEFAULT 0,
    blinker_on_right INTEGER NOT NULL DEFAULT 0,
    autopilot_state TEXT
);
INSERT INTO waypoints_v2 (
    clip_id, frame_index, timestamp_ms,
    latitude_deg, longitude_deg, speed_mps, heading_deg
)
SELECT clip_id, frame_index, timestamp_ms,
       latitude_deg, longitude_deg, speed_mps, heading_deg
  FROM waypoints;
DROP TABLE waypoints;
ALTER TABLE waypoints_v2 RENAME TO waypoints;
CREATE INDEX waypoints_by_clip ON waypoints(clip_id);
CREATE INDEX waypoints_by_clip_frame ON waypoints(clip_id, frame_index);
",
    // v2 -> v3: materialise trips + detected_events (ADR-0019).
    //
    // Adds the derived tables the mapping layer used to
    // recompute in Python on every web request. The worker
    // now writes them once at index time and the web layer
    // runs small targeted SQL — matching v1's proven model.
    //
    // The CREATE TABLE / CREATE INDEX statements below leave
    // the new tables empty. A one-shot backfill job
    // (Indexer::backfill_trips_and_events) populates them on
    // first startup after the migration; the same code path
    // also handles incremental updates as new clips arrive.
    //
    // ON DELETE CASCADE on trip_id keeps detected_events and
    // clip_trip_map consistent when the cleanup task evicts
    // a clip — no stale derived rows can leak.
    "\
CREATE TABLE trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_utc INTEGER NOT NULL,
    end_utc INTEGER NOT NULL,
    start_clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    end_clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    start_lat REAL,
    start_lon REAL,
    end_lat REAL,
    end_lon REAL,
    distance_km REAL NOT NULL DEFAULT 0,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    waypoint_count INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0,
    video_count INTEGER NOT NULL DEFAULT 0,
    bucket TEXT NOT NULL DEFAULT 'recent'
);
CREATE INDEX trips_by_start_utc ON trips(start_utc DESC);
CREATE INDEX trips_by_end_utc ON trips(end_utc DESC);
CREATE TABLE detected_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
    clip_id INTEGER REFERENCES clips(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    timestamp_utc INTEGER NOT NULL,
    latitude_deg REAL,
    longitude_deg REAL,
    speed_mps REAL,
    metadata_json TEXT
);
CREATE INDEX events_by_trip ON detected_events(trip_id);
CREATE INDEX events_by_type_ts ON detected_events(event_type, timestamp_utc DESC);
CREATE INDEX events_by_severity_ts ON detected_events(severity, timestamp_utc DESC);
CREATE INDEX events_by_ts ON detected_events(timestamp_utc DESC);
CREATE TABLE clip_trip_map (
    clip_id INTEGER PRIMARY KEY REFERENCES clips(id) ON DELETE CASCADE,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE
);
CREATE INDEX clip_trip_map_by_trip ON clip_trip_map(trip_id);
",
    // v3 -> v4: carry presentation fields on detected_events.
    //
    // The web layer needs `description` (human-readable text
    // per event), `video_path` (clip file the event came from
    // — derivable via the clip_id FK + clips.relative_path
    // JOIN), and `frame_index` (the SEI frame the event was
    // detected on, so the player can seek to it). Storing
    // them on detected_events lets the web layer SELECT
    // straight from the table without re-deriving in Python
    // or pulling the full clip row for each event. The next
    // materializer rebuild populates them.
    "\
ALTER TABLE detected_events ADD COLUMN description TEXT NOT NULL DEFAULT '';
ALTER TABLE detected_events ADD COLUMN frame_index INTEGER;
",
    // v4 -> v5: raw Tesla event.json metadata.
    //
    // SavedClips/SentryClips event directories carry an
    // `event.json` file whose timestamp/reason/location are
    // authored by the car and must survive trip/materializer
    // rebuilds. This is a raw index table, not a derived table:
    // `Materializer::rebuild_all` may clear `trips` and
    // `detected_events`, but it must never wipe these rows.
    "\
CREATE TABLE clip_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_json_relative_path TEXT NOT NULL UNIQUE,
    event_dir_relative_path TEXT NOT NULL,
    bucket TEXT NOT NULL,
    primary_clip_id INTEGER REFERENCES clips(id) ON DELETE SET NULL,
    timestamp_utc INTEGER NOT NULL,
    est_lat REAL,
    est_lon REAL,
    reason TEXT,
    city TEXT,
    camera TEXT,
    indexed_at_utc INTEGER NOT NULL
);
CREATE INDEX clip_events_by_timestamp ON clip_events(timestamp_utc DESC);
CREATE INDEX clip_events_by_dir ON clip_events(event_dir_relative_path);
CREATE INDEX clip_events_by_primary_clip ON clip_events(primary_clip_id);
",
    // v5 -> v6: separate raw local-wall-clock from corrected UTC.
    //
    // Tesla's `event.json` `timestamp` has NO timezone and is
    // the car's LOCAL wall-clock, but it was being stored in
    // `clip_events.timestamp_utc` verbatim — so an event sat in
    // the wrong UTC day relative to its own GPS-derived trip
    // (which is true UTC), splitting a honk and its drive route
    // across two day buckets. `timestamp_local_naive` preserves
    // the raw wall-clock seconds (interpreted as if UTC) so the
    // local↔UTC offset can be re-derived idempotently on every
    // re-link against the primary clip's SEI `clip_started_utc`;
    // `timestamp_utc` now holds the CORRECTED true-UTC instant.
    // Backfilled equal to the existing value; the next index /
    // re-link pass recomputes the correction.
    "\
ALTER TABLE clip_events ADD COLUMN timestamp_local_naive INTEGER;
UPDATE clip_events SET timestamp_local_naive = timestamp_utc;
",
];

/// Current schema version. Bump when appending to the
/// `MIGRATIONS` constant.
pub const CURRENT_SCHEMA_VERSION: u32 = 6;
