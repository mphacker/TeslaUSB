"""Blueprint for map-based video browser with GPS tracking and event detection."""

import os
import re
from flask import Blueprint, render_template, jsonify, request, redirect, url_for, flash

from config import (
    IMG_CAM_PATH, MAPPING_ENABLED, MAPPING_DB_PATH,
    MAPPING_SAMPLE_RATE, MAPPING_TRIP_GAP_MINUTES, MAPPING_EVENT_THRESHOLDS,
)
from utils import get_base_context
from services.video_service import get_teslacam_path

import logging
logger = logging.getLogger(__name__)

mapping_bp = Blueprint('mapping', __name__, url_prefix='')

# Strict ISO date validation. The day-based map view passes the
# selected day verbatim into a SQL substr() comparison, so we don't
# rely on SQLite to silently coerce — we reject anything that isn't
# exactly YYYY-MM-DD up front. This also prevents a path-traversal
# style attempt against /api/day/<date>/routes from ever reaching
# the database layer.
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


@mapping_bp.before_request
def _require_cam_image():
    if not os.path.isfile(IMG_CAM_PATH):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"error": "Feature unavailable — TeslaCam image not found"}), 503
        flash("Map feature is not available because the TeslaCam disk image has not been created.")
        return redirect(url_for('mode_control.index'))


@mapping_bp.route("/")
def map_view():
    """Main map page with trip routes and event markers."""
    ctx = get_base_context()
    return render_template('mapping.html', page='map', **ctx)


# ---------------------------------------------------------------------------
# Trip APIs
# ---------------------------------------------------------------------------

@mapping_bp.route("/api/trips")
def api_trips():
    """List trips with optional filters."""
    from services.mapping_service import query_trips

    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    # Default 50 m: hides parking-lot blips and isolated sentry recordings
    # from the main trip nav. Pass ?min_distance=0 to include all trips.
    min_distance_km = request.args.get('min_distance', 0.05, type=float)

    bbox = None
    if all(request.args.get(k) for k in ('min_lat', 'min_lon', 'max_lat', 'max_lon')):
        try:
            bbox = (
                float(request.args['min_lat']),
                float(request.args['min_lon']),
                float(request.args['max_lat']),
                float(request.args['max_lon']),
            )
        except (ValueError, TypeError):
            pass

    try:
        trips = query_trips(MAPPING_DB_PATH, limit=limit, offset=offset,
                            bbox=bbox, date_from=date_from, date_to=date_to,
                            min_distance_km=min_distance_km)
        return jsonify({'trips': trips})
    except Exception as e:
        logger.error("Failed to query trips: %s", e)
        return jsonify({'error': str(e)}), 500


@mapping_bp.route("/api/trip/<int:trip_id>/route")
def api_trip_route(trip_id):
    """Get GeoJSON route for a specific trip."""
    from services.mapping_service import query_trip_route

    try:
        waypoints = query_trip_route(MAPPING_DB_PATH, trip_id)
        if not waypoints:
            return jsonify({'error': 'Trip not found'}), 404

        # Normalize video_path for archived clips
        for wp in waypoints:
            vp = wp.get('video_path', '')
            if vp and 'ArchivedClips' in vp:
                basename = vp.rsplit('/', 1)[-1] if '/' in vp else vp
                wp['video_path'] = f'ArchivedClips/{basename}'

        # Build GeoJSON LineString
        coordinates = [[wp['lon'], wp['lat']] for wp in waypoints]
        properties = {
            'trip_id': trip_id,
            'waypoint_count': len(waypoints),
            'waypoints': waypoints,
        }

        geojson = {
            'type': 'Feature',
            'geometry': {
                'type': 'LineString',
                'coordinates': coordinates,
            },
            'properties': properties,
        }
        return jsonify(geojson)
    except Exception as e:
        logger.error("Failed to query trip route: %s", e)
        return jsonify({'error': str(e)}), 500


