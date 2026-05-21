# Charter Review — Phase 5.27 (Failed Jobs / Dead-Letter UI)

**Branch:** `b1-userspace-rust`
**Scope:** Port v1's unified Failed Jobs / Dead-Letter UI to B-1.
Aggregate dead-letter rows from the per-subsystem queues behind one
typed facade + one Flask blueprint + one HTML page; replace v1's
`window.confirm()` flows with the shared modal pattern; keep the
shell render free of DB I/O so the page loads even when a subsystem
is slow.
**Reviewer:** self-audit (mandatory pre-commit gate).

## Files changed / added

| File | LOC | Status |
| --- | ---: | --- |
| `web/teslausb_web/services/jobs_service/__init__.py` | 170 | NEW — `JobsService` facade + `make_jobs_service` factory |
| `web/teslausb_web/services/jobs_service/_models.py` | 86 | NEW — 6 frozen dataclasses + `SubsystemKey` enum |
| `web/teslausb_web/services/jobs_service/_classifier.py` | 222 | NEW — pure clip-value + recommendation rules |
| `web/teslausb_web/services/jobs_service/_redactor.py` | 45 | NEW — path / remote / S3 redactor |
| `web/teslausb_web/services/jobs_service/_indexer_adapter.py` | 96 | NEW — stub adapter (see deviation §) |
| `web/teslausb_web/services/jobs_service/_cloud_sync_adapter.py` | 94 | NEW — `CloudArchiveService` adapter + structural Protocol |
| `web/teslausb_web/blueprints/jobs.py` | 247 | NEW — 5 routes, thin glue |
| `web/teslausb_web/templates/failed_jobs.html` | 623 | NEW — ported from v1's 644-line template |
| `web/teslausb_web/templates/index.html` | +2 / -1 | wired `data-jobs-url` + `url_for('jobs.failed_jobs_page')` |
| `web/teslausb_web/app.py` | +14 | imports + register `jobs_bp` + `_register_jobs_service` |
| `web/tests/test_jobs_service.py` | 360 | NEW — 64 cases (redactor, classifier, adapters, facade) |
| `web/tests/test_jobs_blueprint.py` | 360 | NEW — 20 cases (helpers + 5 routes happy/sad paths) |

All new modules are under the 500-LOC charter ceiling. The template
is 623 lines — comfortably inside the 535–656 budget for a faithful
port (v1 was 644). No new icons were needed — the existing Lucide
sprite already carries every glyph used.

## Gate results

| Gate | Command | Result |
| --- | --- | --- |
| Ruff lint | `python -m ruff check teslausb_web/services/jobs_service teslausb_web/blueprints/jobs.py tests/test_jobs_*.py` | ✅ All checks passed |
| Ruff format | `python -m ruff format <same paths>` | ✅ 9 files reformatted then clean |
| Mypy strict | `python -m mypy teslausb_web/services/jobs_service teslausb_web/blueprints/jobs.py teslausb_web/app.py tests/test_jobs_*.py` | ✅ 10 source files, 0 issues |
| Vulture | `python -m vulture teslausb_web/services/jobs_service teslausb_web/blueprints/jobs.py --min-confidence 80` | ✅ no dead code |
| Bandit | `python -m bandit -q -r teslausb_web/services/jobs_service teslausb_web/blueprints/jobs.py` | ✅ no findings |
| Pytest (full suite, ResourceWarnings as errors) | `python -W error::ResourceWarning -m pytest -q` | ✅ **1717 passed, 23 skipped** |

### Per-module coverage (new code)

| Module | Stmts | Coverage |
| --- | ---: | ---: |
| `blueprints/jobs.py` | 108 | **100%** |
| `services/jobs_service/__init__.py` | 47 | **90%** |
| `services/jobs_service/_classifier.py` | 30 | **98%** |
| `services/jobs_service/_cloud_sync_adapter.py` | 38 | **100%** |
| `services/jobs_service/_indexer_adapter.py` | 25 | **96%** |
| `services/jobs_service/_models.py` | 36 | **100%** |
| `services/jobs_service/_redactor.py` | 15 | **100%** |
| **jobs_service package + blueprint** | **299** | **98%** |

Targets (≥ 85 % jobs_service, ≥ 90 % blueprint, ≥ 80 % total) all
exceeded.

