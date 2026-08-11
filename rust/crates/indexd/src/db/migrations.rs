//! v1 schema DDL and the forward-only migration ladder.
//!
//! The schema realizes contract **D1**
//! ([`docs/specs/contracts/indexd-schema.md`]) plus two internal additions
//! documented inline. It is **PROVISIONAL** until the operator OP-3 freeze;
//! [`SCHEMA_VERSION_NOTE`] records that.
//!
//! Migrations are **forward-only and idempotent**: each carries a unique
//! ascending [`Migration::version`]; [`apply`](super::apply_migrations)
//! runs every migration whose version exceeds the DB's current
//! `MAX(schema_version.version)` inside a single transaction. A DB whose
//! version exceeds [`LATEST_VERSION`] (written by a newer binary) is a hard
//! error — we never downgrade.

/// One forward-only migration step.
pub struct Migration {
    /// Monotonic version this migration brings the DB up to.
    pub version: i64,
    /// Human note recorded in `schema_version.note`.
    pub note: &'static str,
    /// DDL executed as a batch when this migration is applied.
    pub sql: &'static str,
}

/// Marker recorded in the v1 seed row so an inspector can see the schema
/// is not yet frozen.
pub const SCHEMA_VERSION_NOTE: &str = "v1 (PROVISIONAL — pre-OP-3 freeze)";

/// The highest schema version this binary knows how to produce. A DB
/// reporting a higher version was written by a newer `indexd` and must
/// not be opened read-write.
pub const LATEST_VERSION: i64 = 7;

/// The ordered migration ladder. Index order MUST match ascending
/// `version`; [`MIGRATIONS`] is validated by a test.
pub const MIGRATIONS: &[Migration] = &[
    Migration {
        version: 1,
        note: SCHEMA_VERSION_NOTE,
        sql: V1_SQL,
    },
    Migration {
        version: 2,
        note: "v2 — media_entries (p2 read-only inventory)",
        sql: V2_SQL,
    },
    Migration {
        version: 3,
        note: "v3 — clip_events (event.json sidecar)",
        sql: V3_SQL,
    },
    Migration {
        version: 4,
        note: "v4 — front_parse_attempts (durable front parse outcomes)",
        sql: V4_SQL,
    },
    Migration {
        version: 5,
        note: "v5 — front_parse_attempt retry/backoff columns",
        sql: V5_SQL,
    },
    Migration {
        version: 6,
        note: "v6 — cloud sync persistence tables + indexes",
        sql: V6_SQL,
    },
    Migration {
        version: 7,
        note: "v7 — sealed upload set durability schema + guards",
        sql: V7_SQL,
    },
];

/// v2 DDL: the MEDIA (p2) read-only inventory the media screens display.
/// Pure derived state owned by indexd from scannerd's media facts; webd
/// reads it but never writes it. The row identity is
/// `(partition, rel_path)` so the same file name on different partitions
/// (or a future per-folder layout) cannot collide.
const V2_SQL: &str = "
CREATE TABLE media_entries (
    id          INTEGER PRIMARY KEY,
    partition   TEXT    NOT NULL,
    rel_path    TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    size_bytes  INTEGER NOT NULL,
    modified    TEXT,
    updated_at  INTEGER NOT NULL,
    UNIQUE (partition, rel_path)
);
";

/// v3 DDL: raw `event.json` metadata sidecar keyed by event directory.
/// This is NOT derived state: unlike trips/events (which are dropped and
/// rebuilt), these facts are scanner-sourced and survive derive rebuilds.
/// `indexd` writes the rows directly from `scannerd`'s clip-event facts and
/// reads them later at derive time.
const V3_SQL: &str = "
CREATE TABLE clip_events (
    event_dir_key         TEXT    PRIMARY KEY,
    bucket                TEXT    NOT NULL,
    primary_canonical_key TEXT    NOT NULL,
    timestamp_utc         INTEGER NOT NULL,
    timestamp_local_naive INTEGER NOT NULL,
    timestamp_has_offset  INTEGER NOT NULL,
    est_lat               REAL,
    est_lon               REAL,
    reason                TEXT,
    city                  TEXT,
    camera                TEXT,
    updated_at            INTEGER NOT NULL
);
";

