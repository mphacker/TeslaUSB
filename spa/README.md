# TeslaUSB SPA (`spa/`)

Static single-page app for the TeslaUSB B-1 web UI. **Task 5.2** ships the
scaffold, a typed client for the `webd` read-only catalog API, the **home
screen** (a parity reproduction of the legacy settings/device-status
dashboard), and a durable Playwright UAT. The remaining parity screens land in
Task 5.3.

- **Framework:** Preact + Vite + TypeScript (integrator default). Reuses the
  legacy vanilla CSS and imperative libs cleanly via a small React-compatible
  runtime.
- **Output:** a hashed static bundle in `dist/` (`npm run build`), served by
  `webd` from its static dir with an SPA fallback to `index.html`.

---

## ⚠️ Parity approach: degraded-state reproduction (read this)

The captured parity baseline at `docs/tasks/parity-baseline/media-hub/` is the
legacy **settings / device-status dashboard** (route `/settings/`, template
`index.html`): a device-status banner, then **System Health**, **Live Metrics**,
and the collapsed **WiFi / Access Point / Storage & Auto-Cleanup / Mapping &
Indexing / Network File Sharing / Storage Health / System** sections.

On a real device that page is populated by legacy system services
(`/api/system/health`, `/api/system/metrics`, `/api/storage/health`) and its
config panels POST to mutation routes. **`webd` is a read-only catalog API**
(`days`, `trips`, `events`, `clips`, `analytics`, `settings`) — it serves none
of those system endpoints and no mutation routes.

**What we built** (per the integrator's explicit decision): a *faithful visual +
structural reproduction* of that dashboard. For the data `webd` cannot serve we
render the legacy template's **degraded / loading / unknown** initial state
verbatim — never calling the absent endpoints (a 404 fetch would break the
zero-console gate):

- **Device Status** → the exact baseline degraded banner ("Status Unknown").
- **System Health** → reproduces the legacy probe grid exactly. The
  `/api/system/health` probe is not in the read-only catalog API, so the
  overall line and every system-probe row degrade to the legacy "Unknown / —"
  state — **except Video Indexer**, the one row that is catalog-derived: it
  shows `"N clips indexed; newest is M d old"` computed from `GET /api/clips`
  (the baseline's exact phrasing). No system metrics are fabricated.
- **Live Metrics** → the six zero-state tiles ("—") and "Updated — · —" footer.
  Real CPU/MEM need a future **system-metrics** endpoint (a tracked gap flagged
  to the integrator); we do **not** fabricate numbers.
- **Storage Health** → the include's static "Checking… / —" skeleton.
- **Access Point** → the degraded "AP status unavailable" variant.
- **System** → host facts shown as "—" (an on-device concern; not fabricated).

The screen performs two read-only catalog reads: `GET /api/settings` populates
the **Mapping & Indexing** and **Network File Sharing** form fields where prefs
keys map, and `GET /api/clips` drives the **Video Indexer** System-Health row.
The config forms are reproduced for structural parity but are **inert**
(`type="button"` + `onSubmit preventDefault`), so the screen can never mutate.

This is the parity the integrator endorsed ("a catalog-only webd organically
reproduces that degraded look — that IS parity"). Consequently the UAT asserts
the **degraded-state structure + copy** and captures screenshots as artifacts,
but does **not** pixel-diff the PNG (the captured PNG shows populated dev-box
probe data a read-only build cannot reproduce). The full catalog client is
proven separately against the seeded webd (`api-client.spec.ts`), readying 5.3.

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
│   ├── screens/MediaHub.tsx    # the home screen (settings-dashboard parity reproduction)
│   └── styles/hub.css          # metric-tile + storage-health styles (ported from baseline)
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
civil day (3 trips / 6 clips / 3 events / 24 angles) plus a `prefs` row set that
`/api/settings` returns (the home screen binds these; the catalog rows exercise
the API client and ready the 5.3 screens). No crate edits, no native deps (uses
Node's built-in `node:sqlite`).

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
`global-teardown` stops it. Two projects: `desktop-1280` and `mobile-375`. Both
projects run two specs: `media-hub.spec.ts` (the home-screen UAT) and
`api-client.spec.ts` (client-level tests for all 8 catalog endpoints + 404
envelopes against the seeded webd).

```powershell
npx playwright install chromium    # once
npm test                           # full build + serve + drive
$env:UAT_FAST="1"; npm test        # local iteration: reuse existing dist/DB/webd
```

Gates asserted (all required):

1. **Functional parity** — the home screen mounts (`data-screen="settings-dashboard"`),
   Settings is the active nav target, and the degraded states match the baseline
   verbatim: the "Status Unknown" banner, System Health "Loading…", the six
   Live-Metrics "—" tiles, "Storage Health" "Checking…", and the nine
   `details.settings-section` panels in exact baseline order.
2. **Performance** — captures TTFB, DCL, FCP, a content-visible interactive
   proxy, a theme-toggle response time, and the slowest 10 requests, into
   `test/uat/artifacts/perf-<project>.json`. The `<~2s` interactive target is the
   **on-device (Pi)** profile; these are **dev-box** numbers (debug `webd`,
   Chromium on the Windows host, cold cache) and are reported, not asserted,
   against that bar.
3. **Console + network clean** — zero console warnings/errors, zero `pageerror`,
   zero failed requests, and no same-origin non-2xx responses (proves no absent
   system endpoint is called).
4. **Responsive** — renders at 375px and 1280px; full-page screenshots saved to
   `test/uat/artifacts/dashboard-<project>.png`.
5. **Wiring + read-only proof** — the build id baked into the on-disk bundle
   equals `window.__TESLAUSB_BUILD__` in the live page (the executed JS *is* the
   freshly-built bundle); the served HTML references the hashed `/assets/*.js`
   (not `/src/main.tsx`); only the whitelisted catalog GETs (`/api/settings`,
   `/api/clips`) are made; and
   the config forms are *actively* exercised (dispatch submit + Enter) to prove
   they cannot mutate — no POST/PUT/PATCH/DELETE ever leaves the page.

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
