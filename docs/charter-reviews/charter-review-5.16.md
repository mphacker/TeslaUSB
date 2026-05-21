# Charter Review — Phase 5.16 (license_plates + storage_retention + settings_advanced)

**Branch:** `b1-userspace-rust`
**Scope:** Commits `d741554` (5.16a — license_plates service +
blueprint + template), `f2c9227` (5.16b — storage_retention
service + blueprint + cleanup_settings template), `a44be6c`
(5.16c — settings_advanced + system_settings_service, closes
Phase 5.16).
**Reviewer:** retroactive self-audit (Phase 5 close-out backfill).
**Date:** 2026-05-21
**Charter version:** `docs/03-CODE-QUALITY-CHARTER.md` @ b22ab482

## Files in scope (LOC at `48f9515`)

| File | LOC | Notes |
| --- | ---: | --- |
| `blueprints/license_plates.py` | 424 | 5.16a (+5.21 restore tweaks) |
| `services/license_plate_service.py` | 551 | 5.16a — **over ceiling** |
| `templates/license_plates.html` | 1201 | template (5.21 v1-parity restore — see 5.23) |
| `static/js/license_plates.js` | 188 | 5.16a |
| `blueprints/storage_retention.py` | 233 | 5.16b |
| `services/storage_retention_service.py` | 524 | 5.16b — **over ceiling** |
| `templates/cleanup_settings.html` | 374 | 5.16b |
| `blueprints/settings_advanced.py` | 317 | 5.16c |
| `services/system_settings_service.py` | 270 | 5.16c |
| `templates/settings_advanced.html` | 398 | 5.16c |
| Tests | ~2050 | 7 modules |

## Automated gate snapshot (current tree, 48f9515)

| Gate | Status | Notes |
| --- | --- | --- |
| `ruff check` | ✅ | clean |
| `ruff format --check` | ✅ | clean |
| `mypy --strict` | ✅ | 110 src files |
| `vulture --min-confidence 80` | ✅ | clean |
| `bandit -ll` | ✅ | clean |
| `pytest -W error::ResourceWarning --cov-fail-under=80` | ✅ | 1750 passed, 86.66% cov |

## Pillar findings

### Pillar 1 — No Code Smells

* **Major — God modules.** Two services exceed the 500-LOC
  ceiling:
  * `services/license_plate_service.py` (551) — single-file
    domain service for plate CRUD + indexing + photo metadata.
    Acceptable cohesion but flagged for the Phase 6 split.
  * `services/storage_retention_service.py` (524) — retention
    policy engine + apply loop. Splitting candidate is
    policy-eval vs apply.
* `blueprints/settings_advanced.py` (317) sits between target
  (<300) and ceiling (<500) — borderline; acceptable.
* `templates/license_plates.html` was extended to 1201 LOC in
  5.21 for v1 parity (see 5.23 report); not gated.
* No magic-literal violations observed (retention thresholds,
  plate-name regex are named).

### Pillar 2 — Best Architecture Practices

* ✅ All three services Flask-free.
* ✅ Storage retention split: blueprint thin (233 LOC), service
  carries logic, no DB I/O in the route layer.
* ✅ `system_settings_service` is a clean key/value adapter over
  the JSON state file with frozen dataclass return types.

### Pillar 3 — No Shortcuts

* ✅ No `print()`, no bare `except:`, no `: Any`, no
  `# type: ignore`, no `datetime.now()` without `tz=`, no
  `assert` in production.
* **Minor — Unlinked TODOs.** Four `TODO(#photo-upload)`
  markers in `blueprints/license_plates.py` (lines 378, 391,
  403, 412) — placeholder tag rather than a real GH issue
  number. Same pattern as captive_portal (5.15). Recommend
  Phase 6 cleanup sweep link them to a real issue. Routes are
  intentional `not_implemented` stubs.

### Pillar 4 — Fix Bugs Immediately

No bug-fix commits in the 5.16 span. (Note: 5.21 / `dbc70dc`
later added 67 LOC of plate-route work bundled with the mapping
restore — covered in `charter-review-5.21.md` /
`charter-review-5.23.md`.)

### Pillar 5 — No Dead Code

* ✅ Vulture clean.
* The four `TODO(#photo-upload)`-gated routes are exercised by
  the blueprint test suite returning structured `not_implemented`
  bodies — not dead.

## Verdict

- **Blockers:** 0
- **Majors:** 1 (two services >500 LOC)
- **Minors:** 1 (four unlinked `TODO(#photo-upload)` markers in
  `license_plates.py`)
- **Nits:** 0
- **Status:** **APPROVED RETROACTIVELY** with documented Major
  + Minor. No GH issue filed in this dispatch.

## Filed issues

None in this dispatch.

## Notes on retroactive nature

Backfill report. The license_plates blueprint and template were
further modified in 5.21 (`dbc70dc`) for v1 parity — see
`charter-review-5.21.md` and `charter-review-5.23.md`.
