"""One-shot repair: fix waypoint/event timestamps that were poisoned
by Tesla onboard-clock skew at indexing time.

Tesla writes the start-of-recording UTC into the standard MP4
``mvhd`` atom from the GPS-derived clock, while the filename embeds
the car's onboard local clock. When the onboard clock has drifted
(e.g. lost GPS time sync after a firmware update), every clip
recorded during that window is filed under the wrong day. The
indexer historically used the filename as the sole source of
absolute time, so the database carries the wrong timestamps for
every waypoint and detected_event from those clips. The result on
the map UI: a Sunday drive shown as "Mon, May 11" with all GPS
points shifted forward (or backward) by the same constant offset.

This script:

  1. Walks every ``indexed_files`` row whose source MP4 is still on
     disk.
  2. For each clip, compares the filename-derived timestamp to the
     ``mvhd`` UTC; if they differ by more than ``MIN_SKEW_SECONDS``
     (default 60 s) the clip's waypoints and detected_events are
     shifted by the same delta so they land on the correct UTC.
  3. Calls the project's existing trip-merge helper to stitch
     together any trips that overlap after retiming.
  4. Dedupes waypoints within each surviving trip (one row per
     ``(timestamp, lat, lon)``, keeping the row that has a
     ``video_path``).
  5. Dedupes ``detected_events`` rows that became identical after the
     retime (one row per ``(trip_id, timestamp, lat, lon, event_type)``,
     keeping the row that has a ``video_path``). Without this pass,
     events that were inserted twice by the indexer survive the
     waypoint cleanup and show as duplicate pins on the map.
  6. Recomputes per-trip ``start_time``, ``end_time``, start/end
     coordinates, ``distance_km`` and ``duration_seconds`` and
     deletes any trip that ended up with zero waypoints.

Idempotent: when filename ≈ mvhd the row is skipped, so re-running
the script after the indexer fix is harmless.

Usage::

    python -m services.clock_skew_repair --dry-run
    python -m services.clock_skew_repair          # apply
    python -m services.clock_skew_repair --db-path /path/to/geodata.db -v

The script always writes a timestamped backup of the database
before applying changes (skipped for ``--dry-run``).
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

# Allow running the script directly from a checkout without the
# ``scripts/web`` parent on PYTHONPATH.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.dirname(_THIS_DIR)
if _WEB_DIR not in sys.path:
    sys.path.insert(0, _WEB_DIR)

from services import mapping_service, sei_parser  # noqa: E402

# Optional config import — falls back to ``/home/pi/TeslaUSB/...`` defaults
# if the script is invoked from a checkout that hasn't been deployed (so the
# tests can run without spinning up a full config environment). Production
# uses the real config module.
try:
    import config  # noqa: E402
    from services import partition_service  # noqa: E402
    _HAS_CONFIG = True
except Exception:  # pragma: no cover - defensive
    _HAS_CONFIG = False
    config = None
    partition_service = None

logger = logging.getLogger("clock_skew_repair")

# Skew below this is treated as "noise" and ignored. The mvhd time is
# the moment Tesla started writing the file; the filename minute is
# the moment Tesla decided what to call it. They normally differ by a
# few seconds, never by 60+. A 60 s floor protects against retiming
# clips on healthy clocks just because of natural sub-minute jitter.
MIN_SKEW_SECONDS = 60


def _backup_db(db_path: str) -> str:
    """Copy the database file to a timestamped sibling and return its path."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = f"{db_path}.bak.clock_skew_repair.{ts}"
    shutil.copy2(db_path, target)
    return target


def _video_paths_in_db(conn: sqlite3.Connection) -> List[str]:
    """Return the distinct ``video_path`` values referenced by waypoints
    or detected_events.

    Indexed_files keys on ``file_path`` which is the canonical key the
    indexer used at write time; waypoints reference a ``video_path``
    string that may be the same file accessed from a different mount
    (e.g. ``/mnt/gadget/part1-ro/...`` vs ``/home/pi/ArchivedClips/...``).
    We retime by ``video_path`` because that's what the waypoint and
    detected_event rows carry.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT video_path FROM waypoints
        WHERE video_path IS NOT NULL AND video_path != ''
        UNION
        SELECT DISTINCT video_path FROM detected_events
        WHERE video_path IS NOT NULL AND video_path != ''
        """
    ).fetchall()
    return [r[0] for r in rows]


