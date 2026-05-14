"""
Schema definition + migrations for the mapping/geo-index DB.

Phase 3c.2 (#100): extracted from ``mapping_service.py`` to keep the
schema DDL, version constants, backup helper, ``_init_db`` connection
factory, and the v2/v3/v4 migration functions in one place.
``mapping_service`` re-exports ``_init_db``, ``_backup_db`` and
``_SCHEMA_VERSION`` from this module so the (very many) existing
internal call sites and test imports continue to work unchanged.

Dependency direction (one-way, no cycle):
    mapping_migrations does NOT import from mapping_service at module
    load time. The runtime helpers shared with the live indexer
    (``_merge_all_adjacent_trip_pairs``, ``_haversine_km``) are
    lazy-imported inside the migration function bodies — those helpers
    live on ``mapping_service`` because they're hot-path dependencies
    of ``_index_video`` and we don't want them to flicker between
    modules every time the migrations module is loaded.

Power-loss safety:
    - ``_backup_db`` snapshots the SQLite file (via ``shutil.copy2``,
      which is atomic at the OS-write level for a fully-quiesced DB)
      before any destructive migration runs and prunes to
      ``_BACKUP_RETENTION`` copies.
    - Each migration runs inside a SAVEPOINT so a partial failure
      rolls back to the previous schema version; the caller commits
      and returns (skipping the version bump) so the migration retries
      on next startup.
    - ``_init_db`` configures WAL + ``synchronous=NORMAL`` +
      ``busy_timeout=15000`` + a 4 MB WAL size cap with an
      auto-checkpoint at 200 frames so the DB stays bounded under the
      Pi Zero 2 W's tight memory budget while still being durable.
"""

import logging
import os
import shutil
import sqlite3
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database Schema & Management
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 13
_BACKUP_RETENTION = 3  # Keep this many migration backups before pruning oldest

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    start_lat REAL,
    start_lon REAL,
    end_lat REAL,
    end_lon REAL,
    distance_km REAL DEFAULT 0.0,
    duration_seconds INTEGER DEFAULT 0,
    source_folder TEXT,
    indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS waypoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    heading REAL,
    speed_mps REAL,
    acceleration_x REAL,
    acceleration_y REAL,
    acceleration_z REAL,
    gear TEXT,
    autopilot_state TEXT,
    steering_angle REAL,
    brake_applied INTEGER DEFAULT 0,
    blinker_on_left INTEGER DEFAULT 0,
    blinker_on_right INTEGER DEFAULT 0,
    video_path TEXT,
    frame_offset INTEGER
);

CREATE TABLE IF NOT EXISTS detected_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    lat REAL,
    lon REAL,
    event_type TEXT NOT NULL,
    severity TEXT DEFAULT 'info',
    description TEXT,
    video_path TEXT,
    frame_offset INTEGER,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS indexed_files (
    file_path TEXT PRIMARY KEY,
    file_size INTEGER,
    file_mtime REAL,
    indexed_at TEXT,
    waypoint_count INTEGER DEFAULT 0,
    event_count INTEGER DEFAULT 0
);

-- Persistent indexing work queue. One row per pending clip, keyed by
-- canonical_key (RecentClips/ArchivedClips dedup, Saved/Sentry events
-- disambiguated by event folder — see canonical_key()). The single
-- worker thread (services.indexing_worker) drains this; producers
-- (file watcher, archive job, manual button, boot catch-up) just
-- INSERT rows. claimed_by/claimed_at let the worker take an exclusive
-- claim atomically; stale claims (>30 min) are auto-released so a
-- crashed worker can't permanently lock a row.
CREATE TABLE IF NOT EXISTS indexing_queue (
    canonical_key TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    enqueued_at REAL NOT NULL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    previous_last_error TEXT,
    claimed_by TEXT,
    claimed_at REAL,
    source TEXT
);

CREATE INDEX IF NOT EXISTS idx_waypoints_trip ON waypoints(trip_id);
CREATE INDEX IF NOT EXISTS idx_waypoints_coords ON waypoints(lat, lon);
CREATE INDEX IF NOT EXISTS idx_waypoints_timestamp ON waypoints(timestamp);
CREATE INDEX IF NOT EXISTS idx_waypoints_video_path ON waypoints(video_path);
-- Covering index for query_trips' video_count subquery: lets SQLite count
-- DISTINCT video_path per trip without touching the main waypoints table.
-- Without this, /api/trips fans out to 1 + 2N queries (where N = page size)
-- and visibly stalls the map page on databases with thousands of waypoints.
CREATE INDEX IF NOT EXISTS idx_waypoints_trip_video
    ON waypoints(trip_id, video_path);
