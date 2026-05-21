# Charter Review — Phase 5.24 (Storage Analytics)

**Branch:** `b1-userspace-rust`
**Scope:** Replace the Phase 5.4 `analytics` scaffold stub with a real
blueprint + service + template ported from v1, adapted to B-1's
btrfs + mapping-DB architecture. No IMG/loopback assumptions.
**Reviewer:** self-audit (mandatory pre-commit gate, charter §"Phase gates").

## Files changed / added

| File | LOC | Status |
| --- | ---: | --- |
| `web/teslausb_web/config.py` | +27 | `[analytics]` section + parsing |
| `web/teslausb_web/app.py` | +27 | Wires `analytics_service` + blueprint |
| `web/teslausb_web/blueprints/_scaffold.py` | -1 | Removed analytics tuple |
| `web/teslausb_web/blueprints/analytics.py` | 133 | NEW — 5 routes |
| `web/teslausb_web/services/analytics_service/__init__.py` | 213 | NEW — facade + factory |
| `web/teslausb_web/services/analytics_service/_models.py` | 157 | NEW — dataclasses |
| `web/teslausb_web/services/analytics_service/_compute.py` | 294 | NEW — helpers |
| `web/teslausb_web/services/analytics_service/_serializers.py` | 83 | NEW — to_dict |
| `web/teslausb_web/templates/analytics.html` | 332 | NEW — Lucide UI |
| `web/teslausb_web/static/js/analytics.js` | 113 | NEW — driving-stats hook |
| `web/tests/test_analytics_service.py` | 470 | NEW — 36 cases |
| `web/tests/test_analytics_blueprint.py` | 311 | NEW — 18 cases |
| `web/tests/test_templates_skeleton.py` | -1 | Drop `/analytics/` placeholder row |

Every source file is below the charter's hard 500-LOC ceiling
(largest production module: `_compute.py` at 294).

## Gate results

| Gate | Command | Result |
| --- | --- | --- |
| Ruff lint | `python -m ruff check teslausb_web tests` | ✅ All checks passed |
| Ruff format | `python -m ruff format --check teslausb_web tests` | ✅ 151 files clean |
| Mypy strict | `python -m mypy teslausb_web` | ✅ 93 source files, 0 issues |
| Vulture | `python -m vulture teslausb_web --min-confidence 80` | ✅ no dead code |
| Bandit | `python -m bandit -r teslausb_web -ll -q` | ✅ no findings |
| Pytest cov | `python -W error::ResourceWarning -m pytest --cov=teslausb_web --cov-fail-under=80` | ✅ 1543 passed, 23 skipped, **86.33%** total |

Coverage on the new files alone:

| Module | Coverage |
| --- | ---: |
| `blueprints/analytics.py` | 100% |
| `services/analytics_service/__init__.py` | 95% |
| `services/analytics_service/_compute.py` | 99% |
| `services/analytics_service/_models.py` | 100% |
| `services/analytics_service/_serializers.py` | 100% |
| **Combined new-code total** | **99%** |

Both per-file 85% floors comfortably exceeded.

## Render-test

```
GET /analytics/  →  HTTP 200, 19,685 bytes
```

Dashboard renders end-to-end against the default config (no clips
indexed) without raising. Empty-state placeholders engage in the
`analytics is none` and `total_files == 0` branches.

## URL-map verification

```
analytics.api_data            -> /analytics/api/data
analytics.api_health          -> /analytics/api/health
analytics.api_partition_usage -> /analytics/api/partition-usage
analytics.api_video_stats     -> /analytics/api/video-stats
analytics.dashboard           -> /analytics/
```

Five routes registered, matching the v1 contract (minus the v1
IMG-gating `before_request`).

## Pillar walkthrough

### Pillar 1 — Code smells

* ✅ **No god modules.** Service split into `_models`, `_compute`,
  `_serializers`, and a slim `__init__` facade (largest file 294
  lines, charter target < 300).
* ✅ **No long functions.** Largest function is
  `_compute.compute_health` at 22 SLOC. The original 38-line
  loop was refactored — per-partition severity recording lives in
  `_record_severity()` and severity classification in
  `_classify_percent()`.
