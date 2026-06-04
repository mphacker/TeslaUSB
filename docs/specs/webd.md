# SPEC — `webd` (axum REST/SSE API + static SPA host)

> Parent: [`SPEC.md`](./SPEC.md) · Criticality: disposable · Language: Rust (axum/tokio)
> Replaces the Flask app's server role. Serves the SPA and the API; drives
> mutations via the `gadgetd` eject-handoff. Pairs with [`spa.md`](./spa.md).

## 1. Objective

Provide the local web backend: serve the static SPA bundle and expose a typed
**REST + SSE** API over the `indexd` SQLite model and the other services, so the
UI can browse trips/events, stream clips with range requests, manage media
features, configure cloud/retention/wifi, and view storage/system health — at
**parity** with today's feature set.

## 2. Responsibilities

1. **Serve the SPA**: static hashed bundle, correct caching headers, SPA
   fallback routing. Prove (Playwright) the served HTML loads the changed JS.
2. **Read API** (from SQLite, read-only): trips by day, event bubbles, clip
   lists/metadata, analytics, storage + system health, settings.
3. **Video streaming**: serve clip angles to the `<video>` element with HTTP
   **range requests** (reference: `video_service/_range.py`), plus zip/download
   for export (reference: `_zip.py`). **No transcoding** — stream as stored;
   provide a "download to view" fallback for any clip whose codec the browser
   can't decode. Tesla footage is **H.264** ([`SPEC.md` §7](./SPEC.md)), which
   plays natively in all target browsers, so this fallback is an edge-case guard
   (e.g. a future HEVC variant), not the common path. While a
   clip is streaming/exporting, hold a **playback lease (TTL, renewed by heartbeat
   while active)** on the item so `retentiond`'s governor can't evict a file
   mid-read ([`storage.md` §4.1/§5](./storage.md)).
4. **Mutations** (the only ones allowed): delete clip, install/remove
   chime/lightshow/boombox/music/plate/wrap → validate → call the **`gadgetd`
   handoff** → report progress to the UI (SSE/poll). Never write the Tesla FS
   directly.
5. **Config endpoints**: retention policy → `retentiond`; cloud config/queue →
   `uploadd`; wifi/AP → `wifid`. `webd` validates and forwards; it does not own
   those policies.
6. **Progress/streaming events**: SSE (or long-poll) for index progress, handoff
   status, upload queue, job status (reference: jobs blueprint).
7. **Captive portal / AP mode** entry point for first-time WiFi onboarding
   (reference: `captive_portal.py`).

## 3. API surface (parity map to existing blueprints)

| Area | Existing blueprint (reference) | New `webd` routes (indicative) |
|------|-------------------------------|-------------------------------|
| Home / media hub | `media.py`, `index.html` | `GET /api/overview` |
| Trip map + events | `mapping.py` | `GET /api/days`, `/api/trips`, `/api/events` |
| Event → player | `videos.py`, `event_player.html` | `GET /api/clips/:id`, `GET /api/clips/:id/stream` |
| Analytics | `analytics.py` | `GET /api/analytics` |
| Boombox | `boombox.py` | `GET/POST /api/boombox` |
| Music | `music.py` | `GET/POST /api/music` |
| Light shows | `light_shows.py` | `GET/POST /api/lightshows` |
| Lock chimes | `lock_chimes.py` | `GET/POST /api/chimes` |
| License plates | `license_plates.py` | `GET/POST /api/plates` |
| Wraps | `wraps.py` | `GET/POST /api/wraps` |
| Cloud archive | `cloud_archive.py` | `GET/POST /api/cloud/*` → `uploadd` |
| Storage | `storage.py`, `storage_health.py` | `GET /api/storage`, `/api/storage/health` (governor tier, per-FS free bytes+inodes, archive breakdown, pinned/leased/reclaimable, last eviction; see [`storage.md` §6](./storage.md)) |
| System health | `system_health.py` | `GET /api/system/health` |
| Jobs | `jobs.py` | `GET /api/jobs` (SSE) |
| Settings | `settings.py` | `GET/PUT /api/settings` |
| Captive portal | `captive_portal.py` | `GET /portal` |

Finalize exact shapes during build; preserve every user-facing capability above.

## 3.1 Security & trust model

Today's Flask UI has **no app-level login** (verified: cloud **OAuth** is the only
auth; no `login_required`/session gate). The model is **trusted home LAN**, and
this rebuild preserves it — do **not** silently add or remove authentication
(that is an `ASK FIRST` product decision). What `webd` MUST still do:

- **Bind to the LAN/AP interface only** as today; never expose the API to the
  public internet. Mutations (clip delete, media install) are powerful and
  unauthenticated on the trusted segment by design.
- **Secrets at rest:** cloud OAuth **refresh tokens** and WiFi/Samba credentials
  are root-only (`0600`), never world-readable, never logged, never embedded in
  the SPA bundle, never written to the Tesla volume. `webd` reads them via the
  owning service (`uploadd`/`wifid`), not from the browser.
- **AP onboarding** (`/portal`) runs over a **WPA2** AP (never open); the captive
  portal never echoes stored secrets back to the page.
- **Input validation** on every mutation before it reaches the `gadgetd` handoff
  (path traversal, file-type, size) — `gadgetd` executes what it's given, so
  validation is `webd`'s responsibility.

## 4. Non-responsibilities

- Does not write the Tesla FS directly (delegates to `gadgetd` handoff).
- Does not parse video or derive trips/events (reads `indexd`'s DB).
- Does not own retention/cloud/wifi policy (forwards to those services).
- Does not transcode or render the HUD (client-side; see `spa.md`).

## 5. Acceptance criteria

- [ ] Serves the SPA; Playwright confirms the page loads the expected JS modules,
      reaches interactive < ~2 s on the Pi, **zero** console/pageerror.
- [ ] Range-request video playback works; large clips stream within the memory
      cap; download/zip export works; codec fallback path present for any
      browser-undecodable clip (footage is H.264, natively supported).
- [ ] Every existing screen's data is available via the API (parity checklist).
- [ ] Mutations always route through the `gadgetd` handoff and report progress;
      a refused handoff surfaces a friendly "try again" state.
- [ ] Runs within `MemoryMax`; in the OOM kill order it is killed **after**
      `uploadd`/`wifid` and **before** `scannerd`/`retentiond`/`indexd`; `gadgetd`
      is never killed (canonical order: [`SPEC.md` §7](./SPEC.md)).

## 6. Testing

- axum handler tests (read endpoints against a fixture DB; range logic).
- Contract tests for the handoff/forward flows (mocked `gadgetd`/`uploadd`).
- **Mandatory Playwright** E2E + perf + console + screenshot + wiring per
  `SPEC.md` §8 and `.github/copilot-instructions.md`, for every UI-affecting change.

## 7. Boundaries

**ALWAYS** delegate Tesla-FS writes to the handoff; stream (never buffer whole
clips); preserve feature parity; verify UI with Playwright.
**ASK FIRST** before removing/redesigning a screen or endpoint, or adding a heavy
dependency.
**NEVER** transcode; never write the Tesla FS directly; never mount it RW; never
trigger a reboot/gadget restart.