-- Day-based aggregate (/api/days) and per-day route lookup
-- (/api/day/<date>/routes) both filter by substr(start_time,1,10).
-- v7: ``idx_trips_start_time`` keeps a sortable-text scan available
-- for callers that filter by ``start_time >= ?`` (e.g. /api/trips with
-- date_from/date_to).
CREATE INDEX IF NOT EXISTS idx_trips_start_time ON trips(start_time);
CREATE INDEX IF NOT EXISTS idx_events_trip ON detected_events(trip_id);
CREATE INDEX IF NOT EXISTS idx_events_coords ON detected_events(lat, lon);
CREATE INDEX IF NOT EXISTS idx_events_type ON detected_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON detected_events(timestamp);
-- v8: expression indexes on substr(<ts>, 1, 10) for the day-based
-- queries (/api/days, /api/day/<date>/routes, /api/events?date=).
-- Plain idx_trips_start_time / idx_events_timestamp are NOT used by
-- SQLite when the WHERE clause wraps the column in substr() — verified
-- via EXPLAIN QUERY PLAN. Without these expression indexes the day
-- view degrades to a full table scan on every nav, which is unusable
-- on a Pi Zero 2 W with a few thousand trips.
CREATE INDEX IF NOT EXISTS idx_trips_day
    ON trips(substr(start_time, 1, 10));
CREATE INDEX IF NOT EXISTS idx_events_day
    ON detected_events(substr(timestamp, 1, 10));
-- Worker pick-next index: partial index over only unclaimed, ready-to-run
-- rows. Keeps the atomic-claim subquery O(log n) regardless of queue depth.
CREATE INDEX IF NOT EXISTS idx_queue_ready
    ON indexing_queue(priority, enqueued_at)
    WHERE claimed_by IS NULL;
-- Stale-claim recovery scan: lets the worker quickly find rows whose
-- claim has aged out (>30 min, indicating the previous worker crashed).
CREATE INDEX IF NOT EXISTS idx_queue_claimed_at
    ON indexing_queue(claimed_at)
    WHERE claimed_by IS NOT NULL;

