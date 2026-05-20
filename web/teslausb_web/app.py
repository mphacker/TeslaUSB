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
from typing import TYPE_CHECKING

from flask import Flask, abort, jsonify, request, send_from_directory

from teslausb_web.blueprints._scaffold import build_scaffold_blueprints
from teslausb_web.config import WebConfig, load_config
from teslausb_web.services.cache_invalidation import CacheInvalidator

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

    for scaffold_bp in build_scaffold_blueprints():
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


def _register_template_globals(app: Flask) -> None:
    """Inject defaults for the flags ``base.html`` reads.

    `base.html` references a number of context variables (`page`,
    `samba_on`, `*_available`, `auto_refresh`, `operation_in_progress`,
    `videos_available`, etc.) without setting them itself — they're
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
    """

    @app.context_processor
    def _inject_base_defaults() -> dict[str, object]:
        return {
            "page": "",
            "samba_on": False,
            "auto_refresh": False,
            "expandable": False,
            "operation_in_progress": False,
            "estimated_completion": 0,
            "lock_age": 0,
            "map_available": True,
            "analytics_available": True,
            "videos_available": True,
            "cloud_archive_available": True,
            # Composite media-hub flag — `base.html` recomputes this
            # from the sub-flags below, but we ship a default in
            # case the template is refactored.
            "media_available": False,
            "chimes_available": False,
            "music_available": False,
            "shows_available": False,
            "wraps_available": False,
            "boombox_available": False,
            "license_plates_available": False,
        }
