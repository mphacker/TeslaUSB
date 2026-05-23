# ADR-0019 — Materialise trips and detected_events in the worker DB

- **Status**: Accepted
- **Date**: 2026-05-23
- **Branch**: `b1-userspace-rust`
- **Supersedes**: ADR-0017 §"Alternatives considered B — Materialise
  trips/events as worker tables" (was *Rejected for v1*).
- **Driver**: Operator (binding), 2026-05-23 session:
  > *"Look at how V1 handled map loading from the database."*
  >
  > *"Don't take shortcuts. Remember that. And don't forget to do
  > code reviews and update documents."*

## Context

ADR-0017 chose to keep the worker DB lean (`clips` + `waypoints`
only) and derive trips, event detections, and sentry
classifications **at query time in Python**, inside
`web/teslausb_web/services/mapping_queries.py::_load_snapshot`.

That ADR explicitly carried an escape hatch:

> *"We may reconsider if query latency becomes user-visible
> (>200 ms on the map page) — at which point a materialised
> view or a worker-maintained denormalised cache becomes
> justified. Until then: derive."* — ADR-0017 §B

### Measurement that triggered this ADR

On `cybertruckusb.local` (Pi Zero 2 W, 2026-05-23 16:00 EDT,
~1,369 clips / ~95,089 waypoints / 16 MB DB):

| Endpoint                | Cold time | Notes                                       |
|-------------------------|-----------|---------------------------------------------|
| `_load_snapshot` (each) | ~3-5 s    | Full clip+waypoint scan + Python derivation |
| Map page load (7 calls) | ~15-35 s  | 7× `_load_snapshot` runs in parallel        |

The map page makes **7 independent API calls** on first paint
(`/api/stats`, `/api/driving-stats`, `/api/event-charts`,
`/api/sentry-events`, `/api/days`, `/api/all-routes` or
`/api/day/<date>/routes`, `/api/events`). Every one of them
calls `_load_snapshot()` which loads ALL clips, loads ALL
waypoints, groups into trips in Python, derives events in
Python. There is no shared cache, no SQL pushdown, no early
filtering — every endpoint independently rebuilds the entire
derived dataset, then returns a small filtered slice.

The cost scales linearly with archive size. At 1,369 clips it
is 35 s; at 10,000 clips it would be ~4 min per page load.

ADR-0017's own trigger threshold (200 ms) has been exceeded
by **two orders of magnitude**. This ADR re-opens its
alternative B.

### How v1 did this (and why it was fast)

V1's `scripts/web/services/mapping_queries.py` (preserved on
the `main` branch) had a fundamentally different architecture:
the indexer wrote materialised `trips` and `detected_events`
tables to SQLite alongside `waypoints`, and each web endpoint
issued a small targeted SQL query against them.

For example, v1's `get_stats(db_path)` issues seven scalar
queries (`SELECT COUNT(*) FROM trips`,
`SELECT COALESCE(SUM(distance_km), 0) FROM trips`, etc.).
Each runs in milliseconds. The entire endpoint returns in
< 50 ms regardless of archive size. v1's `query_trips`
endpoint is a single `SELECT … FROM trips … LIMIT 50` —
bounded by page size, not by total clips.

The operator's directive — *"Look at how V1 handled map
loading"* — points squarely at this architecture.

## Decision

The Rust worker (`teslausb-worker`) becomes responsible for
**materialising** the derived view that the mapping layer
needs. Three new tables are added to the worker DB
(`/var/lib/teslausb/index.sqlite3`) under schema version 3:

### Schema v3 (added by migration v2 → v3)

```sql
CREATE TABLE trips (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    start_utc         INTEGER NOT NULL,
    end_utc           INTEGER NOT NULL,
    start_clip_id     INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    end_clip_id       INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    start_lat         REAL,
    start_lon         REAL,
    end_lat           REAL,
    end_lon           REAL,
    distance_km       REAL NOT NULL DEFAULT 0,
    duration_seconds  INTEGER NOT NULL DEFAULT 0,
    waypoint_count    INTEGER NOT NULL DEFAULT 0,
    event_count       INTEGER NOT NULL DEFAULT 0,
    video_count       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX trips_by_start_utc ON trips(start_utc DESC);
CREATE INDEX trips_by_day ON trips(date(start_utc, 'unixepoch'));

CREATE TABLE detected_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id         INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    clip_id         INTEGER REFERENCES clips(id) ON DELETE SET NULL,
    event_type      TEXT NOT NULL,
    severity        TEXT NOT NULL,
    timestamp_utc   INTEGER NOT NULL,
    latitude_deg    REAL,
    longitude_deg   REAL,
    speed_mps       REAL,
    metadata_json   TEXT
);
CREATE INDEX events_by_trip ON detected_events(trip_id);
CREATE INDEX events_by_type ON detected_events(event_type);
CREATE INDEX events_by_severity_ts ON detected_events(severity, timestamp_utc DESC);
CREATE INDEX events_by_day_type ON detected_events(date(timestamp_utc, 'unixepoch'), event_type);

CREATE TABLE clip_trip_map (
    clip_id   INTEGER PRIMARY KEY REFERENCES clips(id) ON DELETE CASCADE,
    trip_id   INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE
);
CREATE INDEX clip_trip_map_by_trip ON clip_trip_map(trip_id);
```

