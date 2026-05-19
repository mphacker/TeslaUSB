"""Business-orchestration services.

Layer 2 in the hexagonal layering: blueprints call services;
services orchestrate domain objects and infrastructure adapters
(`config`, `db`, `ipc`). **No `flask` imports here** — services
must be callable from a non-Flask context (tests, future CLI tools,
the worker process) per `docs/03-CODE-QUALITY-CHARTER.md`
§"Architectural Principles / The Layering Rule".

Charter §"Test discipline" sets the bar:
    Coverage gate: ≥ 80% line coverage on
    `web/teslausb_web/services/`

Phase 0.3 establishes the empty subpackage; service modules arrive
incrementally in Phase 5 (one per feature area, with charter-review
per file).
"""

from __future__ import annotations
