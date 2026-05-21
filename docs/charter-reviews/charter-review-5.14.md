# Charter Review — Phase 5.14 (cloud_archive domain)

**Branch:** `b1-userspace-rust`
**Scope:** Commits `60805ef` (5.14a — `cloud_oauth_service`),
`3aa0464` (5.14b — `cloud_rclone_service`), `1742990` (5.14c —
`cloud_archive` package: indexer/uploader engine, package split),
`a53b3f0` (5.14d — blueprint, 33 routes), `0100ab6` (5.14e —
template + JS, closes Phase 5.14).
**Reviewer:** retroactive self-audit (Phase 5 close-out backfill).
**Date:** 2026-05-21
**Charter version:** `docs/03-CODE-QUALITY-CHARTER.md` @ b22ab482

## Files in scope (LOC at `48f9515`)

| File | LOC | Notes |
| --- | ---: | --- |
| `services/cloud_oauth_service.py` | 764 | PKCE OAuth + token refresh |
| `services/cloud_rclone_service.py` | 926 | rclone subprocess wrapper + Graph API |
| `services/cloud_archive/__init__.py` | 43 | facade |
| `services/cloud_archive/discovery.py` | 186 | filesystem walk |
| `services/cloud_archive/kv.py` | 41 | key-value helpers |
| `services/cloud_archive/paths.py` | 94 | path helpers |
| `services/cloud_archive/pipeline.py` | 427 | indexer pipeline |
| `services/cloud_archive/queue_ops.py` | 150 | queue helpers |
| `services/cloud_archive/reconcile.py` | 250 | upload state reconcile |
| `services/cloud_archive/service.py` | 188 | facade impl |
| `services/cloud_archive/settings.py` | 119 | settings adapter |
| `services/cloud_archive/uploader.py` | 372 | uploader |
| `services/cloud_archive/wifi.py` | 30 | wifi gate (nmcli) |
| `services/cloud_archive/worker.py` | 251 | scheduler worker |
| `services/cloud_archive_migrations.py` | 277 | schema |
| `services/cloud_archive_queries.py` | 223 | read-side |
| `blueprints/cloud_archive.py` | 905 | 33 routes |
| `templates/cloud_archive.html` | 2005 | template (note: 5.22 restored to v1-parity, see `charter-review-5.22.md`) |
| `static/js/cloud_archive*.js` (six files) | ~1400 | client modules |
| `helpers/refresh_cloud_token.py` | 75 | cron entry point |
| Tests | ~1900+ | 11 new test modules |

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

* **Major — God modules.** Three Python files over the 500-LOC
  ceiling:
  * `services/cloud_oauth_service.py` (764) — three providers
    (Google, Dropbox, OneDrive/Graph) × (auth URL, token
    exchange, refresh, revoke). Reasonable split candidate is
    one module per provider behind a common `OAuthProvider`
    Protocol.
  * `services/cloud_rclone_service.py` (926) — rclone CLI
    wrapper + Graph drive-mount helper. Both subsystems are
    long-lived but logically distinct; pre-Phase-6 split
    candidate.
  * `blueprints/cloud_archive.py` (905) — 33 routes in one file.
    Reasonable split is by concern: oauth flows, queue ops,
    settings, browse/inspect.
* Templates (`cloud_archive.html` 2005 LOC) are not Python and
  are not gated by the on-disk file-size rule; documented as
  v1-parity carry-over (see 5.22).
* No magic literals found in audit. The hard-coded
  `_GOOGLE_CLIENT_SECRET` at `cloud_oauth_service.py:141` is a
  legitimate public PKCE client identifier (Google's pattern for
  installed apps), labeled `# noqa: S105` correctly.
* No deep nesting found in spot-checks.

### Pillar 2 — Best Architecture Practices

* ✅ Services do not import Flask. Verified via grep.
* ✅ The 5.14c package split (`cloud_archive/`) follows the
  hexagonal pattern: `discovery.py`/`paths.py`/`wifi.py` are
  adapters; `pipeline.py`/`uploader.py`/`reconcile.py` are
  domain; `service.py` is facade.
* ✅ All HTTP / rclone subprocess boundaries are isolated in
  named helpers (`_https_request`, `_run_rclone`) with
  documented `# noqa: S310 / S603` annotations — these are the
  canonical justified exceptions documented inline.
* ✅ Frozen dataclasses on serialised models in
  `cloud_archive/service.py`.

### Pillar 3 — No Shortcuts

* ✅ No `print()`, no bare `except:`, no `: Any`, no
  `# type: ignore`, no `datetime.now()` without `tz=`.
* ✅ All `# noqa` codes are specific: `S105` (hard-coded
  password-like literal, justified for PKCE pub client),
  `S310` / `S603` (urlopen / subprocess.run with validated
  inputs, comments explain why), `PLR0913` (too-many-args, on
  generic builder).
* ✅ No unlinked TODOs in the 5.14 surface.
* ✅ No `assert` in production paths.
* ✅ The vendored Google client id/secret pair (line 141) is
  the documented installed-app credential pattern; bandit
  flagged on `# noqa: S105`, justified.

### Pillar 4 — Fix Bugs Immediately

No bug-fix commits inside the 5.14 span; pure new-feature
addition. No Boy-Scout findings.

### Pillar 5 — No Dead Code

* ✅ Vulture clean at confidence 80.
* ✅ No commented-out blocks observed.
* ✅ `helpers/refresh_cloud_token.py` (cron entrypoint) is
  exercised via `test_refresh_cloud_token.py` (124 LOC, 5 cases)
  and not vulture-flagged.

## Verdict

- **Blockers:** 0
- **Majors:** 1 (god-module: 3 Python files >500 LOC)
- **Minors:** 0
- **Nits:** 0
- **Status:** **APPROVED RETROACTIVELY** with documented Major.
  No GH issue filed — the file-size overruns are known carry-over
  for Phase 6 module-split cleanup.

## Filed issues

None.

## Notes on retroactive nature

Backfill report. The template `cloud_archive.html` was further
restored to v1 parity in commit `336bf37` (5.22) — see
`charter-review-5.22.md`. LOC reported here is as of 48f9515.