def _candidate_archive_root() -> Optional[str]:
    """Return the canonical SD-card archive directory, from config when
    available; falls back to the deployed default."""
    if _HAS_CONFIG:
        try:
            return config.ARCHIVE_DIR
        except AttributeError:
            pass
    return "/home/pi/ArchivedClips"


def _candidate_teslacam_root() -> Optional[str]:
    """Return the active TeslaCam directory on the gadget RO mount.

    Uses ``partition_service.get_mount_path('part1')`` so the result
    follows present/edit mode automatically. Falls back to the
    well-known RO path if config is unavailable (test environments).
    """
    if _HAS_CONFIG:
        try:
            mount = partition_service.get_mount_path('part1')
            if mount:
                return os.path.join(mount, 'TeslaCam')
        except Exception:
            pass
    return "/mnt/gadget/part1-ro/TeslaCam"


def _resolve_existing_path(video_path: str) -> Optional[str]:
    """Return a path to the file that exists on disk.

    Tries cheap remappings first (recorded path, canonical archive
    location, gadget RO mount); only falls back to a directory walk
    if every direct candidate misses. The walk is bounded to the
    archive and TeslaCam roots and breaks on first match per root,
    so worst-case cost is one ``os.walk`` per missing file — which
    only happens for genuinely-orphaned waypoint rows.
    """
    if video_path and os.path.isfile(video_path):
        return video_path

    base = os.path.basename(video_path) if video_path else ""
    norm = (video_path or "").replace("\\", "/")
    archive_root = _candidate_archive_root()
    teslacam_root = _candidate_teslacam_root()

    # Cheap remappings: take whatever sub-path is anchored on a known
    # root and re-anchor it against the live root.
    cheap: List[str] = []
    if archive_root and "ArchivedClips" in norm:
        idx = norm.find("ArchivedClips")
        rel = norm[idx + len("ArchivedClips"):].lstrip("/")
        cheap.append(os.path.join(archive_root, rel))
    if teslacam_root and "TeslaCam" in norm:
        idx = norm.find("TeslaCam")
        rel = norm[idx + len("TeslaCam"):].lstrip("/")
        cheap.append(os.path.join(teslacam_root, rel))
    for c in cheap:
        if c and os.path.isfile(c):
            return c

    # Last-ditch: one walk per known root, break on first basename hit.
    if base:
        for root in (archive_root, teslacam_root):
            if root and os.path.isdir(root):
                for dirpath, _, filenames in os.walk(root):
                    if base in filenames:
                        return os.path.join(dirpath, base)
    return None


def _delta_seconds_for(
    video_path: str,
    current_base_iso: Optional[str],
) -> Optional[Tuple[float, str, str]]:
    """Compute the retime delta for one video file.

    The "old time" is derived from the **current** database state
    (the earliest waypoint timestamp for ``video_path``, adjusted
    back to the start of the clip). This makes the script naturally
    idempotent: after a successful run the waypoints are aligned
    with mvhd, so re-running yields a delta of ~0 and skips.

    ``current_base_iso`` may be ``None`` when no waypoint references
    the clip — in that case we fall back to the filename-derived
    timestamp so we can still detect skew on indexed_files-only rows.

    Returns ``(delta_seconds, old_iso, new_iso)`` if the clip should
    be retimed, ``None`` otherwise (file missing, mvhd unreadable,
    or skew below the floor).
    """
    on_disk = _resolve_existing_path(video_path)
    if not on_disk:
        logger.debug("skip %s: file missing", video_path)
        return None

    mvhd_dt = sei_parser.extract_mvhd_creation_time(on_disk)
    if mvhd_dt is None:
        logger.debug("skip %s: no mvhd", video_path)
        return None

    # Mirror _resolve_recording_time: convert UTC to system-local naive
    # so the math agrees with how the indexer stored timestamps.
    new_local = datetime.fromtimestamp(mvhd_dt.timestamp())
    new_iso = new_local.isoformat()

    old_iso = current_base_iso or mapping_service._timestamp_from_filename(
        video_path
    )
    if not old_iso:
        logger.debug("skip %s: no current base ts and no filename ts",
                     video_path)
        return None

    try:
        old_dt = datetime.fromisoformat(old_iso)
    except ValueError:
        return None
    delta = (new_local - old_dt).total_seconds()
    if abs(delta) < MIN_SKEW_SECONDS:
        return None
    return delta, old_iso, new_iso


