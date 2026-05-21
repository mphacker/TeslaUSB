# Charter Review — Phase 5.18 (cleanup_service package + blueprint)

**Branch:** `b1-userspace-rust`
**Scope:** Commit `22c0d1f` — `services/cleanup/` package (6 modules),
`blueprints/cleanup.py` (268 LOC), preview + report templates,
cleanup.js, +27 LOC to `blueprints/storage_retention.py`, +19 LOC
to `storage_retention_service.py`.
**Reviewer:** retroactive self-audit (Phase 5 close-out backfill).
**Date:** 2026-05-21
**Charter version:** `docs/03-CODE-QUALITY-CHARTER.md` @ b22ab482

## Files in scope (LOC at `48f9515`)

| File | LOC | Notes |
| --- | ---: | --- |
| `blueprints/cleanup.py` | 268 | |
| `services/cleanup/__init__.py` | 29 | |
| `services/cleanup/discovery.py` | 252 | |
| `services/cleanup/execute.py` | 100 | |
| `services/cleanup/preview.py` | 172 | |
| `services/cleanup/report.py` | 295 | |
| `services/cleanup/service.py` | 673 | **over ceiling** |
| `templates/cleanup_preview.html` | 242 | |
| `templates/cleanup_report.html` | 227 | |
| `static/js/cleanup.js` | 77 | |
| Tests | ~1259 | cleanup_blueprint + cleanup_service |

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

* **Major — God module.** `services/cleanup/service.py` (673)
  is over the 500-LOC ceiling. The facade module concentrates
  orphan-scan orchestration, cancellation tokens, run-record
  bookkeeping, and the `CleanupError` hierarchy. Reasonable
  split for Phase 6: move run-record state into a
  `_run_state.py` sibling and keep `service.py` as the slim
  orchestrator.
* `services/cleanup/report.py` (295) is just under target —
  acceptable.
* No magic-literal violations; thresholds (`orphan-age-days`,
  `max-batch`) are named constants.
* No deep nesting found in spot-checks (`service.execute_cleanup`
  uses early-return + dedicated `_classify_*` helpers).

### Pillar 2 — Best Architecture Practices

* ✅ The cleanup package follows the hexagonal split: `discovery`
  (filesystem scan adapter), `preview` (pure transform), `execute`
  (mutation), `report` (persistence), `service` (facade). No
  Flask imports in any sub-module.
* ✅ Frozen dataclasses for `OrphanScan`, `CleanupPreview`,
  `CleanupReport`, `RunRecord`.
* **Minor — Lazy imports.** `services/cleanup/discovery.py:148`
  (`from teslausb_web.services.cleanup.service import
  OrphanScan  # noqa: PLC0415`), `execute.py:36`
  (`CleanupCancelledError`), `execute.py:81` (`CleanupError`)
  use lazy imports to break a circular dependency between the
  facade-level types and the leaf modules that use them. The
  charter allows lazy imports as a documented escape valve, but
  the cleaner pattern is to lift shared types into a
  `_types.py`. Same observation as 5.13's `services/mapping/`.

### Pillar 3 — No Shortcuts

* ✅ No `print()`, no bare `except:`, no `: Any`, no
  `# type: ignore`, no `datetime.now()` without `tz=`, no
  unlinked TODOs, no `assert` in production.
* ✅ `services/cleanup/report.py:89` `start_run_record` carries
  `# noqa: PLR0913` (too many args) — candidate for a
  `RunRecordParams` dataclass; minor data-clumps smell.

### Pillar 4 — Fix Bugs Immediately

5.18 also adjusts `blueprints/storage_retention.py` (+27 LOC) and
`services/storage_retention_service.py` (+19 LOC) — Boy-Scout
adjustments to align the retention contract with the new cleanup
preview/report surface; covered by updated tests in
`test_storage_retention_*.py`. Approved.

### Pillar 5 — No Dead Code

* ✅ Vulture clean.

## Verdict

- **Blockers:** 0
- **Majors:** 1 (`cleanup/service.py` 673 > 500 LOC)
- **Minors:** 2 (lazy-import circular-dep hint, `PLR0913` on
  `report.start_run_record`)
- **Nits:** 0
- **Status:** **APPROVED RETROACTIVELY** with documented Major
  + Minors. No GH issue filed.

## Filed issues

None.

## Notes on retroactive nature

Backfill report. LOC as of 48f9515.
