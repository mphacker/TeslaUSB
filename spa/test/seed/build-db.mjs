// Builds a seeded, read-only-openable indexd catalog SQLite DB for the SPA's
// Playwright UAT. The schema DDL is transcribed verbatim from
// rust/crates/indexd/src/db/migrations.rs (V1_SQL) plus the schema_version v1
// row that webd's catalog guard requires. Sample data mirrors the 0.4 parity
// baseline: one civil day with 3 trips / 6 clips / 3 events.
//
// No crate edits and no native deps: uses Node's built-in `node:sqlite`.
//
// Usage: node test/seed/build-db.mjs [outPath]   (default: <cwd>/test/seed/catalog.db)

import { DatabaseSync } from "node:sqlite";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";

const out = resolve(process.argv[2] ?? resolve(import.meta.dirname, "catalog.db"));
mkdirSync(dirname(out), { recursive: true });
// Start fresh so re-runs are deterministic.
for (const ext of ["", "-wal", "-shm", "-journal"]) {
  try {
    rmSync(out + ext);
  } catch {
    /* not present — fine */
  }
}

// --- Schema (verbatim from indexd V1_SQL) ----------------------------------
const SCHEMA = `
CREATE TABLE schema_version (
    version     INTEGER NOT NULL,
    applied_at  INTEGER NOT NULL,
    note        TEXT
);
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
CREATE TABLE archive_item_clips (
    archive_item_id INTEGER NOT NULL REFERENCES archive_items(id) ON DELETE CASCADE,
    clip_id         INTEGER NOT NULL REFERENCES clips(id)         ON DELETE CASCADE,
    PRIMARY KEY (archive_item_id, clip_id)
);
CREATE INDEX idx_aic_clip ON archive_item_clips(clip_id);
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
CREATE TABLE prefs (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
`;

// --- Sample data: one civil day, 3 trips / 6 clips / 3 events --------------
// 2024-06-01 (UTC). Epoch seconds anchored at local-ish daytime values.
const DAY = "2024-06-01";
// Epoch seconds for 2024-06-01T00:00:00Z + h hours + m minutes. Math.round
// guarantees an INTEGER (float hour math like 8.1%1*60 is not exact, and the
// time columns have INTEGER affinity — a REAL would make webd's i64 reads fail).
const T = (h, m = 0) => Math.round(1717200000 + h * 3600 + m * 60);

const CLIPS = [
  // id, key, start(h), end(h), folder_class, is_sentry, dur_s
  [1, "2024-06-01_07-15-00", 7, 7.2, "RecentClips", 0, 60.0],
  [2, "2024-06-01_08-02-00", 8, 8.1, "SavedClips", 0, 45.0],
  [3, "2024-06-01_12-30-00", 12, 12.1, "SavedClips", 0, 50.0],
  [4, "2024-06-01_14-10-00", 14, 14.2, "SentryClips", 1, 30.0],
  [5, "2024-06-01_18-45-00", 18, 18.1, "RecentClips", 0, 60.0],
  [6, "2024-06-01_21-05-00", 21, 21.1, "SentryClips", 1, 25.0],
];

const ANGLES = ["front", "back", "left_repeater", "right_repeater"];

// Each trip route is a list of [lat, lon, speed_mps] points. Speeds deliberately
// span all six viridis speed buckets (≈11→81 mph) so the speed-coloured
// polylines AND the mph/kmh toggle have a visible, assertable effect. Trip 1 and
// Trip 2 SHARE an overlapping segment (≈37.802,-122.404 → 37.805,-122.401) so a
// click there yields TWO route-disambiguation candidates. Events are seeded at
// coordinates that lie ON a route so their bubbles land on the drawn path.
const ROUTE_1 = [
  [37.772, -122.445, 5], [37.778, -122.438, 9], [37.785, -122.430, 16],
  [37.790, -122.420, 24], [37.796, -122.412, 30], [37.802, -122.404, 16],
  [37.805, -122.401, 9], [37.808, -122.392, 5],
];
const ROUTE_2 = [
  [37.801, -122.408, 9], [37.802, -122.404, 16], [37.805, -122.401, 24],
  [37.815, -122.392, 30], [37.825, -122.384, 36], [37.830, -122.380, 24],
  [37.842, -122.362, 16], [37.858, -122.342, 9],
];
// Trip 3 has NO trip_points — it exercises the pre-decoded polyline-BLOB
// fallback render path (webd decodes trips.polyline when points are absent).
const ROUTE_3_POLYLINE = [
  [37.745, -122.465], [37.760, -122.450], [37.775, -122.430], [37.788, -122.402],
];

const TRIPS = [
  // id, start(h), end(h), distance_m, points([lat,lon,mps]), polylineSegments|null
  // Trip 1: full per-point geometry AND a cached polyline BLOB (both paths live).
  [1, 7, 7.5, 8230.4, ROUTE_1, [ROUTE_1.map((p) => [p[0], p[1]])]],
  // Trip 2: per-point geometry only (NULL polyline → exercises points path).
  [2, 12, 12.6, 12450.9, ROUTE_2, null],
  // Trip 3: no points, polyline BLOB only (exercises the fallback path).
  [3, 18, 18.7, 9875.0, [], [ROUTE_3_POLYLINE]],
];

