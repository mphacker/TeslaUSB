"""Flask application factory.

The factory pattern (``create_app(...)``) lets tests construct
isolated app instances against tmpdir-rooted config files, and lets
gunicorn (see ``wsgi.py``) import a callable rather than a
module-level global.

This module does NOT import any blueprint or service at module
scope — those imports happen inside ``create_app`` so the factory
can be unit-tested without dragging the world in (charter
§"Architectural Principles / Dependency inversion").

Logging is configured via ``logging.getLogger(__name__)`` and the
``--log-config`` flag passed to gunicorn at deploy time. We do NOT
call ``logging.basicConfig`` here — that would mutate global logger
state and break tests that import the factory under pytest's own
log handlers (charter §3 "no shortcut globals").
"""

from __future__ import annotations

import atexit
import errno
import logging
import secrets
import threading
import weakref
from typing import TYPE_CHECKING, cast

from flask import Blueprint, Flask, abort, jsonify, request, send_from_directory

from teslausb_web.blueprints._scaffold import build_scaffold_blueprints
from teslausb_web.blueprints.analytics import analytics_bp
from teslausb_web.blueprints.api import api_bp
from teslausb_web.blueprints.boombox import boombox_bp
from teslausb_web.blueprints.captive_portal import captive_portal_bp
from teslausb_web.blueprints.cleanup import cleanup_bp
from teslausb_web.blueprints.cloud_archive import cloud_archive_bp
from teslausb_web.blueprints.jobs import jobs_bp
from teslausb_web.blueprints.license_plates import license_plates_bp
from teslausb_web.blueprints.light_shows import light_shows_bp
from teslausb_web.blueprints.lock_chimes import lock_chimes_bp
from teslausb_web.blueprints.mapping import mapping_bp
from teslausb_web.blueprints.media import media_bp
from teslausb_web.blueprints.music import music_bp
from teslausb_web.blueprints.settings import settings_dashboard_bp
from teslausb_web.blueprints.settings_advanced import settings_bp
from teslausb_web.blueprints.storage_retention import storage_retention_bp
from teslausb_web.blueprints.system_health import system_health_bp
from teslausb_web.blueprints.videos import videos_bp
from teslausb_web.blueprints.wraps import wraps_bp
from teslausb_web.config import WebConfig, load_config
from teslausb_web.services.analytics_service import make_analytics_service
from teslausb_web.services.boombox_service import make_boombox_service
from teslausb_web.services.cache_invalidation import CacheInvalidator
from teslausb_web.services.chime_group_service import make_chime_group_manager
from teslausb_web.services.chime_scheduler import make_chime_scheduler
from teslausb_web.services.cleanup import make_cleanup_service
from teslausb_web.services.cloud_archive import make_cloud_archive_service
from teslausb_web.services.cloud_oauth_service import CloudOAuthService, make_oauth_service
from teslausb_web.services.cloud_rclone_service import CloudRcloneService, make_rclone_service
from teslausb_web.services.jobs_service import CloudSyncAdapterProtocol, make_jobs_service
from teslausb_web.services.license_plate_service import make_license_plate_service
from teslausb_web.services.light_show_service import make_light_show_service
from teslausb_web.services.mapping import make_mapping_service
from teslausb_web.services.mapping.service import MappingService
from teslausb_web.services.music_service import make_music_service
from teslausb_web.services.photo_plate_service import make_photo_plate_service
from teslausb_web.services.samba_service import SambaError, make_samba_service
from teslausb_web.services.samba_watcher import make_samba_watcher
from teslausb_web.services.storage_retention_service import make_storage_retention_service
from teslausb_web.services.system_settings_service import (
    SystemSettings,
    SystemSettingsService,
    make_system_settings_service,
)
from teslausb_web.services.teslafat_client import TeslaFatClient
from teslausb_web.services.video_service import make_video_service
from teslausb_web.services.wifi_service import make_wifi_service
from teslausb_web.services.wrap_service import make_wrap_service

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

