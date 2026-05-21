# Charter Review — Phase 5.28 (api legacy-compat shim — Phase 5 close-out)

**Branch:** `b1-userspace-rust`
**Scope:** Port v1's 196-LOC `api` blueprint (7 routes) onto B-1.
Preserve every URL external Tesla phone-home scripts depend on,
shim where a B-1 service can satisfy the contract, and surface
structured JSON for routes whose v1 subsystem no longer exists in
B-1. Closes Phase 5.
**Reviewer:** self-audit (mandatory pre-commit gate).

## Files changed / added

| File | LOC | Status |
| --- | ---: | --- |
| `web/teslausb_web/blueprints/api.py` | 327 | NEW — 7 routes, 4 TypedDict bodies, 1 `_api_error` factory |
| `web/teslausb_web/services/lock_chime_service.py` | +60 | added `rename_chime_file()` + exported in `__all__` |
| `web/teslausb_web/app.py` | +2 | import + register `api_bp` |
| `web/tests/test_api_blueprint.py` | 215 | NEW — 14 cases (every route happy + sad paths) |
| `web/tests/test_lock_chime_service.py` | +48 | 6 cases for `rename_chime_file` |

All new modules sit comfortably under the 500-LOC charter ceiling.
No function exceeds the 50-SLOC budget (longest is
`rename_chime_file` at ~25 SLOC including docstring).

## Per-route disposition

| v1 Route | B-1 disposition | Status | Body |
| --- | --- | ---: | --- |
| `GET  /api/operation_status` | **shim** (always not-in-progress) | 200 | `{in_progress, operation, message}` |
| `GET  /api/chime_filenames` | **shim** → `lock_chime_service.list_chime_files` | 200 | `{chime_filenames: [...]}` |
| `POST /api/rename_chime/<old>/<new>` | **shim** → `lock_chime_service.rename_chime_file` (NEW) | 200 / 400 / 404 / 409 / 500 | `{success, old, new}` or `ApiError` |
| `GET  /api/gadget_state` | **drop** (no IPC method) | 503 | `ApiError(error="not_implemented", phase="6")` |
| `POST /api/recent_archive/trigger` | **drop** (no IMG/SD archive subsystem) | 501 | `ApiError(error="not_implemented")` |
| `GET  /api/recent_archive/status` | **drop** (mirrors trigger) | 501 | `ApiError(error="not_implemented")` |
| `POST /api/recover_gadget` | **drop** (Rust worker owns recovery) | 410 | `ApiError(error="deprecated")` |

Every dropped route still **exists** under its original URL with a
structured JSON body — Tesla phone-home scripts that probe these
URLs see an unambiguous machine-readable token rather than 404.

## Gate results (from `web/`)

| Gate | Command | Result |
| --- | --- | --- |
| Ruff lint | `python -m ruff check .` | ✅ All checks passed |
| Ruff format | `python -m ruff format .` | ✅ (9 pre-existing files normalised + my 3) |
| Mypy strict | `python -m mypy teslausb_web` | ✅ 109 source files, 0 issues |
| Vulture | `python -m vulture teslausb_web --min-confidence 80` | ✅ no dead code |
| Bandit | `python -m bandit -r teslausb_web -ll -q` | ✅ no findings |
| Pytest (full, `-W error::ResourceWarning`) | `python -m pytest --cov=teslausb_web --cov-fail-under=80 -q` | ✅ **1737 passed, 23 skipped, 86.55 % total** |
| Render-test (all 7 routes) | manual `Flask.test_client` open | ✅ 200 / 200 / 400 / 503 / 501 / 501 / 410 |

### Per-module coverage (new code)

| Module | Stmts | Coverage |
| --- | ---: | ---: |
| `blueprints/api.py` | 80 | **100 %** |
| `services/lock_chime_service.py` (added function) | +14 | exercised by 6 unit tests + 5 blueprint tests |

The api.py blueprint is small enough that 100 % coverage was the
right floor; achieved.

## B-1 deviations from v1 documented in code

1. **`operation_status` is a constant.** The `in_progress` field is
   always `False` in B-1. v1 used this to coordinate the
   IMG-mount-cycle with the polling JS; B-1 has no IMG / loopback
   subsystem (`docs/00-PLAN.md` "no IMG/loopback" invariant), so
   there is no equivalent state to report. The route is preserved
   so existing polling clients see a steady "no-op" body rather
   than 404. Documented in the route docstring + the module
   header.

2. **`gadget_state` is 503, not 200.** v1 probed configfs directly
   from the web process. In B-1 the Rust `teslafat` daemon owns
   the gadget lifecycle (ADR-0007) but does not yet expose a
   `GetGadgetState` IPC method — that wiring lands in Phase 6 with
   the systemd-unit + gadget supervisor work. The URL is kept so
   external monitors see an explicit `not_implemented` token. To
   track: GitHub issue to file post-merge for the Phase 6 IPC
   method.

3. **`recent_archive/*` are 501, not 200.** v1's recent-archive
   subsystem copied RecentClips into the loopback IMG so a
   phone-home script could `rclone sync` that IMG. B-1 has no IMG
   and no intermediate archive step — the `cloud_archive` worker
   (Phase 5.14 / 5.18) syncs RecentClips directly. The reason
   string in the body explicitly points callers at
   `/cloud_archive/sync_now` as the replacement.

