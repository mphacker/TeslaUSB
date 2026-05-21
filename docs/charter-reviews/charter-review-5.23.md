# Charter Review — Phase 5.23 (license_plates.html v1-parity restore)

**Branch:** `b1-userspace-rust`
**Scope:** The license_plates v1-parity restore was actually
committed inside `dbc70dc` (the Phase 5.21 mapping restore — see
`charter-review-5.21.md` for the bundling note). The standalone
Phase 5.23 commit `9a44a5b` is a one-line PROGRESS.md status flip
("docs: mark Phase 5.23 complete"). This report audits the
plate-restore work (template + bundled blueprint additions) as
it appears at 48f9515.
**Reviewer:** retroactive self-audit (Phase 5 close-out backfill).
**Date:** 2026-05-21
**Charter version:** `docs/03-CODE-QUALITY-CHARTER.md` @ b22ab482

## Files in scope (LOC at `48f9515`)

| File | LOC | Notes |
| --- | ---: | --- |
| `templates/license_plates.html` | 1201 | restored to v1 parity (from `dbc70dc`) |
| `blueprints/license_plates.py` | +67 LOC delta in dbc70dc | route additions |
| `tests/test_license_plates_blueprint.py` | +41 LOC delta in dbc70dc | |
| `docs/01-PROGRESS.md` | +1 / -1 | the 9a44a5b commit |

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

* `templates/license_plates.html` at 1201 LOC is a Jinja
  template; not source. Documented v1-parity carry-over,
  consistent with the rest of the 5.20–5.22 template-restore
  series. The earlier 5.16a template at 528 LOC was a
  cut-down; v1 parity required the expansion.
* The +67 LOC in `blueprints/license_plates.py` add routes for
  the parity-restored template (group filters, search, etc.).
  Total blueprint LOC at 48f9515 is 424 — under target.

### Pillar 2 — Best Architecture Practices

* ✅ Blueprint additions delegate to `license_plate_service` —
  no logic leak into the route layer.
* ✅ Template is HTML+Jinja only; no inline JS logic
  introduced.

### Pillar 3 — No Shortcuts

* The `TODO(#photo-upload)` markers at
  `blueprints/license_plates.py:378,391,403,412` predate this
  restore (introduced in 5.16a). They remain unlinked to a real
  GH issue — see `charter-review-5.16.md` Minor finding.
* No new `# noqa`, `# type: ignore`, or shortcut patterns
  introduced by the 5.23 restore work.

### Pillar 4 — Fix Bugs Immediately

This restore IS a Pillar-4-style fix-on-discovery, closing a
v1-parity gap left by 5.16a (the 528-LOC template lacked group
filters and the recall-event surface). Bundled with appropriate
test additions (+41 LOC). Approved.

### Pillar 5 — No Dead Code

* ✅ Vulture clean.
* ✅ The newly added routes are exercised by the test additions
  in the same commit.

## Verdict

- **Blockers:** 0
- **Majors:** 0
- **Minors:** 0 net new (the four pre-existing
  `TODO(#photo-upload)` are tracked under 5.16)
- **Nits:** 0
- **Status:** **APPROVED RETROACTIVELY (CLEAN).**

## Filed issues

None.

## Notes on retroactive nature

Backfill report. The 5.23 plate-restore work is physically
bundled into commit `dbc70dc` together with the 5.21 mapping
restore; commit `9a44a5b` is a docs-only PROGRESS.md flip. The
charter-relevant content is the plate template + blueprint
delta, covered above.