# Service-worker filename served at the root scope. Matches v1's
# `/tile-cache-sw.js` URL exactly so the existing client-side cache
# implementation works unchanged after the B-1 cutover.
_TILE_CACHE_SW: str = "tile-cache-sw.js"
_TILE_CACHE_MAX_AGE_SECONDS: int = 86_400

_BYTES_PER_MIB: int = 1024 * 1024
_HTTP_PAYLOAD_TOO_LARGE: int = 413


def create_app(
    config: WebConfig | None = None,
    *,
    config_path: Path | None = None,
    allow_defaults: bool = False,
    extra_blueprints: Iterable[object] = (),
) -> Flask:
    """Build and configure a Flask app.

    Args:
        config: Pre-built ``WebConfig``. When ``None``, calls
            :func:`teslausb_web.config.load_config` with
            ``config_path`` and ``allow_defaults``.
        config_path: Forwarded to ``load_config`` when ``config``
            is ``None``. Ignored otherwise.
        allow_defaults: Forwarded to ``load_config``. Tests pass
            ``True``; the gunicorn entry point passes ``False``.
        extra_blueprints: Optional iterable of Flask blueprints to
            register beyond the standard set. Used by tests to
            register a minimal blueprint and by Phase 5.7+ as each
            real blueprint lands.

    Returns:
        A Flask app whose ``app.config`` reflects the loaded
        TOML config and whose error handlers + standard routes
        (tile-cache service worker, catch-all 404) are wired up.
    """
    cfg = config if config is not None else load_config(config_path, allow_defaults=allow_defaults)

    app = Flask("teslausb_web")

    # Resolve secret key: explicit > generated. An empty key in
    # config is allowed in tests (no real cookies) but in
    # production setup.sh writes a 64-char hex key.
    secret = cfg.web.secret_key
    if not secret:
        if not allow_defaults:
            # Production: a missing secret_key is a deployment
            # error. We log loudly but don't crash — gunicorn
            # would just restart us. Generate a transient key
            # so the process starts; sessions break on restart,
            # which is the desired loud-failure mode.
            logger.error(
                "TESLAUSB_WEB secret_key is empty in %s; generating a transient key. "
                "Sessions will not survive a worker restart. Set [web] secret_key to fix.",
                cfg.source_path,
            )
        secret = secrets.token_hex(32)
    app.secret_key = secret

    app.config["MAX_CONTENT_LENGTH"] = cfg.web.max_upload_mb * _BYTES_PER_MIB
    app.config["MAX_FORM_MEMORY_SIZE"] = cfg.web.max_chunk_mb * _BYTES_PER_MIB
    app.config["USE_X_SENDFILE"] = False
    app.config["TEMPLATES_AUTO_RELOAD"] = False
    # Stash the typed config object so blueprints can read
    # `current_app.config["teslausb_config"]` rather than re-loading.
    app.config["teslausb_config"] = cfg

    _register_error_handlers(app)
    _register_standard_routes(app)
    _register_template_globals(app)
    _register_blueprints(app, extra_blueprints)
    _register_cache_invalidator(app, cfg)
    _register_chime_services(app, cfg)
    _register_light_show_services(app, cfg)
    _register_music_services(app, cfg)
    _register_boombox_services(app, cfg)
    _register_license_plate_services(app, cfg)
    _register_storage_retention_services(app, cfg)
    _register_system_settings_services(app, cfg)
    system_settings = app.extensions.get("system_settings_service")
    if not isinstance(system_settings, SystemSettingsService):
        raise RuntimeError("system_settings_service must be registered before samba services")
    _register_samba_services(app, cfg, _get_cache_invalidator(app), system_settings)
    _register_wifi_services(app, cfg)
    _register_cloud_oauth_services(app, cfg)
    _register_cloud_rclone_services(app, cfg)
    oauth_service = app.extensions.get("cloud_oauth_service")
    rclone_service = app.extensions.get("cloud_rclone_service")
    if not isinstance(oauth_service, CloudOAuthService):
        raise RuntimeError("cloud_oauth_service must be registered before cloud_archive_service")
    if not isinstance(rclone_service, CloudRcloneService):
        raise RuntimeError("cloud_rclone_service must be registered before cloud_archive_service")
    _register_cloud_archive_services(app, cfg, rclone_service, oauth_service)
    if "cloud_archive" not in app.blueprints:
        app.register_blueprint(cloud_archive_bp)
    _register_wrap_services(app, cfg)
    _register_mapping_services(app, cfg)
    _register_cleanup_services(app, cfg)
    _register_analytics_service(app, cfg)
    _register_video_service(app, cfg)
    _register_jobs_service(app)

    logger.info(
        "teslausb_web app created (port=%d, max_upload_mb=%d, samba=%s, source=%s)",
        cfg.web.port,
        cfg.web.max_upload_mb,
        cfg.features.samba_enabled,
        cfg.source_path,
    )
    return app


