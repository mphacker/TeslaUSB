# Charter Review — Phase 5.15 (captive_portal + wifi services)

**Branch:** `b1-userspace-rust`
**Scope:** Commit `aac4570` — captive_portal blueprint, wifi_service
+ wifi_state + wifi_support services, captive_portal.html.
**Reviewer:** retroactive self-audit (Phase 5 close-out backfill).
**Date:** 2026-05-21
**Charter version:** `docs/03-CODE-QUALITY-CHARTER.md` @ b22ab482

## Files in scope (LOC at `48f9515`)

| File | LOC | Notes |
| --- | ---: | --- |
| `blueprints/captive_portal.py` | 358 | |
| `services/wifi_service.py` | 399 | |
| `services/wifi_state.py` | 175 | |
| `services/wifi_support.py` | 444 | nmcli/journalctl shells |
| `templates/captive_portal.html` | 382 | (at 5.15 commit) |
| `tests/test_captive_portal_blueprint.py` | 310 | |
| `tests/test_wifi_service.py` | 773 | |

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

* ✅ All four Python source files are under the 500-LOC ceiling;
  the largest (`wifi_support.py` at 444) is the nmcli shell
  adapter — single responsibility (shell out to nmcli /
  journalctl, parse output) and within bounds.
* `templates/captive_portal.html` (382) — under 500; clean.
* No magic literals found in audit. nmcli timeouts and retry
  counts are named module-level constants.

### Pillar 2 — Best Architecture Practices

* ✅ `wifi_service.py` does not import Flask.
* ✅ Clean three-layer split: `wifi_state.py` (in-memory state
  + locks), `wifi_support.py` (nmcli/journalctl I/O adapter),
  `wifi_service.py` (domain orchestrator). The blueprint
  consumes the service only.
* ✅ All subprocess calls in `wifi_state.py:165` and elsewhere
  are tagged `# noqa: S603` with a justification comment
  ("executable path comes from shutil.which") — compliant.

### Pillar 3 — No Shortcuts

* ✅ No `print()`, no bare `except:`, no `: Any`, no
  `# type: ignore`, no `datetime.now()` without `tz=`, no
  `assert` in production.
* **Minor — Unlinked TODOs.**
  * `blueprints/captive_portal.py:305` — `# TODO(#issue-needed):
    implement AP credential update via WifiService`
  * `blueprints/captive_portal.py:345` — `# TODO(#issue-needed):
    implement network reordering via WifiService`
  Both carry the placeholder `#issue-needed` tag rather than a
  real GH issue number. The charter (Pillar 3 + §"Comment-as-
  bug-deferral") requires `TODO(#NNN)` with a real issue. Phase
  5.29-metrics close-out (commit history note "docs: link Phase
  5 close-out TODOs to GH issues #223-#226") addressed the
  analytics TODOs but missed these two. **Recommendation:** file
  during Phase 6 close-out sweep, NOT in this dispatch.

### Pillar 4 — Fix Bugs Immediately

No bug-fix commits in the 5.15 span.

### Pillar 5 — No Dead Code

* ✅ Vulture clean.
* The two TODO-gated routes (lines 305, 345) return
  `not_implemented` JSON — they are intentional stubs, not dead
  code, and exercised by the blueprint test suite.

## Verdict

- **Blockers:** 0
- **Majors:** 0
- **Minors:** 1 (two unlinked `TODO(#issue-needed)` markers in
  `blueprints/captive_portal.py:305,345` — need real GH issue
  numbers)
- **Nits:** 0
- **Status:** **APPROVED RETROACTIVELY** with documented Minor.
  No GH issue filed in this dispatch per scope rules (file
  during Phase 6 close-out alongside the other Phase 5 deferred
  TODOs).

## Filed issues

None in this dispatch.

## Notes on retroactive nature

Backfill report. The unlinked-TODO finding was already partially
addressed by an earlier link-pass that picked up the analytics
blueprint (see `charter-review-5.29-metrics.md`) but missed
captive_portal — recommend the Phase 6 cleanup sweep close this
gap.
