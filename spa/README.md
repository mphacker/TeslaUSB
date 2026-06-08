# TeslaUSB SPA (`spa/`)

Static single-page app for the TeslaUSB B-1 web UI. **Task 5.2** ships the
scaffold, a typed client for the `webd` read-only catalog API, the **media hub**
(home/landing screen), and a durable Playwright UAT. The remaining parity
screens land in Task 5.3.

- **Framework:** Preact + Vite + TypeScript (integrator default). Reuses the
  legacy vanilla CSS and imperative libs cleanly via a small React-compatible
  runtime.
- **Output:** a hashed static bundle in `dist/` (`npm run build`), served by
  `webd` from its static dir with an SPA fallback to `index.html`.

---

## ⚠️ Parity exception (read this)

The captured parity baseline at `docs/tasks/parity-baseline/media-hub/` is, in
fact, the legacy **Settings dashboard** (route `/settings/`, template
`index.html`). It is driven by `/api/system/health`, `/api/system/metrics`, and
`/api/wifi/saved`, and its lower half is a stack of **mutation forms** (WiFi,
Access Point, Storage, Mapping, Sharing, System).

`webd` exposes a **read-only catalog** API only (`days`, `trips`, `events`,
`clips`, `analytics`, `settings`) — none of the system/health/metrics/wifi
endpoints — and Task 5.2 explicitly forbids mutations. A literal reproduction of
that dashboard is therefore impossible against this backend.

**What we built instead:** a read-only **media hub** that reuses the baseline's
exact visual primitives — the device-status card, `settings-section`
disclosure panels, the System-Health rows grid, the metric-tile grid, and the
media-pill nav — but binds them to webd's catalog read API. So it matches the
baseline's *visual language and structure* while honoring the read-only
constraint. This divergence was **flagged to the integrator**; if a different
direction is wanted it is a content swap inside `src/screens/MediaHub.tsx`.

Consequently the UAT asserts **structural + component parity** and captures
screenshots as artifacts, but does **not** pixel-diff against the
settings-dashboard PNG (that mismatch is intentional).

---

## Layout

```
spa/
├── index.html              # entry; pre-paint theme; links carried CSS; mounts /src/main.tsx
├── vite.config.ts          # preact plugin, /api dev proxy, __TESLAUSB_BUILD__ wiring id
├── src/
│   ├── main.tsx            # entry: sets window.__TESLAUSB_BUILD__, renders <MediaHub/>
│   ├── api/{types,client}.ts   # typed read-only webd client (GET-only, 8 endpoints)
│   ├── components/{Shell,Icon}.tsx  # base.html chrome port; lucide sprite helper
│   ├── screens/MediaHub.tsx    # the media hub (deliverable screen)
│   └── styles/hub.css          # metric-tile + health-row styles (ported from baseline)
├── public/static/          # carried legacy assets (style.css, icons, Inter font)
└── test/
    ├── seed/build-db.mjs   # builds a seeded read-only indexd catalog (node:sqlite)
    └── uat/                # durable Playwright UAT (see below)
```

Legacy CSS/fonts/icons under `public/static/` were carried byte-exact from
`origin/b1-userspace-rust` and refactored into the bundle without altering
appearance. Only what the media hub needs now is carried; the rest arrives with
the 5.3 screens.

---

## Develop

```powershell
npm install
# Terminal 1 — run webd against a seeded DB on 127.0.0.1:8080 (see "Serve").
# Terminal 2 — Vite dev server; /api is proxied to webd (WEBD_DEV_TARGET to override):
npm run dev
```

## Build

```powershell
npm run build      # tsc --noEmit + vite build → dist/ (hashed assets)
```

## Seed a catalog

`webd` needs a read-only `indexd` catalog DB. The seeder transcribes the V1
schema verbatim from `rust/crates/indexd/src/db/migrations.rs` and inserts one
civil day (3 trips / 6 clips / 3 events / 24 angles). No crate edits, no native
deps (uses Node's built-in `node:sqlite`).

```powershell
npm run seed                       # → test/seed/catalog.db
node test/seed/build-db.mjs <out>  # custom output path
```

## Serve via `webd`

`webd` serves the static bundle and falls back to `index.html` for non-`/api`
routes. Point it at the built bundle and a seeded DB:

```powershell
$env:WEBD_DB     = "spa\test\seed\catalog.db"   # seeded catalog (read-only)
$env:WEBD_STATIC = "spa\dist"                    # built bundle
$env:WEBD_BIND   = "127.0.0.1:8080"
rust\target\debug\webd.exe
```

---

## UAT (Playwright) — the §5/§6 gate

The suite drives the **real served app**. `global-setup` seeds the catalog,
runs `npm run build`, builds `webd`, spawns it on `127.0.0.1:8131`
(`WEBD_DB`/`WEBD_STATIC`/`WEBD_BIND`), and preflights `/api/days`;
`global-teardown` stops it. Two projects: `desktop-1280` and `mobile-375`.

```powershell
npx playwright install chromium    # once
npm test                           # full build + serve + drive
$env:UAT_FAST="1"; npm test        # local iteration: reuse existing dist/DB/webd
```

Gates asserted (all required):

1. **Functional parity** — device-status card, active nav (Media), System-Health
   rows, all five catalog metric tiles, the single seeded driving day, and the
   five media tiles, each populated with the seeded catalog values.
2. **Performance** — captures TTFB, DCL, FCP, "catalog-metrics-visible" (an
   interactive proxy), a theme-toggle response time, and the slowest 10
   requests, into `test/uat/artifacts/perf-<project>.json`. The `<~2s`
   interactive target is the **on-device (Pi)** profile; these are **dev-box**
   numbers (debug `webd`, Chromium on the Windows host, cold cache) and are
   reported, not asserted, against that bar.
3. **Console + network clean** — zero console warnings/errors, zero `pageerror`,
   zero failed requests, and no same-origin non-2xx responses.
4. **Responsive** — renders at 375px and 1280px; full-page screenshots saved to
   `test/uat/artifacts/hub-<project>.png`.
5. **Wiring + read-only proof** — the build id baked into the on-disk bundle
   equals `window.__TESLAUSB_BUILD__` in the live page (the executed JS *is* the
   freshly-built bundle); the served HTML references the hashed `/assets/*.js`
   (not `/src/main.tsx`); JS/CSS are served with the right content-types; only
   whitelisted catalog GETs are made (all same-origin, no mutations, no forms).

Artifacts (screenshots, perf JSON, `webd.log`, HTML report, traces) are written
under `test/uat/artifacts/` (git-ignored).

---

## API client

`src/api/client.ts` is a typed, **GET-only** client for webd's catalog API
(`days`, `trips`, `trips/:id`, `events`, `clips`, `clips/:id`, `analytics`,
`settings`). Errors surface as `ApiError` carrying the server's
`{ error: { code, message } }` envelope. `view_kind` is passed through as an
opaque string (no closed enum). webd is read-only; the client exposes no
mutations.