/// v4 DDL: durable per-clip front parse outcomes. This records every
/// observed front parse result so failed/retried parses never silently
/// erase provenance. Backfill is conservative: historical clips with any
/// cached waypoints are marked `parsed_with_waypoints`; clips without
/// waypoints are `legacy_unknown` (never inferred as terminal empty).
const V4_SQL: &str = "
CREATE TABLE front_parse_attempts (
    canonical_key     TEXT PRIMARY KEY NOT NULL,
    parse_state       TEXT    NOT NULL,
    parse_fingerprint TEXT,
    parser_version    INTEGER,
    attempted_at      INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

INSERT OR IGNORE INTO front_parse_attempts
    (canonical_key, parse_state, parse_fingerprint, parser_version, attempted_at, updated_at)
SELECT c.canonical_key,
       CASE
           WHEN EXISTS (SELECT 1 FROM clip_waypoints w WHERE w.clip_id = c.id)
           THEN 'parsed_with_waypoints'
           ELSE 'legacy_unknown'
       END,
       NULL,
       NULL,
       CAST(strftime('%s','now') AS INTEGER),
       CAST(strftime('%s','now') AS INTEGER)
  FROM clips c
  JOIN angles a ON a.clip_id = c.id
 WHERE lower(a.camera) = 'front';
";

/// v5 DDL: add retry/backoff tracking columns for durable front parse
/// attempts. Existing rows start at `attempt_count=0` with no backoff.
const V5_SQL: &str = "
ALTER TABLE front_parse_attempts
   ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE front_parse_attempts
   ADD COLUMN next_retry_at INTEGER;
";

/// v6 DDL: cloud-sync persistence (queue, dedup oracle, history, metadata,
/// typed non-secret config).
const V6_SQL: &str = "
CREATE TABLE cloud_upload_queue (
    archive_item_id  INTEGER NOT NULL REFERENCES archive_items(id) ON DELETE CASCADE,
    child_key        TEXT    NOT NULL
                          CHECK(length(child_key) BETWEEN 1 AND 512),
    destination_id   TEXT    NOT NULL
                          CHECK(length(destination_id) BETWEEN 1 AND 128),
    remote_key       TEXT    NOT NULL
                          CHECK(length(remote_key) BETWEEN 1 AND 1024),
    category         TEXT    NOT NULL
                          CHECK(category IN ('event_sentry','trip','bulk')),
    seq              INTEGER NOT NULL CHECK(seq >= 0),
    total_bytes      INTEGER NOT NULL CHECK(total_bytes >= 0),
    bytes_uploaded   INTEGER NOT NULL
                          CHECK(bytes_uploaded >= 0 AND bytes_uploaded <= total_bytes),
    expected_hash    TEXT CHECK(expected_hash IS NULL OR length(expected_hash) <= 256),
    verify_alg       TEXT    NOT NULL
                          CHECK(verify_alg IN ('sha256','md5','crc32c','sha1','quickxor','dropbox','none')),
    content_sha256   TEXT    NOT NULL
                          CHECK(length(content_sha256)=64
                                AND content_sha256 = lower(content_sha256)
                                AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    state            TEXT    NOT NULL
                          CHECK(state IN ('queued','in_progress','done','failed','parked')),
    attempts         INTEGER NOT NULL CHECK(attempts >= 0),
    not_before       INTEGER CHECK(not_before IS NULL OR not_before >= 0),
    last_error       TEXT CHECK(last_error IS NULL OR length(last_error) <= 512),
    PRIMARY KEY(destination_id, remote_key)
);

CREATE TABLE cloud_synced_files (
    destination_id  TEXT    NOT NULL CHECK(length(destination_id) BETWEEN 1 AND 128),
    remote_key      TEXT    NOT NULL CHECK(length(remote_key) BETWEEN 1 AND 1024),
    archive_item_id INTEGER NOT NULL REFERENCES archive_items(id) ON DELETE CASCADE,
    child_key       TEXT    NOT NULL CHECK(length(child_key) BETWEEN 1 AND 512),
    content_sha256  TEXT    NOT NULL
                          CHECK(length(content_sha256)=64
                                AND content_sha256 = lower(content_sha256)
                                AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    verify_alg      TEXT    NOT NULL
                          CHECK(verify_alg IN ('sha256','md5','crc32c','sha1','quickxor','dropbox','none')),
    verify_value    TEXT CHECK(verify_value IS NULL OR length(verify_value) <= 256),
    size_bytes      INTEGER NOT NULL CHECK(size_bytes >= 0),
    synced_at       INTEGER NOT NULL CHECK(synced_at >= 0),
    completion_seq  INTEGER NOT NULL CHECK(completion_seq >= 0),
    PRIMARY KEY(destination_id, remote_key)
);

CREATE TABLE cloud_sync_history (
    id              INTEGER PRIMARY KEY,
    completion_seq  INTEGER NOT NULL CHECK(completion_seq >= 0),
    archive_item_id INTEGER NOT NULL REFERENCES archive_items(id) ON DELETE CASCADE,
    child_key       TEXT    NOT NULL CHECK(length(child_key) BETWEEN 1 AND 512),
    destination_id  TEXT    NOT NULL CHECK(length(destination_id) BETWEEN 1 AND 128),
    outcome         TEXT    NOT NULL CHECK(outcome IN ('uploaded','failed')),
    size_bytes      INTEGER NOT NULL CHECK(size_bytes >= 0),
    at              INTEGER NOT NULL CHECK(at >= 0),
    error_class     TEXT CHECK(error_class IS NULL OR length(error_class) <= 128)
);

CREATE TABLE cloud_meta (
    id                 INTEGER PRIMARY KEY CHECK(id = 1),
    completion_seq     INTEGER NOT NULL CHECK(completion_seq >= 0),
    stats_baseline_seq INTEGER NOT NULL CHECK(stats_baseline_seq >= 0),
    stats_baseline_at  INTEGER NOT NULL CHECK(stats_baseline_at >= 0),
    updated_at         INTEGER NOT NULL CHECK(updated_at >= 0)
);
INSERT OR IGNORE INTO cloud_meta
    (id, completion_seq, stats_baseline_seq, stats_baseline_at, updated_at)
VALUES (1, 0, 0, 0, 0);

CREATE TABLE cloud_provider_config (
    id                   INTEGER PRIMARY KEY CHECK(id = 1),
    sentry_enabled       INTEGER NOT NULL CHECK(sentry_enabled IN (0,1)),
    saved_enabled        INTEGER NOT NULL CHECK(saved_enabled IN (0,1)),
    recent_enabled       INTEGER NOT NULL CHECK(recent_enabled IN (0,1)),
    sentry_priority      INTEGER NOT NULL CHECK(sentry_priority >= 0),
    saved_priority       INTEGER NOT NULL CHECK(saved_priority >= 0),
    recent_priority      INTEGER NOT NULL CHECK(recent_priority >= 0),
    reserve_gb           INTEGER NOT NULL CHECK(reserve_gb >= 0),
    max_attempts         INTEGER NOT NULL CHECK(max_attempts >= 1),
    base_backoff_secs    INTEGER NOT NULL CHECK(base_backoff_secs >= 0),
    keep_until_backed_up INTEGER NOT NULL CHECK(keep_until_backed_up IN (0,1)),
    auto_sync            INTEGER NOT NULL CHECK(auto_sync IN (0,1)),
    updated_at           INTEGER NOT NULL CHECK(updated_at >= 0)
);
INSERT OR IGNORE INTO cloud_provider_config (
    id, sentry_enabled, saved_enabled, recent_enabled,
    sentry_priority, saved_priority, recent_priority,
    reserve_gb, max_attempts, base_backoff_secs,
    keep_until_backed_up, auto_sync, updated_at
)
VALUES (1, 1, 1, 1, 0, 1, 2, 0, 5, 60, 1, 1, 0);

CREATE TABLE cloud_upload_attempts (
    attempt_id      TEXT PRIMARY KEY CHECK(length(attempt_id) BETWEEN 1 AND 128),
    destination_id  TEXT NOT NULL CHECK(length(destination_id) BETWEEN 1 AND 128),
    remote_key      TEXT NOT NULL CHECK(length(remote_key) BETWEEN 1 AND 1024),
    outcome         TEXT NOT NULL CHECK(outcome IN ('uploaded','failed')),
    durable_parent  INTEGER NOT NULL CHECK(durable_parent IN (0,1)),
    completion_seq  INTEGER NOT NULL CHECK(completion_seq >= 0),
    state_after     TEXT NOT NULL CHECK(state_after IN ('queued','in_progress','done','failed','parked')),
    hash            TEXT NOT NULL
                       CHECK((length(hash)=0)
                             OR (length(hash)=64
                                 AND hash = lower(hash)
                                 AND hash NOT GLOB '*[^0-9a-f]*')),
    size_bytes      INTEGER NOT NULL CHECK(size_bytes >= 0),
    created_at      INTEGER NOT NULL CHECK(created_at >= 0)
);

CREATE INDEX idx_cloud_upload_queue_state_category_seq
    ON cloud_upload_queue(state, category, seq);
CREATE INDEX idx_cloud_sync_history_completion_seq
    ON cloud_sync_history(completion_seq);
CREATE INDEX idx_cloud_synced_files_archive_item_id
    ON cloud_synced_files(archive_item_id);
";

/// v7 DDL: sealed-upload-set durability schema and DB-level guardrails.
const V7_SQL: &str = "
ALTER TABLE archive_items
    ADD COLUMN manifest_digest TEXT CHECK(
        manifest_digest IS NULL
        OR (length(manifest_digest)=32
            AND manifest_digest = lower(manifest_digest)
            AND manifest_digest NOT GLOB '*[^0-9a-f]*')
    );
ALTER TABLE archive_items
    ADD COLUMN verified_pass_id TEXT CHECK(
        verified_pass_id IS NULL
        OR (length(verified_pass_id)=32
            AND verified_pass_id = lower(verified_pass_id)
            AND verified_pass_id NOT GLOB '*[^0-9a-f]*')
    );
ALTER TABLE archive_items
    ADD COLUMN source_generation TEXT CHECK(
        source_generation IS NULL OR length(source_generation) BETWEEN 1 AND 256
    );
ALTER TABLE archive_items
    ADD COLUMN source_event_key TEXT CHECK(
        source_event_key IS NULL OR length(source_event_key) BETWEEN 1 AND 512
    );
ALTER TABLE archive_items
    ADD COLUMN source_volume_id TEXT CHECK(
        source_volume_id IS NULL OR length(source_volume_id) BETWEEN 1 AND 128
    );
ALTER TABLE archive_items
    ADD COLUMN segment_set_digest TEXT CHECK(
        segment_set_digest IS NULL
        OR (length(segment_set_digest)=64
            AND segment_set_digest = lower(segment_set_digest)
            AND segment_set_digest NOT GLOB '*[^0-9a-f]*')
    );
ALTER TABLE archive_items
    ADD COLUMN metadata_digest TEXT CHECK(
        metadata_digest IS NULL
        OR (length(metadata_digest)=64
            AND metadata_digest = lower(metadata_digest)
            AND metadata_digest NOT GLOB '*[^0-9a-f]*')
    );

CREATE UNIQUE INDEX uq_archive_items_source_volume_event
    ON archive_items(source_volume_id, source_event_key)
    WHERE source_event_key IS NOT NULL;

CREATE TRIGGER trg_archive_items_source_identity_insert
BEFORE INSERT ON archive_items
WHEN NEW.source_event_key IS NOT NULL
  AND EXISTS (
      SELECT 1
        FROM archive_items existing
       WHERE existing.source_event_key = NEW.source_event_key
         AND (
             NEW.source_volume_id IS NULL
             OR existing.source_volume_id IS NULL
             OR existing.source_volume_id = NEW.source_volume_id
         )
  )
BEGIN
    SELECT RAISE(ABORT, 'conflicting source event identity');
END;

CREATE TRIGGER trg_archive_items_source_identity_update
BEFORE UPDATE OF source_event_key, source_volume_id ON archive_items
WHEN NEW.source_event_key IS NOT NULL
  AND EXISTS (
      SELECT 1
        FROM archive_items existing
       WHERE existing.id <> OLD.id
         AND existing.source_event_key = NEW.source_event_key
         AND (
             NEW.source_volume_id IS NULL
             OR existing.source_volume_id IS NULL
             OR existing.source_volume_id = NEW.source_volume_id
         )
  )
BEGIN
    SELECT RAISE(ABORT, 'conflicting source event identity');
END;

CREATE TABLE cloud_parent_upload_sets (
    upload_set_id           TEXT PRIMARY KEY
                                 CHECK(length(upload_set_id)=32
                                       AND upload_set_id = lower(upload_set_id)
                                       AND upload_set_id NOT GLOB '*[^0-9a-f]*'),
    archive_item_id         INTEGER NOT NULL REFERENCES archive_items(id) ON DELETE CASCADE,
    destination_id          TEXT    NOT NULL CHECK(length(destination_id) BETWEEN 1 AND 128),
    source_manifest_digest  TEXT    NOT NULL
                                 CHECK(length(source_manifest_digest)=32
                                       AND source_manifest_digest = lower(source_manifest_digest)
                                       AND source_manifest_digest NOT GLOB '*[^0-9a-f]*'),
    request_digest          TEXT    NOT NULL
                                 CHECK(length(request_digest)=64
                                       AND request_digest = lower(request_digest)
                                       AND request_digest NOT GLOB '*[^0-9a-f]*'),
    expected_child_count    INTEGER NOT NULL CHECK(expected_child_count > 0),
    created_at              INTEGER NOT NULL CHECK(created_at >= 0),
    finalized_at            INTEGER CHECK(finalized_at IS NULL OR finalized_at >= 0),
    superseded_at           INTEGER CHECK(superseded_at IS NULL OR superseded_at >= 0),
    UNIQUE(upload_set_id, destination_id)
);
CREATE UNIQUE INDEX uq_cloud_parent_upload_sets_current_parent
    ON cloud_parent_upload_sets(archive_item_id)
    WHERE superseded_at IS NULL;
CREATE INDEX idx_cloud_parent_upload_sets_parent_request
    ON cloud_parent_upload_sets(archive_item_id, request_digest);

CREATE TABLE cloud_parent_upload_set_children (
    upload_set_id      TEXT    NOT NULL,
    child_key          TEXT    NOT NULL CHECK(length(child_key) BETWEEN 1 AND 512),
    destination_id     TEXT    NOT NULL CHECK(length(destination_id) BETWEEN 1 AND 128),
    remote_key         TEXT    NOT NULL CHECK(length(remote_key) BETWEEN 1 AND 1024),
    category           TEXT    NOT NULL CHECK(category IN ('event_sentry','trip','bulk')),
    seq                INTEGER NOT NULL CHECK(seq >= 0),
    total_bytes        INTEGER NOT NULL CHECK(total_bytes >= 0),
    manifest_mtime_ms  INTEGER NOT NULL,
    content_sha256     TEXT    NOT NULL
                             CHECK(length(content_sha256)=64
                                   AND content_sha256 = lower(content_sha256)
                                   AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    expected_hash      TEXT    NOT NULL CHECK(length(expected_hash) BETWEEN 1 AND 256),
    verify_alg         TEXT    NOT NULL
                             CHECK(verify_alg IN ('sha256','md5','crc32c','sha1','quickxor','dropbox')),
    PRIMARY KEY(upload_set_id, child_key),
    UNIQUE(upload_set_id, destination_id, remote_key),
    FOREIGN KEY(upload_set_id, destination_id)
        REFERENCES cloud_parent_upload_sets(upload_set_id, destination_id) ON DELETE CASCADE
);

ALTER TABLE cloud_upload_queue
    ADD COLUMN upload_set_id TEXT
        REFERENCES cloud_parent_upload_sets(upload_set_id) ON DELETE SET NULL;
CREATE UNIQUE INDEX uq_cloud_upload_queue_upload_set_child
    ON cloud_upload_queue(upload_set_id, child_key)
    WHERE upload_set_id IS NOT NULL;
CREATE INDEX idx_cloud_upload_queue_upload_set_id
    ON cloud_upload_queue(upload_set_id);

ALTER TABLE cloud_upload_attempts
    ADD COLUMN upload_set_id TEXT
        REFERENCES cloud_parent_upload_sets(upload_set_id) ON DELETE SET NULL;

CREATE TRIGGER trg_archive_items_durable_guard
BEFORE UPDATE OF durable ON archive_items
WHEN OLD.durable = 0
  AND NEW.durable = 1
  AND NOT EXISTS (
      SELECT 1
        FROM cloud_parent_upload_sets s
       WHERE s.archive_item_id = NEW.id
         AND s.superseded_at IS NULL
         AND s.finalized_at IS NOT NULL
         AND NEW.delete_state = 'LIVE'
         AND NEW.manifest_digest IS NOT NULL
         AND NEW.manifest_digest = s.source_manifest_digest
         AND s.expected_child_count > 0
         AND s.expected_child_count = (
             SELECT COUNT(*)
               FROM cloud_parent_upload_set_children c
              WHERE c.upload_set_id = s.upload_set_id
         )
         AND s.expected_child_count = (
             SELECT COUNT(*)
               FROM cloud_upload_queue q
              WHERE q.upload_set_id = s.upload_set_id
         )
         AND NOT EXISTS (
             SELECT 1
               FROM cloud_parent_upload_set_children m
               LEFT JOIN cloud_upload_queue q
                 ON q.upload_set_id = m.upload_set_id
                AND q.destination_id = m.destination_id
                AND q.remote_key = m.remote_key
              WHERE m.upload_set_id = s.upload_set_id
                AND (
                    q.upload_set_id IS NULL
                    OR q.state <> 'done'
                    OR q.content_sha256 <> m.content_sha256
                    OR q.verify_alg <> m.verify_alg
                    OR COALESCE(q.expected_hash, '') <> m.expected_hash
                    OR q.child_key <> m.child_key
                    OR q.category <> m.category
                    OR q.seq <> m.seq
                    OR q.total_bytes <> m.total_bytes
                )
         )
  )
BEGIN
    SELECT RAISE(ABORT, 'durable requires finalized complete upload set');
END;
";

/// v1 DDL: contract D1's proposed schema, plus two internal additions
/// flagged in the build notes:
///   * `trips.polyline` BLOB — the RDP-simplified cached polyline (OQ-2
///     resolves to BOTH durable `trip_points` rows AND a cached blob).
///   * `events.front_frame_index` — the v1 VCL frame index, kept alongside
///     D1's `front_frame_offset` (ms) so v1 parity is preserved without
///     losing D1's millisecond contract.
///   * `clip_waypoints` — a derived, rebuildable cache of the sampled SEI
///     telemetry so trips/events can be re-derived without re-walking the
///     media. Pure derived state (dropped/rebuilt with trips/events).
const V1_SQL: &str = "
-- schema versioning ---------------------------------------------------
CREATE TABLE schema_version (
    version     INTEGER NOT NULL,
    applied_at  INTEGER NOT NULL,
    note        TEXT
);

-- clips: a recording session (a group of camera angles) ---------------
CREATE TABLE clips (
    id             INTEGER PRIMARY KEY,
    canonical_key  TEXT    NOT NULL UNIQUE,
    started_at     INTEGER NOT NULL,
    ended_at       INTEGER,
    partition      TEXT    NOT NULL,
    folder_class   TEXT    NOT NULL,
    is_sentry      INTEGER NOT NULL DEFAULT 0,
    duration_s     REAL,
    availability   TEXT    NOT NULL DEFAULT 'present',
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

-- angles: one camera file within a clip -------------------------------
CREATE TABLE angles (
    id          INTEGER PRIMARY KEY,
    clip_id     INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    camera      TEXT    NOT NULL,
    file_ref    TEXT    NOT NULL,
    view_kind   TEXT    NOT NULL DEFAULT 'archive',
    offset_ms   INTEGER NOT NULL DEFAULT 0,
    duration_s  REAL,
    size_bytes  INTEGER,
    UNIQUE (clip_id, camera)
);

-- clip_waypoints: derived SEI telemetry cache (INTERNAL, rebuildable) -
-- Mirrors the v1 worker waypoints so trips/events can be re-derived
-- without re-walking the media. Front-camera only (derivation source).
CREATE TABLE clip_waypoints (
    clip_id        INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    frame_index    INTEGER NOT NULL,
    offset_ms      REAL    NOT NULL,
    t              INTEGER,
    lat            REAL    NOT NULL,
    lon            REAL    NOT NULL,
    speed          REAL,
    heading        REAL,
    accel_x        REAL,
    accel_y        REAL,
    accel_z        REAL,
    autopilot      TEXT,
    gear           TEXT,
    has_gps_fix    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (clip_id, seq)
);

-- trips: per-day driving segments -------------------------------------
CREATE TABLE trips (
    id           INTEGER PRIMARY KEY,
    day          TEXT    NOT NULL,
    started_at   INTEGER NOT NULL,
    ended_at     INTEGER NOT NULL,
    bbox_min_lat REAL, bbox_min_lon REAL,
    bbox_max_lat REAL, bbox_max_lon REAL,
    distance_m   REAL,
    point_count  INTEGER NOT NULL DEFAULT 0,
    polyline     BLOB,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX idx_trips_day        ON trips(day);
CREATE INDEX idx_trips_started_at ON trips(started_at);

-- trip_points: the GPS polyline (durable rows; OQ-2) ------------------
CREATE TABLE trip_points (
    trip_id  INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    t        INTEGER NOT NULL,
    lat      REAL    NOT NULL,
    lon      REAL    NOT NULL,
    speed    REAL,
    heading  REAL,
    PRIMARY KEY (trip_id, seq)
);

-- events: hard-brake / hard-accel / sharp-turn / autopilot / sentry ---
CREATE TABLE events (
    id                 INTEGER PRIMARY KEY,
    trip_id            INTEGER REFERENCES trips(id) ON DELETE SET NULL,
    clip_id            INTEGER REFERENCES clips(id) ON DELETE SET NULL,
    type               TEXT    NOT NULL,
    severity           INTEGER,
    t                  INTEGER NOT NULL,
    lat                REAL, lon REAL,
    front_frame_offset INTEGER,
    front_frame_index  INTEGER,
    description        TEXT,
    created_at         INTEGER NOT NULL
);
CREATE INDEX idx_events_trip ON events(trip_id);
CREATE INDEX idx_events_clip ON events(clip_id);
CREATE INDEX idx_events_t    ON events(t);

-- archive_items: the retention/value/durability/delete unit -----------
CREATE TABLE archive_items (
    id             INTEGER PRIMARY KEY,
    folder_class   TEXT    NOT NULL,
    path           TEXT    NOT NULL UNIQUE,
    clip_id        INTEGER REFERENCES clips(id) ON DELETE SET NULL,
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    file_count     INTEGER NOT NULL DEFAULT 1,
    archived_at    INTEGER NOT NULL,
    delete_state   TEXT    NOT NULL DEFAULT 'LIVE'
                   CHECK (delete_state IN
                     ('LIVE','DELETE_CLAIMED','DELETING','DELETED',
                      'DELETE_FAILED','QUARANTINED')),
    delete_gen     TEXT,
    bytes_freed    INTEGER,
    durable        INTEGER NOT NULL DEFAULT 0,
    pinned         INTEGER NOT NULL DEFAULT 0,
    user_disposable INTEGER NOT NULL DEFAULT 0,
    has_event_json INTEGER NOT NULL DEFAULT 0,
    has_geo        INTEGER NOT NULL DEFAULT 0,
    event_severity INTEGER,
    sentry_flood   INTEGER NOT NULL DEFAULT 0,
    value_score    INTEGER,
    suppress_until INTEGER,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE INDEX idx_archive_state    ON archive_items(delete_state);
CREATE INDEX idx_archive_class    ON archive_items(folder_class);
CREATE INDEX idx_archive_value    ON archive_items(delete_state, value_score);
CREATE INDEX idx_archive_suppress ON archive_items(suppress_until);
CREATE INDEX idx_archive_candidate
    ON archive_items(folder_class, durable, delete_state, pinned, value_score);

-- archive_item_clips: many-to-many ------------------------------------
CREATE TABLE archive_item_clips (
    archive_item_id INTEGER NOT NULL REFERENCES archive_items(id) ON DELETE CASCADE,
    clip_id         INTEGER NOT NULL REFERENCES clips(id)         ON DELETE CASCADE,
    PRIMARY KEY (archive_item_id, clip_id)
);
CREATE INDEX idx_aic_clip ON archive_item_clips(clip_id);

-- eviction_tombstones: anti-thrash record -----------------------------
CREATE TABLE eviction_tombstones (
    id             INTEGER PRIMARY KEY,
    source_path    TEXT    NOT NULL,
    folder_class   TEXT    NOT NULL,
    size_bytes     INTEGER,
    mtime          INTEGER,
    content_hash   TEXT,
    reason         TEXT    NOT NULL,
    delete_gen     TEXT    NOT NULL,
    durable_at_evict INTEGER NOT NULL DEFAULT 0,
    suppress_until INTEGER NOT NULL,
    created_at     INTEGER NOT NULL
);
CREATE INDEX idx_tombstone_path     ON eviction_tombstones(source_path);
CREATE INDEX idx_tombstone_suppress ON eviction_tombstones(suppress_until);

-- leases: shape owned here, protocol owned by D3 ----------------------
CREATE TABLE leases (
    id              INTEGER PRIMARY KEY,
    archive_item_id INTEGER NOT NULL REFERENCES archive_items(id) ON DELETE CASCADE,
    kind            TEXT    NOT NULL CHECK (kind IN ('upload','playback')),
    holder          TEXT    NOT NULL,
    gen             TEXT    NOT NULL,
    boot_id         TEXT    NOT NULL,
    acquired_wall   INTEGER,
    expires_mono_ms INTEGER NOT NULL,
    preempt_req     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_leases_item   ON leases(archive_item_id);
CREATE INDEX idx_leases_expiry ON leases(boot_id, expires_mono_ms);

-- prefs/settings: UI + policy knobs (JSON values) ---------------------
CREATE TABLE prefs (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
";

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use rusqlite::{Connection, params};

    use super::{
        LATEST_VERSION, MIGRATIONS, V1_SQL, V2_SQL, V3_SQL, V4_SQL, V5_SQL, V6_SQL, V7_SQL,
    };
    use crate::db::{DbError, apply_migrations};

    #[test]
    fn ladder_is_monotonic_and_matches_latest() {
        let mut prev = 0_i64;
        for migration in MIGRATIONS {
            assert!(
                migration.version > prev,
                "migration versions must strictly ascend"
            );
            prev = migration.version;
        }
        assert_eq!(
            prev, LATEST_VERSION,
            "LATEST_VERSION must equal the last migration version"
        );
    }

    #[test]
    fn v4_backfills_front_parse_attempts_conservatively() {
        let mut conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(V1_SQL).unwrap();
        conn.execute_batch(V2_SQL).unwrap();
        conn.execute_batch(V3_SQL).unwrap();
        for version in 1_i64..=3 {
            conn.execute(
                "INSERT INTO schema_version(version, applied_at, note) VALUES(?1, 0, 'seed')",
                params![version],
            )
            .unwrap();
        }

        let now = 1_700_000_000_i64;
        conn.execute(
            "INSERT INTO clips
                (id, canonical_key, started_at, partition, folder_class, is_sentry, availability, created_at, updated_at)
             VALUES
                (1, 'k-has-waypoints', 1000, 'slot0', 'SavedClips', 0, 'present', ?1, ?1),
                (2, 'k-no-waypoints', 2000, 'slot0', 'SavedClips', 0, 'present', ?1, ?1)",
            params![now],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO angles(clip_id, camera, file_ref, view_kind, offset_ms)
             VALUES
                (1, 'front', 'a-front.mp4', 'ro_usb', 0),
                (2, 'front', 'b-front.mp4', 'ro_usb', 0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO clip_waypoints(clip_id, seq, frame_index, offset_ms, lat, lon)
             VALUES(1, 0, 0, 0.0, 1.0, 2.0)",
            [],
        )
        .unwrap();

        let tx = conn.transaction().unwrap();
        tx.execute_batch(V4_SQL).unwrap();
        tx.execute(
            "INSERT INTO schema_version(version, applied_at, note) VALUES(4, 0, 'v4')",
            [],
        )
        .unwrap();
        tx.commit().unwrap();

        let has: String = conn
            .query_row(
                "SELECT parse_state FROM front_parse_attempts WHERE canonical_key = 'k-has-waypoints'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let none: String = conn
            .query_row(
                "SELECT parse_state FROM front_parse_attempts WHERE canonical_key = 'k-no-waypoints'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(has, "parsed_with_waypoints");
        assert_eq!(none, "legacy_unknown");
    }

    #[test]
    fn v5_adds_retry_columns_with_defaults() {
        let mut conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(V1_SQL).unwrap();
        conn.execute_batch(V2_SQL).unwrap();
        conn.execute_batch(V3_SQL).unwrap();
        conn.execute_batch(V4_SQL).unwrap();
        for version in 1_i64..=4 {
            conn.execute(
                "INSERT INTO schema_version(version, applied_at, note) VALUES(?1, 0, 'seed')",
                params![version],
            )
            .unwrap();
        }
        conn.execute(
            "INSERT INTO front_parse_attempts
                (canonical_key, parse_state, parse_fingerprint, parser_version, attempted_at, updated_at)
             VALUES
                ('k1', 'parse_error', 'f00d', 1, 1000, 1000)",
            [],
        )
        .unwrap();

        let tx = conn.transaction().unwrap();
        tx.execute_batch(V5_SQL).unwrap();
        tx.execute(
            "INSERT INTO schema_version(version, applied_at, note) VALUES(5, 0, 'v5')",
            [],
        )
        .unwrap();
        tx.commit().unwrap();

        let row: (i64, Option<i64>) = conn
            .query_row(
                "SELECT attempt_count, next_retry_at FROM front_parse_attempts WHERE canonical_key='k1'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(row.0, 0);
        assert_eq!(row.1, None);
    }

    #[test]
    fn v6_adds_cloud_sync_tables_and_singletons() {
        let mut conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(V1_SQL).unwrap();
        conn.execute_batch(V2_SQL).unwrap();
        conn.execute_batch(V3_SQL).unwrap();
        conn.execute_batch(V4_SQL).unwrap();
        conn.execute_batch(V5_SQL).unwrap();
        for version in 1_i64..=5 {
            conn.execute(
                "INSERT INTO schema_version(version, applied_at, note) VALUES(?1, 0, 'seed')",
                params![version],
            )
            .unwrap();
        }

        let tx = conn.transaction().unwrap();
        tx.execute_batch(V6_SQL).unwrap();
        tx.execute(
            "INSERT INTO schema_version(version, applied_at, note) VALUES(6, 0, 'v6')",
            [],
        )
        .unwrap();
        tx.commit().unwrap();

        let queue_exists: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='cloud_upload_queue'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(queue_exists, 1);

        let meta_row: (i64, i64) = conn
            .query_row(
                "SELECT completion_seq, stats_baseline_seq FROM cloud_meta WHERE id = 1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(meta_row, (0, 0));
    }

    #[test]
    fn v6_enforces_cloud_queue_checks() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(V1_SQL).unwrap();
        conn.execute_batch(V2_SQL).unwrap();
        conn.execute_batch(V3_SQL).unwrap();
        conn.execute_batch(V4_SQL).unwrap();
        conn.execute_batch(V5_SQL).unwrap();
        conn.execute_batch(V6_SQL).unwrap();
        conn.execute(
            "INSERT INTO archive_items
                (id, folder_class, path, size_bytes, file_count, archived_at, created_at, updated_at)
             VALUES (1, 'RecentClips', 'archive/t1', 1, 1, 0, 0, 0)",
            [],
        )
        .unwrap();

        let bad_negative = conn.execute(
            "INSERT INTO cloud_upload_queue
                (archive_item_id, child_key, destination_id, remote_key, category, seq, total_bytes,
                 bytes_uploaded, expected_hash, verify_alg, content_sha256, state, attempts)
             VALUES
                (1, 'c1', 'dest', 'k1', 'bulk', 0, -1, 0, NULL, 'none',
                 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'queued', 0)",
            [],
        );
        assert!(bad_negative.is_err());

        let bad_uploaded_gt_total = conn.execute(
            "INSERT INTO cloud_upload_queue
                (archive_item_id, child_key, destination_id, remote_key, category, seq, total_bytes,
                 bytes_uploaded, expected_hash, verify_alg, content_sha256, state, attempts)
             VALUES
                (1, 'c2', 'dest', 'k2', 'bulk', 0, 5, 6, NULL, 'none',
                 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'queued', 0)",
            [],
        );
        assert!(bad_uploaded_gt_total.is_err());

        let bad_hash_len = conn.execute(
            "INSERT INTO cloud_upload_queue
                (archive_item_id, child_key, destination_id, remote_key, category, seq, total_bytes,
                 bytes_uploaded, expected_hash, verify_alg, content_sha256, state, attempts)
             VALUES
                (1, 'c3', 'dest', 'k3', 'bulk', 0, 5, 0, NULL, 'none', 'abc', 'queued', 0)",
            [],
        );
        assert!(bad_hash_len.is_err());

        let bad_enum = conn.execute(
            "INSERT INTO cloud_upload_queue
                (archive_item_id, child_key, destination_id, remote_key, category, seq, total_bytes,
                 bytes_uploaded, expected_hash, verify_alg, content_sha256, state, attempts)
             VALUES
                (1, 'c4', 'dest', 'k4', 'bogus', 0, 5, 0, NULL, 'none',
                 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc', 'queued', 0)",
            [],
        );
        assert!(bad_enum.is_err());
    }

    #[test]
    fn v7_migrates_from_v6_and_reapply_is_noop() {
        let mut conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        conn.execute_batch(V1_SQL).unwrap();
        conn.execute_batch(V2_SQL).unwrap();
        conn.execute_batch(V3_SQL).unwrap();
        conn.execute_batch(V4_SQL).unwrap();
        conn.execute_batch(V5_SQL).unwrap();
        conn.execute_batch(V6_SQL).unwrap();
        for version in 1_i64..=6 {
            conn.execute(
                "INSERT INTO schema_version(version, applied_at, note) VALUES(?1, 0, 'seed')",
                params![version],
            )
            .unwrap();
        }

        assert_eq!(apply_migrations(&mut conn).unwrap(), LATEST_VERSION);
        assert_eq!(apply_migrations(&mut conn).unwrap(), LATEST_VERSION);
        let rows: i64 = conn
            .query_row("SELECT COUNT(*) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(rows, LATEST_VERSION);
    }

    #[test]
    fn migrates_from_v5_with_existing_rows_to_latest() {
        // The deployed device sits at v5, so the next release applies BOTH v6 and
        // v7 in a single transaction. Seed real rows first: migrating an empty
        // table would not show that the ADD COLUMNs and the new partial UNIQUE
        // index tolerate pre-existing data.
        let mut conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        conn.execute_batch(V1_SQL).unwrap();
        conn.execute_batch(V2_SQL).unwrap();
        conn.execute_batch(V3_SQL).unwrap();
        conn.execute_batch(V4_SQL).unwrap();
        conn.execute_batch(V5_SQL).unwrap();
        for version in 1_i64..=5 {
            conn.execute(
                "INSERT INTO schema_version(version, applied_at, note) VALUES(?1, 0, 'seed')",
                params![version],
            )
            .unwrap();
        }
        for n in 0..3_i64 {
            conn.execute(
                "INSERT INTO archive_items
                    (folder_class, path, archived_at, created_at, updated_at)
                 VALUES ('RecentClips', ?1, 1700000000, 1700000000, 1700000000)",
                params![format!(
                    "/data/teslausb/archive/RecentClips/2026-07-27/e{n}"
                )],
            )
            .unwrap();
        }

        assert_eq!(apply_migrations(&mut conn).unwrap(), LATEST_VERSION);
        assert_eq!(apply_migrations(&mut conn).unwrap(), LATEST_VERSION);

        let (total, keyed): (i64, i64) = conn
            .query_row(
                "SELECT COUNT(*), COUNT(source_event_key) FROM archive_items",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(total, 3, "existing archive rows must survive the upgrade");
        assert_eq!(keyed, 0, "v7 identity columns backfill as NULL");

        // The v6 and v7 cloud tables must both exist after the two-step upgrade.
        let sets: i64 = conn
            .query_row("SELECT COUNT(*) FROM cloud_parent_upload_sets", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(sets, 0);
        let queue: i64 = conn
            .query_row("SELECT COUNT(*) FROM cloud_upload_queue", [], |r| r.get(0))
            .unwrap();
        assert_eq!(queue, 0);
    }

    #[test]
    fn v7_forward_only_guard_rejects_future_schema() {
        let mut conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        conn.execute_batch(V1_SQL).unwrap();
        conn.execute(
            "INSERT INTO schema_version(version, applied_at, note) VALUES (?1, 0, 'future')",
            params![LATEST_VERSION + 1],
        )
        .unwrap();
        let result = apply_migrations(&mut conn);
        assert!(matches!(result, Err(DbError::SchemaTooNew { .. })));
    }

    #[test]
    #[allow(clippy::too_many_lines)]
    fn v7_enforces_new_checks_and_guards() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(V1_SQL).unwrap();
        conn.execute_batch(V2_SQL).unwrap();
        conn.execute_batch(V3_SQL).unwrap();
        conn.execute_batch(V4_SQL).unwrap();
        conn.execute_batch(V5_SQL).unwrap();
        conn.execute_batch(V6_SQL).unwrap();
        conn.execute_batch(V7_SQL).unwrap();

        conn.execute(
            "INSERT INTO archive_items
                (id, folder_class, path, size_bytes, file_count, archived_at, created_at, updated_at)
             VALUES
                (1, 'RecentClips', 'archive/v7-1', 1, 1, 0, 0, 0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO archive_items
                (id, folder_class, path, size_bytes, file_count, archived_at, created_at, updated_at)
             VALUES
                (2, 'RecentClips', 'archive/v7-2', 1, 1, 0, 0, 0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO archive_items
                (id, folder_class, path, size_bytes, file_count, archived_at, created_at, updated_at)
             VALUES
                (3, 'RecentClips', 'archive/v7-3', 1, 1, 0, 0, 0)",
            [],
        )
        .unwrap();

        let bad_manifest_len = conn.execute(
            "UPDATE archive_items SET manifest_digest='abc', verified_pass_id='0123456789abcdef0123456789abcdef'
              WHERE id=1",
            [],
        );
        assert!(bad_manifest_len.is_err());

        let bad_manifest_case = conn.execute(
            "UPDATE archive_items
                SET manifest_digest='ABCDEFabcdefabcdefabcdefabcdefab',
                    verified_pass_id='0123456789abcdef0123456789abcdef'
              WHERE id=1",
            [],
        );
        assert!(bad_manifest_case.is_err());

        let bad_manifest_hex = conn.execute(
            "UPDATE archive_items
                SET manifest_digest='gggggggggggggggggggggggggggggggg',
                    verified_pass_id='0123456789abcdef0123456789abcdef'
              WHERE id=1",
            [],
        );
        assert!(bad_manifest_hex.is_err());

        let bad_verified_len = conn.execute(
            "UPDATE archive_items
                SET manifest_digest='0123456789abcdef0123456789abcdef',
                    verified_pass_id='abc'
              WHERE id=1",
            [],
        );
        assert!(bad_verified_len.is_err());

        let bad_verified_case = conn.execute(
            "UPDATE archive_items
                SET manifest_digest='0123456789abcdef0123456789abcdef',
                    verified_pass_id='ABCDEFabcdefabcdefabcdefabcdefab'
              WHERE id=1",
            [],
        );
        assert!(bad_verified_case.is_err());

        let bad_verified_hex = conn.execute(
            "UPDATE archive_items
                SET manifest_digest='0123456789abcdef0123456789abcdef',
                    verified_pass_id='gggggggggggggggggggggggggggggggg'
              WHERE id=1",
            [],
        );
        assert!(bad_verified_hex.is_err());

        let bad_segment_len = conn.execute(
            "UPDATE archive_items
                SET manifest_digest='0123456789abcdef0123456789abcdef',
                    verified_pass_id='0123456789abcdef0123456789abcdef',
                    segment_set_digest='abc'
              WHERE id=1",
            [],
        );
        assert!(bad_segment_len.is_err());

        let bad_segment_case = conn.execute(
            "UPDATE archive_items
                SET manifest_digest='0123456789abcdef0123456789abcdef',
                    verified_pass_id='0123456789abcdef0123456789abcdef',
                    segment_set_digest='ABCDEFabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd'
              WHERE id=1",
            [],
        );
        assert!(bad_segment_case.is_err());

        let bad_segment_hex = conn.execute(
            "UPDATE archive_items
                SET manifest_digest='0123456789abcdef0123456789abcdef',
                    verified_pass_id='0123456789abcdef0123456789abcdef',
                    segment_set_digest='gggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggg'
              WHERE id=1",
            [],
        );
        assert!(bad_segment_hex.is_err());

        conn.execute(
            "UPDATE archive_items
                SET manifest_digest='0123456789abcdef0123456789abcdef',
                    verified_pass_id='fedcba9876543210fedcba9876543210',
                    segment_set_digest='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
              WHERE id=1",
            [],
        )
        .unwrap();

        conn.execute(
            "UPDATE archive_items
                SET source_event_key='event-key-1',
                    source_volume_id=NULL
              WHERE id=1",
            [],
        )
        .unwrap();
        let null_volume_conflict = conn.execute(
            "UPDATE archive_items
                SET source_event_key='event-key-1',
                    source_volume_id='vol-2'
              WHERE id=2",
            [],
        );
        assert!(null_volume_conflict.is_err());

        conn.execute(
            "UPDATE archive_items
                SET source_event_key='event-key-2',
                    source_volume_id='vol-1'
              WHERE id=2",
            [],
        )
        .unwrap();
        conn.execute(
            "UPDATE archive_items
                SET source_event_key='event-key-2',
                    source_volume_id='vol-2'
              WHERE id=3",
            [],
        )
        .unwrap();

        let durable_without_complete =
            conn.execute("UPDATE archive_items SET durable=1 WHERE id=1", []);
        assert!(durable_without_complete.is_err());
    }
}