def _shift_video_timestamps(conn: sqlite3.Connection,
                            video_path: str,
                            delta_seconds: float) -> Tuple[int, int]:
    """Shift every ``waypoints`` / ``detected_events`` row referencing
    ``video_path`` by ``delta_seconds``. Returns ``(waypoints, events)``
    counts of rows updated.

    Uses a Python-side rewrite (not SQLite's ``datetime(...)``) so the
    output preserves the indexer's canonical ISO format
    (``YYYY-MM-DDTHH:MM:SS[.ffffff]``). SQLite's ``datetime()`` emits
    a space separator (``YYYY-MM-DD HH:MM:SS``) which is byte-different
    from the existing column values, breaking the
    ``GROUP BY (trip_id, timestamp, lat, lon)`` dedup pass that runs
    later.
    """
    wp_rows = conn.execute(
        "SELECT id, timestamp FROM waypoints WHERE video_path = ?",
        (video_path,),
    ).fetchall()
    wp_updates = []
    for r in wp_rows:
        try:
            new_ts = (
                datetime.fromisoformat(r['timestamp']).timestamp()
                + delta_seconds
            )
            wp_updates.append(
                (datetime.fromtimestamp(new_ts).isoformat(), r['id'])
            )
        except (ValueError, TypeError):
            continue
    if wp_updates:
        conn.executemany(
            "UPDATE waypoints SET timestamp = ? WHERE id = ?", wp_updates
        )

    ev_rows = conn.execute(
        "SELECT id, timestamp FROM detected_events WHERE video_path = ?",
        (video_path,),
    ).fetchall()
    ev_updates = []
    for r in ev_rows:
        try:
            new_ts = (
                datetime.fromisoformat(r['timestamp']).timestamp()
                + delta_seconds
            )
            ev_updates.append(
                (datetime.fromtimestamp(new_ts).isoformat(), r['id'])
            )
        except (ValueError, TypeError):
            continue
    if ev_updates:
        conn.executemany(
            "UPDATE detected_events SET timestamp = ? WHERE id = ?", ev_updates
        )
    return len(wp_updates), len(ev_updates)


