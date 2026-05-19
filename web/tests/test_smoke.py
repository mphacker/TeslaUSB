"""Smoke test — the `teslausb_web` package imports cleanly.

Justifies the package's existence as a Phase 0.3 deliverable: if
the package layout, `pyproject.toml`, or `setuptools` config is
broken, this test fails loudly. Replaced by real Flask app-factory
tests in Phase 5.2.
"""

from __future__ import annotations

import importlib


def test_package_imports() -> None:
    """`teslausb_web` is importable and exposes a `__version__`."""
    module = importlib.import_module("teslausb_web")
    assert module.__version__ == "0.1.0"


def test_blueprints_subpackage_imports() -> None:
    """`teslausb_web.blueprints` is importable (empty subpackage)."""
    module = importlib.import_module("teslausb_web.blueprints")
    assert module is not None


def test_services_subpackage_imports() -> None:
    """`teslausb_web.services` is importable (empty subpackage)."""
    module = importlib.import_module("teslausb_web.services")
    assert module is not None
