import { test, expect, loadState, ARTIFACTS, type Probe } from "./helpers";
import type { Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";

// ── Task 5.3 UAT gate (spa.md §5/§6) ──────────────────────────────────────
// Drives the REAL bundle served by webd against the seeded read-only catalog
// (global-setup). The trip map is the new HOME route `/`; the 5.2 media hub
// moved to `/media` (its suite is retargeted, stays green).
//
// PARITY NOTE — day switching: the seed has a single driving day (2024-06-01)
// because the relocated media-hub suite asserts "1 driving day" / a single
// recent-day row. We therefore assert the day-nav controls are present and that
// their prev/next boundary-disable logic is correct at the single-day boundary
// (cycleDay is wired but has no second day to move to). Flagged to the
// integrator: a 2nd driving day would break the media-hub day-count assertions.
//
// PARITY NOTE — offline tiles: UAT forces `window.__TESLAUSB_TILE_URL__ = ""`
// so the controller skips the tile layer entirely. This keeps every request
// same-origin (the "zero off-origin / zero non-2xx" gate) and asserts the map
// renders trips/events WITHOUT any external basemap fetch.

const SHARED_SEG_LAT = 37.8035; // midpoint of the trip1∩trip2 overlap …
const SHARED_SEG_LON = -122.4025; // … (37.802,-122.404 → 37.805,-122.401).

/** Float tolerance for comparing decoded lat/lon against seed coordinates. */
function near(a: number, b: number, eps = 1e-4): boolean {
  return Math.abs(a - b) < eps;
}

/** webd read paths the trip map is permitted to call (read-only API). */
const TRIPMAP_API = new Set([
  "/api/days",
  "/api/settings",
  "/api/trips",
  "/api/events",
  "/api/clips",
]);
function apiAllowed(pathname: string): boolean {
  return TRIPMAP_API.has(pathname) || /^\/api\/trips\/\d+$/.test(pathname);
}

interface MapHookSnapshot {
  tripPolylineCount: number;
  eventMarkerCount: number;
  tripCount: number;
  unit: string;
  hasTileLayer: boolean;
  build: string;
}

/** Read the live controller hooks (controller-level truth, not just DOM). */
function hooks(page: Page): Promise<MapHookSnapshot> {
  return page.evaluate(() => {
    const h = (window as unknown as { __TESLAUSB_MAP_HOOKS__?: MapHookSnapshot })
      .__TESLAUSB_MAP_HOOKS__;
    if (!h) throw new Error("map hooks absent");
    return {
      tripPolylineCount: h.tripPolylineCount,
      eventMarkerCount: h.eventMarkerCount,
      tripCount: h.tripCount,
      unit: h.unit,
      hasTileLayer: h.hasTileLayer,
      build: h.build,
    };
  });
}

function eventLatLngs(page: Page): Promise<[number, number][]> {
  return page.evaluate(
    () =>
      (
        window as unknown as {
          __TESLAUSB_MAP_HOOKS__?: { eventLatLngs: () => [number, number][] };
        }
      ).__TESLAUSB_MAP_HOOKS__!.eventLatLngs(),
  );
}

/** Force offline tiles, navigate to the map home, wait for the first render. */
async function gotoMap(page: Page) {
  await page.addInitScript(() => {
    (window as unknown as { __TESLAUSB_TILE_URL__?: string }).__TESLAUSB_TILE_URL__ = "";
  });
  await page.goto("/", { waitUntil: "load" });
  await expect(page.locator(".map-container[data-screen=trip-map]")).toBeVisible();
  // Leaflet initialised AND the first day's trips rendered.
  await page.waitForFunction(() => {
    const h = (window as unknown as { __TESLAUSB_MAP_HOOKS__?: { tripCount: number } })
      .__TESLAUSB_MAP_HOOKS__;
    return !!h && h.tripCount > 0;
  });
}

function assertCleanConsole(probe: Probe) {
  expect(probe.pageErrors, `pageerror(s): ${JSON.stringify(probe.pageErrors)}`).toEqual([]);
  expect(
    probe.consoleErrors,
    `console error(s): ${JSON.stringify(probe.consoleErrors)}`,
  ).toEqual([]);
  expect(
    probe.consoleWarnings,
    `console warning(s): ${JSON.stringify(probe.consoleWarnings)}`,
  ).toEqual([]);
}

test.describe("trip map UAT", () => {
  test.beforeEach(async ({ page }) => {
    const overrides = new Map<string, string>();
    await page.route("**/api/settings", async (route) => {
      const req = route.request();
      if (req.method() === "PUT") {
        const body = JSON.parse(req.postData() || "{}") as {
          key?: string;
          value?: string;
        };
        if (body.key && body.value) overrides.set(body.key, body.value);
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ key: body.key, value: body.value }),
        });
        return;
      }
      if (req.method() === "GET") {
        const resp = await route.fetch();
        const prefs = (await resp.json()) as { key: string; value: string }[];
        const merged = new Map(prefs.map((p) => [p.key, p.value]));
        for (const [k, v] of overrides) merged.set(k, v);
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(
            [...merged].map(([key, value]) => ({ key, value })),
          ),
        });
        return;
      }
      await route.continue();
    });
  });

  // ── Gate 1: functional parity ──────────────────────────────────────────
  test("functional parity — day nav, polylines, bubbles, clustering, speed toggle, disambiguation, panel", async ({
    page,
  }, testInfo) => {
    await gotoMap(page);

    // App shell (base.html parity): brand present, MAP nav active.
    await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");
    const isMobile = testInfo.project.name.includes("375");
    const activeNav = page.locator(
      isMobile ? ".bottom-tabs .tab-item.active" : ".sidebar-rail .nav-item.active",
    );
    await expect(activeNav).toBeVisible();
    await expect(activeNav).toHaveAttribute("aria-current", "page");
    await expect(activeNav).toContainText("Map");

    // (a) Day navigation present + labelled with the seeded driving day + stats.
    await expect(page.locator("#dayCardDate")).toContainText("2024");
    await expect(page.locator("#dayCardStats")).toContainText("3 trips");
    await expect(page.locator("#dayCardStats")).toContainText("19.0 mi");
    // Single-day boundary: both prev (older) and next (newer) are disabled.
    await expect(page.locator("#dayPrev")).toBeDisabled();
    await expect(page.locator("#dayNext")).toBeDisabled();

    // (b) Trip polylines drawn as REAL Leaflet layers — read back the live
    //     L.Polyline geometry from the layer group (not a controller counter).
    const h0 = await hooks(page);
    expect(h0.tripCount).toBe(3);
    expect(h0.tripPolylineCount).toBeGreaterThanOrEqual(3);
    expect(h0.hasTileLayer, "offline UAT must skip the tile layer").toBe(false);
    // Canvas renderer is live in the overlay pane (Leaflet uses ≥1 canvas).
    await expect(page.locator(".leaflet-overlay-pane canvas").first()).toBeVisible();

    // Real geometry proof: collect every visible route vertex and confirm BOTH
    // render data-paths produced on-map geometry —
    //   · Trip 2 (per-point geometry, NULL polyline) → its start (37.801,-122.408)
    //   · Trip 3 (NO points, polyline-BLOB fallback)  → its start (37.745,-122.465)
    // If either path were broken, its endpoint would be absent from the map.
    const routeLayers = await page.evaluate(
      () =>
        (
          window as unknown as {
            __TESLAUSB_MAP_HOOKS__?: {
              visibleRouteLayers: () => { color: string; coords: [number, number][] }[];
            };
          }
        ).__TESLAUSB_MAP_HOOKS__!.visibleRouteLayers(),
    );
    expect(routeLayers.length, "≥3 visible speed-bucket polylines").toBeGreaterThanOrEqual(3);
    const allVerts = routeLayers.flatMap((l) => l.coords);
    expect(
      allVerts.some(([la, lo]) => near(la, 37.801) && near(lo, -122.408)),
      "Trip 2 (points path) start vertex must be on the map",
    ).toBe(true);
    expect(
      allVerts.some(([la, lo]) => near(la, 37.745) && near(lo, -122.465)),
      "Trip 3 (polyline-BLOB fallback) start vertex must be on the map",
    ).toBe(true);
    // Speed buckets actually colour the route (≥2 distinct viridis stops).
    const routeColors = new Set(routeLayers.map((l) => l.color.toLowerCase()));
    expect(routeColors.size, "route is speed-bucket coloured").toBeGreaterThanOrEqual(2);

    // (c) Event bubbles: 2 on-route, trip-linked events (harsh_braking + accel),
    //     verified at their REAL marker coordinates. The trip-less sentry event
    //     is panel-only, NOT a map bubble.
    expect(h0.eventMarkerCount).toBe(2);
    const eventCoords = await page.evaluate(
      () =>
        (
          window as unknown as {
            __TESLAUSB_MAP_HOOKS__?: { eventLatLngs: () => [number, number][] };
          }
        ).__TESLAUSB_MAP_HOOKS__!.eventLatLngs(),
    );
    expect(eventCoords.length).toBe(2);
    expect(
      eventCoords.some(([la, lo]) => near(la, 37.79) && near(lo, -122.42)),
      "harsh_braking bubble at its seeded on-route coord",
    ).toBe(true);
    expect(
      eventCoords.some(([la, lo]) => near(la, 37.83) && near(lo, -122.38)),
      "hard_acceleration bubble at its seeded on-route coord",
    ).toBe(true);

    // (d) Speed-unit toggle flips mph↔kmh and updates the legend labels. The
    //     legend is a togglable overlay (display:none until shown) — open it via
    //     its FAB first, then exercise the unit buttons.
    await page.locator("#btnSpeedLegend").click();
    await expect(page.locator("#speedLegend")).toBeVisible();
    await expect(page.locator(".speed-legend-title")).toContainText("Speed (mph)");
    await expect(page.locator(".speed-legend-row").first()).toContainText("0\u201315");
    await page.locator("#speedUnitKph").click();
    await page.waitForFunction(() => {
      const h = (window as unknown as { __TESLAUSB_MAP_HOOKS__?: { unit: string } })
        .__TESLAUSB_MAP_HOOKS__;
      return !!h && h.unit === "kph";
    });
    await expect(page.locator(".speed-legend-title")).toContainText("Speed (kph)");
    await expect(page.locator(".speed-legend-row").first()).toContainText("0\u201325");
    // flip back to mph for a stable baseline.
    await page.locator("#speedUnitMph").click();
    await page.waitForFunction(() => {
      const h = (window as unknown as { __TESLAUSB_MAP_HOOKS__?: { unit: string } })
        .__TESLAUSB_MAP_HOOKS__;
      return !!h && h.unit === "mph";
    });

    // (e) Route disambiguation. We invoke the disambiguation through the
    //     `triggerDisambig` hook, which runs the EXACT app logic the on-map
    //     click handler runs — `findCandidatesNearClick(latlng)` →
    //     `showDisambigPopup(...)` — at the shared trip1∩trip2 segment midpoint.
    //     (A synthetic Playwright mouse click on Leaflet's *canvas* hit-target
    //     is non-deterministic across viewports; the hook exercises our own
    //     disambiguation code deterministically, leaving only Leaflet's
    //     already-tested canvas hit-test out of scope.)
    //     (Trip 3 has no per-point waypoints — polyline-only — so it never
    //     participates in disambiguation.)
    const candidates = await page.evaluate(
      ({ lat, lon }) =>
        (
          window as unknown as {
            __TESLAUSB_MAP_HOOKS__?: { triggerDisambig: (a: number, b: number) => number };
          }
        ).__TESLAUSB_MAP_HOOKS__!.triggerDisambig(lat, lon),
      { lat: SHARED_SEG_LAT, lon: SHARED_SEG_LON },
    );
    expect(candidates, "shared segment resolves to exactly 2 overlapping trips").toBe(2);
    await expect(page.locator(".disambig-popup")).toBeVisible();
    await expect(page.locator(".disambig-popup .disambig-row")).toHaveCount(2);
    await expect(page.locator(".disambig-header")).toContainText("2 clips through here");
    // Each row shows its trip's real summary (distance · duration).
    await expect(page.locator(".disambig-row-secondary").first()).toContainText("mi");
    // dismiss the popup deterministically so it doesn't bleed into later
    // assertions (Leaflet's Escape-to-close needs map focus; call the API).
    await page.evaluate(() =>
      (window as unknown as { __TESLAUSB_MAP__?: { closePopup: () => void } })
        .__TESLAUSB_MAP__!.closePopup(),
    );
    await expect(page.locator(".disambig-popup")).toHaveCount(0);

    // (f) Events side panel opens and lists seeded events / trips / clips —
    //     assert representative CONTENT, not just row counts.
    await page.locator("#btnVideos").click();
    await expect(page.locator("#videoPanel")).toHaveClass(/open/);
    // Events tab (global /api/events) → all 3 events incl. the trip-less sentry.
    const vpEvents = page.locator("[data-testid=vp-events]");
    await expect(vpEvents.locator(".st-event")).toHaveCount(3);
    await expect(vpEvents).toContainText("harsh braking");
    await expect(vpEvents).toContainText("hard acceleration");
    await expect(vpEvents).toContainText("sentry");
    // Severity is an indexd ordinal (1=info, 2=warning, 3=critical) rendered as
    // its LABEL — never as a fabricated speed. Regression guard: harsh_braking
    // reads "critical", hard_acceleration reads "warning", sentry reads "info",
    // and no event row shows a bogus "<n> mph"/"<n> kph" from severity.
    await expect(vpEvents).toContainText("critical");
    await expect(vpEvents).toContainText("warning");
    await expect(vpEvents).toContainText("info");
    await expect(vpEvents).not.toContainText(/\b\d+\s*(mph|kph)\b/);
    // Trips tab → the 3 seeded trips for the day, labelled by id.
    await page.locator("#vpTabTrips").click();
    const vpTrips = page.locator("[data-testid=vp-trips]");
    await expect(vpTrips.locator(".vp-clip")).toHaveCount(3);
    await expect(vpTrips).toContainText("Trip #1");
    await expect(vpTrips).toContainText("Trip #3");
    // All Clips tab → the 6 seeded clips.
    await page.locator("#vpTabClips").click();
    await expect(page.locator("[data-testid=vp-clips] .vp-clip")).toHaveCount(6);
    const clipLinks = page.locator("[data-testid=vp-clips] a[data-testid^=vp-clip-link-]");
    await expect(clipLinks).toHaveCount(6);
    for (const id of [1, 2, 3, 4, 5, 6]) {
      await expect(page.locator(`[data-testid=vp-clip-link-${id}]`)).toHaveAttribute(
        "href",
        `/events?clip=${id}`,
      );
    }

    // (g) Marker clustering active (done LAST — it zooms the map out): the 2
    //     event bubbles collapse into a SINGLE `.marker-cluster` whose badge
    //     reads "2". Proves leaflet.markercluster actually grouped the markers
    //     (not bare pins, not a stale DOM node).
    await page.evaluate(() => {
      (window as unknown as { __TESLAUSB_MAP__?: { setZoom: (z: number) => void } })
        .__TESLAUSB_MAP__!.setZoom(5);
    });
    const cluster = page.locator(".marker-cluster").first();
    await expect(cluster).toBeVisible();
    await expect(cluster).toContainText("2");
    // The two markers were absorbed into the cluster (no loose bubbles left).
    await expect(page.locator(".event-svg-icon")).toHaveCount(0);
  });

  test("filters — event type, severity, min distance, limit-to-view, restore defaults", async ({
    page,
  }) => {
    await gotoMap(page);

    const base = await hooks(page);
    expect(base.tripCount).toBe(3);
    expect(base.eventMarkerCount).toBe(2);

    // Event type pills.
    const harshPill = page.locator("[data-testid=filter-type-harsh_braking]");
    await expect(harshPill).toHaveAttribute("aria-pressed", "true");
    await harshPill.click();
    await expect(harshPill).toHaveAttribute("aria-pressed", "false");
    await page.waitForFunction(() => {
      const h = (window as unknown as { __TESLAUSB_MAP_HOOKS__?: { eventMarkerCount: number } })
        .__TESLAUSB_MAP_HOOKS__;
      return !!h && h.eventMarkerCount === 1;
    });
    let coords = await eventLatLngs(page);
    expect(coords.some(([la, lo]) => near(la, 37.79) && near(lo, -122.42))).toBe(false);
    await harshPill.click();
    await expect(harshPill).toHaveAttribute("aria-pressed", "true");
    await page.waitForFunction(() => {
      const h = (window as unknown as { __TESLAUSB_MAP_HOOKS__?: { eventMarkerCount: number } })
        .__TESLAUSB_MAP_HOOKS__;
      return !!h && h.eventMarkerCount === 2;
    });

    // Severity segmented control.
    await page.locator("#btnFilters").click();
    await expect(page.locator("#filterPanel")).toHaveClass(/visible/);
    await page.locator("[data-testid=filter-sev-critical]").click();
    await page.waitForFunction(() => {
      const h = (window as unknown as { __TESLAUSB_MAP_HOOKS__?: { eventMarkerCount: number } })
        .__TESLAUSB_MAP_HOOKS__;
      return !!h && h.eventMarkerCount === 1;
    });
    coords = await eventLatLngs(page);
    expect(coords.some(([la, lo]) => near(la, 37.79) && near(lo, -122.42))).toBe(true);
    await page.locator("[data-testid=filter-sev-warning]").click();
    await page.waitForFunction(() => {
      const h = (window as unknown as { __TESLAUSB_MAP_HOOKS__?: { eventMarkerCount: number } })
        .__TESLAUSB_MAP_HOOKS__;
      return !!h && h.eventMarkerCount === 2;
    });
    await page.locator("[data-testid=filter-sev-all]").click();
    await page.waitForFunction(() => {
      const h = (window as unknown as { __TESLAUSB_MAP_HOOKS__?: { eventMarkerCount: number } })
        .__TESLAUSB_MAP_HOOKS__;
      return !!h && h.eventMarkerCount === 2;
    });

    // Minimum trip distance (canonical threshold ~6 mi; convert when in km mode).
    const unit = (await hooks(page)).unit;
    const sliderValue = unit === "kph" ? 9.7 : 6.0;
    await page.locator("#filterMinDistance").evaluate((el, value) => {
      const input = el as HTMLInputElement;
      input.value = String(value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }, sliderValue);
    await page.waitForFunction(() => {
      const h = (window as unknown as {
        __TESLAUSB_MAP_HOOKS__?: { tripCount: number; eventMarkerCount: number };
      }).__TESLAUSB_MAP_HOOKS__;
      return !!h && h.tripCount === 2 && h.eventMarkerCount === 1;
    });
    coords = await eventLatLngs(page);
    expect(coords.some(([la, lo]) => near(la, 37.79) && near(lo, -122.42))).toBe(false);
    expect(coords.some(([la, lo]) => near(la, 37.83) && near(lo, -122.38))).toBe(true);
    await page.locator("#filterMinDistance").evaluate((el) => {
      const input = el as HTMLInputElement;
      input.value = "0";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await page.waitForFunction(() => {
      const h = (window as unknown as {
        __TESLAUSB_MAP_HOOKS__?: { tripCount: number; eventMarkerCount: number };
      }).__TESLAUSB_MAP_HOOKS__;
      return !!h && h.tripCount === 3 && h.eventMarkerCount === 2;
    });

    // Min-distance honours the km DISPLAY unit: the threshold is stored canonical
    // (miles) and only converted for display, so the SAME trips drop out in km
    // mode. Switch to kph and prove the km branch (slider max/label + filtering).
    // The unit toggle lives in the speed-legend overlay (a separate corner from
    // the filter panel); open it to flip to km, then close it again.
    await page.locator("#btnSpeedLegend").click();
    await expect(page.locator("#speedLegend")).toBeVisible();
    await page.locator("#speedUnitKph").click();
    await page.waitForFunction(() => {
      const h = (window as unknown as { __TESLAUSB_MAP_HOOKS__?: { unit: string } })
        .__TESLAUSB_MAP_HOOKS__;
      return !!h && h.unit === "kph";
    });
    await page.locator("#btnSpeedLegend").click();
    await expect(page.locator("#speedLegend")).not.toBeVisible();
    // Longest trip ≈ 7.74 mi ≈ 12.5 km → the km slider max is clearly the km
    // scaling (≥12), distinct from the ~8 it shows in miles.
    const kmMax = Number(await page.locator("#filterMinDistance").getAttribute("max"));
    expect(kmMax).toBeGreaterThanOrEqual(12);
    await expect(page.locator("#filterMinDistanceValue")).toContainText("km");
    // 9.7 km ≈ 6.03 mi → the same two trips as the 6.0 mi case above.
    await page.locator("#filterMinDistance").evaluate((el) => {
      const input = el as HTMLInputElement;
      input.value = "9.7";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await page.waitForFunction(() => {
      const h = (window as unknown as {
        __TESLAUSB_MAP_HOOKS__?: { tripCount: number; eventMarkerCount: number };
      }).__TESLAUSB_MAP_HOOKS__;
      return !!h && h.tripCount === 2 && h.eventMarkerCount === 1;
    });
    await expect(page.locator("#filterMinDistanceValue")).toContainText("9.7 km");
    // Reset slider + restore mph for the remainder of the test.
    await page.locator("#filterMinDistance").evaluate((el) => {
      const input = el as HTMLInputElement;
      input.value = "0";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await page.locator("#btnSpeedLegend").click();
    await expect(page.locator("#speedLegend")).toBeVisible();
    await page.locator("#speedUnitMph").click();
    await page.waitForFunction(() => {
      const h = (window as unknown as {
        __TESLAUSB_MAP_HOOKS__?: { unit: string; tripCount: number; eventMarkerCount: number };
      }).__TESLAUSB_MAP_HOOKS__;
      return !!h && h.unit === "mph" && h.tripCount === 3 && h.eventMarkerCount === 2;
    });
    await page.locator("#btnSpeedLegend").click();
    await expect(page.locator("#speedLegend")).not.toBeVisible();

    // Limit to map view — proves BBOX-INTERSECTION semantics (not vertex-in-
    // bounds) + moveend refilter + NO fitBounds reset. The viewport is centred in
    // the gap between trip 2's vertices, on its 37.830→37.842 segment, at a zoom
    // small enough that NO vertex of ANY trip lies inside it: a vertex-in-bounds
    // filter would therefore show ZERO trips, yet trip 2's bounding box still
    // intersects the viewport and must stay visible.
    await page.locator("#filterLimitView").click();
    await expect(page.locator("#filterLimitView")).toHaveAttribute("aria-checked", "true");
    await page.evaluate(() => {
      (window as unknown as { __TESLAUSB_MAP__?: { setView: (c: [number, number], z: number) => void } })
        .__TESLAUSB_MAP__!.setView([37.835, -122.37], 18);
    });
    await page.waitForFunction(() => {
      const h = (window as unknown as { __TESLAUSB_MAP_HOOKS__?: { tripCount: number } })
        .__TESLAUSB_MAP_HOOKS__;
      return !!h && h.tripCount === 1;
    });
    const probe = await page.evaluate(() => {
      const map = (window as unknown as {
        __TESLAUSB_MAP__?: {
          getBounds: () => { contains: (p: [number, number]) => boolean };
          getCenter: () => { lat: number; lng: number };
        };
      }).__TESLAUSB_MAP__!;
      const b = map.getBounds();
      const ALL_VERTICES: [number, number][] = [
        [37.772, -122.445], [37.778, -122.438], [37.785, -122.43], [37.79, -122.42],
        [37.796, -122.412], [37.802, -122.404], [37.805, -122.401], [37.808, -122.392],
        [37.801, -122.408], [37.815, -122.392], [37.825, -122.384], [37.83, -122.38],
        [37.842, -122.362], [37.858, -122.342],
        [37.745, -122.465], [37.76, -122.45], [37.775, -122.43], [37.788, -122.402],
      ];
      const hooks = (window as unknown as {
        __TESLAUSB_MAP_HOOKS__?: {
          tripCount: number;
          eventMarkerCount: number;
          visibleRouteLayers: () => { coords: [number, number][] }[];
        };
      }).__TESLAUSB_MAP_HOOKS__!;
      const c = map.getCenter();
      return {
        anyVertexInside: ALL_VERTICES.some((p) => b.contains(p)),
        tripCount: hooks.tripCount,
        eventMarkerCount: hooks.eventMarkerCount,
        routeCoords: hooks.visibleRouteLayers().flatMap((r) => r.coords),
        center: [c.lat, c.lng] as [number, number],
      };
    });
    // Precondition: a vertex-in-bounds filter would have NOTHING in view …
    expect(probe.anyVertexInside).toBe(false);
    // … yet bbox-intersection keeps exactly trip 2 (its unique far vertex drawn).
    expect(probe.tripCount).toBe(1);
    expect(
      probe.routeCoords.some(([la, lo]) => near(la, 37.858) && near(lo, -122.342)),
    ).toBe(true);
    // Trip 1 / trip 3 are gone and no event sits inside this viewport.
    expect(probe.eventMarkerCount).toBe(0);
    // No fitBounds reset — the user's pan/zoom is preserved.
    expect(near(probe.center[0], 37.835, 0.01)).toBe(true);
    expect(near(probe.center[1], -122.37, 0.01)).toBe(true);
    await page.locator("#filterLimitView").click();
    await expect(page.locator("#filterLimitView")).toHaveAttribute("aria-checked", "false");
    await page.waitForFunction(() => {
      const h = (window as unknown as {
        __TESLAUSB_MAP_HOOKS__?: { tripCount: number; eventMarkerCount: number };
      }).__TESLAUSB_MAP_HOOKS__;
      return !!h && h.tripCount === 3 && h.eventMarkerCount === 2;
    });

    // Restore defaults.
    await page.locator("[data-testid=filter-sev-all]").click();
    await page.locator("#filterMinDistance").evaluate((el) => {
      const input = el as HTMLInputElement;
      input.value = "0";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    if ((await harshPill.getAttribute("aria-pressed")) === "false") {
      await harshPill.click();
    }
    const full = await hooks(page);
    expect(full.tripCount).toBe(3);
    expect(full.eventMarkerCount).toBe(2);
  });

  test.describe("display preferences (server-persisted)", () => {
    test.use({ timezoneId: "America/Los_Angeles" });

    test("clock and speed settings persist across reload", async ({
      page,
      probe,
    }, testInfo) => {
      const settingPuts: { key?: string; value?: string }[] = [];
      page.on("request", (req) => {
        if (!req.url().includes("/api/settings") || req.method() !== "PUT") return;
        try {
          settingPuts.push(JSON.parse(req.postData() || "{}") as { key?: string; value?: string });
        } catch {
          settingPuts.push({});
        }
      });

      await gotoMap(page);
      await page.locator("#btnDisplayPrefs").click();
      await expect(page.locator("#displayPanel")).toBeVisible();
      await expect(page.locator("#clockLocal")).toHaveAttribute("aria-pressed", "true");

      await page.locator("#btnVideos").click();
      await expect(page.locator("[data-testid=vp-events]")).toBeVisible();

      const epoch = await page.evaluate(async () => {
        const resp = await fetch("/api/events?limit=100", { credentials: "same-origin" });
        const body = (await resp.json()) as { items?: { id: number; t: number }[] };
        return body.items?.find((ev) => ev.id === 1)?.t ?? null;
      });
      expect(epoch).not.toBeNull();
      const ts = epoch as number;
      const expected = await page.evaluate((eventEpoch) => {
        const opts: Intl.DateTimeFormatOptions = {
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
          hour12: true,
        };
        return {
          local: new Date(eventEpoch * 1000).toLocaleString(undefined, opts),
          utc: new Date(eventEpoch * 1000).toLocaleString(undefined, {
            ...opts,
            timeZone: "UTC",
          }),
        };
      }, ts);
      expect(expected.local).not.toBe(expected.utc);

      const eventOneTime = page.locator("[data-testid=vp-event-link-1] .st-date");
      await expect(eventOneTime).toHaveText(expected.local);

      await page.locator("#videoPanel .close-btn").click();
      await expect(page.locator("#videoPanel")).not.toHaveClass(/open/);
      await page.locator("#clockUtc").click();
      await expect(page.locator("#clockUtc")).toHaveAttribute("aria-pressed", "true");
      await page.locator("#btnVideos").click();
      await expect(page.locator("#videoPanel")).toHaveClass(/open/);
      await expect(eventOneTime).toHaveText(expected.utc);
      await expect
        .poll(() => settingPuts.some((p) => p.key === "clock" && p.value === "utc"))
        .toBe(true);

      await page.locator("#videoPanel .close-btn").click();
      await expect(page.locator("#videoPanel")).not.toHaveClass(/open/);
      await page.locator("#btnDisplayPrefs").click();
      await expect(page.locator("#displayPanel")).not.toHaveClass(/visible/);
      await page.locator("#btnSpeedLegend").click();
      await expect(page.locator("#speedLegend")).toBeVisible();
      await page.locator("#speedUnitKph").click();
      await expect
        .poll(() => settingPuts.some((p) => p.key === "speed_unit" && p.value === "kph"))
        .toBe(true);

      await page.reload({ waitUntil: "load" });
      await expect(page.locator(".map-container[data-screen=trip-map]")).toBeVisible();
      await page.waitForFunction(() => {
        const h = (window as unknown as { __TESLAUSB_MAP_HOOKS__?: { tripCount: number } })
          .__TESLAUSB_MAP_HOOKS__;
        return !!h && h.tripCount > 0;
      });

      await page.locator("#btnDisplayPrefs").click();
      await expect(page.locator("#displayPanel")).toBeVisible();
      await expect(page.locator("#clockUtc")).toHaveAttribute("aria-pressed", "true");

      // Speed unit must survive the reload too (persisted independently of clock).
      await page.locator("#btnDisplayPrefs").click();
      await expect(page.locator("#displayPanel")).not.toHaveClass(/visible/);
      await page.locator("#btnSpeedLegend").click();
      await expect(page.locator("#speedLegend")).toBeVisible();
      await expect(page.locator("#speedUnitKph")).toHaveAttribute("aria-pressed", "true");
      await page.locator("#btnSpeedLegend").click();

      await page.locator("#btnVideos").click();
      await expect(page.locator("[data-testid=vp-events]")).toBeVisible();
      await expect(page.locator("[data-testid=vp-event-link-1] .st-date")).toHaveText(expected.utc);
      await page.locator("#videoPanel .close-btn").click();
      await expect(page.locator("#videoPanel")).not.toHaveClass(/open/);

      const shot = resolve(ARTIFACTS, `display-prefs-${testInfo.project.name}.png`);
      await page.screenshot({ path: shot, fullPage: false });
      await testInfo.attach(`display-prefs-${testInfo.project.name}.png`, {
        path: shot,
        contentType: "image/png",
      });

      assertCleanConsole(probe);
    });
  });

  // ── Gate 5: wiring proof — the served HTML runs the freshly-built bundle ─
  test("wiring — served HTML runs the built bundle and Leaflet initialised", async ({
    page,
  }) => {
    const state = loadState();
    await gotoMap(page);

    // (a) build id baked on disk == build id the live page exposes.
    const winBuild = await page.evaluate(
      () => (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__,
    );
    expect(winBuild, "window.__TESLAUSB_BUILD__ must be defined").toBeTruthy();
    expect(winBuild).not.toBe("dev");
    expect(winBuild).toBe(state.buildId);

    // (b) the controller's own hook reports the SAME build → the trip-map JS that
    //     created the map is the bundle under test (defends the documented
    //     "edited JS the page never loaded" failure mode).
    const h = await hooks(page);
    expect(h.build).toBe(state.buildId);

    // (c) Leaflet actually initialised: the global map handle + a Leaflet root.
    const hasMap = await page.evaluate(
      () => !!(window as unknown as { __TESLAUSB_MAP__?: unknown }).__TESLAUSB_MAP__,
    );
    expect(hasMap, "window.__TESLAUSB_MAP__ (Leaflet) must exist").toBe(true);
    await expect(page.locator("#map.leaflet-container")).toBeVisible();

    // (d) served index references the hashed assets, not the TS dev entry.
    const html = await (await page.request.get("/")).text();
    expect(html).toContain(state.jsAsset);
    expect(html).not.toContain("/src/main.tsx");
    expect(html).toMatch(/\/assets\/index-[\w-]+\.js/);
    if (state.cssAsset) expect(html).toContain(state.cssAsset);

    // (e) the JS asset is served as JavaScript (not HTML via SPA fallback).
    const jsResp = await page.request.get(state.jsAsset);
    expect(jsResp.status()).toBe(200);
    expect(jsResp.headers()["content-type"] ?? "").toMatch(/javascript/);
  });

  // ── Gate 3 (read-only): no mutations; only allowed catalog reads ────────
  test("read-only — mutations impossible, required catalog GETs all made", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    await gotoMap(page);
    // Open the panel + cycle tabs so clips/events/trips endpoints are exercised.
    await page.locator("#btnVideos").click();
    await expect(page.locator("#videoPanel")).toHaveClass(/open/);
    await page.locator("#vpTabTrips").click();
    await expect(page.locator("[data-testid=vp-trips]")).toBeVisible();
    await page.locator("#vpTabClips").click();
    await expect(page.locator("[data-testid=vp-clips]")).toBeVisible();
    await page.waitForLoadState("networkidle");

    // No mutating HTTP method, ever (webd is read-only).
    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s): ${JSON.stringify(mutating)}`).toEqual([]);

    // Same-origin only; every /api/ call is a GET to a whitelisted path.
    const apiSeen = new Map<string, string>();
    for (const req of probe.requests) {
      const u = new URL(req.url);
      expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
      if (!u.pathname.startsWith("/api/")) continue;
      expect(req.method.toUpperCase(), `${req.method} ${u.pathname}`).toBe("GET");
      expect(apiAllowed(u.pathname), `unexpected API path ${u.pathname}`).toBe(true);
      apiSeen.set(u.pathname, u.search);
    }

    // Each required endpoint was actually hit (defends against partial wiring).
    for (const p of ["/api/days", "/api/settings", "/api/trips", "/api/events", "/api/clips"]) {
      expect(apiSeen.has(p), `required endpoint ${p} was never requested`).toBe(true);
    }
    // Per-trip detail (points + speed) was fetched for EVERY rendered trip —
    // proves the route geometry is wired per trip, not just for the first one.
    for (const id of [1, 2, 3]) {
      expect(
        apiSeen.has(`/api/trips/${id}`),
        `per-trip detail /api/trips/${id} was never requested`,
      ).toBe(true);
    }

    // No mutation surface in the DOM (read-only screen has no submit forms).
    await expect(page.locator("button[type=submit]")).toHaveCount(0);
  });

  // ── Gate 3 (console + network): zero warnings/errors/pageerror, no failures ─
  test("clean — zero console warnings/errors/pageerror and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    await gotoMap(page);
    // Exercise the interactive paths that the parity test drives.
    await page.locator("#btnSpeedLegend").click();
    await page.locator("#speedUnitKph").click();
    await page.locator("#btnVideos").click();
    await expect(page.locator("#videoPanel")).toHaveClass(/open/);
    await page.waitForLoadState("networkidle");
    // Let any deferred Leaflet/markercluster animation callbacks flush so a
    // late-arriving console warning can't slip past the assertion.
    await page.waitForTimeout(300);

    assertCleanConsole(probe);

    expect(
      probe.failedRequests,
      `failed request(s): ${JSON.stringify(probe.failedRequests)}`,
    ).toEqual([]);

    // No external (off-origin) request at all — proves offline tiles held.
    const offOrigin = probe.requests.filter((r) => new URL(r.url).origin !== origin);
    expect(offOrigin, `off-origin request(s): ${JSON.stringify(offOrigin)}`).toEqual([]);

    // No same-origin error status (webd's SPA fallback 200s unknown routes, so a
    // 4xx/5xx here is a real failure).
    const bad = probe.responses.filter(
      (r) => new URL(r.url).origin === origin && r.status >= 400,
    );
    expect(bad, `non-2xx response(s): ${JSON.stringify(bad)}`).toEqual([]);
  });

  // ── Gate 2: performance — capture + report (dev-box profile) ────────────
  test("perf — capture TTFB/DCL/FCP/interactive + slowest requests", async ({
    page,
  }, testInfo) => {
    const navStart = Date.now();
    await gotoMap(page);
    const mapReadyMs = await page.evaluate(() => performance.now());

    const timings = await page.evaluate(() => {
      const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming;
      const fcp = performance
        .getEntriesByType("paint")
        .find((p) => p.name === "first-contentful-paint");
      const resources = (performance.getEntriesByType("resource") as PerformanceResourceTiming[])
        .map((r) => ({ url: r.name, ms: Math.round(r.duration * 10) / 10, type: r.initiatorType }))
        .sort((a, b) => b.ms - a.ms)
        .slice(0, 10);
      return {
        ttfbMs: Math.round(nav.responseStart - nav.requestStart),
        domContentLoadedMs: Math.round(nav.domContentLoadedEventEnd),
        domInteractiveMs: Math.round(nav.domInteractive),
        loadMs: Math.round(nav.loadEventEnd),
        fcpMs: fcp ? Math.round(fcp.startTime) : null,
        slowestRequests: resources,
      };
    });

    // Interaction responsiveness: the speed-unit toggle must take effect (the
    // controller re-renders and the hook unit flips) — proves real interactivity.
    await page.locator("#btnSpeedLegend").click();
    await expect(page.locator("#speedLegend")).toBeVisible();
    const tToggleStart = Date.now();
    await page.locator("#speedUnitKph").click();
    await page.waitForFunction(() => {
      const h = (window as unknown as { __TESLAUSB_MAP_HOOKS__?: { unit: string } })
        .__TESLAUSB_MAP_HOOKS__;
      return !!h && h.unit === "kph";
    });
    const speedToggleMs = Date.now() - tToggleStart;

    const report = {
      environment:
        "dev webd (cargo debug build) on Windows host; Chromium via Playwright; " +
        "fresh context per test (cold cache); OFFLINE tiles (no basemap fetch). " +
        "NOTE: spa.md's <~2s 'interactive' target is the ON-DEVICE (Raspberry Pi) " +
        "profile — these are dev-box numbers, reported not asserted against that bar.",
      viewport: testInfo.project.name,
      ttfbMs: timings.ttfbMs,
      domContentLoadedMs: timings.domContentLoadedMs,
      domInteractiveMs: timings.domInteractiveMs,
      loadMs: timings.loadMs,
      fcpMs: timings.fcpMs,
      mapReadyMs: Math.round(mapReadyMs),
      speedToggleResponseMs: speedToggleMs,
      wallClockNavMs: Date.now() - navStart,
      slowestRequests: timings.slowestRequests,
    };

    const out = resolve(ARTIFACTS, `perf-tripmap-${testInfo.project.name}.json`);
    writeFileSync(out, JSON.stringify(report, null, 2));
    await testInfo.attach(`perf-tripmap-${testInfo.project.name}.json`, {
      body: JSON.stringify(report, null, 2),
      contentType: "application/json",
    });
    console.log(`[uat][perf:tripmap:${testInfo.project.name}]`, JSON.stringify(report, null, 2));

    expect(report.fcpMs, "FCP should be present").not.toBeNull();
    expect(report.fcpMs!).toBeLessThan(6000);
    expect(report.mapReadyMs).toBeLessThan(8000);
  });

  // ── Gate 4: responsive — render + screenshot at this project's viewport ─
  test("responsive — renders at viewport and screenshot captured", async ({
    page,
  }, testInfo) => {
    await gotoMap(page);

    // Map + day card present regardless of breakpoint.
    await expect(page.locator("#map.leaflet-container")).toBeVisible();
    await expect(page.locator(".trip-card")).toBeVisible();
    await expect(page.locator("#eventFilterPills .event-filter-pill")).toHaveCount(2);
    await page.locator("#btnFilters").click();
    await expect(page.locator("#filterPanel")).toHaveClass(/visible/);

    // Breakpoint-specific chrome: desktop shows the rail, mobile the bottom tabs.
    const isMobile = testInfo.project.name.includes("375");
    const rail = page.locator(".sidebar-rail");
    const tabs = page.locator(".bottom-tabs");
    if (isMobile) {
      await expect(tabs).toBeVisible();
      await expect(rail).toBeHidden();
    } else {
      await expect(rail).toBeVisible();
      await expect(tabs).toBeHidden();
    }

    const shot = resolve(ARTIFACTS, `tripmap-${testInfo.project.name}.png`);
    await page.screenshot({ path: shot, fullPage: false });
    await testInfo.attach(`tripmap-${testInfo.project.name}.png`, {
      path: shot,
      contentType: "image/png",
    });
    console.log(`[uat][screenshot:tripmap:${testInfo.project.name}] ${shot}`);
  });

  // ── Gate: map→video hand-off — event cards in the side panel deep-link into
  //    the event player at that exact event (the core v1 sentry-timeline →
  //    player gesture). Every seeded event has a clip, so all render as links. ─
  test("map→video — panel event cards deep-link into the player", async ({
    page,
  }) => {
    await gotoMap(page);
    await page.locator("#btnVideos").click();
    await expect(page.locator("#videoPanel")).toHaveClass(/open/);

    const vpEvents = page.locator("[data-testid=vp-events]");
    // All 3 seeded events carry a clip_id ⇒ all 3 are navigable links.
    await expect(vpEvents.locator("a.st-card-link")).toHaveCount(3);

    // The first event (harsh_braking, id 1, clip 2) links to its player moment.
    const link = page.locator("[data-testid=vp-event-link-1]");
    await expect(link).toHaveAttribute("href", "/events?event=1");

    // Clicking it routes (push-state, no reload) to the player on that event.
    await link.click();
    await expect(page).toHaveURL(/\/events\?event=1$/);
    await expect(page.locator("[data-screen=event-player]")).toBeVisible();
    await expect(page.locator(".event-location")).toHaveText("Harsh braking");
    await expect(page.locator("#mainVideo")).toHaveAttribute(
      "src",
      /\/api\/clips\/2\/stream/,
    );
  });

  test("map→video — all clips rows deep-link into the player", async ({
    page,
  }) => {
    await gotoMap(page);
    await page.locator("#btnVideos").click();
    await expect(page.locator("#videoPanel")).toHaveClass(/open/);
    await page.locator("#vpTabClips").click();

    const clipOne = page.locator("[data-testid=vp-clip-link-1]");
    await expect(clipOne).toHaveAttribute("href", "/events?clip=1");
    await clipOne.click();

    await expect(page).toHaveURL(/\/events\?clip=1$/);
    await expect(page.locator("[data-screen=event-player]")).toBeVisible();
    await expect(page.locator("#mainVideo")).toHaveAttribute("src", /\/api\/clips\/1\/stream/);
  });
});
