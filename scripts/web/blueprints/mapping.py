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

mapping_bp = Blueprint('mapping', __name__, url_prefix='/map')


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
