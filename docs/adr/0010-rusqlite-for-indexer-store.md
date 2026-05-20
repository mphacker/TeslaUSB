# ADR-0010 — `rusqlite` for the indexer waypoint store

| Status   | Accepted |
|----------|----------|
| Date     | 2026-05-20 |
| Deciders | mphacker (operator), Copilot CLI (charter enforcement) |
| Phase    | Phase 4b.2 (indexer) |
| Triggers | Charter §"ADR discipline" — new third-party dep + cross-module schema |

## Context

Phase 4b.2 introduces the indexer: an inotify-driven loop that
watches `RecentClips` / `SavedClips` / `SentryClips` for new
`*.mp4` files, runs each through the Phase 4b.1z SEI walker,
and persists the resulting `Waypoint`s to a database the cleanup
worker (4b.3) and the future web map (Phase 5+) will read.

We need a query model that supports:

* "Does clip `X` have ≥ 1 GPS-fix waypoint?" — cleanup needs
  this to decide whether to preserve a `RecentClips` file past
  its retention deadline.
* "All waypoints for clip `X` ordered by `frame_index`" — the
  map view replays the route.
* "All clips between `t0` and `t1` whose centroid is within
  `(lat, lon, radius_km)`" — future trips view.
* Single-writer (indexer) + multiple readers (cleanup, web)
  with no writer-blocks-reader contention.

## Options considered

### Option A — File-per-clip JSON sidecars

* **Pros:** No new dep. Atomic write via `rename`. Trivially
  cleanable (`rm` of the `.mp4` deletes its sidecar by
  convention).
* **Cons:** Every "has GPS?" query is N file opens. Range
  queries need a directory walk. Power-cut leaves orphan
  sidecars on every interrupted write. Web map sub-second
  latency on a 10 000-clip backlog is implausible.

### Option B — `redb` (pure-Rust embedded key-value store)

* **Pros:** No FFI, no `libsqlite3`. ACID-tx out of the box.
* **Cons:** Range queries on `(timestamp, lat, lon)` need a
  manual index. No `SELECT ... WHERE` ergonomics. The web layer
  will eventually want to issue `SELECT clip_id FROM clips
  WHERE bbox_intersects(...)` from Python — `redb` would force
  us to ship a custom IPC for every query shape.

### Option C — `sled` (pure-Rust LSM)

* Same query-language objection as redb, plus `sled` has
  known durability gaps that are open issues at this point —
  ruled out on charter §3 "no shortcuts".

### Option D — `rusqlite` (selected)

* **Pros:**
  * **Battle-tested.** SQLite is the most-deployed database
    engine on Earth. Power-cut durability story is documented
    end-to-end. WAL mode gives us single-writer + concurrent
    readers (the exact 4b architecture).
  * **Queries match the workload.** Range / bbox queries are
    natural SQL. The cleanup worker's "preserve if has GPS"
    check is one indexed `EXISTS` query.
  * **One file on disk.** Easy to back up; easy to migrate;
    easy to inspect with `sqlite3 *.db` on the Pi.
  * **Python ecosystem already uses it.** When the Phase 5 web
    UI lands it can open the same file read-only — no IPC
    needed for read-side queries.
  * **Stable Rust API.** rusqlite has been at 1.x since 2014;
    no churn risk on the schema layer.
* **Cons:**
  * Pulls `libsqlite3-sys` which links to either a system
    `libsqlite3` or its own bundled C source. We pick the
    `bundled` feature so the cross-build container does not
    need a target `libsqlite3-dev` and the produced binary
    has no extra apt-package install dependency on the Pi.
    Bundling adds ~600 KB to the stripped binary; acceptable
    on the 4-8 GB SD card budget.
  * Synchronous (blocking) API. The indexer's tokio runtime
    must wrap every DB call in `spawn_blocking`. Acceptable
    because indexing is already CPU-bound (SEI parse > DB
    write) and a single-writer setup means there is at most
    one blocking call in flight.
  * Adds a `cc` build-time dep (compiles the bundled SQLite
    C source). Cross-build container already has `gcc-arm-*`
    + libc headers, no new packages.

## Decision

Adopt **`rusqlite`** with the `bundled` feature for the
`teslausb-worker::store` module. WAL mode enabled at open
time. Schema versioned via a single `schema_version` row in a
`meta` table; migrations run inside one transaction on startup.

## Rust integration shape

* New crate dep on `teslausb-worker` only (`Cargo.toml`):
  ```toml
  rusqlite = { version = "0.31", features = ["bundled"] }
  ```
  `teslausb-core` does **not** get this dep — the schema layer
  lives in the worker per Layer-3 adapter rule.
* All DB access goes through `teslausb_worker::store::Store`,
  which wraps `rusqlite::Connection` behind a typed-error API
  (`StoreError`, thiserror). No `unwrap` on row reads.
* Every public `Store` method has an integration test using
  an in-memory SQLite (`Connection::open_in_memory()`).
* Schema (initial v1):
  ```sql
  CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  CREATE TABLE clips (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      relative_path TEXT NOT NULL UNIQUE,
      bucket TEXT NOT NULL,          -- 'recent' | 'saved' | 'sentry'
      clip_started_utc INTEGER,      -- unix seconds, NULL if mvhd missing
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
  ```

## Charter compliance

* Layering: `rusqlite` is a Layer-3 infrastructure dep, lives
  in `teslausb-worker` only, never imported by `teslausb-core`.
* No shortcuts: typed errors, WAL mode, single-writer discipline
  enforced by code review (only the indexer task may take the
  `Store::writer()` handle).
* No dead code: every column has a query that reads it.
* ADR documented (this file).

## Consequences

* Backup story: the operator can `sqlite3 teslausb.db .backup`
  to capture a consistent snapshot without stopping the
  indexer.
* If the file is corrupted (SD-card write hole) the indexer
  rebuilds from scratch by re-walking the backing tree. We
  treat the store as a cache, not a source of truth — the
  source of truth is always the `.mp4` files on disk.
* Adds the only `unsafe`-via-FFI surface in the workspace
  (`libsqlite3-sys`). We accept that — SQLite's C code has
  been audited far more thoroughly than any pure-Rust
  equivalent of its age.