def _register_error_handlers(app: Flask) -> None:
    """Wire the OSError-ENOSPC handler that v1 had on `web_control.py`."""

    @app.errorhandler(OSError)
    def _handle_os_error(exc: OSError) -> ResponseReturnValue:
        # Mirrors v1: a tmpdir-exhaustion mid-upload should not
        # 500; surface a clean message instead. Other OSErrors
        # bubble up so debugging stays honest.
        if exc.errno != errno.ENOSPC:
            raise exc
        msg = "Upload too large for available memory. Try uploading fewer or smaller files."
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "error": msg}), _HTTP_PAYLOAD_TOO_LARGE
        return msg, _HTTP_PAYLOAD_TOO_LARGE


def _register_standard_routes(app: Flask) -> None:
    """Routes every B-1 deploy needs regardless of which blueprints are loaded."""

    @app.route("/tile-cache-sw.js")
    def _tile_cache_service_worker() -> ResponseReturnValue:
        # Service workers can only control pages within their own
        # scope; the SW must be served from the application root so
        # it can intercept tile requests on any page. Matches v1.
        static_folder = app.static_folder
        if static_folder is None:
            abort(404)
        return send_from_directory(
            static_folder,
            _TILE_CACHE_SW,
            mimetype="application/javascript",
            max_age=_TILE_CACHE_MAX_AGE_SECONDS,
        )

    @app.route("/healthz")
    def _healthz() -> ResponseReturnValue:
        # gunicorn / nginx / systemd-Notify can hit this for liveness.
        # Pure stdlib JSON response so the route works even before
        # any blueprint is registered.
        return jsonify({"status": "ok"}), 200


def _register_blueprints(app: Flask, extras: Iterable[object]) -> None:
    """Register scaffolding blueprints + any extras supplied by tests.

    Phase 5.4 wires placeholder blueprints (`mapping`, `analytics`,
    `media`, `cloud_archive`, `settings`) so `base.html` can render
    via `url_for(...)` before the real per-feature blueprints land
    in Phase 5.7-5.16. Each real blueprint replaces its scaffold
    with the same `name` + endpoint contract.

    `extras` are registered FIRST so tests / future increments can
    pre-register the real blueprint and have the scaffold step
    skip it (Flask raises if a name is registered twice — we
    detect and skip rather than raise).
    """
    registered_names: set[str] = set()
    for bp in extras:
        app.register_blueprint(bp)  # type: ignore[arg-type]
        # Track by Blueprint.name when available so the scaffold
        # step below doesn't double-register.
        bp_name = getattr(bp, "name", None)
        if isinstance(bp_name, str):
            registered_names.add(bp_name)

    # Real B-1 blueprints (Phase 5.7+). Each replaces a scaffold of
    # the same name where one exists; system_health has no scaffold
    # (it is API-only with no URL in base.html).
    real_blueprints: tuple[Blueprint, ...] = (
        system_health_bp,
        lock_chimes_bp,
        light_shows_bp,
        music_bp,
        boombox_bp,
        media_bp,
        license_plates_bp,
        storage_retention_bp,
        settings_dashboard_bp,
        settings_bp,
        captive_portal_bp,
        wraps_bp,
        mapping_bp,
        cleanup_bp,
        analytics_bp,
        videos_bp,
        jobs_bp,
        api_bp,
    )
    for bp in real_blueprints:
        if bp.name in registered_names:
            continue
        app.register_blueprint(bp)
        registered_names.add(bp.name)

    for scaffold_bp in build_scaffold_blueprints():
        if scaffold_bp.name == "cloud_archive":
            continue
        if scaffold_bp.name in registered_names:
            continue
        app.register_blueprint(scaffold_bp)


