# Charter Review — Phase 5.17 (samba_service + samba_watcher)

**Branch:** `b1-userspace-rust`
**Scope:** Commit `2c3cc40` — samba_service (563 LOC), samba_watcher
(287 LOC), app.py wiring, +21 LOC tweak to `system_settings_service`,
two new test modules totalling 898 LOC.
**Reviewer:** retroactive self-audit (Phase 5 close-out backfill).
**Date:** 2026-05-21
**Charter version:** `docs/03-CODE-QUALITY-CHARTER.md` @ b22ab482

## Files in scope (LOC at `48f9515`)

| File | LOC | Notes |
| --- | ---: | --- |
| `services/samba_service.py` | 563 | smb.conf renderer + state |
| `services/samba_watcher.py` | 287 | inotify-driven config reload |
| `services/system_settings_service.py` | +21 | extension only |
| `app.py` | +79 | service wiring |
| `tests/test_samba_service.py` | 521 | |
| `tests/test_samba_watcher.py` | 377 | |
| `tests/test_app_factory.py` | +76 | |

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

* **Major — God module.** `services/samba_service.py` (563) is
  over the 500-LOC ceiling. The module fuses smb.conf rendering,
  systemd unit control, and state caching. Pre-Phase-6 split
  candidate: extract `_smbconf_render.py` (pure string-template
  layer) from the IO/orchestration layer.
* `samba_watcher.py` (287) is under target and clean.
* No magic literals found in audit (inotify masks, paths are
  named constants).

### Pillar 2 — Best Architecture Practices

* ✅ Neither service imports Flask.
* ✅ `samba_watcher.py` is a clean adapter: inotify thread →
  debouncer → calls into `samba_service.reconcile()`.
* ✅ Subprocess invocations carry `# noqa: S603` with the
  shutil.which justification.
* ✅ Frozen dataclasses on state snapshots.

### Pillar 3 — No Shortcuts

* ✅ No `print()`, no bare `except:`, no `: Any`, no
  `# type: ignore`, no `datetime.now()` without `tz=`, no
  unlinked TODOs, no `assert` in production.

### Pillar 4 — Fix Bugs Immediately

The 5.17 commit also adjusts `system_settings_service` (+21 LOC,
-tests +19) for a small contract change discovered while wiring
samba state — Boy-Scout fix, bundled with the increment, with
test coverage adjustments. Approved.

### Pillar 5 — No Dead Code

* ✅ Vulture clean.

## Verdict

- **Blockers:** 0
- **Majors:** 1 (samba_service.py 563 > 500 LOC)
- **Minors:** 0
- **Nits:** 0
- **Status:** **APPROVED RETROACTIVELY** with documented Major
  on file size. No GH issue filed.

## Filed issues

None.

## Notes on retroactive nature

Backfill report. LOC quoted is as of 48f9515.
