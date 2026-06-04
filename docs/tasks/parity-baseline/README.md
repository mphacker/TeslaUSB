# Legacy Flask UI — Parity Baseline (Task 0.4)

**Reference-only.** This directory is the **visual + structural parity baseline** of the
**old Flask web app** (the one living under `web/` on branch `b1-userspace-rust`),
captured with real Chromium (Playwright) **before that app is decommissioned**. The new
static SPA — built in **tasks 5.2 / 5.3** — must reproduce the look and layout recorded
here. Nothing in this folder is loaded at runtime; it is documentation.

- **Spec it satisfies:** `docs/specs/spa.md §3` (the 16 screens that define parity).
- **Machine-readable manifest:** [`index.json`](./index.json) — every spa screen → route +
  artifact files + per-viewport HTTP status / perf / console / request counts. SPA tasks
  5.2/5.3 can diff against it programmatically.

## What was captured

For **every screen**, at **two viewports** — `mobile-375` (375×812) and
`desktop-1440` (1440×900) — four things were recorded:

| Artifact | File | Purpose |
| --- | --- | --- |
| Full-page screenshot | `<slug>/<viewport>.png` | the visual parity target |
| DOM dump | `<slug>/dom-<viewport>.html` | structure / class names / copy text |
| Perf + network + console | `<slug>/meta-<viewport>.json` | nav timing, slowest requests, console msgs |

A combined run log is in [`_capture-summary.json`](./_capture-summary.json).

## Screens (spa.md §3)

| # | spa.md screen | Capture dir | Route hit |
| --- | --- | --- | --- |
| 1 | media hub | `media-hub/` | `/settings/` |
| 2 | trip map | `trip-map/` | `/` |
| 3 | event player | `event-player/` | `/videos/event/SavedClips/2024-06-01_18-30-00` |
| 4 | video overlay HUD | `video-overlay-hud/` | `/` + `openVideoOverlay(...)` |
| 5 | analytics | `analytics/` | `/analytics/` |
| 6 | boombox | `boombox/` | `/boombox/` |
| 7 | music | `music/` | `/music/` |
| 8 | light shows | `light-shows/` | `/light_shows/` |
| 9 | lock chimes | `lock-chimes/` | `/lock_chimes/` |
| 10 | license plates | `license-plates/` | `/license_plates/` |
| 11 | wraps | `wraps/` | `/wraps/` |
| 12 | cloud archive | `cloud-archive/` | `/cloud/` |
| 13 | storage settings | `storage/` | `/storage/` |
| 14 | storage health | `storage/` | `/storage/` (same page — see caveats) |
| 15 | failed jobs | `failed-jobs/` | `/jobs` |
| 16 | captive portal | `captive-portal/` | `/captive-portal` |

**Bonus capture:** `trip-map-events-panel/` records the slide-out **event/video browser
panel** of the trip map (Events tab open). It is not a separate spa.md screen but is the
event-browsing surface of the trip map; included because the SPA must reproduce it.

So there are **17 capture dirs** for **16 spa.md screens** (storage-settings and
storage-health share `storage/`; the events panel is a bonus).

## How it was produced

- **App:** the legacy `teslausb_web` Flask app, retrieved from `b1-userspace-rust` and run
  **outside this repo** (in a WSL Ubuntu Python 3.13 venv, `flask 3.1.3`), served by the
  Flask dev server and reached from Windows at `http://localhost:8080`.
- **Data:** the app reads several SQLite DBs and media folders that are normally produced
  by the Rust worker / the Pi. For this baseline they were **seeded with small, realistic
  sample data** (one day of 3 trips / 6 clips / 3 events, a SMPTE test-pattern MP4 per
  clip, sample chimes/light-shows/wraps/plates/boombox/music, a cloud-sync queue with
  synced/pending/failed rows, and 2 dead-letter failed jobs). The seeding scripts live
  outside the repo and are **not** part of this deliverable — only the captured artifacts
  are committed.
- **Profile:** this is a **dev-server profile on a dev box, not a Pi.** The perf numbers in
  `meta-*.json` are indicative only — **not** a pass/fail performance gate.

## Dev-box caveats (NOT parity defects)

These screens render in their natural off-Pi state because the underlying hardware probes
do not exist on a dev box. The **layout / look** is still the parity target:

- **storage health** — SMART / fsck / disk-health probes degrade to "unknown" off-Pi.
- **captive portal** — wifi scanning fails off-Pi (no `nmcli`/`iwconfig`); the splash
  layout is the target.
- **video overlay HUD / event player** — clips are generated SMPTE test-pattern MP4s and
  the telemetry HUD shows seeded values, not real SEI/GPS telemetry.
- **storage-settings vs storage-health** — on the legacy app these are the **same page**
  (`/storage/`); both spa.md screens reference the single `storage/` capture.

## Using this from tasks 5.2 / 5.3

1. Open the target screen's `desktop-1440.png` / `mobile-375.png` as the visual reference.
2. Use `dom-*.html` for exact copy text, section ordering, and class/structure cues.
3. Read `index.json` for the route each screen maps to and any per-screen notes.
4. Treat the dev-box caveats above as expected differences, not regressions.

All screens captured **HTTP 200 with zero console errors** at both viewports.