def _register_cache_invalidator(app: Flask, cfg: WebConfig) -> None:
    """Instantiate one :class:`CacheInvalidator` and stash it on the app.

    Blueprints that mutate Tesla-visible files (chimes, light shows,
    music, wraps, boombox, license-plate images) call
    ``current_app.extensions["cache_invalidator"].schedule()`` after a
    successful write. The singleton lives for the lifetime of the
    gunicorn worker; ``atexit`` ensures pending timers are cancelled
    and any in-flight cycle drains during graceful shutdown so the
    medium-change pipeline never leaves Tesla in a half-cycled state.

    Tests construct apps via ``create_app`` and inherit the invalidator
    for free. The wired command is ``["sudo", str(cache_invalidate_script)]``
    matching the sudoers fragment installed in Phase 4c.2.
    """
    invalidator = CacheInvalidator(
        command=("sudo", str(cfg.paths.cache_invalidate_script)),
    )
    app.extensions["cache_invalidator"] = invalidator
    atexit.register(invalidator.shutdown)


def _get_cache_invalidator(app: Flask) -> CacheInvalidator:
    invalidator = app.extensions.get("cache_invalidator")
    if not isinstance(invalidator, CacheInvalidator):
        raise RuntimeError("cache_invalidator extension is not configured")
    return invalidator


def _register_chime_services(app: Flask, cfg: WebConfig) -> None:
    """Construct the chime group manager and scheduler once at app startup."""
    app.extensions["chime_group_manager"] = make_chime_group_manager(cfg)
    app.extensions["chime_scheduler"] = make_chime_scheduler(cfg)


def _register_light_show_services(app: Flask, cfg: WebConfig) -> None:
    """Construct the light-show service once at app startup."""
    app.extensions["light_show_service"] = make_light_show_service(cfg)


def _register_music_services(app: Flask, cfg: WebConfig) -> None:
    """Construct the music service once at app startup."""
    app.extensions["music_service"] = make_music_service(cfg)


def _register_boombox_services(app: Flask, cfg: WebConfig) -> None:
    """Construct the boombox service once at app startup."""
    app.extensions["boombox_service"] = make_boombox_service(
        cfg,
        schedule_cache_invalidation=_get_cache_invalidator(app).schedule,
    )


def _register_license_plate_services(app: Flask, cfg: WebConfig) -> None:
    """Construct the tracked license-plate service and photo-plate service."""
    app.extensions["license_plate_service"] = make_license_plate_service(cfg)
    app.extensions["photo_plate_service"] = make_photo_plate_service(cfg)


def _register_storage_retention_services(app: Flask, cfg: WebConfig) -> None:
    """Construct the storage-retention service once at app startup."""
    app.extensions["storage_retention_service"] = make_storage_retention_service(cfg)


def _register_system_settings_services(app: Flask, cfg: WebConfig) -> None:
    """Construct the advanced-settings service and shared IPC client once."""
    app.extensions["system_settings_service"] = make_system_settings_service(cfg)
    app.extensions["teslafat_client"] = TeslaFatClient(cfg.paths.ipc_socket)


