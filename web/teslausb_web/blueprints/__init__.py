"""Flask blueprints — HTTP route modules.

Each blueprint owns one user-facing feature area and lives in its
own module so route ownership is clear in `git blame`. Per
`docs/00-PLAN.md` Phase 5 the planned modules are:

* `mapping.py`         — map page + integrated video panel
* `settings.py`        — config + diagnostics
* `lock_chimes.py`     — lock chime picker + upload
* `light_shows.py`     — light show library
* `music.py`           — music library (when enabled)
* `wraps.py`           — PNG wrap library
* `cloud_archive.py`   — cloud sync dashboard
* `network_sharing.py` — Samba enable/disable toggle
* `system_health.py`   — disk, memory, services status
* `captive_portal.py`  — splash page for AP-connected devices

Phase 0.3 establishes the empty subpackage; module files arrive in
Phase 5 (one per increment, with charter-review per file).
"""

from __future__ import annotations
