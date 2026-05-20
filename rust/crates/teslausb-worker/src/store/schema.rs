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
];

/// Current schema version. Bump when appending to the
/// `MIGRATIONS` constant.
pub const CURRENT_SCHEMA_VERSION: u32 = 1;
