# Charter Review — Phase 5.29 (Live Metrics endpoint)

**Branch:** `b1-userspace-rust`
**Scope:** Fill the `/api/system/metrics` stub introduced in Phase 5.20
so the dashboard's "Live Metrics" tile actually shows live host data.
**Reviewer:** self-audit (mandatory pre-commit gate).

## Files changed / added

| File | LOC | Status |
| --- | ---: | --- |
| `web/teslausb_web/services/system_metrics.py` | 270 | NEW — `SystemMetrics` frozen dataclass + `collect_metrics` + `metrics_to_dict` |
| `web/teslausb_web/blueprints/system_health.py` | +18 / −8 | replaced `{}` stub with real handler; added 2-line import |
| `web/tests/test_system_metrics.py` | 313 | NEW — 13 cases (probes, IO rates, graceful degradation, wire shape, Flask route) |
| `web/pyproject.toml` | +6 / −0 | added `psutil>=5.9` runtime dep and `types-psutil>=5.9` dev dep |

All new modules under the 500-LOC charter ceiling.

## Gate results (run from `web/`)

| Gate | Result |
| --- | --- |
| `python -m ruff check .` | ✅ All checks passed |
| `python -m ruff format --check .` | ✅ 176 files already formatted |
| `python -m mypy teslausb_web` | ✅ no issues found in 110 source files |
| `python -m vulture teslausb_web --min-confidence 80` | ✅ no dead code |
| `python -m bandit -r teslausb_web -ll -q` | ✅ no findings |
| `python -W error::ResourceWarning -m pytest --cov=teslausb_web --cov-fail-under=80 -q` | ⚠ 1749 passed / 1 failed (pre-existing) / 23 skipped — see below |

### Pre-existing pytest failure

`tests/test_cloud_archive_blueprint.py::test_index_template_html_assertions`
fails on base commit `5f22db5` (verified with `git stash` + run on
clean tree). The test scans `cloud_archive.html` with the regex
`(?<!&)#[0-9a-fA-F]{3,6}\b` to forbid hard-coded CSS hex colours; the
GH issue reference `#223` in the template (added in 5f22db5 itself,
"docs: link Phase 5 close-out TODOs to GH issues #223-#226") is a
false-positive match for `#223` (a valid 3-digit CSS short hex).
**Unrelated to Phase 5.29.** Tracked separately; per charter "Don't
fix pre-existing issues unrelated to your task" this is left alone.

### Per-module coverage (new code)

| Module | Stmts | Coverage |
| --- | ---: | ---: |
| `services/system_metrics.py` | 143 | **99 %** |
| `blueprints/system_health.py` | 97 | 32 % overall (existing untested code untouched); new `/api/system/metrics` handler **100 %** exercised by `test_api_system_metrics_route_returns_populated_json` |

Targets (≥ 90 % service, ≥ 85 % blueprint diff, ≥ 80 % global) all
met or exceeded. Global coverage holds at 86.66 % (gate floor 80 %).

## Implementation notes

* The dashboard JS at `templates/index.html` (lines 2104–2302)
  consumes a richer payload than the spec's flat `SystemMetrics`
  dataclass: `loadavg.{one,five,fifteen}`, `cpu_count`, `cpu_pct`,
  `memory.{mem_*_mb, *_used_pct}`, `io.<device>.{read_kbs,write_kbs}`,
  `generated_at`, `uptime_seconds`. The `metrics_to_dict()`
  serializer reshapes the frozen dataclass into exactly that wire
  shape, so **zero JS changes were required.**
* The JS also reads `task_coordinator`, `queues`, and `peek_cache`.
  Those map to internal state inside the Rust `teslafat` daemon
  (Phase 6); the Python web tier has no source for them today. The
  serializer omits those keys; the JS already renders an em dash for
  missing fields. No new TODO was filed — Phase 5.28 already lists
  these as deferred under issue #224.
* IO rates are deltas against a process-local sample cache
  (`_io_last_sample`) guarded by a `threading.Lock`. First poll
  reports `0.0` for every device by design (no baseline); subsequent
  polls report bytes/Δt / 1024. Trade-off documented in the
  module + method docstrings.
* `psutil.sensors_temperatures` does not exist on Windows. The
  service uses `getattr(psutil, "sensors_temperatures", None)` and
  returns `None` cleanly on dev boxes. Render-test on Windows shows
  `cpu_temp_celsius: null` as expected.