@mapping_bp.route("/api/waypoints-for-clip")
def api_waypoints_for_clip():
    """Look up waypoints matching a video clip path (or nearby clips in same trip)."""
    from services.mapping_service import get_db_connection

    video_path = request.args.get('path', '')
    if not video_path:
        return jsonify({'waypoints': []})

    try:
        conn = get_db_connection(MAPPING_DB_PATH)
        # First try exact match on the video_path
        rows = conn.execute(
            """SELECT w.* FROM waypoints w
               WHERE w.video_path = ? ORDER BY w.id""",
            (video_path,)
        ).fetchall()

        if rows:
            # Found — also get all waypoints from the same trip for full HUD
            trip_id = rows[0]['trip_id']
            all_wps = conn.execute(
                """SELECT * FROM waypoints WHERE trip_id = ? ORDER BY id""",
                (trip_id,)
            ).fetchall()
            conn.close()
            return jsonify({'waypoints': [dict(r) for r in all_wps], 'trip_id': trip_id})

        # No exact match — try matching by base path (without -front.mp4 suffix)
        base = video_path.replace('-front.mp4', '').replace('-back.mp4', '')
        rows = conn.execute(
            """SELECT DISTINCT trip_id FROM waypoints
               WHERE video_path LIKE ? LIMIT 1""",
            (f'%{base}%',)
        ).fetchall()

        if rows:
            trip_id = rows[0]['trip_id']
            all_wps = conn.execute(
                """SELECT * FROM waypoints WHERE trip_id = ? ORDER BY id""",
                (trip_id,)
            ).fetchall()
            conn.close()
            return jsonify({'waypoints': [dict(r) for r in all_wps], 'trip_id': trip_id})

        conn.close()
        return jsonify({'waypoints': []})
    except Exception as e:
        logger.error("Failed to look up waypoints for clip: %s", e)
        return jsonify({'waypoints': []})


# ---------------------------------------------------------------------------
# Event APIs
# ---------------------------------------------------------------------------

@mapping_bp.route("/api/events")
def api_events():
    """List detected events with optional filters."""
    from services.mapping_service import query_events

    # When ``date`` is supplied the request is asking for a complete
    # day's worth of markers (the map renders all of them). Allow up
    # to 5000 in that case so a busy sentry-event day is not silently
    # truncated. The unscoped listing keeps the older 1000 cap because
    # nothing on the page actually wants more than a few hundred.
    raw_limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    event_type = request.args.get('type')
    severity = request.args.get('severity')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    date = request.args.get('date')

    # Reject malformed single-day filters early so we never pass a
    # non-canonical string into the substr() comparison. ``date_from``
    # and ``date_to`` are open-ended strings (callers may pass
    # full ISO timestamps for fine-grained windows) so we don't
    # validate those.
    if date is not None and not _DATE_RE.match(date):
        return jsonify({'error': 'date must be YYYY-MM-DD'}), 400

    if raw_limit is None or raw_limit <= 0:
        raw_limit = 100
    cap = 5000 if date else 1000
    limit = min(raw_limit, cap)

    bbox = None
    if all(request.args.get(k) for k in ('min_lat', 'min_lon', 'max_lat', 'max_lon')):
        try:
            bbox = (
                float(request.args['min_lat']),
                float(request.args['min_lon']),
                float(request.args['max_lat']),
                float(request.args['max_lon']),
            )
        except (ValueError, TypeError):
            pass

    try:
        events = query_events(MAPPING_DB_PATH, limit=limit, offset=offset,
                              event_type=event_type, severity=severity,
                              bbox=bbox, date_from=date_from, date_to=date_to,
                              date=date)
        return jsonify({'events': events})
    except Exception as e:
        logger.error("Failed to query events: %s", e)
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Day-based APIs (powering the day navigator on the map page)
# ---------------------------------------------------------------------------

# Default trip-distance threshold matches /api/trips so the day card's
# trip count never advertises trips the map omits. 50 m hides parking-
# lot blips and stationary sentry recordings.
_DEFAULT_DAY_MIN_DISTANCE_KM = 0.05
# Maximum days returned by /api/days. The day navigator scrolls
# locally; 365 covers a full year of indexed history. Higher values
# would just bloat the initial payload without changing the UI.
_DAYS_LIMIT_MAX = 365
_DAYS_LIMIT_DEFAULT = 60