## B-1 deviations from v1

1. **`archive` subsystem fully dropped.** v1 unified three
   subsystems: `archive`, `indexer`, `cloud_sync`. B-1's cleanup
   pipeline is a fire-and-forget filesystem move with no queue layer
   (`docs/00-PLAN.md` "no IMG/loopback" invariant), so there is no
   `archive` dead-letter store to surface. The blueprint, service,
   and UI never expose an `archive` filter pill, and the
   `SubsystemKey` enum is closed at `{indexer, cloud_sync}`. A
   `POST /api/jobs/retry` with `subsystem=archive` is a 400.
   Documented in module docstrings on `blueprints/jobs.py` and
   `services/jobs_service/_models.py`.

2. **`indexer` adapter is a stub.** B-1's mapping service
   (`services/mapping/`) does not yet have a failed-scan dead-letter
   table — `stale_scan.py` surfaces failures via logs only. The
   adapter therefore returns `[]` / `0` for `list_rows` / `count`
   and `retry` / `delete` are no-ops. The pill stays enabled so the
   day the underlying store lands, only `_indexer_adapter.py`
   changes. The kept-ready `_build_row` helper is exercised by a
   unit test so the future wiring path is type-checked today.
   Tracking issue: https://github.com/mphacker/TeslaUSB/issues/222.

3. **`window.confirm()` → in-page modal.** The bulk Retry / Delete
   flows now use a theme-aware modal (`#confirmModal`) that supports
   `Escape` to cancel + `Enter` to confirm, matching the rest of the
   B-1 UI.

## Charter audit

### Architecture & dependency inversion
- `services/jobs_service/` package contains **zero** Flask imports.
  All Flask APIs (`current_app`, `jsonify`, `render_template`,
  `request`) live exclusively in `blueprints/jobs.py`.
- `CloudSyncAdapterProtocol` is a concrete class declaring the four
  methods the adapter needs from a cloud-archive service — production
  `CloudArchiveService` satisfies it structurally, and tests pass a
  `FakeCloud` directly without monkey-patching.
- The factory `make_jobs_service` takes `mapping_service` and
  `cloud_archive_service` arguments; the app.py wiring passes them
  through but the package never reads `app.extensions` itself.

### Type discipline
- `mypy --strict` clean. No `Any`, no `dict[str, Any]` in any new
  module. JSON payloads cross the boundary as `dict[str, object]`.
- Six frozen + slotted dataclasses + one `StrEnum` form the public
  surface. The blueprint's `_MutationRequest` is also frozen+slotted.

### Error handling
- `JobsServiceError` wraps every invalid client input; the blueprint
  catches it and emits HTTP 400 with the `{error, allowed}` JSON
  envelope.
- No `print`. No bare `except`. Logging via the module logger.

### Privacy
- `redact_last_error` strips `/mnt/...`, `/home/...`, `/var/...`,
  `/run/...`, `/tmp/...` paths; rclone-style `remote:bucket/...`
  references; and S3 virtual-host endpoints. Output capped at
  600 chars with `" …"` ellipsis so a runaway rclone stack trace
  cannot blow up the JSON payload.

### UI / UX
- CSS uses **only existing custom properties** from `style.css`
  (`--ds-accent-primary/success/warning`, `--msg-*-bg`,
  `--bg-overlay`, `--shadow-hover`, `--error-text`). No new hex
  literals introduced anywhere.
- All clickable controls have `min-height: 44px` (touch target).
- Modal supports `Escape` / `Enter` / backdrop-click. `aria-pressed`
  on filter pills; `role="listitem"` on each card; descriptive
  `aria-label` on every icon-only button.
- Failed jobs JSON is loaded client-side after page render — the
  shell renders in ~40 KB even when both subsystem DBs are slow.

### Dead code
- Vulture clean. `IndexerAdapter._build_row` is the only "future
  wiring" helper; it is exercised by
  `tests/test_jobs_service.py::TestIndexerAdapter::test_build_row_helper_produces_typed_row`
  so it is not dead.

### Security
- Bandit clean. No `subprocess`, no `eval`, no `shell=True`. JSON
  body parsed with `request.get_json(silent=True)`; type-checked
  before use.

## Outcome

- **Blockers:** 0
- **Majors:** 0
- **Minors:** 0

Ready to commit.
