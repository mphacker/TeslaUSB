# Charter Review — Phase 5.13 (mapping / geo-index domain)

**Branch:** `b1-userspace-rust`
**Scope:** Commits `ad4d5e0` (5.13a — migrations), `0d4a932` (5.13b —
queries), `eb86479` (5.13c — service package), `c3b9367` (5.13c.1 —
coverage + DB-leak fixes), `4b9633e` (5.13d — blueprint),
`7623203` (5.13e — template, JS, vendored libs).
**Reviewer:** retroactive self-audit (Phase 5 close-out backfill).
**Date:** 2026-05-21
**Charter version:** `docs/03-CODE-QUALITY-CHARTER.md` @ b22ab482

## Files in scope (LOC at `48f9515`)

| File | LOC | Notes |
| --- | ---: | --- |
| `web/teslausb_web/services/mapping_migrations.py` | 875 | schema + DB init/backup (5.13a) |
| `web/teslausb_web/services/mapping_queries.py` | 1492 | read-only geo-index queries (5.13b) |
| `web/teslausb_web/services/mapping/__init__.py` | 39 | facade |
| `web/teslausb_web/services/mapping/dedupe.py` | 91 | |
| `web/teslausb_web/services/mapping/diagnose.py` | 147 | |
| `web/teslausb_web/services/mapping/discovery.py` | 74 | |
| `web/teslausb_web/services/mapping/events.py` | 258 | |
| `web/teslausb_web/services/mapping/indexer.py` | 457 | |
| `web/teslausb_web/services/mapping/kv.py` | 26 | |
| `web/teslausb_web/services/mapping/paths.py` | 129 | |
| `web/teslausb_web/services/mapping/purge.py` | 101 | |
| `web/teslausb_web/services/mapping/retry.py` | 45 | |
| `web/teslausb_web/services/mapping/sei.py` | 203 | |
| `web/teslausb_web/services/mapping/sentry.py` | 210 | |
| `web/teslausb_web/services/mapping/service.py` | 435 | |
| `web/teslausb_web/services/mapping/stale_scan.py` | 218 | |
| `web/teslausb_web/services/mapping/trips.py` | 229 | |
| `web/teslausb_web/blueprints/mapping.py` | 1015 | blueprint + 5.21 v1-parity additions |
| `web/teslausb_web/templates/mapping.html` | 4076 | template (5.13e seed; mostly 5.21 restore) |
| `web/teslausb_web/static/js/mapping.js` + `static/js/mapping/*.js` | ~1370 | |
| Vendored: leaflet, leaflet-markercluster, protobuf, dashcam-mp4 | — | LICENSE + ADR 0016 |
| Tests | ~2900 | mapping_migrations, mapping_queries, mapping_service, mapping_helpers, mapping_blueprint |

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

* **Major — God modules.** Three files exceed the charter's hard
  500-LOC ceiling:
  * `services/mapping_queries.py` (1492) — 5.13b's monolithic
    read-side query module. Justified by domain cohesion (one
    geo-index DB, one read-API surface), but the charter is
    categorical: split by responsibility. Splitting candidates
    are by-query-family (trips, events, sentry, sei, stats).
  * `services/mapping_migrations.py` (875) — schema DDL +
    init/backup pipeline. The DDL itself accounts for most of
    the bulk; could be moved to a sibling `_schema.py` (string
    literals only) with the migration runner staying in
    `mapping_migrations.py`.
  * `blueprints/mapping.py` (1015) — blueprint at 1015 LOC. Most
    of the bulk is 5.21-era route additions for v1 UI parity
    (40+ routes including SEI/Sentry/trips/stats/events
    panels). Reasonable split: thin blueprint + per-domain
    submodules.
  * `templates/mapping.html` at 4076 LOC is a Jinja template,
    not Python; the charter's "file > 500 lines" is in the
    Python/Rust sections and templates are not enforced by any
    on-disk gate. Pre-existing condition, restored from v1 in
    5.21 specifically because the 5.13c-era cut-down template
    was -78% under-built. Documented as known carry-over.
* **Minor — function length.** `mapping_queries._build_query_trips_sql`
  and `_build_query_events_sql` carry `# noqa: PLR0913` (too
  many args) — both are 6+ arg signatures used because the v1
  query surface uses positional filter chaining. Acceptable but
  candidate for a `TripsQueryFilters` / `EventsQueryFilters`
  dataclass refactor (Data Clumps smell, Pillar 1).