def _dedupe_detected_events(conn: sqlite3.Connection) -> int:
    """Drop duplicate ``detected_events`` rows produced by re-indexing.

    When the indexer processes the same physical clip twice (which can
    happen if ``waypoints.video_path`` was nulled by ``purge_deleted_videos``
    between runs — the dedup at ``mapping_service._index_video`` keys on
    ``video_path IN (...)`` and misses NULL rows), two sets of events get
    inserted. Phase A's per-clip retime then shifts both sets to the same
    timestamp, after which they become exact ``(trip_id, timestamp, lat,
    lon, event_type)`` duplicates.

    The earlier waypoint dedup catches the matching waypoint duplicates;
    this function provides the analogous coverage for ``detected_events``.
    Without it, the map UI shows the same event pin twice (one with a
    valid ``video_path`` and one with ``video_path=NULL`` left over from
    a prior purge).

    Returns the count of rows deleted.

    The "keep" ranking matches the waypoint dedup: prefer non-NULL
    ``video_path``, prefer paths under ``ArchivedClips`` (durable SD-card
    location) over ``RecentClips`` (Tesla's rotating buffer), break ties
    by ``id``.

    The function runs two passes — one over rows with a real ``trip_id``
    and one over Sentry/Saved rows with ``trip_id IS NULL`` — instead of
    using a sentinel value in the GROUP BY. This avoids any risk of
    colliding with a real ``trip_id`` that someday matches the sentinel
    and keeps the SQL semantics explicit.
    """
    deleted = 0

    # --- Pass 1: rows with a concrete trip_id ---------------------
    dups_with_trip = conn.execute(
        """SELECT trip_id, timestamp, lat, lon, event_type, COUNT(*) AS cnt
           FROM detected_events
           WHERE timestamp IS NOT NULL AND trip_id IS NOT NULL
           GROUP BY trip_id, timestamp, lat, lon, event_type
           HAVING cnt > 1"""
    ).fetchall()
    for d in dups_with_trip:
        # Positional access so the helper works whether or not the
        # caller set ``conn.row_factory = sqlite3.Row`` (the
        # production ``repair()`` does; isolated unit tests may not).
        trip_id, ts, lat, lon, ev_type = d[0], d[1], d[2], d[3], d[4]
        rows = conn.execute(
            """SELECT id FROM detected_events
               WHERE trip_id = ?
                 AND timestamp = ? AND lat = ? AND lon = ?
                 AND event_type = ?
               ORDER BY
                 CASE WHEN video_path IS NOT NULL AND video_path != ''
                      THEN 0 ELSE 1 END,
                 CASE WHEN video_path LIKE '%ArchivedClips%'
                      THEN 0 ELSE 1 END,
                 id""",
            (trip_id, ts, lat, lon, ev_type),
        ).fetchall()
        drop_ids = [(r[0],) for r in rows[1:]]
        if drop_ids:
            conn.executemany(
                "DELETE FROM detected_events WHERE id = ?", drop_ids
            )
            deleted += len(drop_ids)

    # --- Pass 2: Sentry/Saved rows with trip_id IS NULL -----------
    dups_null_trip = conn.execute(
        """SELECT timestamp, lat, lon, event_type, COUNT(*) AS cnt
           FROM detected_events
           WHERE timestamp IS NOT NULL AND trip_id IS NULL
           GROUP BY timestamp, lat, lon, event_type
           HAVING cnt > 1"""
    ).fetchall()
    for d in dups_null_trip:
        ts, lat, lon, ev_type = d[0], d[1], d[2], d[3]
        rows = conn.execute(
            """SELECT id FROM detected_events
               WHERE trip_id IS NULL
                 AND timestamp = ? AND lat = ? AND lon = ?
                 AND event_type = ?
               ORDER BY
                 CASE WHEN video_path IS NOT NULL AND video_path != ''
                      THEN 0 ELSE 1 END,
                 CASE WHEN video_path LIKE '%ArchivedClips%'
                      THEN 0 ELSE 1 END,
                 id""",
            (ts, lat, lon, ev_type),
        ).fetchall()
        drop_ids = [(r[0],) for r in rows[1:]]
        if drop_ids:
            conn.executemany(
                "DELETE FROM detected_events WHERE id = ?", drop_ids
            )
            deleted += len(drop_ids)

    return deleted