def _register_samba_services(
    app: Flask,
    cfg: WebConfig,
    cache_invalidator: CacheInvalidator,
    system_settings: SystemSettingsService,
) -> None:
    """Construct the Samba daemon + watcher pair and bind them to settings changes."""
    samba_service = make_samba_service(cfg)
    samba_watcher = make_samba_watcher(cfg, cache_invalidator)
    app.extensions["samba_service"] = samba_service
    app.extensions["samba_watcher"] = samba_watcher

    state_lock = threading.RLock()
    active_enabled = False

    def _disable() -> None:
        nonlocal active_enabled
        with state_lock:
            if not active_enabled:
                return
        watcher_stopped = samba_watcher.shutdown(timeout=5.0)
        if not watcher_stopped:
            logger.warning("Samba watcher did not stop cleanly within timeout")
        try:
            samba_service.stop(timeout=5.0)
        except SambaError:
            logger.exception("Failed to stop Samba service")
        with state_lock:
            active_enabled = False

    def _enable() -> None:
        nonlocal active_enabled
        with state_lock:
            if active_enabled:
                return
        try:
            samba_service.start()
            samba_watcher.start()
        except SambaError:
            logger.exception("Failed to start Samba service")
            samba_watcher.shutdown(timeout=1.0)
            try:
                samba_service.stop(timeout=1.0)
            except SambaError:
                logger.exception("Failed to roll back Samba service startup")
            return
        with state_lock:
            active_enabled = True

    def _apply(settings: SystemSettings) -> None:
        if settings.samba_enabled:
            _enable()
        else:
            _disable()

    unsubscribe = system_settings.subscribe(_apply)
    _apply(system_settings.get_settings())

    def _shutdown() -> None:
        unsubscribe()
        _disable()

    atexit.register(_shutdown)
    app.extensions["samba_service_finalizer"] = weakref.finalize(app, _shutdown)


def _register_wifi_services(app: Flask, cfg: WebConfig) -> None:
    """Construct the Wi-Fi / captive-portal service once at app startup."""
    app.extensions["wifi_service"] = make_wifi_service(cfg)


def _register_cloud_oauth_services(app: Flask, cfg: WebConfig) -> None:
    """Construct the cloud OAuth service once at app startup."""
    app.extensions["cloud_oauth_service"] = make_oauth_service(cfg)


def _register_cloud_rclone_services(app: Flask, cfg: WebConfig) -> None:
    """Construct the rclone wrapper once and inject the OAuth service."""
    oauth_service = app.extensions.get("cloud_oauth_service")
    if not isinstance(oauth_service, CloudOAuthService):
        raise RuntimeError("cloud_oauth_service must be registered before cloud_rclone_service")
    app.extensions["cloud_rclone_service"] = make_rclone_service(cfg, oauth_service)


def _register_cloud_archive_services(
    app: Flask,
    cfg: WebConfig,
    rclone_svc: CloudRcloneService,
    oauth_svc: CloudOAuthService,
) -> None:
    """Construct the cloud_archive service and register lifecycle hooks."""
    archive_svc = make_cloud_archive_service(cfg, rclone_svc, oauth_svc)
    app.extensions["cloud_archive_service"] = archive_svc
    if cfg.features.cloud_archive_enabled:
        archive_svc.start()

    @app.teardown_appcontext
    def _shutdown_cloud_archive(_exc: BaseException | None) -> None:
        archive_svc.shutdown(timeout=5.0)

    atexit.register(archive_svc.shutdown)
    app.extensions["cloud_archive_service_finalizer"] = weakref.finalize(
        app,
        archive_svc.shutdown,
    )


def _register_wrap_services(app: Flask, cfg: WebConfig) -> None:
    """Construct the wrap service once at app startup."""
    app.extensions["wrap_service"] = make_wrap_service(cfg)


def _register_mapping_services(app: Flask, cfg: WebConfig) -> None:
    """Construct the mapping service once and register cleanup hooks."""
    mapping_service = make_mapping_service(cfg)
    app.extensions["mapping_service"] = mapping_service
    atexit.register(mapping_service.shutdown)
    app.extensions["mapping_service_finalizer"] = weakref.finalize(
        app,
        mapping_service.shutdown,
    )


