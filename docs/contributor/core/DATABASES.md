# Databases

TeslaUSB has two SQLite databases on the SD card. This doc gives
the high-level structure of each, lists the tables and their roles,
and points you at the schema definitions and migrations.

For column-by-column documentation, see
`reference/DB_SCHEMAS.md` *(planned, Wave 10)*. For the rules
around the protected "trips are sacred" contract, see
`subsystems/MAPPING_AND_TRIPS.md` *(planned, Wave 3)*.

---

## At a glance

| Database         | Owns                                                                          |
|------------------|-------------------------------------------------------------------------------|
| `geodata.db`     | Trips, waypoints, detected events, indexed file records, the indexing queue   |
| `cloud_sync.db`  | Cloud-upload state, the archive queue, the LES queue, sync sessions           |

Both files live next to the repo at runtime (the gadget_web service
runs from the repo directory, so `geodata.db` and `cloud_sync.db`
sit alongside `config.yaml`). They are **gitignored**; under no
circumstance should they be committed.

Both use SQLite's WAL mode for concurrent reader / single-writer
access from the Flask app's worker threads.

Both are **migrated automatically** on `gadget_web` startup. The
schema versions are tracked in `mapping_migrations.py` (which
defines schemas for **both** databases despite the name — see
"Module split" below).

---

## `geodata.db`

Holds everything the map page needs.

### Tables

| Table              | Purpose                                                                 |
|--------------------|-------------------------------------------------------------------------|
| `trips`            | One row per drive, defined as a continuous waypoint sequence            |
| `waypoints`        | One row per indexed video frame: lat, lon, telemetry                    |
| `detected_events`  | Events detected from waypoints (harsh brake, FSD disengage, etc.)       |
| `indexed_files`    | One row per video file the indexer has processed                        |
| `indexing_queue`   | Files waiting to be indexed; consumed by the indexing worker            |

### `trips`

A trip is a contiguous drive — waypoints separated by less than
`mapping.trip_gap_minutes` minutes (default 5) belong to the same
trip. Trips are **sacred**: only an explicit user "Delete Trip"
action may remove them. Cleanup paths that find an orphaned
`indexed_files` row only NULL the `video_path` references on
related waypoints and events; they never touch the `trips` row.

Approximate columns (see `mapping_migrations.py::_SCHEMA_SQL` for
exact definitions): `id, start_time, end_time, start_lat,
start_lon, end_lat, end_lon, distance_km, duration_seconds,
source_folder, indexed_at`.

### `waypoints`

One row per indexed video frame. Carries the GPS point plus the
full telemetry payload extracted from the H.264 SEI: speed,
heading, accelerations, gear, autopilot state, steering, brake
status, blinker positions. The `video_path` and `frame_offset`
columns let the UI seek back to the source clip when the user
clicks a point on the map.

Approximate columns: `id, trip_id, timestamp, lat, lon, heading,
speed_mps, acceleration_x, acceleration_y, acceleration_z, gear,
autopilot_state, steering_angle, brake_applied, blinker_on_left,
blinker_on_right, video_path, frame_offset`.

### `detected_events`

Events triggered by waypoint analysis: harsh brake, emergency
brake, hard accel, sharp turn, speeding, FSD engage, FSD disengage.
Thresholds are configurable under `mapping.event_detection` in
`config.yaml`.

Approximate columns: `id, trip_id, timestamp, lat, lon, event_type,
severity, description, video_path, frame_offset, metadata`.

### `indexed_files`

Booking record for which video files have been processed. Keyed
by `file_path` (the canonical path on disk). Used to dedupe and to
drive the daily stale-scan that orphans rows whose underlying file
has been deleted.

Approximate columns: `file_path, file_size, file_mtime, indexed_at,
waypoint_count, event_count`.

### `indexing_queue`

Inputs to the indexing worker. **Single producer / single consumer
pattern** — multiple producers (file watcher, archive worker, boot
catch-up scan, manual trigger) call `enqueue_for_indexing()` /
`enqueue_many_for_indexing()`; the indexing worker thread is the
only consumer.

Approximate columns: `canonical_key, file_path, priority,
enqueued_at, next_attempt_at, attempts, last_error,
previous_last_error, claimed_by, claimed_at, source`.

`canonical_key` is `mapping_service.canonical_key(path)`, which
maps both the SD-card view and the USB-RO view of the same file
to the same string so duplicate enqueues from different producers
no-op.