const EVENTS = [
  // id, trip_id, clip_id, type, severity, t(h), lat, lon, off_ms, frame, desc
  // Two trip-linked, on-route bubbles (Sentry-derived hard-brake + accel) …
  [1, 1, 2, "harsh_braking", 3, 7.3, 37.79, -122.42, 1500, 45, "Harsh braking"],
  [2, 2, 3, "hard_acceleration", 2, 12.4, 37.83, -122.38, 1800, 54, "Hard acceleration"],
  // … and one trip-less Sentry event (panel-only; not a per-trip map bubble).
  [3, null, 4, "sentry", 1, 14.15, 37.76, -122.44, null, null, "Sentry event"],
];

/** Axis-aligned bbox of a [lat,lon][] list (nulls when empty). */
function bboxOf(coords) {
  if (!coords.length) {
    return { minLat: null, minLon: null, maxLat: null, maxLon: null };
  }
  let minLat = Infinity, minLon = Infinity, maxLat = -Infinity, maxLon = -Infinity;
  for (const [la, lo] of coords) {
    if (la < minLat) minLat = la;
    if (la > maxLat) maxLat = la;
    if (lo < minLon) minLon = lo;
    if (lo > maxLon) maxLon = lo;
  }
  return { minLat, minLon, maxLat, maxLon };
}

/**
 * Encode polyline segments into the indexd big-endian BLOB webd decodes:
 * `u32 segment_count`, then per segment `u32 point_count` followed by
 * `point_count × (f64 lat, f64 lon)` (see webd/src/polyline.rs).
 */
function encodePolyline(segments) {
  const totalPoints = segments.reduce((n, s) => n + s.length, 0);
  const buf = Buffer.alloc(4 + segments.length * 4 + totalPoints * 16);
  let off = 0;
  buf.writeUInt32BE(segments.length, off);
  off += 4;
  for (const seg of segments) {
    buf.writeUInt32BE(seg.length, off);
    off += 4;
    for (const [lat, lon] of seg) {
      buf.writeDoubleBE(lat, off);
      off += 8;
      buf.writeDoubleBE(lon, off);
      off += 8;
    }
  }
  return buf;
}

const db = new DatabaseSync(out);
db.exec("PRAGMA foreign_keys=ON;");
// Rollback journal (not WAL) so the resulting file is trivially openable with
// SQLITE_OPEN_READ_ONLY and leaves no -wal/-shm sidecars for webd to trip on.
db.exec("PRAGMA journal_mode=DELETE;");
db.exec(SCHEMA);

db.exec(
  "INSERT INTO schema_version (version, applied_at, note) VALUES (1, 0, 'v1 (PROVISIONAL — pre-OP-3 freeze)');",
);

const insClip = db.prepare(
  "INSERT INTO clips (id, canonical_key, started_at, ended_at, partition, folder_class, is_sentry, duration_s, availability, created_at, updated_at) VALUES (?, ?, ?, ?, 'p1', ?, ?, ?, 'present', 0, 0)",
);
const insAngle = db.prepare(
  "INSERT INTO angles (clip_id, camera, file_ref, view_kind, offset_ms, duration_s, size_bytes) VALUES (?, ?, ?, 'archive', 0, ?, ?)",
);
for (const [id, key, sh, eh, fc, sentry, dur] of CLIPS) {
  insClip.run(id, key, T(Math.floor(sh), (sh % 1) * 60), T(Math.floor(eh), (eh % 1) * 60), fc, sentry, dur);
  ANGLES.forEach((cam, i) => {
    insAngle.run(id, cam, `p1/${key}/${cam}.mp4`, dur, 1_000_000 + i * 100_000);
  });
}

const insTrip = db.prepare(
  "INSERT INTO trips (id, day, started_at, ended_at, bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon, distance_m, point_count, polyline, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
);
const insPoint = db.prepare(
  "INSERT INTO trip_points (trip_id, seq, t, lat, lon, speed, heading) VALUES (?, ?, ?, ?, ?, ?, ?)",
);
for (const [id, sh, eh, dist, route, polySegs] of TRIPS) {
  const coords = route.length
    ? route.map((p) => [p[0], p[1]])
    : polySegs
      ? polySegs.flat()
      : [];
  const bbox = bboxOf(coords);
  const blob = polySegs ? encodePolyline(polySegs) : null;
  const startSec = T(Math.floor(sh), (sh % 1) * 60);
  insTrip.run(
    id,
    DAY,
    startSec,
    T(Math.floor(eh), (eh % 1) * 60),
    bbox.minLat,
    bbox.minLon,
    bbox.maxLat,
    bbox.maxLon,
    dist,
    route.length,
    blob,
  );
  route.forEach((p, s) => {
    insPoint.run(id, s, startSec + s * 60, p[0], p[1], p[2], 90);
  });
}

