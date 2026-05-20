"""gunicorn WSGI entry point.

`/usr/bin/env gunicorn teslausb_web.wsgi:app` (configuration from
``config/gunicorn.conf.py``, landing in Phase 5.19) reads ``app``
from this module. Production deploys never construct the factory
themselves — they import this module so the loaded config is the
one ``setup.sh`` (Phase 6) installed at
``/etc/teslausb/teslausb-web.toml``.

Tests must NOT import this module — they should call
``teslausb_web.app.create_app(allow_defaults=True)`` directly so
they don't trip the "missing config file" guard.
"""

from __future__ import annotations

from teslausb_web.app import create_app

# `allow_defaults=False` is the deliberate production setting: if
# `/etc/teslausb/teslausb-web.toml` is missing, the import fails
# with a path-anchored ConfigError, which surfaces as a gunicorn
# worker start failure — exactly the loud feedback we want when
# setup.sh hasn't completed.
app = create_app(allow_defaults=False)
