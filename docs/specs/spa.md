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
| Trip map | `mapping.html` | day nav, trip routes, **event bubbles** (honk/Sentry/hard brake/accel), marker clustering, speed-unit toggle, **display prefs (units + local/UTC clock, server-persisted)**, route disambiguation |
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
- **Deep-link to any event (`/events?event=<id>`):** the player loads the newest
  page of events for its in-screen playlist, so a deep-linked event **older than
  that window** (or one filtered out for having no clip) won't be found in the
  loaded list. Rather than silently falling back to the newest event, the player
  resolves the id directly via `GET /api/events/{id}` and plays that event in a
  **direct-event mode** (full event metadata + HUD seek to `front_frame_offset_ms`,
  but no prev/next playlist nav — the surrounding events aren't loaded), symmetric
  with the `?clip=<id>` direct-clip mode. If the lookup `404`s (event deleted /
  unknown id) it falls back to the playlist top — or, when a `?clip=<id>` is also
  present, to that clip — and shows a brief non-blocking notice; a transient
  fetch/server error shows a distinct "couldn't load" notice. The same resolution
  runs on a same-path `?event=` change while mounted (no remount).
- **Downloads (§4.2):** the event player's camera selector exposes two download
  affordances, both gated on the clip being archive-backed and resolved. (1)
  **Download All** (`#downloadButton`) — the whole clip as a ZIP of its archive
  angles via `GET /api/clips/{id}/export` (`api.exportUrl`). (2) **Download
  Angle** (`#downloadAngleButton`) — just the **currently-selected** camera's
  archive MP4 via `GET /api/clips/{id}/angles/{camera}/download`
  (`api.downloadUrl`); its target follows the active camera and it disables when
  the current angle is not `archive`-backed (`ro_usb` angles aren't downloadable,
  matching `/stream`). Both are native `<a download>` anchors — the browser does
  the byte transfer; webd serves `Content-Disposition: attachment`.
- **Trip-map filters (§4.1):** the trip map carries four client-side "visible
  set" filters applied over the already-loaded day (no `webd` round-trip): event
  **type** (toggle pill per type present that day), minimum **severity**
  (All / Info+ / Warning+ / Critical → 0/1/2/3; a threshold hides null-severity
  events), minimum **trip distance** (stored canonical in miles; the slider max,
  value and label convert to mi/km per the user unit), and **limit to map view**
  (bbox). With limit-to-view ON a trip shows iff its bounding box *intersects*
  the current viewport — bbox-intersection, **not** vertex-in-bounds, so a route
  that crosses the viewport with all vertices outside still shows. An event shows
  iff its own lat/lng is in bounds. A trip-**linked** event hides when its parent
  trip is filtered out (e.g. by min-distance/bbox); a trip-**less** event is
  unaffected by trip/distance/bbox-trip filtering. The default state (all types
  on, severity All, min-distance 0, limit-to-view off) reproduces the unfiltered
  render exactly. The map never `fitBounds` on an empty visible set; under
  limit-to-view a debounced `moveend` refilters without a fitBounds reset; click
  route-disambiguation reads the *filtered* visible trips. Filter pills reseed to
  "all on"   on day change. v1 parity note: filters scope the **map markers** only — the
  side-panel lists are an independent global catalog browser (see "Side-panel
  catalog browser" below), NOT a filtered view of the day. Multi-day **date range**
  is out of scope for v1 of this lane.
- **Side-panel catalog browser (§4.1):** the slide-up side panel's three tabs —
  **Events**, **Trips**, **All Clips** — are each a **global, newest-first,
  progressively-loaded** list over the whole catalog (NOT day-scoped and NOT
  affected by the map filters; the map stays the filtered day view). Each tab pulls
  from its cursor-paginated endpoint (`GET /api/events`, `GET /api/trips/page`,
  `GET /api/clips`, all `(date DESC, id DESC)` — see [`webd-api.md §2.1.1`](./contracts/webd-api.md)),
  rendering the first page on open and fetching the next page via an
  `IntersectionObserver` sentinel as the user scrolls — so the Pi never serves one
  giant list. Pagination state is **per tab**: one in-flight request at a time,
  aborted and reset on tab switch / unmount; responses arriving after a switch are
  ignored; items dedupe by `id`. A tab shows a "Loading…" affordance while a page is
  in flight and stops fetching when `next_cursor` is `null` (end of catalog). The
  snapshot-pinned cursor means rows recorded while the user scrolls don't shift the
  list; they appear the next time the tab is opened.
- **Display preferences (§4.1/§4.15, server-persisted):** the trip map exposes
  two display knobs persisted **server-side** (via `PUT /api/settings` → indexd
  `SetPref`, see `webd.md §3.2`) so they survive reload and are shared across
  browsers. (1) **Speed unit** (`speed_unit` ∈ `mph|kph`) — the existing
  speed-legend toggle now persists. (2) **Display clock** (`clock` ∈ `local|utc`,
  default `local`) via a new **Display** gear FAB + panel (`#btnDisplayPrefs` /
  `#displayPanel`, controls `#clockLocal`/`#clockUtc`): when `utc`, every map
  timestamp (controller popups via `fmtLocalTime`, side-panel lists via `fmtClock`)
  renders in UTC. Both prefs seed from `/api/settings` on mount and update
  **optimistically with rollback on failure** (a `role=status` error toast on the
  map surface); an already-open Leaflet popup is rebuilt with the new clock on next
  open. The map's `speed_unit` (singular) is intentionally distinct from the
  settings-dashboard `speed_units` (plural) until a future key-unification lane.
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