-- v10: archive_queue. Producer-only in Phase 2a (issue #76); the worker
-- that drains it lands in Phase 2b. Rows accumulate harmlessly until
-- then. Keyed by ``source_path`` (UNIQUE) so the inotify producer, the
-- 60-s rescan producer, and the boot catch-up scan can all use
-- ``INSERT OR IGNORE`` for cheap idempotent enqueue. ``priority``
-- follows the issue spec (post-#178 mapping):
-- 1=SentryClips/SavedClips event clips (highest-value footage —
--   something physically happened to the car),
-- 2=RecentClips (driving / dashcam footage; SEI-peek skip-stationary
--   handles parked-no-event clips at copy time so they don't compete),
-- 3=anything else (e.g. ArchivedClips back-fill).
-- ``status`` transitions through pending → claimed → copied (terminal)
-- or → source_gone / error / dead_letter (terminal). ``expected_size``
-- and ``expected_mtime`` are captured at enqueue time so the Phase
-- 2b worker can detect "Tesla still writing" by re-stat-ing before
-- the copy.
CREATE TABLE IF NOT EXISTS archive_queue (
    id INTEGER PRIMARY KEY,
    source_path TEXT UNIQUE NOT NULL,
    dest_path TEXT,
    priority INTEGER DEFAULT 3,
    status TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    last_error TEXT,
    previous_last_error TEXT,
    enqueued_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by TEXT,
    copied_at TEXT,
    expected_size INTEGER,
    expected_mtime REAL
);
-- Worker pick-next index: partial over only ready rows, ordered by
-- priority then mtime (closest-to-TTL first within each priority band).
-- The worker's pick query is ``SELECT ... WHERE status='pending' ORDER
-- BY priority ASC, expected_mtime ASC LIMIT 1``; this index makes it
-- O(log n) regardless of queue depth.
CREATE INDEX IF NOT EXISTS archive_queue_ready
    ON archive_queue(status, priority, expected_mtime)
    WHERE status = 'pending';
-- Files-lost banner index (Phase 4.3 / v11): the Settings card polls
-- ``count_source_gone_recent`` every 15 s, which scans rows with
-- ``status='source_gone'`` filtered by ``claimed_at`` recency. The
-- ``archive_queue`` table grows monotonically (no retention today),
-- so without a partial index this becomes a full table scan once
-- ``source_gone_count`` reaches the thousands (which it already has
-- on production devices — 2 387 rows seen in the wild). The partial
-- index keeps the lookup O(log n) and is tiny because only the
-- ``source_gone`` rows are present.
CREATE INDEX IF NOT EXISTS archive_queue_source_gone_claimed
    ON archive_queue(claimed_at)
    WHERE status = 'source_gone';
"""


def _backup_db(db_path: str, target_version: int) -> Optional[str]:
    """Make a copy of the DB before a destructive migration.

    Returns the backup path on success, None on failure (migration still proceeds).
    Old backups beyond ``_BACKUP_RETENTION`` are pruned.
    """
    if not os.path.isfile(db_path):
        return None
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = f"{db_path}.bak.v{target_version}.{ts}"
        shutil.copy2(db_path, backup_path)
        logger.info("Backed up geo-index DB to %s", backup_path)

        # Prune older backups
        backups = sorted(
            f for f in os.listdir(os.path.dirname(db_path) or '.')
            if f.startswith(os.path.basename(db_path) + '.bak.')
        )
        if len(backups) > _BACKUP_RETENTION:
            for old in backups[:-_BACKUP_RETENTION]:
                try:
                    os.remove(os.path.join(os.path.dirname(db_path), old))
                except OSError:
                    pass
        return backup_path
    except Exception as e:
        logger.warning("Failed to back up DB before migration: %s", e)
        return None


def _init_db(db_path: str) -> sqlite3.Connection:
    """Initialize the SQLite database with schema if needed."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    # Tuned for Pi Zero 2 W (512 MB RAM) where mmap exhaustion under
    # concurrent indexer + web load was producing spurious "disk I/O error"
    # responses from SQLite. The combination of a small per-connection page
    # cache, no file mmap, capped WAL size, and frequent autocheckpoint
    # keeps each connection's memory footprint bounded so we never run out
    # of address space when many waitress threads open connections in
    # parallel during a heavy indexer run.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA cache_size=-2000")        # 2 MB per connection
    conn.execute("PRAGMA mmap_size=0")             # disable file-mmap (saves vmem)
    conn.execute("PRAGMA temp_store=MEMORY")       # avoid temp files on slow SD
    conn.execute("PRAGMA journal_size_limit=4194304")   # cap WAL at 4 MB
    conn.execute("PRAGMA wal_autocheckpoint=200")  # checkpoint every ~800 KB
    conn.execute("PRAGMA foreign_keys=ON")

    # Check schema version. Older code used INSERT OR REPLACE on a PRIMARY KEY
    # column, which actually added a new row each time, so older DBs may have
    # multiple rows. Use MAX() to read the effective version.
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row['v'] if row and row['v'] is not None else 0
    except sqlite3.OperationalError:
        current = 0

    if current < _SCHEMA_VERSION:
        # Backup before any destructive migration (only when an existing DB
        # is being upgraded, not on first install)
        if current > 0:
            _backup_db(db_path, _SCHEMA_VERSION)

        conn.executescript(_SCHEMA_SQL)
        # Migrations for existing databases
        if current < 2:
            # v2: add blinker columns to waypoints
            for col in ('blinker_on_left', 'blinker_on_right'):
                try:
                    conn.execute(f"ALTER TABLE waypoints ADD COLUMN {col} INTEGER DEFAULT 0")
                except sqlite3.OperationalError:
                    pass  # Column already exists
        if current > 0 and current < 3:
            # v3: clean up duplicate trips/waypoints from earlier indexer bugs.
            # Wrapped in a savepoint so a failure during migration doesn't leave
            # the schema_version bumped without the data fixes applied.
            try:
                conn.execute("SAVEPOINT migrate_v3")
                _migrate_v2_to_v3(conn)
                conn.execute("RELEASE SAVEPOINT migrate_v3")
            except Exception as e:
                conn.execute("ROLLBACK TO SAVEPOINT migrate_v3")
                conn.execute("RELEASE SAVEPOINT migrate_v3")
                logger.error("Migration v2->v3 failed, leaving schema at v2: %s", e)
                conn.commit()
                return conn  # Skip schema_version bump so it retries next startup
        if current > 0 and current < 4:
            # v4: re-evaluate Sentry/Saved clips with Tesla's event.json
            # (which has accurate GPS) instead of the prior nearest-waypoint
            # guess. We do this by clearing their indexed_files rows so the
            # next indexer run re-processes them through the new code path.
            try:
                conn.execute("SAVEPOINT migrate_v4")
                _migrate_v3_to_v4(conn)
                conn.execute("RELEASE SAVEPOINT migrate_v4")
            except Exception as e:
                conn.execute("ROLLBACK TO SAVEPOINT migrate_v4")
                conn.execute("RELEASE SAVEPOINT migrate_v4")
                logger.error("Migration v3->v4 failed, leaving schema at v3: %s", e)
                conn.commit()
                return conn
        if current > 0 and current < 9:
            # v9: one-shot repair pass for trips that were fragmented
            # by the matching-SQL boundary bug fixed in this version.
            # The bug: ``ORDER BY ABS(new_start - existing.start)`` plus
            # the float-imprecise ``(julianday(...)-julianday(...))*86400``
            # condition caused phantom-fragmented trips when files
            # arrived out-of-order during indexer pauses (e.g., archive-
            # lock starvation incident May 2026 — McDonald's drive split
            # into 6 trips). The runtime ``_merge_adjacent_trips_for``
            # added in this version prevents future fragmentation, but
            # only sweeps the just-touched anchor's neighbourhood, so
            # bad data already in the table will linger unless a future
            # clip happens to bridge it. This one-shot global merge
            # repairs the existing damage.
            try:
                # Phase 3c.2 (#100): the merge helper stays in
                # ``mapping_service`` because it's a hot-path runtime
                # dependency of ``_index_video``. Lazy import avoids a
                # circular dependency at module load.
                from services.mapping_service import (
                    _merge_all_adjacent_trip_pairs,
                    _TRIP_GAP_MINUTES_DEFAULT,
                )
                conn.execute("SAVEPOINT migrate_v9")
                merged = _merge_all_adjacent_trip_pairs(
                    conn, _TRIP_GAP_MINUTES_DEFAULT * 60,
                )
                conn.execute("RELEASE SAVEPOINT migrate_v9")
                if merged:
                    logger.info(
                        "Migration v8->v9: merged %d phantom-fragmented "
                        "trip pairs", merged,
                    )
            except Exception as e:
                conn.execute("ROLLBACK TO SAVEPOINT migrate_v9")
                conn.execute("RELEASE SAVEPOINT migrate_v9")
                logger.error(
                    "Migration v8->v9 failed, leaving schema at v8: %s", e,
                )
                conn.commit()
                return conn
        # v5: covering index ``idx_waypoints_trip_video`` for the
        # ``/api/trips`` page-load N+1 fix. The index is created by the
        # ``executescript(_SCHEMA_SQL)`` call above (CREATE INDEX IF NOT
        # EXISTS), so no separate data migration is needed — the schema
        # version bump is the trigger.
        # v6: ``indexing_queue`` table for the queue-based indexer
        # redesign. Created by the executescript call; no data migration
        # because there's nothing in the queue on the first upgrade — the
        # boot catch-up scan will repopulate from indexed_files diff.
        # v7: ``idx_trips_start_time`` for callers that range-scan
        # by ``start_time`` directly (e.g. /api/trips with date_from
        # bounds). Created by the executescript above.
        # v8: expression indexes ``idx_trips_day`` and
        # ``idx_events_day`` on ``substr(<ts>, 1, 10)`` so the day-
        # based map queries can avoid full scans. Plain timestamp
        # indexes do NOT cover ``substr(col, 1, 10) = ?``. The
        # expression indexes are created by the executescript above
        # (CREATE INDEX IF NOT EXISTS is idempotent); no data
        # migration required.
        # v10: ``archive_queue`` table + ``archive_queue_ready``
        # partial index for the Phase 2a producers (issue #76).
        # Producer-only at this version — the boot catch-up scan,
        # 60-s rescan, and inotify file watcher all enqueue rows but
        # nothing drains them yet. The Phase 2b worker (separate PR)
        # will be the consumer. Created by the executescript above
        # (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
        # are idempotent); no data migration required.
        # v11: ``archive_queue_source_gone_claimed`` partial index for
        # the Phase 4.3 (#101) files-lost banner. The Settings card
        # polls ``count_source_gone_recent`` every 15 s, which would
        # otherwise scan the entire growing-without-bound
        # ``archive_queue`` table once the ``source_gone`` count
        # reaches the thousands. The partial index keeps the query
        # O(log n) and is tiny because only ``source_gone`` rows are
        # included. Created by the executescript above; no data
        # migration required.
        if current > 0 and current < 12:
            # v12 (#132): ``previous_last_error`` column on
            # ``archive_queue`` and ``indexing_queue`` so the Failed
            # Jobs UI can show multi-cycle failure history (failed →
            # retried → failed-with-different-error). On the next
            # failure each worker rotates the prior ``last_error``
            # into ``previous_last_error`` before writing the new
            # error. ALTER is idempotent on retry — duplicate-column
            # OperationalError is caught and ignored.
            for table in ('archive_queue', 'indexing_queue'):
                try:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN previous_last_error TEXT"
                    )
                except sqlite3.OperationalError:
                    pass
        if current > 0 and current < 13:
            # v13 (#178): swap archive_queue priority constants so
            # SentryClips/SavedClips events drain BEFORE RecentClips.
            # Pre-#178: P1=RecentClips, P2=Events. Post-#178: P1=Events,
            # P2=RecentClips. Producers re-tag new rows with the correct
            # constant once the code update lands; this migration flips
            # the existing in-flight backlog so users don't have to wait
            # for the old rows to drain the slow way.
            #
            # Only non-terminal statuses are touched — pending/claimed/
            # error rows will be re-inspected by the worker, so their
            # priority must reflect the new mapping. Terminal-status
            # rows (copied, source_gone, skipped_stationary, dead_letter)
            # are left alone: their ``priority`` value is historical and
            # mutating it would mislead future debugging of "what got
            # picked when".
            #
            # CASE form is symmetric and idempotent — running the
            # statement twice is a no-op modulo a second swap. The
            # ``current < 13`` gate prevents that anyway.
            try:
                conn.execute("SAVEPOINT migrate_v13")
                cur = conn.execute(
                    """
                    UPDATE archive_queue
                       SET priority = CASE priority
                                          WHEN 1 THEN 2
                                          WHEN 2 THEN 1
                                          ELSE priority
                                      END
                     WHERE status IN ('pending', 'claimed', 'error')
                       AND priority IN (1, 2)
                    """
                )
                flipped = cur.rowcount
                conn.execute("RELEASE SAVEPOINT migrate_v13")
                if flipped:
                    logger.info(
                        "Migration v12->v13: flipped priority on %d "
                        "non-terminal archive_queue row(s) "
                        "(events now P1, RecentClips now P2)", flipped,
                    )
            except Exception as e:
                conn.execute("ROLLBACK TO SAVEPOINT migrate_v13")
                conn.execute("RELEASE SAVEPOINT migrate_v13")
                logger.error(
                    "Migration v12->v13 failed, leaving schema at v12: %s", e,
                )
                conn.commit()
                return conn
        conn.execute("DELETE FROM schema_version")
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (_SCHEMA_VERSION,)
        )
        conn.commit()
        logger.info("Geo-index database initialized (v%d) at %s", _SCHEMA_VERSION, db_path)

    return conn


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Clean up duplicate trips and waypoints from earlier indexer bugs.

    Earlier versions of the indexer:
      * Created separate trips for the same physical drive when the videos
        were ingested from different source folders (RecentClips vs ArchivedClips).
      * Stored duplicate waypoints with the same ``(timestamp, lat, lon)`` but
        different ``video_path`` strings (one per copy of the video).
      * Recorded ``source_folder='..'`` for ArchivedClips because of a path
        normalization bug.

    This one-time migration:
      1. Repairs ``source_folder='..'`` rows by inferring from waypoint paths.
      2. Merges trips whose time windows overlap or are within
         ``_TRIP_GAP_MINUTES_DEFAULT`` minutes of each other (regardless of
         source_folder).
      3. Dedupes waypoints within each trip by ``(timestamp, lat, lon)``,
         preferring the row whose ``video_path`` references ArchivedClips
         (most durable storage).
      4. Recomputes ``start_time``, ``end_time``, start/end coords,
         ``distance_km`` and ``duration_seconds`` for every trip; deletes
         trips left with no waypoints.
    """
    # Phase 3c.2 (#100): the trip-gap default and merge helper stay in
    # ``mapping_service`` because they're runtime hot-path dependencies
    # of ``_index_video``. Lazy import inside the migration body keeps
    # the dependency one-way at module load time.
    from services.mapping_service import (
        _TRIP_GAP_MINUTES_DEFAULT,
        _merge_all_adjacent_trip_pairs,
        _haversine_km,
    )
    gap_seconds = _TRIP_GAP_MINUTES_DEFAULT * 60
    log_parts: List[str] = []

    # --- Phase 1: source_folder='..' ---
    bad = conn.execute(
        "SELECT id FROM trips WHERE source_folder = '..' OR source_folder LIKE '..%'"
    ).fetchall()
    fixed_src = 0
    for r in bad:
        wp = conn.execute(
            "SELECT video_path FROM waypoints "
            "WHERE trip_id = ? AND video_path IS NOT NULL ORDER BY id LIMIT 1",
            (r['id'],),
        ).fetchone()
        if wp and wp['video_path']:
            vp = wp['video_path'].replace('\\', '/')
            if 'ArchivedClips' in vp:
                folder = 'ArchivedClips'
            elif '/' in vp:
                folder = vp.split('/')[0]
            else:
                folder = 'Unknown'
            conn.execute(
                "UPDATE trips SET source_folder = ? WHERE id = ?",
                (folder, r['id']),
            )
            fixed_src += 1
    log_parts.append(f"fixed {fixed_src} '..' source_folder rows")

    # --- Phase 2: merge overlapping/close trips ---
    # Repeatedly find any pair of trips whose windows are within gap_seconds
    # of each other (in either direction) and merge the higher-id into the lower.
    merged = _merge_all_adjacent_trip_pairs(conn, gap_seconds)
    log_parts.append(f"merged {merged} overlapping trip pairs")

    # --- Phase 3: dedupe waypoints within a trip ---
    dups = conn.execute(
        """SELECT trip_id, timestamp, lat, lon, COUNT(*) AS cnt
           FROM waypoints
           WHERE trip_id IS NOT NULL
           GROUP BY trip_id, timestamp, lat, lon
           HAVING COUNT(*) > 1"""
    ).fetchall()
    deduped = 0
    for d in dups:
        ids = conn.execute(
            """SELECT id, video_path FROM waypoints
               WHERE trip_id = ? AND timestamp = ? AND lat = ? AND lon = ?
               ORDER BY
                 CASE WHEN video_path LIKE '%ArchivedClips%' THEN 0 ELSE 1 END,
                 id""",
            (d['trip_id'], d['timestamp'], d['lat'], d['lon']),
        ).fetchall()
        # Keep the first (durable / lowest id), delete the rest
        drop_ids = [(r['id'],) for r in ids[1:]]
        if drop_ids:
            conn.executemany("DELETE FROM waypoints WHERE id = ?", drop_ids)
            deduped += len(drop_ids)
    log_parts.append(f"deduped {deduped} duplicate waypoints")

    # --- Phase 4: recompute trip stats; drop empty trips ---
    # Distance is computed per video file (in frame/id order) and summed,
    # because Tesla videos can overlap in time (e.g. when a saved clip is
    # triggered alongside RecentClips). Sorting all waypoints globally by
    # timestamp would interleave overlapping recordings and produce huge
    # phantom jumps. start_time/end_time still come from min/max timestamp.
    trips = conn.execute("SELECT id FROM trips").fetchall()
    recomputed = 0
    dropped_empty = 0
    for t in trips:
        bounds = conn.execute(
            "SELECT MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts "
            "FROM waypoints WHERE trip_id = ?",
            (t['id'],),
        ).fetchone()
        if not bounds or not bounds['first_ts']:
            conn.execute("DELETE FROM trips WHERE id = ?", (t['id'],))
            dropped_empty += 1
            continue
        first_ts, last_ts = bounds['first_ts'], bounds['last_ts']
        first_row = conn.execute(
            "SELECT lat, lon FROM waypoints WHERE trip_id = ? "
            "AND timestamp = ? ORDER BY id LIMIT 1",
            (t['id'], first_ts),
        ).fetchone()
        last_row = conn.execute(
            "SELECT lat, lon FROM waypoints WHERE trip_id = ? "
            "AND timestamp = ? ORDER BY id DESC LIMIT 1",
            (t['id'], last_ts),
        ).fetchone()
        # Distance summed per video file. Batched in a single query
        # (#142, follows the Phase 5.1 / PR #141 shape used in
        # mapping_service._index_video). The legacy ``1 + N`` pattern
        # — one DISTINCT query plus one waypoint fetch per video — is
        # replaced by a single ``ORDER BY video_path, id`` walk with a
        # per-video boundary cursor so we never haversine across
        # different videos (Tesla can write overlapping clips and a
        # global sort would create phantom GPS jumps).
        total_dist = 0.0
        rows = conn.execute(
            "SELECT video_path, lat, lon FROM waypoints "
            "WHERE trip_id = ? AND video_path IS NOT NULL "
            "ORDER BY video_path, id",
            (t['id'],),
        ).fetchall()
        prev = None
        prev_video = None
        for w in rows:
            video_path = w['video_path']
            if prev is not None and video_path == prev_video:
                total_dist += _haversine_km(
                    prev['lat'], prev['lon'],
                    w['lat'], w['lon'],
                )
            prev = w
            prev_video = video_path
        try:
            dur = max(0, int((
                datetime.fromisoformat(last_ts)
                - datetime.fromisoformat(first_ts)
            ).total_seconds()))
        except (ValueError, TypeError):
            dur = 0
        conn.execute(
            """UPDATE trips SET
               start_time = ?, end_time = ?,
               start_lat = ?, start_lon = ?,
               end_lat = ?, end_lon = ?,
               distance_km = ?, duration_seconds = ?
               WHERE id = ?""",
            (first_ts, last_ts,
             first_row['lat'] if first_row else None,
             first_row['lon'] if first_row else None,
             last_row['lat'] if last_row else None,
             last_row['lon'] if last_row else None,
             total_dist, dur, t['id']),
        )
        recomputed += 1
    log_parts.append(
        f"recomputed stats for {recomputed} trips; dropped {dropped_empty} empty"
    )

    logger.info("Migration v2->v3: %s", "; ".join(log_parts))


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Re-evaluate Sentry/Saved clips with Tesla's event.json.

    Earlier versions inferred Sentry/Saved event locations from the
    nearest waypoint, which was inaccurate (often pointed at a different
    physical location). Tesla actually writes a precise event.json with
    est_lat/est_lon in each event folder.

    To pick this up for clips already in the database, we delete:
      1. The existing inferred-location detected_events rows
         (they have metadata.inferred_location=true), and
      2. The indexed_files rows for SavedClips/SentryClips clips with
         zero waypoints — so the next indexer run re-processes them
         through the new event.json-aware code path.

    Driving clips (those with waypoints) are left untouched.
    """
    # Drop old inferred events so they get recreated from event.json
    cur = conn.execute(
        "DELETE FROM detected_events "
        "WHERE event_type IN ('saved', 'sentry') "
        "AND metadata IS NOT NULL "
        "AND (metadata LIKE '%inferred_location%' "
        "     OR metadata LIKE '%nearest_waypoint%')"
    )
    deleted_events = cur.rowcount
    logger.info("v3->v4: cleared %d stale inferred events", deleted_events)

    # Clear indexed_files rows for SavedClips/SentryClips zero-waypoint
    # entries so they get re-indexed with event.json reading
    cur = conn.execute(
        "DELETE FROM indexed_files "
        "WHERE waypoint_count = 0 "
        "AND (file_path LIKE '%/SavedClips/%' "
        "     OR file_path LIKE '%/SentryClips/%' "
        "     OR file_path LIKE '%\\SavedClips\\%' "
        "     OR file_path LIKE '%\\SentryClips\\%')"
    )
    cleared_files = cur.rowcount
    logger.info("v3->v4: cleared %d Sentry/Saved indexed_files entries for re-processing",
                cleared_files)