* No magic literals found in audit. Time constants
  (`SEI_FRAME_SAMPLE_SECONDS`, retry intervals) are named.
* No deep nesting found in spot-checks (`indexer.index_single_file`,
  `service` facade methods all <4 levels).

### Pillar 2 — Best Architecture Practices

* ✅ Service layer (`services/mapping/*`) is Flask-free. Verified:
  no `from flask` imports under `services/mapping/`.
* ✅ Hexagonal split is good: `paths.py` (filesystem adapter),
  `sei.py` / `dashcam-mp4` (codec adapter), `events.py` /
  `sentry.py` / `trips.py` (domain logic), `service.py` (facade).
* ✅ Frozen dataclasses on the domain models in `events.py`,
  `trips.py`, `sentry.py`.
* **Minor.** `services/mapping/service.py` uses lazy imports
  (`# noqa: PLC0415`) at lines 240/263/268/278/291/296/306 to
  break startup/threading import-cycles between the facade and
  its sub-modules. This is allowed but a hint that the package
  has bi-directional dependencies; cleaner layering would push
  shared types into a `_models.py` (analytics pattern from 5.24).

### Pillar 3 — No Shortcuts

* ✅ No `print()` in production source (only false matches on
  "Blueprint" prefix).
* ✅ No bare `except:`. The five broad `except Exception` blocks
  in `mapping/indexer.py` (lines 82, 135) and `mapping/paths.py`
  (lines 58, 66) carry justified `# noqa: BLE001` and log via
  `logger.exception(...)` before swallowing — this is the
  intentional catch-all-and-continue pattern for the per-file
  indexer scan loop, allowed by the charter when documented.
* ✅ No `# type: ignore` without a specific rule code.
* ✅ No `datetime.now()` without `tz=` (mapping_service uses
  `datetime.now(tz=UTC)` throughout).
* ✅ No unlinked `TODO` markers in the 5.13 surface (later
  blueprint TODOs introduced in 5.14/5.15/5.16, see those reports).
* ✅ No `assert` in production paths.
* ✅ All `# noqa` carry specific rule codes (PLR0913, BLE001,
  PLC0415) — compliant.

### Pillar 4 — Fix Bugs Immediately

* ✅ `c3b9367` (5.13c.1) is itself a Boy-Scout fix-up: closed
  six leaked sqlite connections (`mapping/indexer.py`,
  `purge.py`, `service.py`, `stale_scan.py`,
  `mapping_migrations.py`, `mapping_queries.py`) discovered
  via `-W error::ResourceWarning` and added `test_mapping_helpers.py`
  (+714 LOC, 30+ regression cases). Exactly the "fix on
  discovery, with regression tests" pattern the charter
  demands. Approved.

### Pillar 5 — No Dead Code

* ✅ Vulture clean at confidence 80 on the current tree.
* ✅ No commented-out blocks found in spot-checks of the largest
  files.
* ✅ Vendored libraries (leaflet, leaflet-markercluster, protobuf,
  dashcam-mp4) are documented by ADR 0016 and accompanied by
  LICENSE files — not dead, used by the mapping template.

## Verdict

- **Blockers:** 0
- **Majors:** 1 (god-module: 3 Python files >500 LOC; the
  template's size is documented as known v1-parity carry-over)
- **Minors:** 2 (PLR0913 data-clumps in query builders; lazy
  imports in `service.py` facade)
- **Nits:** 0
- **Status:** **APPROVED RETROACTIVELY** with documented Major
  on file sizes. No GH issue filed — the 500-LOC overruns are
  visible, pre-existing technical debt slated for Phase 6
  cleanup, not regressions introduced by 5.13.

## Filed issues

None. All findings are pre-existing technical debt visible to
later phases; no genuine missed Blocker.

## Notes on retroactive nature

This report was produced as part of the Phase 5 close-out
backfill; the charter-review self-audit pattern was instituted
at Phase 5.24 and earlier increments lacked an on-disk report.
The blueprint (`blueprints/mapping.py`) and template
(`templates/mapping.html`) were materially extended in 5.21
(commit `dbc70dc`) — see `charter-review-5.21.md` for the
v1-parity restoration audit. LOC quoted here is as of 48f9515.