The `clip_trip_map` table makes per-clip trip lookups O(1)
without scanning `trips` rows, and lets the indexer cheaply
re-assign a clip when a neighbouring gap closes.

### Derivation rules (preserved verbatim from ADR-0017)

These rules live in two new Rust modules
(`trips.rs`, `events.rs`) and the existing Python derivation
modules are deleted. Identical semantics; just executed once
at index time and persisted, instead of recomputed per request.

- **Trip grouping:** clips with the same `bucket` (excluding
  `sentry`) whose `clip_started_utc` values are within
  `trip_gap_seconds` (default **300 s**) form one trip.
- **Speed-limit event:** waypoint with `speed_mps > 35.76`
  (80 mph). Severity = `warning`.
- **Hard-accel:** `acceleration_x > 3.5`. Severity = `warning`.
- **Harsh-brake:** `acceleration_x < -4.0`. Severity = `warning`.
- **Emergency-brake:** `acceleration_x < -7.0`. Severity =
  `critical`. (Supersedes a co-occurring harsh-brake.)
- **Sharp-turn:** `|acceleration_y| > 4.0`. Severity = `warning`.
- **Autopilot transition:** `autopilot_state` differs from the
  previous waypoint in the same trip. Severity = `info`.
- **Sentry event:** `bucket = 'sentry'` and `gps_waypoint_count = 0`
  on the clip. Materialised as a degenerate single-clip
  "trip" only if needed for the events panel; otherwise stored
  with `trip_id = NULL`-equivalent sentinel.

Trip-membership and event-detection are **idempotent**:
re-running them on a stable set of clips produces the same
output. This is critical for the recompute-on-insert pipeline
(O.4) and the backfill job (O.5).

### Worker pipeline hook

When `Indexer::handle_event` finishes inserting a clip's
waypoints, it calls a new method `Indexer::refresh_trip(clip_id)`
which:

1. Locates the clip in `clip_trip_map`. If absent, this is a
   newly indexed clip.
2. Finds neighbouring clips in the same `bucket` whose
   `clip_started_utc` is within `trip_gap_seconds` on either
   side. They form a candidate trip group.
3. Compares against existing `trips` rows for those clips:
   - Same group, same membership → no-op.
   - New clip extends an existing trip → `UPDATE trips SET
     end_utc, end_clip_id, …`.
   - New clip merges two existing trips → `DELETE` the older
     row, `UPDATE` the merged one, `UPDATE clip_trip_map`.
   - No existing trip → `INSERT trips`, `INSERT clip_trip_map`.
4. Re-derives `detected_events` for the affected trip (one
   `DELETE WHERE trip_id = ?` + N `INSERT`s) inside a
   transaction.

All updates run inside a single SQLite transaction per clip so
mid-write crashes can't leave a half-derived trip behind.

### Backfill job

The v2 → v3 migration runs only the `CREATE TABLE` /
`CREATE INDEX` statements; the new tables are initially empty.
At supervisor startup (post-migration), if `trips` is empty
but `clips` has rows, run a one-shot `backfill_trips_and_events()`
job that streams clips in `clip_started_utc` order and calls
`refresh_trip` for each. This is logged at INFO and runs in
the indexer task before the watcher starts so newly-arriving
clips don't race with the rebuild.

The backfill is resumable: if it crashes halfway, the next
run re-derives only the clips that aren't in `clip_trip_map`.

## Alternatives considered

### A. Snapshot cache in the Python layer (the stopgap I started)

Add a TTL+mtime-keyed cache around `_load_snapshot()` so the
seven parallel calls on one page load share a single
computation. Reduces 7× cost to 1× — first paint goes from
~35 s to ~5 s.

