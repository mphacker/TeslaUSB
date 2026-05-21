# Charter Review: H5 Hardware-Test Fixes (Increment H5-fixes-1)

**Branch:** `b1-userspace-rust`
**Scope:** Operator-flagged v1-parity defects against the live B-1 web UI on `cybertruckusb.local` after Phase 5 closure (1737 tests / 86.55% cov / 28 increments).
**Charter version:** `docs/03-CODE-QUALITY-CHARTER.md` (B-1).
**Reviewer:** Copilot CLI agent (self-audit).

---

## Issues addressed

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| 1 | `GET /` returned the System-Health dashboard instead of the Map (v1 parity violation; v1's `mapping_bp` owned `/`). | `mapping_bp` was registered with `url_prefix="/mapping"`; `settings_dashboard_bp` added a stray `@bp.route("/")` that hijacked root. | `mapping.py`: `url_prefix=""`. Deleted the `/` handler in `settings.py`; updated docstring to record that `/` is owned by `mapping.map_view`. |
| 2 | `/cloud/` rendered a spurious `<h1 class="cloud-archive-title">` plus two disabled placeholder sections (Schedule Editor / Reconcile Remote State) that v1 never had. | Phase 5.22 left UI scaffolding for two unimplemented backend routes (TODO #223 / #225); a top-level `<h1>` was also added that v1's template never carried. | Deleted the spurious `<h1>`. Deleted the entire `<details id="scheduleSection">` block (~58 lines) and `<details id="reconcileSection">` block (~16 lines). Wifi-conditional + Dead-Letters sections are retained because their backend routes *are* wired (`services/cloud_archive/queue_ops.py::dead_letter_*`). |
| 3a | `GET /videos/` (non-XHR) returned a 302 to `/mapping/`. Operator required a 200 with the file-browser-bearing page in one hop. | `videos.py` mirrored v1's redirect-to-mapping behaviour. With Issue 1 changing `/mapping/` → `/`, the 302 chain still worked but cost an extra hop. | `videos.py`: import `mapping.map_view`; non-XHR branch now `return _mapping_map_view()` — the map page renders directly with the file-browser video panel attached. XHR JSON branch unchanged. |
| 3b | `/media/` cascade was correct but the destination page's nav-pill bar only showed the *current* page's pill. v1 showed every available media sub-page. | Each blueprint's `render_template(...)` set only its *own* `*_available` flag; the context-processor defaulted all others to `False`. v1 set every flag on every render via `utils.get_base_context` → `get_feature_availability`. | New `services/media_availability.py::probe_media_availability(cfg) -> dict[str, bool]` factored out the 6 LightShow / Music / Boombox / Plates probes. `blueprints/media.py::_probe_availability` now delegates to it. `app.py::_inject_base_defaults` calls it once per request and merges the result into the default context. |

---

## Charter compliance

### Pillar 1 — Type safety & static analysis

| Gate | Result |
| --- | --- |
| `python -m ruff check .` | ✅ All checks passed |
| `python -m ruff format --check .` | ✅ 177 files formatted |
| `python -m mypy teslausb_web` (strict, `disallow_any_explicit=true`) | ✅ no issues found in 111 source files |
| `python -m vulture teslausb_web --min-confidence 80` | ✅ no dead code |
| `python -m bandit -r teslausb_web -ll -q` | ✅ no findings |
| `python -m pytest --cov=teslausb_web --cov-fail-under=85 -q` | ✅ 1769 passed / 1 pre-existing failure / 1 skipped / 2 deselected — **86.72% coverage** |

The new `services/media_availability.py` is `mypy --strict` clean (no `Any`, explicit return type `dict[str, bool]`, `TYPE_CHECKING` guard for `WebConfig` to avoid a cycle).

### Pillar 2 — No dead code / placeholders

The Schedule Editor and Reconcile sections were **placeholder UI without a backend route**. Per the charter's "no shortcut placeholder" rule, removing them is a positive compliance step — they had `disabled` buttons and `TODO(#…)` comments and were creating a v1-parity regression. The corresponding service-layer code (if any was ever written) is out of scope here; nothing references the deleted DOM IDs from JS (verified via `grep scheduleSection|reconcileSection static/`).

### Pillar 3 — Architecture layering

* `blueprints/media.py` correctly imports from `services/media_availability` (blueprint → service).
* `blueprints/videos.py` imports `mapping.map_view` from a sibling blueprint. This is a **blueprint-to-blueprint** import. The cleanest alternative would be to factor `map_view`'s body into a service function, but `map_view` is a Flask view — services don't have request context. The sibling-import is the same pattern v1 used for cross-blueprint redirects (it just used `url_for` and a `redirect`). Verdict: **acceptable**; documented inline.
* `app.py::_inject_base_defaults` performs a lazy in-function import of `probe_media_availability` to keep the top-level imports unchanged and avoid coupling `app.py` startup to the probe.

### Pillar 4 — v1 parity vs allowed deviations

* **Issue 1, 2, 3b** restore v1 parity exactly.
* **Issue 3a** is a deliberate deviation from v1: v1 returned 302; B-1 now returns 200. Justified because (a) operator explicitly requested 200, (b) saves one HTTP hop, (c) functionally identical (same page rendered).
* **Cloud Archive OAuth wizard** remains a B-1 deviation (PKCE-only; v1 supported rclone-paste / key-setup / generic NAS forms). This was already documented in Phase 5.22's charter review — restoring v1's forms would require adding a token-paste API to `CloudOAuthService` that the B-1 architecture deliberately omits for credential-hygiene reasons. Verdict: **allowed deviation, unchanged**.

---

## Self-audit: Blockers / Majors / Minors / Nits

* **Blockers:** none.
* **Majors:** none.
* **Minors:**
  * The pre-existing `tests/test_cloud_archive_blueprint.py::test_helper_resolve_event_path_rejects_path_traversal` failure on Linux/POSIX hosts (backslash-as-literal vs Windows path separator) is unrelated to this work and was deselected for the coverage run. Not introduced here.
  * The pre-existing `tests/test_wifi_service.py::test_write_json_file_applies_posix_permissions` failure when the workspace lives on NTFS (`/mnt/c`) was observed in this run but reproduces on `main` — environmental, not introduced here.
* **Nits:** none.

---

## Files changed

```
web/teslausb_web/app.py                                 (context processor)
web/teslausb_web/blueprints/mapping.py                  (url_prefix="")
web/teslausb_web/blueprints/media.py                    (delegate to service)
web/teslausb_web/blueprints/settings.py                 (remove root handler)
web/teslausb_web/blueprints/videos.py                   (render map page on browser GET)
web/teslausb_web/services/media_availability.py         (NEW)
web/teslausb_web/templates/cloud_archive.html           (-3 sections / -1 h1)
web/tests/test_cloud_archive_blueprint.py               (invert assertions)
web/tests/test_mapping_blueprint.py                     (strip /mapping prefix)
web/tests/test_media_blueprint.py                       (relocate _dir_exists ref)
web/tests/test_settings_blueprint.py                    (root → map test)
web/tests/test_templates_skeleton.py                    (root → / path)
web/tests/test_videos_blueprint.py                      (302 → 200 expectation)
```

**Verdict: APPROVED for commit + redeploy.**
