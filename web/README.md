# `web/` — TeslaUSB B-1 Flask web UI

Python package housing the browser-facing dashboard. **UI only** —
all business logic lives in the Rust binaries under `rust/crates/`.
The web app talks to the Rust daemon over the IPC envelope defined
in `teslausb-core` (see `teslausb_web/ipc.py`, Phase 5).

```text
web/
├── pyproject.toml             single source of truth for ruff + mypy + pytest config
├── teslausb_web/
│   ├── __init__.py            package marker (Phase 0.3)
│   ├── app.py                 Flask factory                     (Phase 5.2)
│   ├── wsgi.py                gunicorn entry point              (Phase 5.2)
│   ├── config.py              reads same TOML as the Rust side   (Phase 5.2)
│   ├── ipc.py                 Unix-socket JSON-line IPC client   (Phase 5.2)
│   ├── db.py                  read-only sqlite3 view of geodata  (Phase 5.2)
│   ├── blueprints/            HTTP route modules                 (Phase 5.x)
│   ├── services/              business orchestration             (Phase 5.x)
│   ├── templates/             Jinja2 templates                   (Phase 5.3 — ported verbatim from v1)
│   └── static/                CSS / fonts / SVG icons / JS       (Phase 5.3 — ported verbatim from v1)
└── tests/
    ├── __init__.py
    ├── conftest.py            shared pytest fixtures
    └── test_smoke.py          package importability
```

## Phase 0.3 state

Empty Python package skeleton. The smoke test (`pytest`) confirms:

* The package imports cleanly.
* `blueprints/` and `services/` subpackages import cleanly.
* `setuptools` package discovery works.

Module files (`app.py`, `wsgi.py`, etc.) land in Phase 5 increments
per `docs/00-PLAN.md`.

## Dev install

From the repo root:

```bash
cd web
python3.11 -m venv .venv             # the charter pins 3.11 (Bookworm)
source .venv/bin/activate
pip install -e '.[dev]'
```

`[dev]` brings in `ruff`, `mypy`, `pytest`, `pytest-cov`, `vulture`,
`bandit`. `setup-dev.sh` (Phase 0.6) automates this for a clean dev
box.

## CI gates

From `web/`:

```bash
ruff check .
ruff format --check .
mypy teslausb_web tests
pytest --cov=teslausb_web --cov-fail-under=80
vulture teslausb_web --min-confidence 80
bandit -r teslausb_web -ll
```

Coverage gate intentionally lives on the CLI (not in `pyproject`'s
default `addopts`) so work-in-progress increments aren't blocked by
the 80 % floor before code lands. CI enforces it for merged
branches per `docs/03-CODE-QUALITY-CHARTER.md` §"CI Gates".

## Layering

`teslausb_web/services/` must NOT import `flask`. Services are pure
orchestration callable from any context (tests, future CLI tools,
the Rust worker via IPC). Blueprints depend on services; services
depend on `config` / `db` / `ipc` adapters; adapters depend on
infrastructure. Charter §"Architectural Principles / The Layering
Rule" — violation is a blocker at charter-review.
