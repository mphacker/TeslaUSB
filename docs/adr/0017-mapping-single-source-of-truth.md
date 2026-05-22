# ADR-0017 — Mapping uses the worker DB as the single source of truth

- **Status**: Accepted
- **Date**: 2026-05-22
- **Branch**: `b1-userspace-rust`
- **Driver**: Operator (binding): *"I want it simple with no
  duplication of data or code. So rethink this again."* — and:
  *"don't take shortcuts. remember that. and don't forget to
  do code reviews and update documents."*

## Context

B-1 historically shipped TWO indexers that both walked the same
MP4 files and both parsed the same SEI NAL units:

1. The **Rust worker** (`teslausb-worker`) — uses `walk_clip()` in
   `crates/teslausb-core/src/sei/` (the vendored `tesla-dashcam-mp4`
   path) and persists results to `/var/lib/teslausb/index.sqlite3`.
   Schema: `clips` + `waypoints`.

2. The **Python mapping subsystem** (`web/teslausb_web/services/mapping/`)
   — a parallel implementation introduced in Phase 5.13 that
   maintains its own SQLite DB at `/var/lib/teslausb/mapping.db` with
   tables `trips`, `waypoints`, `detected_events`, `indexed_files`,
   `indexing_queue`. It re-parses every MP4 via its own SEI reader
   (`services/mapping/sei.py`), runs its own trip-detection pass
   (`indexer.py`), its own event-detection pass (`events.py`), its
   own sentry classifier (`sentry.py`), its own dedupe
   (`dedupe.py`), and writes the results to `mapping.db`.

### Why two indexers existed

When Phase 5.13 landed the map page, the worker's schema was
**too lean** to support the read paths the UI needed — it only
stored `(latitude, longitude, speed, heading)` and lacked
`acceleration_x/y/z`, `gear`, `steering_angle`, `brake_applied`,
`blinker_*`, and `autopilot_state`. Rather than extend the worker
schema, the team built a second parser+DB in Python.

### Why this is wrong

1. **Code duplication** — two SEI parsers maintained in lock-step.
   `walk_clip` (Rust) and `mapping/sei.py` (Python) must agree on
   byte-for-byte parsing of an opaque vendor format. Drift is a
   silent data-quality bug.
2. **Data duplication** — every clip's GPS trail lives twice on
   disk. Storage waste is small; cognitive overhead is large.
3. **The Python queue was never drained.** Phase 5.13 shipped
   `indexing_queue` schema + `boot_catchup_scan` enqueue, but
   **no consumer was ever implemented**. The 2026-05-22 operator
   report ("map says no mapped drives yet" after a 90-minute drive)
   was the symptom: 447 files sat enqueued, `mapping.db` was last
   written 2026-05-21 12:01, the map showed nothing — while the
   worker DB had 10,020 waypoints across 550 clips for the same
   drive window, all correctly parsed.
4. **Operator-visible failure mode** — the system appeared broken
   even though every video was correctly indexed, just in the
   wrong DB.

## Decision

The Rust worker DB (`/var/lib/teslausb/index.sqlite3`) becomes the
**single source of truth** for all clip indexing and GPS/telemetry
storage. The Python mapping subsystem is reduced to a **pure read
layer** that derives trips, events, and sentry classifications **on
the fly via SQL** over the worker DB.

Concretely:

- The worker DB schema is extended (`schema_version = 2`) to
  store every field the worker already parses from SEI:
  `acceleration_x/y/z`, `gear`, `steering_angle`, `brake_applied`,
  `blinker_on_left/right`, `autopilot_state`. See `M.1-M.4`
  commit `cc9d121` and the schema source in
  `rust/crates/teslausb-worker/src/store/schema.rs`.
- `mapping.db` is deleted, both the file and the schema.
- The 12 Python files in `web/teslausb_web/services/mapping/`
  (`sei.py`, `indexer.py`, `events.py`, `trips.py`, `sentry.py`,
  `dedupe.py`, `stale_scan.py`, `purge.py`, `discovery.py`,
  `diagnose.py`, `retry.py`, `kv.py`) plus
  `services/mapping_migrations.py` are deleted.
- `services/mapping_queries.py` is rewritten as a read-only
  service over `index.sqlite3`. Public dataclass signatures
  (`TripRow`, `RouteWaypoint`, `EventRow`, `DayRow`, etc.) and
  the `MappingQueries` class surface are preserved so
  `blueprints/mapping.py` keeps working unchanged at the route
  level.
- `blueprints/mapping.py` loses its queue endpoints
  (`/api/index/{trigger,rebuild,cancel,status,diagnose}`); the
  worker indexes live and continuously, no manual triggering is
  meaningful.
- Trips are derived by **SQL CTE** grouping `clips` rows on a
  configurable `clip_started_utc` gap (default **5 minutes**).
  Trip waypoints are the concatenated `waypoints` rows ordered by
  `(clip_started_utc, frame_index)`. Distance is the running
  haversine sum over consecutive waypoints, computed in SQL.
- Events are derived from waypoint deltas at query time:
  - speed-limit: `speed_mps > 35.76` (80 mph)
  - hard-accel: `acceleration_x > 3.5`
  - harsh-brake: `acceleration_x < -4.0`
  - emergency-brake: `acceleration_x < -7.0`
  - sharp-turn: `abs(acceleration_y) > 4.0`
  - autopilot-transitions: `autopilot_state` changes between
    consecutive waypoints in the same trip
