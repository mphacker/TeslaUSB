"""conftest.py — set up imports and generated artefacts before test collection.

* Adds ``scripts/web`` to ``sys.path`` so ``from services.* import ...``
  works without an editable install.
* Auto-compiles ``services/dashcam_pb2.py`` from its ``.proto`` source
  before any test module is *collected*. Two test modules
  (``test_mapping_service.py``, ``test_sei_parser.py``) do
  ``from services.dashcam_pb2 import SeiMetadata`` at import time, and
  that file is gitignored on purpose (it's a generated artefact). On a
  fresh checkout — or in CI — collection used to abort with
  ``ModuleNotFoundError`` before any test ran. We pre-compile here using
  the same path the runtime uses (``sei_parser._get_sei_metadata_class``),
  so failures (e.g. ``protoc`` missing) surface with that helper's clear
  error message instead of an opaque import error.

This file is loaded by pytest BEFORE collection, which is the correct
phase to do generation (a session-scoped fixture would run too late
because collection happens first).

Closes #84.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts', 'web'))

# Eagerly compile the protobuf module before any test imports it.
# Wrapped in try/except so a missing protoc surfaces a single clear
# warning during collection rather than 100s of cascading collection
# errors. Tests that don't use dashcam_pb2 will still run.
try:
    from services.sei_parser import _get_sei_metadata_class as _compile_proto

    _compile_proto()
except Exception as exc:  # noqa: BLE001 — collection-time best-effort
    import warnings

    warnings.warn(
        "Could not pre-compile services/dashcam_pb2.py for tests: "
        f"{exc}. Tests that import dashcam_pb2 directly will fail to "
        "collect. Install 'protobuf-compiler' (Debian/Ubuntu: "
        "`sudo apt install -y protobuf-compiler`) and re-run pytest.",
        stacklevel=1,
    )