def _dedupe_and_recompute(conn: sqlite3.Connection) -> Tuple[int, int, int, int]:
    """Run dedup + recompute identical to ``_migrate_v2_to_v3`` phases 3+4.

    Returns ``(deduped_waypoints, recomputed_trips, dropped_empty_trips,
    deduped_events)``.
    """
    # --- Dedupe waypoints by (trip_id, timestamp, lat, lon) -------------
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
                 CASE WHEN video_path IS NOT NULL AND video_path != ''
                      THEN 0 ELSE 1 END,
                 CASE WHEN video_path LIKE '%ArchivedClips%' THEN 0 ELSE 1 END,
                 id""",
            (d['trip_id'], d['timestamp'], d['lat'], d['lon']),
        ).fetchall()
        drop_ids = [(r['id'],) for r in ids[1:]]
        if drop_ids:
            conn.executemany("DELETE FROM waypoints WHERE id = ?", drop_ids)
            deduped += len(drop_ids)

    # --- Cross-trip merge: same (timestamp, lat, lon) in two trips ------
    # After retiming, the same physical drive that was indexed twice
    # under different days will have waypoints with identical
    # (timestamp, lat, lon) in two different trips. Pick the older
    # trip as the keeper, transfer any video_path from the duplicate
    # to the keeper if the keeper lacks one, then drop the duplicate.
    cross = conn.execute(
        """SELECT timestamp, lat, lon, COUNT(DISTINCT trip_id) AS trips
           FROM waypoints
           WHERE trip_id IS NOT NULL AND timestamp IS NOT NULL
           GROUP BY timestamp, lat, lon
           HAVING COUNT(DISTINCT trip_id) > 1"""
    ).fetchall()
    cross_merged = 0
    for c in cross:
        rows = conn.execute(
            """SELECT id, trip_id, video_path FROM waypoints
               WHERE timestamp = ? AND lat = ? AND lon = ?
               ORDER BY trip_id, id""",
            (c['timestamp'], c['lat'], c['lon']),
        ).fetchall()
        keep = rows[0]
        for r in rows[1:]:
            if (
                (keep['video_path'] is None or keep['video_path'] == '')
                and r['video_path']
            ):
                conn.execute(
                    "UPDATE waypoints SET video_path = ? WHERE id = ?",
                    (r['video_path'], keep['id']),
                )
            conn.execute("DELETE FROM waypoints WHERE id = ?", (r['id'],))
            cross_merged += 1
    deduped += cross_merged

    # --- Recompute per-trip stats; drop empty trips ---------------------
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
        # Per-video distance, summed (matches _migrate_v2_to_v3 logic).
        total_dist = 0.0
        videos = conn.execute(
            "SELECT DISTINCT video_path FROM waypoints "
            "WHERE trip_id = ? AND video_path IS NOT NULL",
            (t['id'],),
        ).fetchall()
        for v in videos:
            wps = conn.execute(
                "SELECT lat, lon FROM waypoints "
                "WHERE trip_id = ? AND video_path = ? ORDER BY id",
                (t['id'], v['video_path']),
            ).fetchall()
            for j in range(1, len(wps)):
                total_dist += mapping_service._haversine_km(
                    wps[j-1]['lat'], wps[j-1]['lon'],
                    wps[j]['lat'], wps[j]['lon'],
                )
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
    events_deduped = _dedupe_detected_events(conn)
    return deduped, recomputed, dropped_empty, events_deduped


def repair(db_path: str, dry_run: bool = False) -> dict:
    """Run the full repair against ``db_path``. Returns a stats dict."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(db_path)

    backup = None
    if not dry_run:
        backup = _backup_db(db_path)
        logger.info("Database backed up to %s", backup)

    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    stats = {
        "files_scanned": 0,
        "files_skipped": 0,
        "files_retimed": 0,
        "waypoints_shifted": 0,
        "events_shifted": 0,
        "trips_merged": 0,
        "waypoints_deduped": 0,
        "events_deduped": 0,
        "trips_recomputed": 0,
        "trips_dropped_empty": 0,
        "max_skew_seconds": 0.0,
        "backup_path": backup,
        "dry_run": dry_run,
    }

    video_paths = _video_paths_in_db(conn)
    logger.info("Found %d distinct video_path values in DB", len(video_paths))

    # Look up the current "base" timestamp for each video — the
    # earliest waypoint timestamp minus its frame_offset_ms (so we
    # compare against the start of the clip, not its first GPS
    # sample). This makes the script naturally idempotent.
    current_bases: dict = {}
    for vp in video_paths:
        row = conn.execute(
            "SELECT timestamp, frame_offset FROM waypoints "
            "WHERE video_path = ? "
            "ORDER BY timestamp ASC LIMIT 1",
            (vp,),
        ).fetchone()
        if row and row['timestamp']:
            try:
                base_dt = datetime.fromisoformat(row['timestamp'])
                offset_ms = row['frame_offset'] or 0
                base_dt = base_dt - timedelta(milliseconds=offset_ms)
                current_bases[vp] = base_dt.isoformat()
            except (ValueError, TypeError):
                current_bases[vp] = None
        else:
            current_bases[vp] = None

    # --- Phase A: per-clip retime ---------------------------------------
    skewed: List[Tuple[str, float, str, str]] = []
    for vp in video_paths:
        stats["files_scanned"] += 1
        result = _delta_seconds_for(vp, current_bases.get(vp))
        if result is None:
            stats["files_skipped"] += 1
            continue
        delta, old_iso, new_iso = result
        skewed.append((vp, delta, old_iso, new_iso))
        if abs(delta) > stats["max_skew_seconds"]:
            stats["max_skew_seconds"] = abs(delta)

    logger.info(
        "Phase A: %d clips skipped (no skew / no mvhd / missing), "
        "%d clips need retiming (max skew %.0f s)",
        stats["files_skipped"], len(skewed), stats["max_skew_seconds"],
    )

    if dry_run:
        for vp, delta, old_iso, new_iso in skewed[:25]:
            logger.info(
                "  WOULD SHIFT %s by %+.0fs  (%s -> %s)",
                os.path.basename(vp), delta, old_iso, new_iso,
            )
        if len(skewed) > 25:
            logger.info("  ... and %d more (use -v for all)", len(skewed) - 25)
        conn.close()
        return stats

    try:
        conn.execute("SAVEPOINT clock_skew_repair")
        for vp, delta, old_iso, new_iso in skewed:
            wp, ev = _shift_video_timestamps(conn, vp, delta)
            stats["waypoints_shifted"] += wp
            stats["events_shifted"] += ev
            stats["files_retimed"] += 1
            logger.debug(
                "  shifted %s by %+.0fs (%d wp, %d ev)",
                os.path.basename(vp), delta, wp, ev,
            )

        # --- Phase B: recompute trip windows, then merge + dedup --------
        # ``_merge_all_adjacent_trip_pairs`` reads the trip's
        # ``start_time`` / ``end_time`` columns to decide which trips
        # are mergeable, but Phase A only shifted the waypoint rows —
        # the parent trip's window is still labelled with the wrong
        # day. Refresh those bounds from the (now-correct) waypoint
        # timestamps before merging, otherwise the merge query
        # silently sees the trips as days apart and never collapses
        # the trip-74-vs-trip-75 duplicates.
        conn.execute(
            "UPDATE trips SET "
            "  start_time = (SELECT MIN(timestamp) FROM waypoints "
            "                WHERE trip_id = trips.id), "
            "  end_time   = (SELECT MAX(timestamp) FROM waypoints "
            "                WHERE trip_id = trips.id) "
            "WHERE EXISTS ("
            "  SELECT 1 FROM waypoints WHERE trip_id = trips.id"
            ")"
        )

        gap_seconds = mapping_service._TRIP_GAP_MINUTES_DEFAULT * 60
        stats["trips_merged"] = mapping_service._merge_all_adjacent_trip_pairs(
            conn, gap_seconds,
        )
        deduped, recomputed, dropped, events_deduped = _dedupe_and_recompute(conn)
        stats["waypoints_deduped"] = deduped
        stats["events_deduped"] = events_deduped
        stats["trips_recomputed"] = recomputed
        stats["trips_dropped_empty"] = dropped

        conn.execute("RELEASE SAVEPOINT clock_skew_repair")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT clock_skew_repair")
        conn.execute("RELEASE SAVEPOINT clock_skew_repair")
        conn.commit()
        raise
    finally:
        conn.close()

    return stats


