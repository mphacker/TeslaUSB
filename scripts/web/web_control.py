#!/usr/bin/env python3
"""
USB Gadget Web Control Interface

A Flask web application for controlling USB gadget modes.
Organized using blueprints for better maintainability.
"""

import logging
import sys

from flask import Flask
import os

# Configure logging to stderr (captured by systemd journal)
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:%(name)s:%(message)s',
    stream=sys.stderr,
)

# Import configuration
from config import SECRET_KEY, WEB_PORT, GADGET_DIR, MAX_UPLOAD_SIZE_MB, MAX_UPLOAD_CHUNK_MB

# Flask app initialization
app = Flask(__name__)
app.secret_key = SECRET_KEY

# Upload limits (protect RAM-constrained devices)
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_SIZE_MB * 1024 * 1024
app.config['MAX_FORM_MEMORY_SIZE'] = MAX_UPLOAD_CHUNK_MB * 1024 * 1024

# Production optimizations
app.config['USE_X_SENDFILE'] = False  # Disabled - requires nginx/apache
app.config['TEMPLATES_AUTO_RELOAD'] = False  # Disable template watching - saves memory

# Register blueprints
from blueprints import (
    mode_control_bp,
    videos_bp,
    lock_chimes_bp,
    light_shows_bp,
    music_bp,
    wraps_bp,
    media_bp,
    analytics_bp,
    mapping_bp,
    cleanup_bp,
    api_bp,
    fsck_bp,
    captive_portal_bp,
    catch_all_redirect,
    cloud_archive_bp,
    boombox_bp,
)

app.register_blueprint(mapping_bp)
app.register_blueprint(mode_control_bp)
app.register_blueprint(videos_bp)
app.register_blueprint(lock_chimes_bp)
app.register_blueprint(light_shows_bp)
app.register_blueprint(music_bp)
app.register_blueprint(boombox_bp)
app.register_blueprint(wraps_bp)
app.register_blueprint(media_bp)
app.register_blueprint(analytics_bp)
app.register_blueprint(cleanup_bp)
app.register_blueprint(api_bp)
app.register_blueprint(fsck_bp)
app.register_blueprint(cloud_archive_bp)
# Register captive portal blueprint LAST to avoid conflicting with other routes
app.register_blueprint(captive_portal_bp)


# Global error handler for upload space exhaustion
@app.errorhandler(OSError)
def handle_os_error(e):
    """Catch OSError (e.g., temp space exhaustion during large uploads)."""
    import errno
    from flask import request, jsonify, flash, redirect
    if e.errno == errno.ENOSPC:
        msg = "Upload too large for available memory. Try uploading fewer or smaller files."
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": False, "error": msg}), 413
        flash(msg, "error")
        return redirect(request.referrer or '/')
    raise e  # Re-raise non-space errors


# Serve tile cache service worker from root scope (SW scope must match serving path)
@app.route('/tile-cache-sw.js')
def tile_cache_service_worker():
    from flask import send_from_directory
    return send_from_directory(
        app.static_folder, 'tile-cache-sw.js',
        mimetype='application/javascript',
        max_age=86400,
    )

# Add catch-all route for captive portal (must be last)
@app.route('/<path:path>')
def wildcard_redirect(path):
    result = catch_all_redirect(path)
    if result:
        return result
    # If catch_all_redirect returns None, let Flask handle it normally (404)
    from flask import abort
    abort(404)