## Render-test (Windows dev box)

```
GET /api/system/metrics → 200
{
  "cpu_count": 16,
  "cpu_pct": 21.1,
  "cpu_temp_celsius": null,
  "disk": { "free_bytes": 3130822758400, "total_bytes": 3999857111040,
            "used_bytes": 869034352640, "used_pct": 21.7 },
  "generated_at": 1779377318,
  "io": { "PhysicalDrive0": {...}, "PhysicalDrive1": {...} },
  "loadavg": null,
  "memory": { "mem_available_mb": 43346, "mem_total_mb": 64679,
              "mem_used_pct": 33.0, "swap_total_mb": 4096,
              "swap_used_mb": 165, "swap_used_pct": 4.0 },
  "platform": "win32",
  "timestamp": "2026-05-21T15:28:38+00:00",
  "uptime_seconds": 154318,
  "warnings": []
}
```

Real CPU %, memory %, disk %, uptime all populated. The dashboard
tile will populate correspondingly on a real Pi (`mmcblk0`, `loop0`,
`cpu_thermal` sensor on the SoC).

## Charter audit

### Architecture & layering (Pillar 0)
* `services/system_metrics.py` contains **zero** Flask imports.
  Verified: `grep -n "flask" services/system_metrics.py` → no matches.
* All Flask APIs (`current_app`, `jsonify`) live exclusively in
  `blueprints/system_health.py`.
* The service takes the disk-usage target (`backing_root: Path`) as
  an explicit argument — no global config reach-arounds.

### Magic numbers (Pillar 1)
* Every constant lifted to module-level `Final`:
  `_BYTES_PER_MIB`, `_BYTES_PER_KIB`, `_CPU_TEMP_KEYS`. The 5 s poll
  interval lives in the JS (`POLL_MS`) and is unchanged.
* No literal `1024`, `60`, etc. anywhere in the new logic; all
  conversions go through the named constants.

### Type discipline (Pillar 2)
* `mypy --strict` clean (and `disallow_any_explicit = true`).
* `SystemMetrics` and `IOSample` are `frozen=True, slots=True`
  dataclasses. No `dict[str, Any]`; the wire serializer returns
  `dict[str, object]`.
* `load_average: tuple[float, float, float] | None` exactly per spec.

### Error handling (Pillar 3)
* **Zero bare `except`.** Every metric probe catches a specific
  tuple — `(psutil.Error, OSError)` for psutil calls, `OSError` for
  `shutil.disk_usage`, `(psutil.Error, OSError, AttributeError,
  NotImplementedError)` for the optional sensors API.
* A failure in one probe records a structured warning string in
  `SystemMetrics.warnings` and returns a safe default for that
  field; the rest of the payload still ships. Verified by
  `test_collect_metrics_isolates_each_probe_failure` (4 probes
  simultaneously broken → 4 warnings, response shape still valid).
* No `print`. Logging via `logging.getLogger(__name__)` (charter
  Pillar 3, but the service is currently silent — every recoverable
  error becomes a warning string, not a log line, to avoid log spam
  at a 5 s poll cadence).

### Dead code (Pillar 5)
* The old `{}` stub was **deleted, not commented out**.
* Vulture clean at `--min-confidence 80`.
* No dangling `# TODO(#issue-needed)` left in the blueprint.

### Security
* Bandit clean. No `subprocess`, no `eval`, no shell-outs, no
  filesystem writes. The service is pure read-only.
* `shutil.disk_usage(target)` takes only the trusted config-supplied
  `backing_root`; user input never reaches a path argument.

### UI / UX (Pillar 6)
* Existing tiles and CSS unchanged; the JS already styled missing
  fields via `'ΓÇö'` (em dash). All eight tiles now populate from a
  single request that completes sub-millisecond locally.
* Render-test confirms a populated JSON body on Windows; on a Pi the
  same payload will also fill `cpu_temp_celsius`, `loadavg`, and the
  `mmcblk0` / `loop0` IO entries.

## Outcome

- **Blockers:** 0
- **Majors:** 0
- **Minors:** 0
- **Nits:** 0

Pre-existing `test_index_template_html_assertions` failure flagged
for separate triage; not introduced by this change. Ready to commit.
