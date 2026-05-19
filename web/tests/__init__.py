"""Test suite for `teslausb_web`.

Mirrors the source tree: `tests/blueprints/test_<name>.py` for
HTTP-route tests using the Flask test client; `tests/services/
test_<name>.py` for pure-logic service tests. Fixtures shared via
`conftest.py`.

Per `docs/03-CODE-QUALITY-CHARTER.md` §"Test discipline":
* `pytest` with `--strict-markers` and `--strict-config` (default in
  `pyproject.toml`).
* No sleep-based timing.
* No real network — Flask test client only.
* `tmp_path` fixture for filesystem isolation, never `/tmp` directly.
"""

from __future__ import annotations