4. **`recover_gadget` is 410 Gone.** v1's manual configfs-rebuild
   button raced the systemd-supervised gadget worker in B-1. The
   Rust `teslausb-worker` owns gadget recovery autonomously
   (ADR-0006), so the manual web-side trigger is not just
   unnecessary but actively harmful. The route returns the strong
   `deprecated` token so callers stop polling it.

## Charter audit

### Pillar 1 — Architectural Principles / Dependency inversion
- `blueprints/api.py` imports **only** Flask + `lock_chime_service`.
  No subprocess, no filesystem walks, no IPC calls — every byte of
  work happens behind a service module.
- The new `rename_chime_file` lives in `services/lock_chime_service.py`
  (the layer that already owns chime files). The blueprint is a
  pure HTTP-status-mapper.

### Pillar 2 — Type discipline
- `mypy --strict` clean. **No `Any` anywhere.**
- Every response body is a named :class:`TypedDict`
  (`OperationStatus`, `ChimeFilenameList`, `RenameChimeOk`,
  `ApiError`) built by a typed factory (`_api_error`) — no raw
  dict literals sprinkled at call sites.
- All public route functions return
  :data:`flask.typing.ResponseReturnValue`.

### Pillar 3 — Error handling & defaults
- No `print`. No bare `except`. Every exception caught is
  typed (`FileNotFoundError`, `FileExistsError`, `ValueError`,
  `LockChimeFileError`).
- `chime_filenames` swallows `LockChimeFileError` and returns an
  empty list (intentional: external scripts that poll this before
  upload prefer an empty allowlist to a crashed response). The
  swallow is logged via `logger.exception`.
- Every dropped route documents its replacement (or absence
  thereof) in the `reason` field of the body. No silent 404s.

### Pillar 4 — User-visible contracts
- All 4 URL-preserved endpoints kept their exact v1 paths.
- 3 dropped endpoints kept their exact v1 paths with structured
  bodies (no silent breakage).

### Pillar 5 — Dead code
- Vulture clean. The `_write_real_wav` test helper is used by 6
  tests; the `_api_error` factory by every drop route. No
  commented-out code, no unused imports.

### Pillar 6 — Security
- Bandit clean. No `subprocess`, no `eval`, no `shell=True`.
- `rename_chime_file` re-applies `_validate_library_name` to both
  old + new names *and* round-trips the new name through
  `secure_filename` so a caller cannot smuggle a path-traversal
  destination past the URL routing.

### Stringly-typed code
- All structured error bodies are `TypedDict` instances built by
  one factory (`_api_error`). No raw `{"error": ...}` dict
  literals sprinkled across routes.

## Outcome

- **Blockers:** 0
- **Majors:** 0
- **Minors:** 0
- **Nits:** 0

Ready to commit.

---

## Phase 5 close-out summary

Phase 5 shipped 28 incrementally-numbered increments (5.1 – 5.28)
porting v1's Flask UI onto B-1's userspace-Rust + btrfs
architecture.

**v1 deviations carried into B-1:**

* **No IMG / loopback subsystem.** Every v1 endpoint that depended
  on configfs / loopback / IMG mount cycling was either re-pointed
  at the equivalent B-1 service (cloud_archive, cleanup, mapping)
  or surfaced as a documented 410 / 501 / 503 (this increment).
* **No mode-toggle.** v1's read-only ↔ edit toggle was removed
  in 5.16 — B-1 partitions are always RW from the Pi's view; the
  Tesla sees a fresh FAT32 every poll thanks to the userspace
  filesystem driver.
* **No fsck/IMG/loopback maintenance UI.** Removed in 5.16/5.20.
  Btrfs scrub lives in `system_health` instead.
* **No web-side gadget recovery.** Owned by the Rust
  `teslausb-worker` supervisor (ADR-0006).
* **`archive` subsystem fully dropped from Failed Jobs.** B-1 cleanup
  is a fire-and-forget filesystem move with no queue layer (5.27).

**Quality bar at Phase 5 close:**

* All 6 gates green: ruff, ruff format, mypy --strict, vulture,
  bandit, pytest with `-W error::ResourceWarning`.
* Test count: **1737 passed, 23 skipped**.
* Total coverage: **86.55 %** (charter floor 80 %).
* 27 of 28 increments have a dedicated charter-review report on
  disk (`docs/charter-reviews/`); 5.13 – 5.23 reviews live in the
  session-state archive (operator decision, tracked).

**Outstanding tech-debt items surfaced at Phase 5 close (to land
in pre-Phase-6 sweep):**

1. 3 unlinked TODOs in 5.22 `cloud_archive.html` (need backing GH
   issues).
2. Charter-review backfill for 5.13 – 5.23 (5.24+ have on-disk
   reports; older increments are in session-state only).
3. 14 `PytestUnraisableExceptionWarning` baseline (pre-existing).
4. `/api/system/metrics` stub from 5.20 needs filling (placeholder
   returns `{}` so the Live Metrics tiles render an em dash).
5. Phase 5.20 "Live Metrics" backing endpoint.
6. Phase 5.28 deferred work tracked here:
   - `/api/gadget_state`: needs a `GetGadgetState` IPC method on
     the Rust `teslafat` daemon (Phase 6).
   - `/api/operation_status`: stays constant unless a B-1 long-
     running operation concept is ever introduced.
7. The 9-file `ruff format` drift fixed in passing during this
   gate run (pre-existing; charter Pillar 1 wash).

Phase 5 is **CLOSED**. Phase H5 (web app on hardware) is the next
gate; Phase 6 (`setup.sh` + `uninstall.sh`) follows.