@mapping_bp.route("/api/days")
def api_days():
    """Return a list of days that have trips and/or detected events.

    Powers the day navigator (prev/next chevrons + day card stats).
    Each day row carries enough metadata to render the card without
    a follow-up call:

      * ``date`` (``YYYY-MM-DD``)
      * ``trip_count`` / ``total_distance_km`` — trips meeting
        ``min_distance`` (default 50 m, matches ``/api/trips``)
      * ``event_count`` / ``sentry_count``
      * ``first_start`` / ``last_end`` (NULL on event-only days)

    Query params:
      * ``limit`` — max days to return (default 60, capped at 365)
      * ``min_distance`` — trip distance threshold in km
        (default 0.05 = 50 m). Pass 0 to include parking blips.
    """
    from services.mapping_service import query_days

    limit = request.args.get('limit', _DAYS_LIMIT_DEFAULT, type=int)
    if limit is None or limit <= 0:
        limit = _DAYS_LIMIT_DEFAULT
    limit = min(limit, _DAYS_LIMIT_MAX)

    min_distance_km = request.args.get(
        'min_distance', _DEFAULT_DAY_MIN_DISTANCE_KM, type=float
    )
    if min_distance_km is None or min_distance_km < 0:
        min_distance_km = _DEFAULT_DAY_MIN_DISTANCE_KM

    try:
        days = query_days(MAPPING_DB_PATH, limit=limit,
                          min_distance_km=min_distance_km)
        return jsonify({'days': days})
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to query days: %s", e)
        return jsonify({'error': str(e)}), 500


@mapping_bp.route("/api/day/<date>/routes")
def api_day_routes(date):
    """Return all trip routes that started on ``date`` (``YYYY-MM-DD``).

    Powers the multi-trip overlay for a selected day. One server
    round trip returns every trip's metadata + waypoints in a single
    JSON payload, so the client doesn't fan out to per-trip
    ``/api/trip/<id>/route`` calls (which would be expensive on a
    multi-trip day with hundreds of waypoints each).

    Response shape::

        {
          "date": "2026-05-04",
          "trips": [
            {
              "trip_id": int,
              "start_time", "end_time",
              "start_lat", "start_lon", "end_lat", "end_lon",
              "distance_km", "duration_seconds",
              "source_folder",
              "waypoints": [{"id", "lat", "lon", "speed_mps",
                             "video_path", ...}, ...]
            },
            ...
          ]
        }
    """
    from services.mapping_service import query_day_routes

    if not _DATE_RE.match(date):
        return jsonify({'error': 'date must be YYYY-MM-DD'}), 400

    min_distance_km = request.args.get(
        'min_distance', _DEFAULT_DAY_MIN_DISTANCE_KM, type=float
    )
    if min_distance_km is None or min_distance_km < 0:
        min_distance_km = _DEFAULT_DAY_MIN_DISTANCE_KM

    try:
        result = query_day_routes(MAPPING_DB_PATH, date,
                                  min_distance_km=min_distance_km)
        # Normalize ArchivedClips video paths so the client can
        # use them as relative URLs without further parsing — same
        # contract as /api/trip/<id>/route.
        for trip in result.get('trips', []):
            for wp in trip.get('waypoints', []):
                vp = wp.get('video_path') or ''
                if vp and 'ArchivedClips' in vp:
                    basename = vp.rsplit('/', 1)[-1] if '/' in vp else vp
                    wp['video_path'] = f'ArchivedClips/{basename}'
        result['date'] = date
        return jsonify(result)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to query day routes: %s", e)
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Stats & Indexer APIs
# ---------------------------------------------------------------------------

@mapping_bp.route("/api/stats")
def api_stats():
    """Get summary statistics."""
    from services.mapping_service import get_stats

    try:
        return jsonify(get_stats(MAPPING_DB_PATH))
    except Exception as e:
        logger.error("Failed to get stats: %s", e)
        return jsonify({'error': str(e)}), 500


@mapping_bp.route("/api/index/status")
def api_index_status():
    """Return the current indexing-worker status.

    The UI polls this to drive the "Indexing…" banner. Banner is
    shown whenever ``active_file`` is truthy — either a file is
    actively being parsed, or a manual rebuild has just been
    triggered. The bare boot catch-up scan does NOT set
    ``active_file`` (it only enqueues), so the banner stays off
    while the queue is being filled but no parsing is happening.
    """
    try:
        from services import indexing_worker
        status = indexing_worker.get_worker_status()
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to fetch worker status: %s", e)
        status = {
            'worker_running': False,
            'active_file': None,
            'queue_depth': 0,
            'files_done_session': 0,
            'last_drained_at': None,
            'last_error': str(e),
            'source': None,
        }
    return jsonify(status)


