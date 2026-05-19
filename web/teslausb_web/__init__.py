"""`teslausb_web` — Flask UI for TeslaUSB B-1.

UI-only Flask app. **All business logic lives in the Rust binaries**
(`rust/crates/teslafat`, `rust/crates/teslausb-worker`). This package
exposes a browser-facing dashboard for status, settings, video
browsing, lock chimes, light shows, cloud archive, and network
sharing toggles, and talks to the Rust daemon over the IPC envelope
defined in `teslausb-core` (see `ipc.py`, Phase 5).

Per `docs/03-CODE-QUALITY-CHARTER.md` §"Best Architecture Practices"
the package is structured in layers:

* `app.py` / `wsgi.py`       — Flask factory + gunicorn entry (Layer 4)
* `blueprints/`              — HTTP routes (Layer 4 adapters)
* `services/`                — business orchestration (Layer 2)
* `config.py`, `db.py`, `ipc.py` — infrastructure adapters (Layer 3)

Phase 0.3 establishes the package skeleton (this file plus empty
`blueprints/` and `services/` subpackages). Modules land in Phase 5
per `docs/00-PLAN.md`.
"""

from __future__ import annotations

__version__ = "0.1.0"
