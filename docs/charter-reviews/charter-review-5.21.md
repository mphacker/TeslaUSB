# Charter Review — Phase 5.21 (mapping.html v1-parity restore + bundled plate routes)

**Branch:** `b1-userspace-rust`
**Scope:** Commit `dbc70dc` — restored `templates/mapping.html`
from the -78%-undersized 5.13c-era cut down (910 LOC) to v1
parity (4076 LOC at 48f9515). Also bundled: +749 LOC to
`templates/license_plates.html` (the 5.23 plate-restore work
that was committed together) and +67 LOC to
`blueprints/license_plates.py`, +34 LOC to
`tests/test_mapping_blueprint.py`, +41 LOC to
`tests/test_license_plates_blueprint.py`.
**Reviewer:** retroactive self-audit (Phase 5 close-out backfill).
**Date:** 2026-05-21
**Charter version:** `docs/03-CODE-QUALITY-CHARTER.md` @ b22ab482

## Files in scope (LOC at `48f9515`)

| File | LOC | Notes |
| --- | ---: | --- |
| `templates/mapping.html` | 4076 | v1-parity restore (was 910) |
| `templates/license_plates.html` | 1201 | bundled plate restore (see 5.23) |
| `blueprints/license_plates.py` | +67 | bundled plate routes |
| `tests/test_mapping_blueprint.py` | +34 | |
| `tests/test_license_plates_blueprint.py` | +41 | |
| `tests/test_templates_skeleton.py` | +8 | |

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

* `templates/mapping.html` at 4076 LOC exceeds any reasonable
  source-file ceiling — but the charter file-size rule lives in
  the Python/Rust source standards and HTML templates are not
  gated by the on-disk checks. The size is a deliberate v1-parity
  artefact: the 5.13c cut-down was -78% under-built and broke
  user-visible UI features (Sentry inspector, SEI overlay,
  trip-detail drawer, event filters). The 5.21 restore is the
  documented remedy. Documented carry-over.
* No magic-literal violations in the blueprint additions
  (route paths, status constants are named).

### Pillar 2 — Best Architecture Practices

* ✅ The bundled blueprint additions in `license_plates.py` are
  v1-parity route shims; they delegate to the existing
  `license_plate_service` without leaking logic.
* ✅ The mapping template change is template-only — no service
  surface touched.

### Pillar 3 — No Shortcuts

* ✅ No new `print()`, `except:`, `: Any`, `# type: ignore`,
  bare `datetime.now()`, or `assert` introduced.
* ✅ Template content does not introduce inline `style="color:…"`
  hex values per spot-check; CSS tokens used throughout.
* See `charter-review-5.16.md` for the pre-existing
  `TODO(#photo-upload)` markers, which this commit did not
  modify.

### Pillar 4 — Fix Bugs Immediately

This commit IS itself a fix-on-discovery: 5.13c shipped with a
v1-parity gap that was documented in the commit message
("was -78% under-built in 5.13c"). 5.21 closes it. Approved
under Pillar 4 ("fix the visible regression at discovery").

### Pillar 5 — No Dead Code

* ✅ Vulture clean.
* ✅ The restored template's macros and JS hooks are all
  consumed by the corresponding blueprint routes (verified by
  the blueprint test additions in this same commit, +34 LOC).

## Verdict

- **Blockers:** 0
- **Majors:** 0 (template size is documented v1-parity carry-
  over, not a Python source ceiling violation)
- **Minors:** 0
- **Nits:** 0
- **Status:** **APPROVED RETROACTIVELY (CLEAN).**

## Filed issues

None.

## Notes on retroactive nature

Backfill report. `dbc70dc` bundles both the 5.21 mapping
restore and the 5.23 license_plates restore in a single
commit. The plate-specific findings are covered separately in
`charter-review-5.23.md`.