def _register_cleanup_services(app: Flask, cfg: WebConfig) -> None:
    """Construct the cleanup service after mapping and retention are ready."""
    retention_service = app.extensions.get("storage_retention_service")
    mapping_service = app.extensions.get("mapping_service")
    if retention_service is None or mapping_service is None:
        raise RuntimeError("cleanup_service requires storage_retention_service and mapping_service")
    cleanup_service = make_cleanup_service(
        cfg,
        retention_service,
        mapping_service,
        _get_cache_invalidator(app).schedule,
    )
    app.extensions["cleanup_service"] = cleanup_service
    if hasattr(retention_service, "bind_preview_summary_provider"):
        retention_service.bind_preview_summary_provider(cleanup_service.preview_summary)
    atexit.register(cleanup_service.shutdown)
    app.extensions["cleanup_service_finalizer"] = weakref.finalize(
        app,
        cleanup_service.shutdown,
    )


def _register_analytics_service(app: Flask, cfg: WebConfig) -> None:
    """Construct the analytics service after the mapping service is ready."""
    mapping_service = app.extensions.get("mapping_service")
    if not isinstance(mapping_service, MappingService):
        raise RuntimeError("analytics_service requires mapping_service")
    app.extensions["analytics_service"] = make_analytics_service(cfg, mapping_service)


def _register_video_service(app: Flask, cfg: WebConfig) -> None:
    """Construct the videos service once at app startup."""
    app.extensions["video_service"] = make_video_service(cfg)


def _register_jobs_service(app: Flask) -> None:
    """Construct the unified Failed Jobs facade.

    Depends on mapping_service (indexer adapter) and
    cloud_archive_service (cloud_sync adapter) — both are already
    registered by the time we get here.
    """
    mapping_service = app.extensions.get("mapping_service")
    cloud_archive_service = app.extensions.get("cloud_archive_service")
    typed_mapping = mapping_service if isinstance(mapping_service, MappingService) else None
    # CloudArchiveService satisfies CloudSyncAdapterProtocol structurally.
    typed_cloud = (
        cast("CloudSyncAdapterProtocol", cloud_archive_service)
        if cloud_archive_service is not None
        else None
    )
    app.extensions["jobs_service"] = make_jobs_service(
        mapping_service=typed_mapping,
        cloud_archive_service=typed_cloud,
    )


def _register_template_globals(app: Flask) -> None:
    """Inject defaults for the flags ``base.html`` reads.

    `base.html` references a number of context variables (`page`,
    `samba_on`, `*_available`, `auto_refresh`, `operation_in_progress`)
    without setting them itself — they're
    expected to be supplied by each view's `render_template` call or
    by a context processor. To keep `base.html` standalone-renderable
    (which we exploit in Phase 5.4 tests + the captive-portal page
    that doesn't have business logic to compute these), we register
    a context processor that supplies a conservative default for
    every flag.

    Real views override these via the `render_template` kwargs they
    pass. The Jinja precedence rules ensure view-supplied kwargs
    win over context-processor defaults, so this is purely a
    safety net — not a behaviour shift.

    The media ``*_available`` flags are hard-set to True per operator
    directive (H5 fixes 2): "media pill bar on every media-area page
    must show all six pills". B-1 has no IMG/loopback layer, so
    individual file presence is handled inside per-feature pages.
    """

    @app.context_processor
    def _inject_base_defaults() -> dict[str, object]:
        cfg = app.config.get("teslausb_config")
        _ = cfg  # B-1 has no IMG/loopback layer; all media areas are always available
        media_flags: dict[str, bool] = {
            "chimes_available": True,
            "music_available": True,
            "shows_available": True,
            "wraps_available": True,
            "boombox_available": True,
            "license_plates_available": True,
        }
        defaults: dict[str, object] = {
            "page": "",
            "samba_on": False,
            "auto_refresh": False,
            "expandable": False,
            "operation_in_progress": False,
            "estimated_completion": 0,
            "lock_age": 0,
            "map_available": True,
            "analytics_available": True,
            "cloud_archive_available": True,
            # Composite media-hub flag — `base.html` recomputes this
            # from the sub-flags below, but we ship a default in
            # case the template is refactored.
            "media_available": any(media_flags.values()),
            "storage_retention_available": False,
            "cleanup_available": False,
        }
        defaults.update(media_flags)
        return defaults
