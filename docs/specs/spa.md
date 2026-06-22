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
- **Open any clip from the panel:** selecting a row in the side panel's **All
  Clips** tab opens the event player for that clip (`/events?clip=<id>`), per
  Requirements §4.1 ("selecting one opens the event player"). The player resolves
  a clip directly from `/api/clips/{id}` when the `?clip=<id>` deep-link matches
  no event in the events playlist (event-less clips — e.g. a RecentClips clip
  with no Sentry/honk event): it plays from `t=0` with no event nav and no HUD
  telemetry. Archive-backed clips stream; `ro_usb` (not-yet-archived) clips show
  the "Not yet archived" overlay (`data-testid=video-unarchived`) and fire no
  doomed `/stream` request.
- **Scaling:** player scales to full-page and full-screen with the HUD intact.
- **Mutations show progress:** delete/install operations reflect handoff progress
  and a friendly "try again" if `gadgetd` refuses (car busy).
- **Retention/cloud/wifi config** screens drive the respective services via
  `webd`.

## 5. UI/UX user-acceptance testing (Playwright MCP + durable suite)

UI/UX quality is a **user-acceptance gate**, not an afterthought. Two
complementary mechanisms, both required:

- **Playwright MCP — interactive UI/UX inspection (agent-driven).** During
  development and review, drive the **served app** through the **Playwright MCP**
  to interactively verify the UI is **accurate**, **renders quickly**, **looks
  professional**, and would pass **strict user-acceptance testing**. Use it to
  open each screen, exercise real flows, capture perf/console/screenshots, and
  catch CSS/layout/z-index/wiring bugs DOM-only assertions miss.
- **Durable Playwright suite — the repeatable gate (CI + humans).** The MCP
  session is interactive and agent-scoped; it is **not** the portable gate. A
  **checked-in Playwright suite** (with screenshots / perf / console evidence as
  artifacts) is the acceptance gate that CI and human contributors run. "Used the
  MCP once" does **not** satisfy this — the durable tests must exist (§6).

### UAT criteria (every UI-affecting change)

- [ ] **Functional accuracy / parity** — the screen does what the corresponding
      current screen does (§3 checklist); flows produce correct results.
- [ ] **Render speed** — report navigation TTFB, DOMContentLoaded, FCP, time-to-
      interactive, and the slowest 5–10 network requests; interactive in **< ~2 s**
      under the defined test environment below.
- [ ] **Professional appearance** — polished **execution of the existing parity
      baseline**: aligned, no overflow/clipping/jank, consistent spacing/typography
      (Inter), correct light/dark behavior. This is *parity done well*, **not** a
      new visual language (changing the design is ASK-FIRST, §7).
- [ ] **Zero** console warnings/errors and **zero** `pageerror`.
- [ ] **Responsive** at **375px** and **≥1280px**; the change is visibly present;
      basic accessibility (focus order, contrast, 44×44 touch targets) holds.
- [ ] **Wiring proof** — the changed JS module is actually loaded by the served
      HTML (inspect `<script>` tags / `window` bootstrap / network waterfall).

### Test environment (state it; don't conflate)

Perf numbers are meaningless without the environment. Each run records:

- **Server:** the Pi (`webd` serving the hashed bundle) for the on-device target,
  or a local dev `webd` for fast iteration — say which.
- **Browser host + network:** the machine running the browser (phone/laptop/dev
  box) and the network profile (LAN vs throttled), since the SPA runs in the
  *client's* browser, not on the Pi.
- **Cache state:** cold vs warm; **viewport:** 375px and ≥1280px.

The headline "**interactive < ~2 s**" target is for the **on-device** profile
(server on the Pi, browser on a typical LAN client, cold cache). Dev-server runs
are for iteration, not the acceptance number.

## 6. Testing

- Component/unit tests for map, player/HUD sync, and media managers.
- **Durable Playwright E2E** covering each screen in the parity checklist, with
  the UAT criteria above asserted (perf budgets, zero console/pageerror,
  screenshots at 375px + ≥1280px, wiring proof) and the artifacts retained. This
  suite is the acceptance gate; the Playwright MCP is the interactive companion
  for authoring/diagnosing it.

## 7. Boundaries

**ALWAYS** preserve the existing look/feel and full feature set; render the HUD
client-side; verify with the **Playwright MCP** (interactive UI/UX UAT) **and** a
**durable Playwright suite** (perf/console/screenshot/wiring) as the gate.
**ASK FIRST** before changing the visual design, removing a screen, or swapping a
parity-critical library (Leaflet/Chart.js/`dashcam-mp4`).
**NEVER** transcode on the client's behalf server-side; never ship a UI change
unverified by Playwright; never treat a one-off MCP look as a substitute for the
durable test; never adopt MapLibre or a heavy framework that breaks parity.