def main(argv: Optional[List[str]] = None) -> int:
    default_db = (
        config.MAPPING_DB_PATH if _HAS_CONFIG and hasattr(config, "MAPPING_DB_PATH")
        else "/home/pi/TeslaUSB/geodata.db"
    )
    parser = argparse.ArgumentParser(
        description="Repair waypoint/event timestamps poisoned by Tesla "
                    "onboard-clock skew. Reads MP4 mvhd atom for ground-truth "
                    "UTC and shifts DB rows by the resulting delta. "
                    "Idempotent.",
    )
    parser.add_argument(
        "--db-path",
        default=default_db,
        help="Path to geodata.db (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report what would change without writing",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose per-file logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        stats = repair(args.db_path, dry_run=args.dry_run)
    except FileNotFoundError as e:
        logger.error("Database not found: %s", e)
        return 2
    except Exception as e:
        logger.exception("Repair failed: %s", e)
        return 1

    logger.info("=" * 60)
    logger.info("Clock-skew repair %s", "DRY RUN" if args.dry_run else "complete")
    logger.info("  files scanned:       %d", stats["files_scanned"])
    logger.info("  files skipped:       %d (no skew, no mvhd, or missing)",
                stats["files_skipped"])
    logger.info("  files retimed:       %d", stats["files_retimed"])
    logger.info("  max skew observed:   %.0f s", stats["max_skew_seconds"])
    logger.info("  waypoints shifted:   %d", stats["waypoints_shifted"])
    logger.info("  events shifted:      %d", stats["events_shifted"])
    logger.info("  trips merged:        %d", stats["trips_merged"])
    logger.info("  waypoints deduped:   %d", stats["waypoints_deduped"])
    logger.info("  events deduped:      %d", stats["events_deduped"])
    logger.info("  trips recomputed:    %d", stats["trips_recomputed"])
    logger.info("  trips dropped empty: %d", stats["trips_dropped_empty"])
    if stats["backup_path"]:
        logger.info("  backup written:      %s", stats["backup_path"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
