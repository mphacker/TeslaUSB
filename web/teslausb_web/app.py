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

import errno
import logging
import secrets
from typing import TYPE_CHECKING

from flask import Flask, abort, jsonify, request, send_from_directory

from teslausb_web.config import WebConfig, load_config

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
    _register_blueprints(app, extra_blueprints)

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
    """Register all known blueprints.

    Currently a stub: the real per-feature blueprints land in
    Phase 5.7 - 5.16 (one per increment). Extras passed by tests
    are registered as-is so the factory itself is exercised.
    """
    for bp in extras:
        app.register_blueprint(bp)  # type: ignore[arg-type]