@mapping_bp.route("/api/index/trigger", methods=['POST'])
def api_index_trigger():
    """Scan the TeslaCam tree for un-indexed clips and enqueue them.

    This is the cheap "Scan for new clips" action. It walks the
    filesystem once, diffs against ``indexed_files``, and enqueues
    orphan canonical keys. The worker drains the queue in the
    background.
    """
    if not MAPPING_ENABLED:
        return jsonify({
            'success': False,
            'message': 'Mapping is disabled in config.yaml',
        }), 400

    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return jsonify({
            'success': False,
            'message': 'TeslaCam not accessible',
        }), 503

    from services.mapping_service import boot_catchup_scan
    try:
        summary = boot_catchup_scan(
            MAPPING_DB_PATH, teslacam_path, source='manual',
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Manual scan failed: %s", e)
        return jsonify({'success': False, 'message': str(e)}), 500

    return jsonify({
        'success': True,
        'message': (
            f"Scan complete: {summary['enqueued']} new clip(s) queued "
            f"({summary['already_indexed']} already indexed)."
        ),
        'summary': summary,
    })


@mapping_bp.route("/api/index/rebuild", methods=['POST'])
def api_index_rebuild():
    """Destructively rebuild the entire map index (advanced).

    Drops every row from ``indexed_files``, ``waypoints``,
    ``detected_events``, ``trips``, and ``indexing_queue``, then
    re-walks the TeslaCam tree to enqueue every front-camera clip.
    Use only when parsers/thresholds change and existing data needs
    to be reparsed from scratch.
    """
    if not MAPPING_ENABLED:
        return jsonify({
            'success': False,
            'message': 'Mapping is disabled in config.yaml',
        }), 400

    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return jsonify({
            'success': False,
            'message': 'TeslaCam not accessible',
        }), 503

    # Require an explicit confirm flag so a stray POST cannot wipe
    # someone's index. The UI must send this from the confirmation
    # dialog.
    data = request.get_json(silent=True) or {}
    if not data.get('confirm'):
        return jsonify({
            'success': False,
            'message': 'Confirmation required (set confirm=true).',
        }), 400

    import sqlite3
    from services.mapping_service import boot_catchup_scan, clear_all_queue
    from services import indexing_worker

    # Pause the worker for the destructive sweep so it can't be
    # mid-INSERT into a table we're about to wipe. If the worker
    # can't pause within 30s, abort — the user can try again.
    paused = indexing_worker.pause_worker(timeout=30.0)
    if not paused:
        # Clear the pause flag so the worker keeps running.
        indexing_worker.resume_worker()
        return jsonify({
            'success': False,
            'message': (
                'Indexer is busy parsing a clip. Please try again in '
                'a few seconds.'
            ),
        }), 503
    try:
        with sqlite3.connect(MAPPING_DB_PATH) as conn:
            conn.execute("DELETE FROM waypoints")
            conn.execute("DELETE FROM detected_events")
            conn.execute("DELETE FROM trips")
            conn.execute("DELETE FROM indexed_files")
            conn.commit()
        cleared = clear_all_queue(MAPPING_DB_PATH)
        summary = boot_catchup_scan(
            MAPPING_DB_PATH, teslacam_path, source='manual',
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Rebuild failed: %s", e)
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        indexing_worker.resume_worker()

    return jsonify({
        'success': True,
        'message': (
            f"Index rebuild started: cleared queue ({cleared} pending), "
            f"enqueued {summary['enqueued']} clip(s) for re-parsing."
        ),
        'summary': summary,
    })


@mapping_bp.route("/api/index/cancel", methods=['POST'])
def api_index_cancel():
    """Cancel any pending queue items.

    The currently-claimed file (if any) is allowed to finish so
    in-flight work isn't wasted. Useful if a user accidentally
    triggered a rebuild on a Pi with thousands of clips.
    """
    from services.mapping_service import clear_pending_queue
    try:
        n = clear_pending_queue(MAPPING_DB_PATH)
    except Exception as e:  # noqa: BLE001
        logger.error("Cancel failed: %s", e)
        return jsonify({'success': False, 'message': str(e)}), 500
    return jsonify({
        'success': True,
        'message': f"Cleared {n} queued item(s).",
    })


@mapping_bp.route("/api/index/diagnose")
def api_index_diagnose():
    """Diagnose SEI parsing on sample videos for troubleshooting."""
    from services.mapping_service import diagnose_video

    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return jsonify({'error': 'TeslaCam not accessible'}), 503

    max_videos = request.args.get('max', 3, type=int)
    max_videos = min(max_videos, 10)  # Cap at 10

    try:
        result = diagnose_video(teslacam_path, max_videos=max_videos)
        return jsonify(result)
    except Exception as e:
        logger.error("Diagnosis failed: %s", e)
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Driving Stats & Event Analytics APIs
# ---------------------------------------------------------------------------

@mapping_bp.route("/api/driving-stats")
def api_driving_stats():
    """Get driving behavior statistics."""
    from services.mapping_service import get_driving_stats
    try:
        return jsonify(get_driving_stats(MAPPING_DB_PATH))
    except Exception as e:
        logger.error("Failed to get driving stats: %s", e)
        return jsonify({'error': str(e)}), 500


@mapping_bp.route("/api/event-charts")
def api_event_charts():
    """Get event data formatted for Chart.js."""
    from services.mapping_service import get_event_chart_data
    try:
        return jsonify(get_event_chart_data(MAPPING_DB_PATH))
    except Exception as e:
        logger.error("Failed to get event chart data: %s", e)
        return jsonify({'error': str(e)}), 500


@mapping_bp.route("/api/sentry-events")
def api_sentry_events():
    """Return all detected events (sentry, saved, and driving) — DB only.

    Performance: this endpoint used to enrich each event with a filesystem
    call to ``get_event_details()``. With 200 events that meant 200
    directory listings + JSON parses + per-file ``stat()`` calls inline,
    which made the map page panel sluggish and held the geo-index
    connection open for several hundred ms. The list now returns DB-only
    data; the client lazily fetches per-event details via
    :func:`api_event_details` for cards as they scroll into view.
    """
    from services.mapping_service import query_events

    try:
        # Fetch ALL detected events — sentry, saved, and driving events
        # (hard_acceleration, sharp_turn, fsd_disengage, harsh_brake, etc.)
        events = query_events(MAPPING_DB_PATH, limit=200)
        # Sort by timestamp descending (most recent first)
        events.sort(key=lambda e: e.get('timestamp', ''), reverse=True)

        enriched = []
        for ev in events:
            vp = ev.get('video_path', '')
            # Normalize path: handle both relative (RecentClips/...) and
            # absolute-like (../../../../home/pi/ArchivedClips/...) paths
            if 'ArchivedClips' in vp:
                basename = vp.rsplit('/', 1)[-1] if '/' in vp else vp
                source_folder = 'ArchivedClips'
                event_folder = ''
            else:
                parts = vp.replace('\\', '/').split('/')
                source_folder = parts[0] if parts else ''
                event_folder = parts[1] if len(parts) > 2 else ''

            result = dict(ev)
            result['event_folder'] = event_folder
            result['source_folder'] = source_folder
            # Provide a clean video_path for the frontend
            if source_folder == 'ArchivedClips':
                basename = vp.rsplit('/', 1)[-1] if '/' in vp else vp
                result['video_path'] = f'ArchivedClips/{basename}'
            enriched.append(result)

        return jsonify({'events': enriched})
    except Exception as e:
        logger.error("Failed to get sentry events: %s", e)
        return jsonify({'error': str(e)}), 500


@mapping_bp.route("/api/event-details/<folder>/<event_name>")
def api_event_details(folder, event_name):
    """Lazy filesystem details for a single sentry/saved event card.

    Called by the map-page event panel as cards scroll into view, so the
    initial list render isn't blocked by per-event ``os.listdir`` /
    ``os.stat`` work. Returns ``clip_count``, ``camera_count``, and
    ``size_mb`` — the three fields the panel surfaces.
    """
    from services.video_service import get_event_details

    folder = os.path.basename(folder)
    event_name = os.path.basename(event_name)

    teslacam = get_teslacam_path()
    if not teslacam:
        return jsonify({'error': 'TeslaCam not accessible'}), 503

    if folder == 'ArchivedClips':
        try:
            from config import ARCHIVE_DIR, ARCHIVE_ENABLED
            folder_path = ARCHIVE_DIR if ARCHIVE_ENABLED else None
        except ImportError:
            folder_path = None
    else:
        folder_path = os.path.join(teslacam, folder)

    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({'error': f'Folder not found: {folder}'}), 404

    try:
        details = get_event_details(folder_path, event_name)
    except Exception as e:
        logger.warning("Failed to get details for %s/%s: %s",
                       folder, event_name, e)
        return jsonify({'error': 'Failed to read event details'}), 500

    if not details:
        return jsonify({'clip_count': 0, 'camera_count': 0, 'size_mb': 0})

    cam_count = len([
        v for v in (details.get('camera_videos') or {}).values() if v
    ])
    return jsonify({
        'clip_count': len(details.get('clips') or []),
        'camera_count': cam_count,
        'size_mb': details.get('size_mb', 0),
    })


@mapping_bp.route("/api/event-clips/<folder>/<event_name>")
def api_event_clips(folder, event_name):
    """Get clip filenames for an event folder. Used by the overlay player."""
    folder = os.path.basename(folder)
    event_name = os.path.basename(event_name)

    teslacam = get_teslacam_path()
    if not teslacam:
        return jsonify({'error': 'TeslaCam not accessible'}), 503

    # ArchivedClips lives on SD card, not under TeslaCam
    if folder == 'ArchivedClips':
        try:
            from config import ARCHIVE_DIR, ARCHIVE_ENABLED
            if not ARCHIVE_ENABLED:
                return jsonify({'error': 'Archive not enabled'}), 404
            folder_path = ARCHIVE_DIR
        except ImportError:
            return jsonify({'error': 'Archive not configured'}), 404
    else:
        folder_path = os.path.join(teslacam, folder)

    if not os.path.isdir(folder_path):
        return jsonify({'error': f'Folder not found: {folder}'}), 404

    # Event-based folders (SavedClips, SentryClips)
    event_path = os.path.join(folder_path, event_name)
    if os.path.isdir(event_path):
        try:
            clips = sorted([
                f for f in os.listdir(event_path)
                if f.endswith('.mp4') and '-front' in f
            ])
        except OSError:
            clips = []

        clip_paths = [f'{folder}/{event_name}/{c}' for c in clips]
        first_front = clips[0] if clips else ''
        return jsonify({
            'folder': folder,
            'event': event_name,
            'structure': 'events',
            'first_front': first_front,
            'front_clips': clip_paths,
        })

    # Flat folder (RecentClips, ArchivedClips) — session-based
    flat_file = os.path.join(folder_path, f'{event_name}-front.mp4')
    if not os.path.isfile(flat_file):
        # Before giving up, check if the file was archived (RecentClips → ArchivedClips)
        if folder != 'ArchivedClips':
            try:
                from config import ARCHIVE_DIR, ARCHIVE_ENABLED
                if ARCHIVE_ENABLED:
                    archive_file = os.path.join(ARCHIVE_DIR, f'{event_name}-front.mp4')
                    if os.path.isfile(archive_file):
                        clip_path = f'ArchivedClips/{event_name}-front.mp4'
                        return jsonify({
                            'folder': 'ArchivedClips',
                            'event': event_name,
                            'structure': 'flat',
                            'first_front': f'{event_name}-front.mp4',
                            'front_clips': [clip_path],
                        })
            except ImportError:
                pass

        return jsonify({
            'error': 'Video file no longer exists. Tesla may have overwritten it. Try re-indexing.',
            'folder': folder,
            'event': event_name,
            'front_clips': [],
        }), 404

    clip_path = f'{folder}/{event_name}-front.mp4'
    return jsonify({
        'folder': folder,
        'event': event_name,
        'structure': 'flat',
        'first_front': f'{event_name}-front.mp4',
        'front_clips': [clip_path],
    })