* ✅ **No magic literals.**
  * Storage-health thresholds (80 / 90 / 95 %) live in the new
    `AnalyticsSection` config dataclass.
  * Theoretical record rate (0.4 GB/hr) lives in
    `AnalyticsSection.theoretical_gb_per_hour`.
  * Tesla clip cadence (60 s) is named `CLIPS_PER_HOUR` in
    `_models.py`.
  * High-confidence threshold (100 clips) is named
    `HIGH_CONFIDENCE_VIDEO_COUNT`.
  * `1024**3` lives in `BYTES_PER_GIB`.
* ✅ **No deep nesting.** Maximum depth is 3 (loop → if → branch),
  achieved via early `continue` in `compute_health` and helper
  factoring in `_accumulate_row`.
* ✅ **No print().** All diagnostics go through `logger.warning`.

### Pillar 2 — Dead code & shortcuts

* ✅ No commented-out code, no `# TODO` markers introduced.
* ✅ No bare `except:` — every catch names a concrete exception
  type (`OSError`, `sqlite3.Error`, `AnalyticsDataError`,
  `AnalyticsError`).
* ✅ Vulture clean at confidence 80.

### Pillar 3 — Architecture & layering

* ✅ **Service ↔ blueprint boundary respected.** The blueprint
  never touches the mapping DB directly; everything goes through
  `AnalyticsService`. The service never imports Flask.
* ✅ **No IMG/loopback assumptions.** v1's `iter_all_partitions`
  helper is gone. We probe filesystem roots via
  `shutil.disk_usage` and dedupe by `st_dev` so a single backing
  volume reports once.
* ✅ **Typed exceptions** for the boundary:
  `AnalyticsError → AnalyticsConfigError, AnalyticsDataError`,
  inheriting from `RuntimeError`. Blueprint maps
  `AnalyticsDataError` to 503 and other `AnalyticsError`
  subclasses to 500.
* ✅ **Frozen dataclasses.** Every public model is
  `@dataclass(frozen=True, slots=True)`. The only mutable
  dataclass is the private `_FolderAccumulator` scratch bucket.
* ✅ **tz-aware datetimes only.** `utc_now()` always returns
  `datetime.now(tz=UTC)`; `iso_from_mtime()` uses `tz=UTC`.

### Pillar 4 — UI/UX charter

* ✅ **Lucide SVG icons** (`icon-bar-chart-2`, `icon-hard-drive`,
  `icon-clock`, `icon-video`, `icon-folder`, `icon-info`,
  `icon-alert-triangle`, `icon-alert-circle`, `icon-check-circle`).
  No emoji ported from v1.
* ✅ **CSS tokens only.** No inline hex colors. `analytics.js`
  resolves chart-palette colours from `--color-*` custom
  properties at runtime.
* ✅ **Inline `style="width:…%"`** on progress bars is allowed by
  the charter exception ("width is not a colour").
* ✅ **Aria-labels** on every icon-only progress bar and severity
  badge; decorative icons carry `aria-hidden="true"` and
  `focusable="false"`.
* ✅ **No `unpkg.com`** or third-party CDN URLs.

### Pillar 5 — Test discipline

* ✅ 54 new tests (36 service + 18 blueprint), all named for
  behaviour (`test_db_error_raises_typed_exception`,
  `test_factory_dedups_partitions_by_device`, etc.).
* ✅ No `sleep()` in tests. SQLite uses `:memory:` with a fake
  `MappingService.open_db()` context manager.
* ✅ `pytest -W error::ResourceWarning` clean — the fake
  closes its sqlite connection in a `finally`.
* ✅ Branch coverage enabled by repo config (`--cov-branch` in
  `pyproject.toml`); new modules sit at 99 % combined.

## Findings

* **Blockers:** none.
* **Majors:** none.
* **Minors:** none.
* **Nits:**
  * `analytics.js` ships ahead of the `/api/driving-stats` and
    `/api/event-charts` endpoints (those live in `mapping.py`
    and already exist on B-1; the JS is a no-op when they return
    `has_data: false`, so no degradation).
  * `_compute.summarize_indexed_files` returns a tuple sorted by
    `size_bytes`; ties resolve by dict-insertion order. Documented
    in tests; no Charter rule applies.

## Sign-off

All six charter gates pass. New code at 99 % coverage. Render-test
shows 19,685-byte dashboard against the default config. No
Blockers or Majors — proceeding to commit.
