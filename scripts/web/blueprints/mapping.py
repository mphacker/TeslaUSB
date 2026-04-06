"""Blueprint for map-based video browser with GPS tracking and event detection."""

import os
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
                            bbox=bbox, date_from=date_from, date_to=date_to)
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

    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    event_type = request.args.get('type')
    severity = request.args.get('severity')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

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
                              bbox=bbox, date_from=date_from, date_to=date_to)
        return jsonify({'events': events})
    except Exception as e:
        logger.error("Failed to query events: %s", e)
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
    """Get current indexer status."""
    from services.mapping_service import get_indexer_status
    return jsonify(get_indexer_status())


@mapping_bp.route("/api/index/trigger", methods=['POST'])
def api_index_trigger():
    """Manually trigger the geo-indexer."""
    from services.mapping_service import start_indexer

    if not MAPPING_ENABLED:
        return jsonify({'success': False, 'message': 'Mapping is disabled in config.yaml'}), 400

    teslacam_path = get_teslacam_path()
    if not teslacam_path:
        return jsonify({'success': False, 'message': 'TeslaCam not accessible'}), 503

    success, message = start_indexer(
        db_path=MAPPING_DB_PATH,
        teslacam_path=teslacam_path,
        sample_rate=MAPPING_SAMPLE_RATE,
        thresholds=MAPPING_EVENT_THRESHOLDS,
        trip_gap_minutes=MAPPING_TRIP_GAP_MINUTES,
    )
    return jsonify({'success': success, 'message': message})


@mapping_bp.route("/api/index/cancel", methods=['POST'])
def api_index_cancel():
    """Cancel the running indexer."""
    from services.mapping_service import cancel_indexer
    success, message = cancel_indexer()
    return jsonify({'success': success, 'message': message})


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
    """Get sentry and saved events enriched with filesystem details."""
    from services.mapping_service import query_events
    from services.video_service import get_event_details

    try:
        # Fetch sentry events
        sentry = query_events(MAPPING_DB_PATH, limit=100, event_type='sentry')
        # Fetch saved events
        saved = query_events(MAPPING_DB_PATH, limit=100, event_type='saved')
        events = sentry + saved
        # Sort combined list by timestamp descending
        events.sort(key=lambda e: e.get('timestamp', ''), reverse=True)

        teslacam = get_teslacam_path()
        enriched = []
        for ev in events:
            vp = ev.get('video_path', '')
            parts = vp.replace('\\', '/').split('/')
            source_folder = parts[0] if parts else ''
            event_folder = parts[1] if len(parts) > 2 else ''

            result = dict(ev)
            result['event_folder'] = event_folder
            result['source_folder'] = source_folder

            if teslacam and event_folder:
                folder_path = os.path.join(teslacam, source_folder)
                try:
                    details = get_event_details(folder_path, event_folder)
                    if details:
                        cam_count = len([
                            v for v in (details.get('camera_videos') or {}).values() if v
                        ])
                        result['clip_count'] = len(details.get('clips') or [])
                        result['camera_count'] = cam_count
                        result['size_mb'] = details.get('size_mb', 0)
                except Exception:
                    pass

            enriched.append(result)

        return jsonify({'events': enriched})
    except Exception as e:
        logger.error("Failed to get sentry events: %s", e)
        return jsonify({'error': str(e)}), 500


@mapping_bp.route("/api/event-clips/<folder>/<event_name>")
def api_event_clips(folder, event_name):
    """Get clip filenames for an event folder. Used by the overlay player."""
    folder = os.path.basename(folder)
    event_name = os.path.basename(event_name)

    teslacam = get_teslacam_path()
    if not teslacam:
        return jsonify({'error': 'TeslaCam not accessible'}), 503

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

    # Flat folder (RecentClips) — session-based
    flat_file = os.path.join(folder_path, f'{event_name}-front.mp4')
    if not os.path.isfile(flat_file):
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