const insEvent = db.prepare(
  "INSERT INTO events (id, trip_id, clip_id, type, severity, t, lat, lon, front_frame_offset, front_frame_index, description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
);
for (const [id, trip, clip, type, sev, th, lat, lon, off, frame, desc] of EVENTS) {
  insEvent.run(id, trip, clip, type, sev, T(Math.floor(th), (th % 1) * 60), lat, lon, off, frame, desc);
}

const insPref = db.prepare("INSERT INTO prefs (key, value) VALUES (?, ?)");
// Generic prefs.
insPref.run("speed_unit", "mph");
insPref.run("map_provider", "osm");
// Config-section bindings consumed by the settings dashboard (/api/settings):
// the Mapping & Indexing and Network File Sharing forms read these keys. Values
// are deliberately chosen to DIFFER from the screen's template defaults
// (mph / 85 / 10 / "" / false) so the UAT can prove the fields are bound to the
// /api/settings response and not just showing hard-coded fallbacks.
insPref.run("speed_units", "kph");
insPref.run("speed_limit_mph", "75");
insPref.run("trip_gap_minutes", "15");
insPref.run("display_timezone", "America/Los_Angeles");
insPref.run("samba_enabled", "true");

// --- Verification: fail loudly rather than emit a subtly-broken catalog ------
function one(sql) {
  const row = db.prepare(sql).get();
  return row ? Object.values(row)[0] : undefined;
}
const checks = [];
const integrity = one("PRAGMA integrity_check");
if (integrity !== "ok") checks.push(`integrity_check=${integrity}`);
const fkBad = db.prepare("PRAGMA foreign_key_check").all();
if (fkBad.length) checks.push(`foreign_key_check failed: ${JSON.stringify(fkBad)}`);
if (one("SELECT COALESCE(MAX(version),0) FROM schema_version") !== 1)
  checks.push("schema_version != 1");
// Time columns must be stored as INTEGER, never REAL.
for (const [tbl, col] of [
  ["clips", "started_at"],
  ["clips", "ended_at"],
  ["trips", "started_at"],
  ["trips", "ended_at"],
  ["events", "t"],
  ["trip_points", "t"],
]) {
  const bad = one(
    `SELECT COUNT(*) FROM ${tbl} WHERE ${col} IS NOT NULL AND typeof(${col}) != 'integer'`,
  );
  if (bad) checks.push(`${tbl}.${col} has ${bad} non-integer values`);
}
const counts = {
  trips: one("SELECT COUNT(*) FROM trips"),
  clips: one("SELECT COUNT(*) FROM clips"),
  events: one("SELECT COUNT(*) FROM events"),
  angles: one("SELECT COUNT(*) FROM angles"),
};
if (counts.trips !== 3 || counts.clips !== 6 || counts.events !== 3)
  checks.push(`unexpected counts ${JSON.stringify(counts)}`);

// Map-render preconditions: routes must form a visible path, the overlapping
// segment must exist on two trips (disambiguation), and the BLOB fallback path
// (trip 3) must carry a non-NULL polyline that trip has no points for.
const tripPoints = one("SELECT COUNT(*) FROM trip_points");
if (tripPoints !== ROUTE_1.length + ROUTE_2.length)
  checks.push(`trip_points=${tripPoints} (expected ${ROUTE_1.length + ROUTE_2.length})`);
const polyTrips = one("SELECT COUNT(*) FROM trips WHERE polyline IS NOT NULL");
if (polyTrips !== 2) checks.push(`trips with polyline BLOB=${polyTrips} (expected 2)`);
const t3pts = one("SELECT COUNT(*) FROM trip_points WHERE trip_id=3");
if (t3pts !== 0) checks.push(`trip 3 must have 0 points (got ${t3pts})`);
const t3poly = one("SELECT polyline IS NOT NULL FROM trips WHERE id=3");
if (!t3poly) checks.push("trip 3 must carry a polyline BLOB for the fallback path");
const eventTypes = one("SELECT COUNT(DISTINCT type) FROM events");
if (eventTypes !== 3) checks.push(`distinct event types=${eventTypes} (expected 3)`);
const tripLinked = one("SELECT COUNT(*) FROM events WHERE trip_id IS NOT NULL");
if (tripLinked !== 2) checks.push(`trip-linked events=${tripLinked} (expected 2)`);

db.close();

// No rollback-journal sidecars should remain after a clean close.
for (const ext of ["-wal", "-shm", "-journal"]) {
  if (existsSync(out + ext)) checks.push(`stale sidecar ${out + ext}`);
}

if (checks.length) {
  console.error("seed verification FAILED:\n - " + checks.join("\n - "));
  process.exit(1);
}
console.log(
  `seeded catalog → ${out}  (${counts.trips} trips / ${counts.clips} clips / ${counts.events} events / ${counts.angles} angles)`,
);