if __name__ == "__main__":
    print(f"Starting Tesla USB Gadget Web Control")
    print(f"Gadget directory: {GADGET_DIR}")
    print(f"Access the interface at: http://0.0.0.0:{WEB_PORT}/")

    # Video indexing is triggered by WiFi connect (NetworkManager dispatcher)
    # No boot-time indexing needed — the dispatcher handles it automatically

    # Start RecentClips archive timer (copies clips to SD card before Tesla deletes them)
    try:
        from services.video_archive_service import start_archive_timer
        start_archive_timer()
    except Exception as e:
        print(f"Warning: Failed to start archive timer: {e}")

    # Start file watcher for new video detection (triggers indexing + cloud sync)
    try:
        from services.file_watcher_service import start_watcher, register_callback
        watch_paths = []
        # Watch TeslaCam on USB (RO mount)
        from config import RO_MNT_DIR
        teslacam_ro = os.path.join(RO_MNT_DIR, 'part1-ro', 'TeslaCam')
        if os.path.isdir(teslacam_ro):
            watch_paths.append(teslacam_ro)
        # Watch ArchivedClips on SD card
        try:
            from config import ARCHIVE_DIR, ARCHIVE_ENABLED
            if ARCHIVE_ENABLED and os.path.isdir(ARCHIVE_DIR):
                watch_paths.append(ARCHIVE_DIR)
        except ImportError:
            pass
        if watch_paths:
            # Register auto-indexing callback so new videos are indexed
            # without manual button clicks or WiFi connect events.
            try:
                from config import (
                    MAPPING_ENABLED, MAPPING_DB_PATH,
                    MAPPING_SAMPLE_RATE, MAPPING_EVENT_THRESHOLDS,
                    MAPPING_TRIP_GAP_MINUTES,
                )
                from services.video_service import get_teslacam_path
                if MAPPING_ENABLED:
                    def _on_new_videos(file_paths):
                        from services.mapping_service import trigger_auto_index
                        tc = get_teslacam_path()
                        if tc:
                            trigger_auto_index(
                                db_path=MAPPING_DB_PATH,
                                teslacam_path=tc,
                                sample_rate=MAPPING_SAMPLE_RATE,
                                thresholds=MAPPING_EVENT_THRESHOLDS,
                                trip_gap_minutes=MAPPING_TRIP_GAP_MINUTES,
                            )
                    register_callback(_on_new_videos)
                    print("File watcher → auto-indexing callback registered")
            except Exception as e:
                print(f"Warning: Failed to register indexing callback: {e}")

            start_watcher(watch_paths)
            print(f"File watcher started for {len(watch_paths)} paths")
    except Exception as e:
        print(f"Warning: Failed to start file watcher: {e}")

    # Auto-start cloud sync if WiFi is already connected and provider is configured.
    # The dispatcher only fires on WiFi connect events — if the Pi boots into WiFi
    # (or the service restarts while on WiFi), sync would never start without this.
    try:
        from config import (
            CLOUD_ARCHIVE_ENABLED, CLOUD_ARCHIVE_PROVIDER,
            CLOUD_ARCHIVE_DB_PATH, CLOUD_PROVIDER_CREDS_PATH,
        )
        if (CLOUD_ARCHIVE_ENABLED and CLOUD_ARCHIVE_PROVIDER
                and os.path.isfile(CLOUD_PROVIDER_CREDS_PATH)):
            from services.cloud_archive_service import trigger_auto_sync
            from services.video_service import get_teslacam_path
            teslacam = get_teslacam_path()
            if teslacam:
                trigger_auto_sync(teslacam, CLOUD_ARCHIVE_DB_PATH)
                print("Cloud auto-sync triggered on startup")
    except Exception as e:
        print(f"Warning: Cloud auto-sync startup failed: {e}")

    # Auto-index on startup if enabled — catches videos that arrived while
    # the service was down or between WiFi connect events.
    try:
        from config import (
            MAPPING_ENABLED, MAPPING_INDEX_ON_STARTUP, MAPPING_DB_PATH,
            MAPPING_SAMPLE_RATE, MAPPING_EVENT_THRESHOLDS,
            MAPPING_TRIP_GAP_MINUTES,
        )
        if MAPPING_ENABLED and MAPPING_INDEX_ON_STARTUP:
            from services.mapping_service import trigger_auto_index
            from services.video_service import get_teslacam_path
            teslacam = get_teslacam_path()
            if teslacam:
                trigger_auto_index(
                    db_path=MAPPING_DB_PATH,
                    teslacam_path=teslacam,
                    sample_rate=MAPPING_SAMPLE_RATE,
                    thresholds=MAPPING_EVENT_THRESHOLDS,
                    trip_gap_minutes=MAPPING_TRIP_GAP_MINUTES,
                )
                print("Startup geo-indexing triggered")
    except Exception as e:
        print(f"Warning: Startup indexing failed: {e}")

    # Try to use Waitress if available, otherwise fall back to Flask dev server
    try:
        from waitress import serve
        print("Using Waitress production server")
        # 4 threads for Pi Zero 2 W — one extra for API polling while sync runs
        serve(app, host="0.0.0.0", port=WEB_PORT, threads=4, channel_timeout=120,
              send_bytes=4194304)  # 4MB send buffer for better video streaming
    except ImportError:
        print("Waitress not available, using Flask development server")
        print("WARNING: Flask dev server is slow for large files. Install waitress: pip3 install waitress")
        app.run(host="0.0.0.0", port=WEB_PORT, debug=False, threaded=True)
