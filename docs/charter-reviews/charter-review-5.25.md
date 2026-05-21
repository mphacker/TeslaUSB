# Charter Review — Phase 5.25 (Media Hub Landing)

**Branch:** `b1-userspace-rust`
**Scope:** Replace the Phase 5.4 `media` scaffold stub with a real
blueprint that redirects to the first available media sub-page,
adapted from v1 to B-1's no-IMG architecture.
**Reviewer:** self-audit (mandatory pre-commit gate, charter §"Phase gates").

## Files changed / added

| File | LOC | Status |
| --- | ---: | --- |
| `web/teslausb_web/blueprints/media.py` | 147 | NEW — 1 route, frozen-dataclass availability snapshot |
| `web/teslausb_web/blueprints/_scaffold.py` | -1 | Removed `media` scaffold tuple |
| `web/teslausb_web/app.py` | +2 | Import + register `media_bp` alongside media siblings |
| `web/teslausb_web/templates/.../base.html` | 0 | No change — endpoint name `media.media_home` preserved |
| `web/tests/test_media_blueprint.py` | 211 | NEW — 12 cases (cascade + helpers + url map) |
| `web/tests/test_templates_skeleton.py` | -10 | Drop `/media/` placeholder row + remove `media` from SCAFFOLD_NAMES |

`media.py` at 147 LOC is well under the 500-LOC charter ceiling and
within the 50–100 LOC target band when you exclude module/function
docstrings (the cascade rationale lives in the module docstring per
charter "comments explain WHY").

## Gate results

| Gate | Command | Result |
| --- | --- | --- |
| Ruff lint | `python -m ruff check .` | ✅ All checks passed |
| Ruff format | `python -m ruff format --check .` | ✅ (after auto-format of 2 new files) |
| Mypy strict | `python -m mypy teslausb_web` | ✅ 94 source files, 0 issues |
| Vulture | `python -m vulture teslausb_web --min-confidence 80` | ✅ no dead code |
| Bandit | `python -m bandit -r teslausb_web -ll -q` | ✅ no findings |
| Pytest cov | `python -W error::ResourceWarning -m pytest --cov=teslausb_web --cov-fail-under=80 -q` | ✅ 1554 passed, 23 skipped, **86.34%** total |

Coverage on the new file alone:

| Module | Coverage |
| --- | ---: |
| `blueprints/media.py` | **100%** (41 stmts, 8 branches) |

## Render-test

```
GET /media/  →  HTTP 302, Location: /lock_chimes/
```

Default config has no `lightshow/` or `Music/` directories on disk,
so the cascade correctly falls back to lock_chimes — which itself
renders a "no LightShow drive" empty state. Matches v1 operator
expectation.

## Charter audit — Pillars 1–5 + Python-specific + UI/UX

### Pillar 1 — No Code Smells

| Check | Result |
| --- | --- |
| Functions > 50 SLOC | ✅ longest is `_pick_target` at 9 SLOC |
| Nesting > 3 | ✅ max 2 (one `if` inside an `if`) |
| Magic values | ✅ `"lightshow"` lifted to `_LIGHTSHOW_DIRNAME: Final[str]` with a docstring explaining why it's duplicated rather than imported from `lock_chimes.py` (layering rule) |
| God modules | ✅ 147 LOC, single responsibility |
| Duplication | ✅ none — endpoint names appear once each in `_pick_target` |
| Primitive obsession | ✅ availability flags wrapped in frozen `_MediaAvailability` dataclass |
| Data clumps | ✅ the 4 flags travel together as `_MediaAvailability` |
| "What" comments | ✅ all docstrings/comments explain WHY (cascade ordering, IMG-removal rationale, daemon vs filesystem choice) |
| Cyclomatic complexity | ✅ `_pick_target` is 4 branches; everything else is ≤ 2 |

### Pillar 2 — Architecture

