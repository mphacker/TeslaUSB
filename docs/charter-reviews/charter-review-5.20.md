# Charter Review — Phase 5.20 (settings dashboard — v1 parity)

**Branch:** `b1-userspace-rust`
**Scope:** Commits `9e4d377` (5.20 — settings dashboard
consolidated v1 parity: `blueprints/settings.py`,
`templates/index.html`, captive_portal/settings_advanced/
system_health wiring) and `7be2da4` (docs-only PROGRESS.md
expansion + 5.21–5.28 backfill queue).
**Reviewer:** retroactive self-audit (Phase 5 close-out backfill).
**Date:** 2026-05-21
**Charter version:** `docs/03-CODE-QUALITY-CHARTER.md` @ b22ab482

## Files in scope (LOC at `48f9515`)

| File | LOC | Notes |
| --- | ---: | --- |
| `blueprints/settings.py` | 278 | NEW — settings dashboard |
| `blueprints/captive_portal.py` | +54 | settings hooks |
| `blueprints/settings_advanced.py` | +17 | settings hooks |
| `blueprints/system_health.py` | +19 | settings hooks |
| `templates/index.html` | 2304 | restored to v1 parity |
| `templates/settings_advanced.html` | +8 | |
| `tests/test_settings_blueprint.py` | 125 | NEW |
| `tests/test_settings_advanced_blueprint.py` | +18 | |
| `tests/test_templates_skeleton.py` | +3 | |

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

* ✅ `blueprints/settings.py` at 278 LOC is under the 300 target.
* `templates/index.html` at 2304 LOC is the v1 dashboard
  template restored 1:1. The charter's file-size rule applies to
  source modules (Pillar 1 sits in the Python/Rust standards
  block); HTML templates are not gated by any on-disk linter
  and the template is the v1 parity artefact specifically
  flagged in `docs/05-UI-UX-DESIGN-SYSTEM.md` as required for
  user-facing dashboard fidelity. Documented carry-over.
* No magic literals in `blueprints/settings.py`; route paths,
  template names, and form-field constants are all named.

### Pillar 2 — Best Architecture Practices

* ✅ `blueprints/settings.py` is a thin blueprint that delegates
  to existing services (`system_settings_service`,
  `storage_retention_service`, `wifi_service`, `samba_service`,
  `lock_chime_service`, `captive_portal`). No business logic
  bled into the blueprint.
* ✅ The +54 / +17 / +19 LOC additions in captive_portal /
  settings_advanced / system_health are pure helper exports
  (read-only views consumed by the dashboard) — they don't
  introduce new boundaries.

### Pillar 3 — No Shortcuts

* ✅ No `print()`, no bare `except:`, no `: Any`, no
  `# type: ignore`, no `datetime.now()` without `tz=`, no
  unlinked TODOs in the 5.20 surface, no `assert` in
  production.
* ✅ The template restore preserved v1's HTML structure but
  re-pointed icons to Lucide SVG sprites (per UI/UX charter
  §5) and CSS tokens — verified by spot-check.

### Pillar 4 — Fix Bugs Immediately

The +54 LOC in `captive_portal.py` includes a status-shape
correction discovered while wiring the dashboard (matched by
test changes in `test_settings_advanced_blueprint.py`). Boy-Scout
fix, bundled with the increment. Approved.

### Pillar 5 — No Dead Code

* ✅ Vulture clean. The dashboard exercises every dependent
  service.

## Verdict

- **Blockers:** 0
- **Majors:** 0 (the >500-LOC item is a Jinja template, not a
  Python source file; documented v1-parity carry-over)
- **Minors:** 0
- **Nits:** 0
- **Status:** **APPROVED RETROACTIVELY (NEAR-CLEAN).**

## Filed issues

None.

## Notes on retroactive nature

Backfill report. Commit `7be2da4` is a docs-only PROGRESS.md
update — no charter surface. LOC as of 48f9515.