`status` is implicit in the column shape: `claimed_by IS NOT NULL`
means a worker has the row; otherwise it's `pending`. Outcomes
("indexed", "deferred", "errored") are terminal — successful and
permanent-fail rows are deleted, retry rows have `attempts`
incremented and `next_attempt_at` set.

---

## `cloud_sync.db`

Holds everything related to cloud uploads and to the archive copy
queue. Despite "cloud" in the name, the **archive queue** lives
here too — both subsystems share the DB so a single backup file
captures the entire transient queue state.

### Tables

| Table                | Purpose                                                              |
|----------------------|----------------------------------------------------------------------|
| `archive_queue`      | Files queued for copy from RO USB mount → `~/ArchivedClips/`          |
| `cloud_synced_files` | Per-file cloud-upload state                                           |
| `cloud_sync_sessions`| Audit log of sync runs                                                |
| `live_event_queue`   | Per-event LES upload queue (only used when LES is enabled)            |

### `archive_queue`

The archive worker drains this. Producers: archive_producer (boot
catch-up + watcher-driven), and the `archive_producer.enqueue_*`
helpers called manually from triggered re-scans.

Approximate columns: `id, source_path, dest_path, priority, status,
attempts, last_error, previous_last_error, enqueued_at, claimed_at,
claimed_by, copied_at, expected_size, expected_mtime`.

`status` is an explicit enum:
`pending`, `claimed`, `copied`, `source_gone`, `skipped_stationary`,
`error`, `dead_letter`. Terminal: `copied`, `source_gone`,
`skipped_stationary`, `dead_letter`. Retry: `error` (with backoff).

`priority`:

| Constant            | Value | Folders matched                        |
|---------------------|-------|----------------------------------------|
| `PRIORITY_EVENTS`   | 1     | `SentryClips/`, `SavedClips/`          |
| `PRIORITY_RECENT_CLIPS` | 2 | `RecentClips/`                          |
| `PRIORITY_OTHER`    | 3     | Anything else (boot/, root, …)         |

