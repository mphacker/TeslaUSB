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
    boombox_bp,
    wraps_bp,
    license_plates_bp,
    media_bp,
    analytics_bp,
    mapping_bp,
    cleanup_bp,
    api_bp,
    fsck_bp,
    captive_portal_bp,
    catch_all_redirect,
    cloud_archive_bp,
    live_events_bp,
    archive_queue_bp,
)

app.register_blueprint(mapping_bp)
app.register_blueprint(mode_control_bp)
app.register_blueprint(videos_bp)
app.register_blueprint(lock_chimes_bp)
app.register_blueprint(light_shows_bp)
app.register_blueprint(music_bp)
app.register_blueprint(boombox_bp)
app.register_blueprint(wraps_bp)
app.register_blueprint(license_plates_bp)
app.register_blueprint(media_bp)
app.register_blueprint(analytics_bp)
app.register_blueprint(cleanup_bp)
app.register_blueprint(api_bp)
app.register_blueprint(fsck_bp)
app.register_blueprint(cloud_archive_bp)
app.register_blueprint(live_events_bp)
app.register_blueprint(archive_queue_bp)
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

    # Phase 2b (issue #76): the legacy ``start_archive_timer`` periodic
    # thread is gone. The new flow is queue-driven: ``archive_producer``
    # enqueues into ``archive_queue``, and ``archive_worker`` drains
    # the queue one file at a time. Both are started below, after the
    # file watcher is wired so the worker's `wake()` from the producer
    # callback path lands cleanly.

    # Start file watcher for new video detection. The callback enqueues
    # individual paths into the indexing_queue table; the indexing
    # worker (started below) drains the queue one file at a time.
    try:
        from services.file_watcher_service import (
            start_watcher, register_callback, register_delete_callback,
            register_event_json_callback, register_archive_callback,
        )
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
            try:
                from config import MAPPING_ENABLED, MAPPING_DB_PATH
                if MAPPING_ENABLED:
                    def _on_new_videos(file_paths):
                        from services.mapping_service import (
                            enqueue_many_for_indexing,
                        )
                        items = [(p, None) for p in file_paths if p]
                        if items:
                            enqueue_many_for_indexing(
                                MAPPING_DB_PATH, items, source='watcher',
                            )

                    def _on_deleted_videos(file_paths):
                        # Mirror deletes immediately so the map page
                        # doesn't keep showing trips/events for clips
                        # the user (or Tesla) just removed.
                        from services.mapping_service import (
                            purge_deleted_videos,
                        )
                        try:
                            purge_deleted_videos(
                                MAPPING_DB_PATH,
                                deleted_paths=list(file_paths),
                            )
                        except Exception as e:
                            print(f"Warning: purge_deleted_videos failed: {e}")

                    register_callback(_on_new_videos)
                    register_delete_callback(_on_deleted_videos)
                    print("File watcher → indexing queue producer registered")
            except Exception as e:
                print(f"Warning: Failed to register watcher callbacks: {e}")

            # Live Event Sync producer: enqueue Sentry/Saved events the
            # moment Tesla writes event.json. Independent of the
            # indexing callback above; both fire from the same inotify
            # watcher with no extra file descriptors.
            try:
                from config import LIVE_EVENT_SYNC_ENABLED
                if LIVE_EVENT_SYNC_ENABLED:
                    def _on_new_event_json(file_paths):
                        from services.live_event_sync_service import (
                            enqueue_event_json,
                        )
                        try:
                            enqueue_event_json(list(file_paths))
                        except Exception as e:
                            print(f"Warning: LES enqueue failed: {e}")

                    register_event_json_callback(_on_new_event_json)
                    print("File watcher → Live Event Sync producer registered")
            except Exception as e:
                print(f"Warning: Failed to register LES watcher callback: {e}")

            # Archive queue producer (issue #76 Phase 2a): mirror the
            # mp4 callback into the new archive_queue table. Phase 2a
            # is producer-only — entries accumulate, no worker drains
            # them until Phase 2b. Independent enable flag so existing
            # installs that don't want the queue can disable it cleanly.
            try:
                from config import ARCHIVE_QUEUE_ENABLED
                if ARCHIVE_QUEUE_ENABLED:
                    def _on_new_videos_for_archive(file_paths):
                        from services.archive_queue import (
                            enqueue_many_for_archive,
                        )
                        try:
                            enqueue_many_for_archive(list(file_paths))
                        except Exception as e:
                            print(
                                "Warning: archive_queue enqueue failed: "
                                f"{e}"
                            )

                    register_archive_callback(_on_new_videos_for_archive)
                    print("File watcher → archive_queue producer registered")
            except Exception as e:
                print(
                    "Warning: Failed to register archive_queue watcher "
                    f"callback: {e}"
                )

            start_watcher(watch_paths)
            print(f"File watcher started for {len(watch_paths)} paths")
    except Exception as e:
        print(f"Warning: Failed to start file watcher: {e}")

    # Start the indexing worker (single low-priority thread that drains
    # indexing_queue). This replaces the old "trigger_auto_index" full
    # filesystem walk that used to run on startup, on mode-switch, and
    # on every WiFi connect — those triggers caused the constantly-
    # flashing "Indexing…" banner. The worker only shows the banner
    # while it's actively parsing one specific file.
    try:
        from config import MAPPING_ENABLED, MAPPING_DB_PATH
        if MAPPING_ENABLED:
            from services.video_service import get_teslacam_path
            from services import indexing_worker
            from services.mapping_service import boot_catchup_scan
            tc = get_teslacam_path()
            if tc:
                # Catch-up scan first: any clip on disk that isn't in
                # indexed_files becomes a new queue row. Cheap (no
                # video parsing); takes tens of milliseconds even on a
                # full SD card. Worker picks them up afterwards.
                try:
                    summary = boot_catchup_scan(MAPPING_DB_PATH, tc)
                    print(
                        "Boot catch-up scan: "
                        f"scanned={summary['scanned']}, "
                        f"already_indexed={summary['already_indexed']}, "
                        f"enqueued={summary['enqueued']}"
                    )
                except Exception as e:
                    print(f"Warning: boot catch-up scan failed: {e}")
                indexing_worker.start_worker(MAPPING_DB_PATH, tc)
                print("Indexing worker started")
                # Independent safety net for stale geodata rows. Runs
                # ~daily with jitter; cheap (one os.path.isfile per
                # indexed_files row) and only logs the count it cleans.
                from services.mapping_service import (
                    start_daily_stale_scan,
                )
                from services.video_service import (
                    get_teslacam_path as _get_tc,
                )
                start_daily_stale_scan(MAPPING_DB_PATH, _get_tc)
                print("Daily stale scan scheduled")
    except Exception as e:
        print(f"Warning: Failed to start indexing worker: {e}")

    # Archive queue producer thread (issue #76 Phase 2a). Mirrors the
    # indexing worker's lifecycle: starts after the watcher is
    # registered so the boot catch-up scan and the every-60-s rescan
    # observe the same TeslaCam root. Failure here must never take
    # down gadget_web.
    try:
        from config import (
            ARCHIVE_QUEUE_ENABLED,
            ARCHIVE_QUEUE_RESCAN_INTERVAL_SECONDS,
            ARCHIVE_QUEUE_BOOT_CATCHUP_ENABLED,
            MAPPING_DB_PATH as _ARCHIVE_QUEUE_DB,
        )
        if ARCHIVE_QUEUE_ENABLED:
            from services.video_service import get_teslacam_path
            from services import archive_producer
            tc = get_teslacam_path()
            if tc:
                archive_producer.start_producer(
                    tc,
                    db_path=_ARCHIVE_QUEUE_DB,
                    rescan_interval_seconds=(
                        ARCHIVE_QUEUE_RESCAN_INTERVAL_SECONDS
                    ),
                    boot_catchup_enabled=(
                        ARCHIVE_QUEUE_BOOT_CATCHUP_ENABLED
                    ),
                )
                print("Archive queue producer started (Phase 2a)")
    except Exception as e:
        print(f"Warning: Failed to start archive queue producer: {e}")

    # Archive queue worker thread (issue #76 Phase 2b). Drains
    # ``archive_queue`` one file at a time, copying USB-side clips
    # into ``ARCHIVE_DIR`` and enqueueing them into the indexer queue.
    # The producer above is the only thing that puts rows into the
    # queue; this worker is the only thing that takes them out. The
    # legacy ``video_archive_service`` periodic timer has been removed
    # in favor of this pair.
    try:
        from config import (
            ARCHIVE_QUEUE_ENABLED,
            ARCHIVE_DIR,
            MAPPING_DB_PATH as _ARCHIVE_WORKER_DB,
        )
        if ARCHIVE_QUEUE_ENABLED:
            from services.video_service import get_teslacam_path
            from services import archive_worker
            tc = get_teslacam_path()
            archive_worker.start_worker(
                _ARCHIVE_WORKER_DB,
                ARCHIVE_DIR,
                teslacam_root=tc,
            )
            print("Archive queue worker started (Phase 2b)")

            # Phase 2c: archive watchdog + retention prune. Single
            # daemon thread that observes the queue/worker, exposes
            # ``/api/archive/status``, and runs the daily retention
            # prune on ``ArchivedClips``. Pure local-FS observer — it
            # never touches the USB gadget.
            try:
                from services import archive_watchdog
                archive_watchdog.start_watchdog(
                    _ARCHIVE_WORKER_DB, ARCHIVE_DIR,
                )
                print("Archive watchdog started (Phase 2c)")
            except Exception as e:  # noqa: BLE001
                print(
                    f"Warning: Failed to start archive watchdog: {e}"
                )
    except Exception as e:
        print(f"Warning: Failed to start archive queue worker: {e}")

    # Live Event Sync worker: starts BEFORE cloud_archive auto-trigger so
    # any persistent LES queue (from a prior reboot/WiFi outage) gets
    # the priority it's contractually owed. The trigger_auto_sync()
    # call below will then see ready LES work and skip — letting LES
    # drain first. Worker thread blocks on threading.Event.wait() when
    # idle (< 0.1% CPU baseline).
    try:
        from config import LIVE_EVENT_SYNC_ENABLED
        if LIVE_EVENT_SYNC_ENABLED:
            from services.live_event_sync_service import start as _les_start
            if _les_start():
                print("Live Event Sync worker started")
    except Exception as e:
        # LES failure must NEVER take down gadget_web. Log and continue.
        print(f"Warning: Failed to start Live Event Sync worker: {e}")

    # Auto-start cloud sync if WiFi is already connected and provider is configured.
    # The dispatcher only fires on WiFi connect events — if the Pi boots into WiFi
    # (or the service restarts while on WiFi), sync would never start without this.
    # NOTE: trigger_auto_sync() consults has_ready_live_event_work() and skips
    # when LES has work, so the LES start above takes effect even on the very
    # first dispatcher fire.
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

    # NOTE: ``MAPPING_INDEX_ON_STARTUP`` is intentionally no longer
    # honored. The boot catch-up scan above has the same effect with
    # none of the cost (no full re-parse of clips already indexed).
    # The config flag is kept in config.yaml for now to avoid breaking
    # existing installs; it just becomes a no-op.
    try:
        from config import (
            MAPPING_INDEX_ON_STARTUP, MAPPING_INDEX_ON_MODE_SWITCH,
        )
        if MAPPING_INDEX_ON_STARTUP or MAPPING_INDEX_ON_MODE_SWITCH:
            print(
                "INFO: mapping.index_on_startup / mapping.index_on_mode_switch "
                "are deprecated and now have no effect. The persistent indexing "
                "queue + worker handle this automatically — to force a re-scan, "
                "use the 'Scan for new clips' button on the map page."
            )
    except Exception:
        pass

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
