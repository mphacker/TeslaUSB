"""Public mapping service package for B-1 Phase 5.13c.

The v1 ``mapping_service.py`` file was split into focused modules so each file
stays under the charter file-size ceiling:

- ``service``: facade, config, public API, lifecycle
- ``indexer``: per-file indexing + purge reconciliation
- ``events`` / ``trips``: domain helpers
- ``discovery`` / ``paths`` / ``sentry`` / ``diagnose``: filesystem + metadata
- ``retry`` / ``kv`` / ``stale_scan`` / ``sei``: infrastructure adapters

Phase 5.13c keeps a Python SEI fallback so web-side tests can run without the
future Rust worker IPC bridge. The Rust worker remains the long-term indexing
home; this package is the compatibility domain layer for parity.
"""

from __future__ import annotations

from teslausb_web.services.mapping.service import (
    DiagnoseError,
    IndexerError,
    IndexOutcome,
    IndexResult,
    MappingService,
    MappingServiceConfig,
    MappingServiceError,
    make_mapping_service,
)

__all__ = (
    "DiagnoseError",
    "IndexOutcome",
    "IndexResult",
    "IndexerError",
    "MappingService",
    "MappingServiceConfig",
    "MappingServiceError",
    "make_mapping_service",
)
