# ADR-0024 — Persist raw Tesla `event.json` as a `clip_events` table (schema v5)

- **Status**: Accepted
- **Date**: 2026-06-02
- **Branch**: `b1-userspace-rust`
- **Relates to**: ADR-0017 (mapping single source of truth),
  ADR-0019 (materialise trips and `detected_events`),
  ADR-0010 (rusqlite for the indexer store).
- **Driver**: Operator bugs (2026-06-02): Saved/Sentry honk clips
  never appeared on the map, and the root page never landed on an
  event-only day. Root cause: the worker indexed clips/waypoints but
  never read the Tesla `event.json` files that mark Saved/Sentry
  events.

## Context

The maps page must show "everything that drops a pin" for a given
day, including Saved-clip and Sentry events such as a horn-honk. Each
such event is described by a Tesla `event.json` file that sits in the
event directory alongside the camera `.mp4` clips. It carries the
authoritative event `timestamp`, an estimated `est_lat`/`est_lon`, a
`reason` code (e.g. `user_interaction_honk`), `city`, and `camera`.

Before this change the worker DB held only `clips` + `waypoints`
(schema v1/v2) and the derived `trips` / `detected_events` /
`clip_trip_map` tables (schema v3, ADR-0019). `detected_events` is
**derived** from SEI waypoint telemetry (harsh-brake, speeding,
autopilot, etc.) — it is not a place to store Tesla's own
externally-authored event metadata. There was no table representing
the raw `event.json` events, so:

1. Honk/Sentry events could not be surfaced as map pins.
2. The "latest date with data" union (trip start vs. event time)
   could not consider event-only days.

## Decision

Add one **additive** migration (v4 → v5) creating a `clip_events`
table that stores raw, parsed `event.json` metadata. This is distinct
from `detected_events` (SEI-derived) by design: `clip_events` is
externally-authored ground truth Tesla wrote, not a derivation, so it
is **not** cleared or rebuilt by the trip materializer's
`rebuild_all`.

```sql
-- v4 -> v5: raw Tesla event.json events (Saved/Sentry).
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
```

Key design points:

- **`event_json_relative_path` is the unique key**, so re-indexing the
  same event is an idempotent upsert.
- **`primary_clip_id` is a nullable FK with `ON DELETE SET NULL`** so
  deleting a clip never orphans nor cascades away the event row; the
  event survives with a null clip reference and can be re-linked when
  the directory is re-scanned.
- **`est_lat`/`est_lon` are nullable.** The parser coerces Tesla's
  string-or-number coordinates to finite `f64` and stores `NULL` when
  the value is absent, malformed, non-finite, or the `0,0` null
  island — mirroring the Python `_parse_latlon` contract so a missing
  pin still leaves a listable event.
- **`timestamp_utc` is authoritative** for both the map marker time
  and playback seek. It is parsed as UTC when Tesla omits an offset,
  and converted from an explicit `Z`/`±HH:MM`/`±HHMM`/`±HH` offset
  when present (see `clip_event::parse_timestamp_utc`).
- **Population** happens in both the bootstrap recursive index and the
  completed-file watcher (ADR-0019's front-only, debounced pipeline,
  extended by the p2a recursion increment). `rebuild_all` does **not**
  clear `clip_events`.
- **Primary-clip linking** matches the event directory against
  `clips.relative_path` with an escaped `LIKE … ESCAPE '\'` prefix.
  Escaping is required because Tesla event directory names embed `_`
  (e.g. `2026-06-01_20-11-00`), which is a single-character wildcard
  in SQL `LIKE` and would otherwise mis-link to a sibling directory.

`CURRENT_SCHEMA_VERSION` is bumped 4 → 5; the migration list is
append-only per the schema module's invariant.

## Alternatives considered

### A. Reuse `detected_events` for `event.json` events

Store honk/Sentry events as rows in the existing `detected_events`
table with a synthetic `event_type`.

**Rejected.** `detected_events` is owned by the SEI derivation and is
deleted/rebuilt per trip on every clip insert (ADR-0019). Raw Tesla
metadata would be wiped on the next `refresh_trip`. Mixing
externally-authored facts with derived data also violates the single-
responsibility boundary that ADR-0019 established.

### B. Derive events at query time in Python from `event.json`

Have the Flask layer read `event.json` files on each map request.

**Rejected — it re-introduces exactly the per-request filesystem cost
ADR-0019 removed.** The whole point of the materialised model is that
the read path is pure-DB and O(rows returned). Event metadata belongs
in the worker DB next to the clips it describes.

### C. Parse the offset with `chrono`

Pull in `chrono`/`time` to parse the ISO-8601 timestamp.

**Rejected for now.** The worker deliberately keeps its dependency
surface small (ADR-0010 / cross-compile-only ADR-0008). The existing
hand-rolled civil-date arithmetic already covers the no-offset/`Z`
cases; extending it to fixed numeric offsets is a few lines with unit
tests and avoids a new dependency + ADR churn. Revisit if richer
calendar handling (named zones, DST) is ever required — Tesla does not
emit those.

## Consequences

### Positive

- Saved/Sentry events (honk, sentry triggers) surface as map pins, and
  event-only days become reachable as the latest landing date.
- The read path stays pure-DB and bounded; no per-request filesystem
  scan.
- Deleting a clip is safe (`ON DELETE SET NULL`); the event row
  persists and can be re-linked.

### Negative

- One more table to keep populated in both the bootstrap and watcher
  paths. Mitigation: a single `record_clip_event` upsert keyed on the
  unique `event_json_relative_path`, plus a re-link pass per directory.
- The worker now parses a second on-disk JSON format (`event.json`) in
  addition to SEI. Mitigation: parsing is isolated in
  `clip_event.rs` with unit tests for string/number coords, the null
  island, malformed coords, missing timestamp, and `Z`/offset/no-offset
  timestamps.

### Migration

Additive `CREATE TABLE`/`CREATE INDEX` only; existing rows untouched.
The table is initially empty and fills as the bootstrap index and
watcher process event directories. Rollback: reverting the worker
binary leaves the table inert (no reader fails because the web read
path treats a missing/empty `clip_events` as "no events").

## References

- ADR-0017 — mapping single source of truth.
- ADR-0019 — materialise trips and `detected_events` (the derived
  tables `clip_events` is intentionally separate from).
- ADR-0010 — rusqlite for the indexer store.
- Schema: `rust/crates/teslausb-worker/src/store/schema.rs`
  (migration v4 → v5, `CURRENT_SCHEMA_VERSION = 5`).
- Parser: `rust/crates/teslausb-worker/src/clip_event.rs`.
- Linking: `rust/crates/teslausb-worker/src/store/clip_events.rs`.
