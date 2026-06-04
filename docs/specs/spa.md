# SPEC — SPA (parity UI: media hub, trip map, event player + HUD, media managers)

> Parent: [`SPEC.md`](./SPEC.md) · Criticality: static assets · Served by [`webd.md`](./webd.md)
> Decision: **visual parity** with today's Flask UI — same layout, colors,
> typography, and screen flow, reimplemented as a small static SPA.

## 1. Objective

Rebuild the existing web UI as a small **static single-page app** that looks and
behaves like today, talks to the `webd` REST/SSE API, and renders the dashcam
**telemetry HUD client-side** (no server transcoding). Achieve parity on every
screen; do not redesign the visual language.

## 2. Tech (parity-driven)

- **Framework:** a small one — Preact / Svelte / Solid (no heavy SPA stack;
  **open: choose one at build**). Ship a hashed static bundle (`npm run build` →
  `dist/`) served by `webd`.
- **Map:** **Leaflet + Leaflet.markercluster** (the libs the current UI uses) to
  preserve look/feel. MapLibre is **rejected** (would change the visual language;
  parity wins).
- **Charts:** **Chart.js** (current analytics dependency).
- **HUD / SEI:** reuse the existing client approach — the vendored
  `dashcam-mp4` parser + protobuf and a **Canvas/WebGL overlay** over the native
  `<video>` element. Footage is **H.264** ([`SPEC.md` §7](./SPEC.md)), natively
  decodable in all target browsers. SEI is parsed at index time server-side for
  trip/event data; the in-player HUD overlays telemetry on the playing video
  client-side.
- **Audio tooling:** preserve the audio trimmer (`lamejs`) used for chimes/boombox.
- **Assets:** carry over the existing CSS (`style.css`, `mapping*.css`,
  `analytics.css`), fonts (Inter), and icon sprites to guarantee visual parity;
  refactor into the SPA build without altering appearance.

## 3. Screens (parity checklist — maps to today's templates)

| Screen | Today's template | Must preserve |
|--------|------------------|---------------|
| Home / media hub | `index.html`, `media_hub_nav.html` | landing, nav, media tiles |
| Trip map | `mapping.html` | day nav, trip routes, **event bubbles** (honk/Sentry/hard brake/accel), marker clustering, speed-unit toggle, route disambiguation |
| Event player | `event_player.html` | click event/path → front-cam at that moment; angle group as one **clip**; full-page/full-screen scaling **with HUD**; delete clip |
| Video overlay HUD | `mapping/video_overlay*` | telemetry overlay synced to playback |
| Analytics | `analytics.html` | the existing charts |
| Boombox | `boombox.html` | upload/trim/assign |
| Music | `music.html` | manage music |
| Light shows | `light_shows.html` | manage light shows |
| Lock chimes | `lock_chimes.html` | manage + scheduler |
| License plates | `license_plates.html` | manage plates |
| Wraps | `wraps.html` | manage wraps |
| Cloud archive | `cloud_archive.html` | provider/browse/queue/sync controls |
| Storage settings | `storage_settings.html` | storage config |
| Storage health | `_storage_health_section.html` | health widgets |
| Failed jobs | `failed_jobs.html` | job list/status |
| Captive portal | `captive_portal.html` | first-run WiFi onboarding |

Every capability above must exist post-rebuild; appearance must match.

## 4. Behavior requirements

- **Clip model:** a group of angle videos recorded together is one "clip"; the
  player shows angles together; delete acts on the clip (→ `webd` → handoff).
- **Jump-to-moment:** clicking an event or a point on the trip path opens the
  front-cam video at that timestamp with the HUD active.
- **Scaling:** player scales to full-page and full-screen with the HUD intact.
- **Mutations show progress:** delete/install operations reflect handoff progress
  and a friendly "try again" if `gadgetd` refuses (car busy).
- **Retention/cloud/wifi config** screens drive the respective services via
  `webd`.

## 5. Performance & quality gates (mandatory — `.github/copilot-instructions.md`)

Every UI change is verified with **Playwright** against the served app:
- [ ] Interactive in < ~2 s on the Pi; report navigation TTFB, DOMContentLoaded,
      FCP, and the slowest 5–10 network requests.
- [ ] **Zero** console warnings/errors and **zero** `pageerror`.
- [ ] Screenshot at **375px** and **≥1280px**; the change is visibly present.
- [ ] Prove the changed JS module is actually loaded by the served HTML (inspect
      `<script>` tags / `window` bootstrap / network waterfall).
- [ ] Visual parity confirmed against the corresponding current screen.

## 6. Testing

- Component/unit tests for map, player/HUD sync, and media managers.
- Playwright E2E covering each screen in the parity checklist, with the perf +
  console + screenshot + wiring assertions above.

## 7. Boundaries

**ALWAYS** preserve the existing look/feel and full feature set; render the HUD
client-side; verify with Playwright (perf/console/screenshot/wiring).
**ASK FIRST** before changing the visual design, removing a screen, or swapping a
parity-critical library (Leaflet/Chart.js/`dashcam-mp4`).
**NEVER** transcode on the client's behalf server-side; never ship a UI change
unverified by Playwright; never adopt MapLibre or a heavy framework that breaks
parity.