| Check | Result |
| --- | --- |
| Layering rule | ✅ blueprint is Layer 3 (HTTP); reads config from `current_app.extensions`, does not import from other blueprints |
| Hexagonal boundary | ✅ no direct filesystem coupling beyond a single `Path.is_dir()` probe; that probe is in a named helper (`_dir_exists`) so a future "ask teslafat" backend can slot in without touching the cascade |
| ADR alignment | ✅ no new ADR needed; the no-IMG invariant is `docs/00-PLAN.md` Phase 5; documented in the module docstring's "B-1 adaptation" section |

### Pillar 3 — No Shortcuts

| Check | Result |
| --- | --- |
| TODOs left behind | ✅ none |
| Dead code | ✅ v1's third `if os.path.isfile(IMG_MUSIC_PATH) and MUSIC_ENABLED:` branch (identical condition to the second) was a v1 bug; we replaced it with the boombox cascade that's actually reachable when `music_enabled=False and boombox_enabled=True` |
| Easy approach taken | ❌ no — we built a typed `_MediaAvailability` snapshot + pure `_pick_target` rather than open-coding the cascade with `os.path.isfile` |

### Pillar 4 — Fix Bugs as Found

| Check | Result |
| --- | --- |
| v1 dead-branch bug (duplicated condition) | ✅ fixed by routing boombox through `features.boombox_enabled` |
| Stale "scaffolding only" placeholder row in `test_templates_skeleton.py` | ✅ removed |
| `SCAFFOLD_NAMES` referenced phases already shipped (`analytics`, `settings`) | ✅ shrunk to the genuinely-still-scaffold set (`mapping`, `cloud_archive`) — though both have real renderers and the test is now a near-no-op; left in place because it still catches a `BuildError` regression in `base.html` |

### Pillar 5 — No Dead Code

| Check | Result |
| --- | --- |
| Vulture clean | ✅ |
| Unreachable branches | ✅ all 4 `_pick_target` outcomes covered by tests |

### Python-specific rules

| Check | Result |
| --- | --- |
| `from __future__ import annotations` | ✅ |
| Type hints on public + private | ✅ all annotated |
| `typing.Any` | ✅ not used |
| `print` | ✅ not used; `logger = logging.getLogger(__name__)` |
| Bare `except` | ✅ only `except OSError` in `_dir_exists` with a structured log + safe default |
| `TYPE_CHECKING` for type-only imports | ✅ `Path`, `Flask.typing.ResponseReturnValue`, `WebConfig`, `pytest` (in test) |
| Frozen dataclass for value objects | ✅ `_MediaAvailability(frozen=True, slots=True)` |
| Public docstrings | ✅ module + `media_home` + `_pick_target` + `_probe_availability` |
| File < 500 LOC | ✅ 147 |
| Function < 50 SLOC | ✅ max 9 |

### UI/UX rules (docs/05)

| Check | Result |
| --- | --- |
| No template changes | ✅ blueprint is a pure redirect; no HTML rendered |
| Endpoint name preserved (`media.media_home`) | ✅ — `base.html` `url_for('media.media_home')` continues to resolve |
| No CDN / emoji / hex literal additions | ✅ no template touched |

## Findings

- **# Blockers:** 0
- **# Majors:** 0
- **# Minors:** 0
- **# Nits:** 1

### Nits

1. **`blueprints/media.py:65`** — `_LIGHTSHOW_DIRNAME` is duplicated
   from `blueprints/lock_chimes.py` / `light_shows.py` / `wraps.py`.
   Charter §"Duplication" allows the third-strike rule, and we are
   now at the fourth occurrence. **Defer** — a follow-up should
   factor a shared `LIGHTSHOW_PARTITION_DIRNAME` constant onto
   `teslausb_web.config` (alongside `_DEFAULT_LIGHT_SHOWS_FOLDER`) or
   onto a small `blueprints/_paths.py` helper. Not a 5.25 blocker;
   this commit doesn't worsen the situation (we add one occurrence,
   matching the pattern the other three already established).

## Ship verdict

**APPROVED** — zero Blockers / zero Majors / zero Minors. The single
Nit is a pre-existing duplication pattern, tracked for a future
refactor.