(Constants in `archive_queue.py`. Lower number = higher priority.
After PR #178 / #179 the priority order is "events first, then
RecentClips, then other"; this overrode an earlier ordering.)

### `cloud_synced_files`

Per-file cloud upload state. Status enum: `pending`, `uploading`,
`synced`, `failed`, `dead_letter`.

A row is marked `synced` only **after rclone confirms upload AND
the DB commit completes AND fsync returns**. Partials detected at
restart (status `uploading` with no recent rclone process) are
reset to `pending`.

Approximate columns: `id, file_path, file_size, file_mtime,
remote_path, status, synced_at, retry_count, last_error,
previous_last_error`.

### `cloud_sync_sessions`

Audit-log table tracking each sync session: when it started, what
triggered it, how many files moved, total bytes, errors. Used by
the cloud sync UI to show "last sync" history.

### `live_event_queue`

LES-only. Populated by the file watcher's `event.json` callback
firing `live_event_sync_service.enqueue_event_json()`.

Approximate columns: `id, event_dir, event_json_path,
event_timestamp, event_reason, upload_scope, status, enqueued_at,
uploaded_at, next_retry_at, attempts, last_error,
previous_last_error, bytes_uploaded`. **Unique on `event_dir`** —
duplicate enqueues of the same event are no-ops.

`status` enum: `pending`, `uploading`, `uploaded`, `failed`.
`upload_scope` is one of `event_minute` (default — event.json plus
the 6 cameras matching the timestamp's `YYYY-MM-DD_HH-MM` prefix)
or `event_folder` (every `.mp4` in the event dir).

Retry backoff is the constant
`LIVE_EVENT_RETRY_BACKOFF_SECONDS = [30, 120, 300, 900, 3600]` —
five attempts, growing from 30 seconds to 1 hour. After the last
attempt, the row moves to `failed` and stays there until a manual
retry via `/api/live_events/retry/<id>`.

---

## Module split

The schema and queue API are intentionally split across modules:

| Module                                                | Owns                                                                 |
|-------------------------------------------------------|----------------------------------------------------------------------|
| `services/mapping_migrations.py`                      | DDL for **both** databases, schema version constants, migrations      |
| `services/indexing_queue_service.py`                  | Indexing-queue API (enqueue / claim / complete / defer / release)     |
| `services/mapping_service.py`                         | Indexing core (`index_single_file`, `IndexResult`, trip merge, …)     |
| `services/mapping_queries.py`                         | Read-only query helpers for the map UI (route polylines, RDP, …)     |
| `services/archive_queue.py`                           | Archive-queue API + status enum                                       |
| `services/cloud_archive_service.py`                   | Cloud upload state mutations                                          |
| `services/live_event_sync_service.py`                 | LES queue mutations                                                   |

Backward compatibility: `mapping_service` re-exports many of the
schema/migration symbols so existing imports keep working. **New
code should import from the dedicated module** (`indexing_queue_service`,
`mapping_migrations`, `mapping_queries`) directly — the re-exports
exist only to avoid breaking older callers.

---

## Migrations

Schema versioning lives in `mapping_migrations.py`:

```python
_SCHEMA_VERSION = N        # current target version
_BACKUP_RETENTION = M      # number of backups to keep
_SCHEMA_SQL = "..."        # canonical CREATE TABLE / CREATE INDEX
```

On each `gadget_web` startup:

1. Open both DBs in WAL mode.
2. Read `PRAGMA user_version`.
3. If less than `_SCHEMA_VERSION`, run the version-specific
   migration helpers in order (`_migrate_to_v2`, `_migrate_to_v3`, …).
4. Each migration helper is **idempotent** — safe to re-run after a
   crashed migration.
5. Take a backup snapshot before each migration; rotate per
   `_BACKUP_RETENTION`.

When you change the schema:

1. Bump `_SCHEMA_VERSION`.
2. Append the new DDL to `_SCHEMA_SQL`.
3. Add a `_migrate_to_vN` helper that performs the upgrade against
   an older live DB.
4. Add a test that builds a vN-1 DB, runs the migration, and
   asserts the expected vN shape.
5. Document the change in
   `reference/DB_SCHEMAS.md` *(planned, Wave 10)*.

**Never delete a column without a deprecation cycle**. Always
write the migration to leave old columns in place; remove them in
a later release once you're sure no rollback is needed.

---

## Decision points

| Question                                                  | Where decided                                            |
|-----------------------------------------------------------|----------------------------------------------------------|
| Should this enqueue be skipped as a duplicate?            | `mapping_service.canonical_key()` + UNIQUE index         |
| Should this row become `dead_letter`?                     | `attempts >= retry_max_attempts` (per-subsystem config)  |
| Should retention delete this orphaned `indexed_files`?    | `mapping_service.purge_deleted_videos()` (yes, but **never** the trip row) |
| Should the migration take a backup first?                 | Always — every migration call snapshots the live DB      |

---

## Failure modes & recovery

### Database file missing on startup

`gadget_web` recreates it from the canonical schema, then runs all
migrations. No error to the user. The new DB is empty — boot
catch-up scan repopulates `indexing_queue`, the archive worker
catches up, and the indexer fills `trips` / `waypoints` /
`detected_events` over the next several hours.

### Database file corrupt

SQLite returns `SQLITE_CORRUPT` on the first read. `gadget_web`
logs a critical error and refuses to start. Recovery:

1. Restore the most recent migration backup from `*.db.bak.*`.
2. Failing that, delete the corrupt file and let `gadget_web`
   recreate it. Trip history is lost but Tesla footage on the SD
   card is unaffected.

### Stale claims from a crashed worker

Boot recovery resets every claim with `claimed_at` older than the
stale threshold. `archive_queue.recover_stale_claims()` and
`indexing_queue_service.recover_stale_claims()` run automatically
at worker startup. No data loss.

### Mid-write power loss

WAL mode plus the atomic-write contract (temp file → fsync →
rename) means a partially written DB page is rolled back at next
open. Workers' transactions are short and committed before
acknowledging completion to producers.

---

## Source files

- `scripts/web/services/mapping_migrations.py` — schemas + migrations
- `scripts/web/services/indexing_queue_service.py` — indexing queue
- `scripts/web/services/mapping_service.py` — indexing core, trip merge,
  `purge_deleted_videos`
- `scripts/web/services/mapping_queries.py` — read-only query helpers
  for the map UI
- `scripts/web/services/archive_queue.py` — archive queue + status enum
- `scripts/web/services/cloud_archive_service.py` — cloud upload state
- `scripts/web/services/live_event_sync_service.py` — LES queue