- Sentry events are derived from clip presence: rows in `clips`
  with `bucket = 'sentry'` and zero `gps_waypoint_count`. No
  separate `sentry.py` classifier.

## Alternatives considered

### A. Implement the missing Python queue drainer

**Rejected.** This was the smallest possible fix for the
operator-visible symptom (map shows nothing) but it cements the
dual-parser architecture forever and explicitly violates the
operator's binding directive against duplication. The Rust
parser is already correct; running a Python re-parser in
parallel is wasted CPU on a Pi Zero 2 W and a permanent
maintenance liability.

### B. Materialise trips/events as worker tables

Have the worker run trip-detection and event-detection itself
and persist `trips`/`detected_events` tables alongside `clips`
and `waypoints`. The web layer would then be pure SELECT.

**Rejected for v1 of this rewrite.** The trip-gap threshold,
event-detection thresholds, and sentry classification rules are
likely to evolve as the operator uses the map. Computing them
at query time keeps tuning fast (no migration, no
re-classification batch). We may reconsider if query latency
becomes user-visible (>200 ms on the map page) — at which point
a materialised view or a worker-maintained denormalised cache
becomes justified. Until then: derive.

### C. Two separate worker DBs (clips + trips)

**Rejected.** Adds a join across DBs for no compensating
benefit. SQLite handles a few thousand clips and ~100K
waypoints in one DB without measurable cost.

### D. Keep `mapping.db`, point it at worker DB via ATTACH

A SQLite ATTACH would let the Python layer cross-query without a
parallel parser. **Rejected** because it leaves the dead
parsing/queue code in the tree, perpetuating the documentation
and review burden of a dual architecture that no longer has a
purpose.

## Consequences

### Positive

- One parser. One DB. One source of truth for "did this clip
  have GPS, and where was it." Drift impossible.
- Web map page works whenever the worker is healthy — no
  separate background process to be missing or wedged.
- 12 Python files + their tests deleted from the maintenance
  surface (estimated -3,500 LOC).
- Worker SEI capture is now lossless w.r.t. what the SeiMessage
  struct contains: acceleration / gear / steering / blinkers /
  AP state all flow to the DB. Future analytics work has the
  fields it needs without further schema changes.
- The UNIQUE-PK rollback bug (composite `(clip_id, frame_index)`
  PK rolled back the whole clip when Tesla emitted ≥ 2 SEI NALs
  between slices) is fixed as a side effect of the schema
  rewrite — synthetic `id` PK lets duplicates persist.

### Negative

- Initial map load runs the trip-derivation SQL on every page
  view. Mitigated by the worker DB's indexes on
  `clips(bucket, clip_started_utc)` and `waypoints(clip_id, frame_index)`
  plus per-day route caching (preserved from v1).
- Sentry classification is now structural ("bucket=sentry +
  no GPS") instead of semantic (the old `_infer_sentry_event`
  had hand-tuned heuristics). For Tesla SentryClips this is
  equivalent — Tesla doesn't write SEI in sentry events because
  the car is parked.
- Tesla writes SEI **intermittently** — some clips have 0 SEI
  NAL units, some have thousands. Operator may see clips on
  disk that don't appear on the map. This is correct (Tesla
  didn't emit GPS for that recording) and is the same behaviour
  the prior architecture had; making it visible in the
  documentation prevents future "where did my videos go" panic.

### Migration

- M.1-M.4 (committed `cc9d121`): schema v2 + telemetry expansion
  + UNIQUE-PK fix + record_clip 16-column INSERT + 3 new tests.
- M.5: rewrite `mapping_queries.py` against worker DB.
- M.6: trim `blueprints/mapping.py` (delete queue endpoints).
- M.7: delete obsolete Python files.
- M.8: trim `config.py` mapping section.
- M.9: rewrite the 5 mapping test files against a synthetic
  worker DB fixture.
- M.10 (this ADR).
- M.11: update `docs/00-PLAN.md` + `docs/01-PROGRESS.md`.
- M.12: hardware deploy (re-arm dead-man, scp binary, restart
  worker, verify migration, restart web, hit `/mapping`).
- M.13: `sudo rm /var/lib/teslausb/mapping.db`.
- M.14: charter-review skill on the full Phase M diff.
- M.15: per-increment commits to `b1-userspace-rust`.

## References

- Operator directive on duplication: 2026-05-22 session,
  *"I want it simple with no duplication of data or code."*
- Operator directive on rigour: 2026-05-22 session,
  *"don't take shortcuts. remember that. and don't forget to
  do code reviews and update documents."*
- Charter pillars 1 (no smells), 2 (best architecture), 5 (no
  dead code) — `docs/03-CODE-QUALITY-CHARTER.md`.
- Schema v2 migration: `rust/crates/teslausb-worker/src/store/schema.rs`
  (commit `cc9d121`).
- SEI walker (the canonical parser):
  `rust/crates/teslausb-worker/src/sei.rs` + vendored
  `tesla-dashcam-mp4` (ADR-0016).
- Diagnostic proof the walker is lossless on every SEI Tesla
  writes (742/742, 261/261, 4/4 NALs decoded across sampled
  drive-window clips): session notes 2026-05-22.