**Rejected as the primary fix.** It does not change the
per-derivation cost (still 3-5 s) and it grows with archive
size. Operator on a 10k-clip archive would still see 30+ s
first paint. v1's pattern doesn't have this asymptote because
the SQL it runs is bounded by page size, not archive size.
This option may still ship as a defensive measure *during*
the Phase O rollout, but it is not the resting state.

### B. Push derivation into SQL CTEs (no schema change)

Keep the lean schema but rewrite `mapping_queries.py` to run
trip-grouping and event detection via recursive SQL CTEs
against `clips` + `waypoints` on each call.

**Rejected.** Recursive CTEs over 100k rows are slow on the
Pi's SQLite build (no native window functions for autopilot
state transitions; haversine in SQL is awkward). And the
derived data would still be recomputed per call. The first-
class fix is to write the derived data once at index time.

### C. Materialise in a sibling Python process

A Python service watches the worker DB and writes a derived
`mapping.db` next to it.

**Rejected — it is exactly the v1 mistake ADR-0017 fixed.**
Two parsers, two DBs, drift potential, queue-never-drained
risk. The whole point of ADR-0017 was *one source of truth*;
the derivation belongs in the worker, not in a sibling.

### D. Materialise on first read instead of at index time

`_load_snapshot` writes its result to a `derived_cache` table
on first miss; subsequent calls hit the table.

**Rejected.** Moves the cost from "every request" to "first
request after an indexer write" — better, but not eliminated.
The user-visible variance (some loads instant, some 30 s) is
worse than a steady-state ~50 ms because operators retry
when a page hangs, masking real bugs as "slow page". Derive
once, at index time, and the page is fast every time.

## Consequences

### Positive

- Map page load becomes O(rows returned), not O(archive size).
  Expected: < 500 ms on the Pi for both first paint and
  subsequent navigation.
- All seven map endpoints become small targeted SQL queries
  (matches v1's proven model). `_load_snapshot()` is deleted.
- Trip and event semantics live in **one place** (Rust) and
  are tested as part of the worker test suite. No more
  Python↔derivation drift potential.
- `mapping_trip_derivation.py` (~414 LOC) and
  `mapping_event_derivation.py` (~255 LOC) are deleted. The
  rewritten `mapping_queries.py` is significantly smaller.
- The new `clip_trip_map` table makes "given this clip, what
  trip does it belong to?" — a question the sentry inspector
  and per-clip waypoint endpoints ask — an O(1) PK lookup.

### Negative

- **Worker complexity grows.** Two new modules in Rust
  (~800 LOC total estimate including tests) plus migration
  plus backfill. The worker now has a "derived data" pipeline
  it didn't have before.
- **Tuning thresholds requires a worker rebuild + deploy.**
  Under ADR-0017, the operator could edit Python numbers and
  reload the web. Under this ADR, changing an event-detection
  threshold means a Rust build. **Mitigation:** the thresholds
  go through the existing `worker.toml` config so they can be
  tuned without a rebuild; only structural changes
  (new event types) require code.
- **Backfill on first deploy may be long.** ~1,400 clips on
  the Pi probably finishes in 30-60 s. Logged and resumable.
  Operator should expect the map to be empty for that window.
- **A bug in trip-grouping logic now produces persisted bad
  data** (instead of a transient wrong page that goes away on
  refresh). Mitigation: the recompute-on-insert is idempotent,
  so fixing the bug + redeploying corrects every trip on the
  next clip insertion. A manual `re-derive` admin command may
  be added later if needed.

### Migration

See `docs/01-PROGRESS.md` Phase O table for the full step
list (O.0 - O.14). Roll-back path: revert the worker binary
to its pre-O.13 `.b1-backup-<timestamp>` and the new tables
become inert (the old `_load_snapshot` Python code is gone,
but reverting the web binary alongside the worker restores
the read path).

## References

- ADR-0017 (this supersedes its §B): mapping single source of
  truth.
- ADR-0018: LUN-aware cleanup pressure (same session, related
  hardware verification work that surfaced this).
- v1 reference implementation:
  `scripts/web/services/mapping_queries.py` on the `main`
  branch — see `get_stats`, `query_trips`, `get_event_chart_data`.
- Operator directive 2026-05-23:
  *"Look at how V1 handled map loading from the database."*
- Operator directive 2026-05-22 (still binding):
  *"don't take shortcuts. … and don't forget to do code
  reviews and update documents."*
- Latency measurement on `cybertruckusb.local`: this session's
  hardware logs, 2026-05-23 16:00 EDT.
