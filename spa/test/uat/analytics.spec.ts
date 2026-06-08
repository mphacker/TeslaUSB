import { test, expect, loadState, ARTIFACTS, type Probe } from "./helpers";
import type { Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";

// ── Task 5.3 UAT gate (spa.md §5/§6) ──────────────────────────────────────
// Drives the REAL bundle served by webd against the seeded read-only catalog
// (global-setup) at `/analytics`. The analytics screen is a parity carry of the
// legacy Flask "Storage Analytics Dashboard" (analytics.html / analytics.css).
//
// PARITY NOTE — data boundary: webd's read-only catalog exposes ONLY
// `GET /api/analytics` (trip/event aggregates). It serves NO storage-probe,
// partition, video-file or folder data, and this lane may not add a webd
// endpoint (ASK-FIRST). So — exactly as the sibling MediaHub does for the
// system-probe sections it cannot read — the storage-analytics half of the page
// renders the legacy DEGRADED state (a legacy-styled `.alert`) rather than
// fabricating drive/partition/folder numbers. The half webd CAN back is live:
// Driving Statistics (distance/trips/events) + the two Chart.js charts.
//
// PARITY NOTE — thin charts: the seed is a single driving day (2024-06-01) with
// 3 trips and 3 distinct event types, because the media-hub/trip-map suites
// assert those exact counts. So the trips-by-day chart is a single bar and the
// events-by-type doughnut has 3 slices. Extending the seed would break the
// sibling suites; the charts are asserted against these real (thin) datasets.

const EM_DASH = "\u2014";

/** webd read paths the analytics screen is permitted to call (read-only API). */
const ANALYTICS_API = new Set(["/api/analytics"]);

interface ChartSnapshot {
  type: string;
  labels: string[];
  data: number[];
  elementCount: number;
  elementSizes: number[];
  destroyed: boolean;
}

interface AnalyticsHooks {
  build: string;
  isRendered: boolean;
  events: ChartSnapshot | null;
  trips: ChartSnapshot | null;
}

/** Read the LIVE Chart.js instances back through the controller hooks (chart
 *  truth, not just `<canvas>` DOM presence). */
function hooks(page: Page): Promise<AnalyticsHooks> {
  return page.evaluate(() => {
    const h = (
      window as unknown as {
        __TESLAUSB_ANALYTICS_HOOKS__?: {
          build: string;
          isRendered: () => boolean;
          events: () => unknown;
          trips: () => unknown;
        };
      }
    ).__TESLAUSB_ANALYTICS_HOOKS__;
    if (!h) throw new Error("analytics hooks absent");
    return {
      build: h.build,
      isRendered: h.isRendered(),
      events: h.events() as AnalyticsHooks["events"],
      trips: h.trips() as AnalyticsHooks["trips"],
    };
  });
}

/** Count non-transparent pixels in a canvas (proves Chart.js actually painted
 *  to the bitmap — a blank/zero-size canvas would be all-transparent). */
function nonBlankPixels(page: Page, canvasId: string): Promise<number> {
  return page.evaluate((id) => {
    const c = document.getElementById(id) as HTMLCanvasElement | null;
    if (!c) throw new Error(`canvas #${id} absent`);
    if (c.width === 0 || c.height === 0) return 0;
    const ctx = c.getContext("2d");
    if (!ctx) throw new Error("2d context unavailable");
    const { data } = ctx.getImageData(0, 0, c.width, c.height);
    let painted = 0;
    for (let i = 3; i < data.length; i += 4) {
      if (data[i] !== 0) painted++;
    }
    return painted;
  }, canvasId);
}

/** Navigate to /analytics and wait for the live charts to render with real,
 *  non-zero element geometry. Waiting on the LIVE Chart meta geometry (arc
 *  circumference / bar height > 0) — not just an `isRendered` flag — removes the
 *  Chart.js responsive-resize race so the snapshot/pixel assertions are stable. */
async function gotoAnalytics(page: Page) {
  await page.goto("/analytics", { waitUntil: "load" });
  await expect(
    page.locator("#analyticsDashboard[data-screen=analytics]"),
  ).toBeVisible();
  await page.waitForFunction(() => {
    const h = (
      window as unknown as {
        __TESLAUSB_ANALYTICS_HOOKS__?: {
          isRendered: () => boolean;
          events: () => { elementSizes: number[] } | null;
          trips: () => { elementSizes: number[] } | null;
        };
      }
    ).__TESLAUSB_ANALYTICS_HOOKS__;
    if (!h || !h.isRendered()) return false;
    const e = h.events();
    const t = h.trips();
    return (
      !!e &&
      !!t &&
      e.elementSizes.length > 0 &&
      e.elementSizes.every((s) => s > 0) &&
      t.elementSizes.length > 0 &&
      t.elementSizes.every((s) => s > 0)
    );
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

test.describe("analytics UAT", () => {
  // ── Gate 1: functional parity ──────────────────────────────────────────
  test("functional parity — header, degraded storage, live driving stats, live charts", async ({
    page,
  }, testInfo) => {
    await gotoAnalytics(page);

    // App shell (base.html parity): brand present, ANALYTICS nav active.
    await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");
    const isMobile = testInfo.project.name.includes("375");
    const activeNav = page.locator(
      isMobile ? ".bottom-tabs .tab-item.active" : ".sidebar-rail .nav-item.active",
    );
    await expect(activeNav).toBeVisible();
    await expect(activeNav).toHaveAttribute("aria-current", "page");
    await expect(activeNav).toContainText("Analytics");

    // (a) Legacy dashboard header (verbatim parity copy).
    await expect(page.locator("#analyticsDashboard h2")).toContainText(
      "Storage Analytics Dashboard",
    );

    // (b) Storage-analytics half is the legacy DEGRADED state (by design — webd
    //     serves no storage/partition/video/folder data). Honest copy, no
    //     fabricated numbers.
    const degraded = page.locator("[data-testid=storage-degraded]");
    await expect(degraded).toBeVisible();
    await expect(degraded).toContainText("Storage analytics unavailable");
    // The genuine-read-failure alert is NOT shown (the read succeeded).
    await expect(page.locator("[data-testid=analytics-unavailable]")).toHaveCount(0);

    // (c) Driving Statistics — LIVE from /api/analytics (seed: 30556.3 m / 3
    //     trips / 3 events → 19.0 mi). Fields webd can't serve show the "—".
    const ds = page.locator("[data-testid=driving-stats]");
    await expect(ds).toBeVisible();
    await expect(page.locator("#dsTotalDist")).toHaveText("19.0 mi");
    await expect(page.locator("#dsTripCount")).toHaveText("3");
    await expect(page.locator("#dsEventCount")).toHaveText("3");
    // webd-absent telemetry fields render the legacy em-dash placeholder.
    await expect(page.locator("#dsTotalTime")).toHaveText(EM_DASH);
    await expect(page.locator("#dsAvgSpeed")).toHaveText(EM_DASH);
    await expect(page.locator("#dsMaxSpeed")).toHaveText(EM_DASH);
    await expect(page.locator("#dsFsdPct")).toHaveText(EM_DASH);
    await expect(page.locator("#dsWarnCount")).toHaveText(EM_DASH);
    await expect(page.locator("#dsEvPer100")).toHaveText(EM_DASH);

    // (d) Charts: both <canvas> elements present and the loading placeholder gone.
    await expect(page.locator("[data-testid=charts-loading]")).toHaveCount(0);
    await expect(page.locator("#eventsByTypeChart")).toBeVisible();
    await expect(page.locator("#tripsByDayChart")).toBeVisible();

    // (e) LIVE chart truth — read the actual Chart.js instances' datasets +
    //     rendered element metadata (NOT pre-render API data, NOT just DOM).
    const h = await hooks(page);
    expect(h.isRendered).toBe(true);

    // Events-by-Type doughnut: 3 slices, the 3 humanised seed event types
    // (alpha order: hard_acceleration, harsh_braking, sentry), each count = 1.
    expect(h.events, "events chart snapshot present").not.toBeNull();
    expect(h.events!.type).toBe("doughnut");
    expect(h.events!.destroyed).toBe(false);
    expect(h.events!.labels).toEqual([
      "Hard Acceleration",
      "Harsh Braking",
      "Sentry",
    ]);
    expect(h.events!.data).toEqual([1, 1, 1]);
    expect(h.events!.elementCount).toBe(3);
    // Every arc actually rendered with a non-zero circumference.
    expect(h.events!.elementSizes.length).toBe(3);
    for (const s of h.events!.elementSizes) expect(s).toBeGreaterThan(0);

    // Trips-by-Day bar: single seeded driving day, 3 trips.
    expect(h.trips, "trips chart snapshot present").not.toBeNull();
    expect(h.trips!.type).toBe("bar");
    expect(h.trips!.destroyed).toBe(false);
    expect(h.trips!.labels).toEqual(["2024-06-01"]);
    expect(h.trips!.data).toEqual([3]);
    expect(h.trips!.elementCount).toBe(1);
    expect(h.trips!.elementSizes.length).toBe(1);
    expect(h.trips!.elementSizes[0]).toBeGreaterThan(0);

    // (f) The charts actually PAINTED to laid-out (non-zero-box) bitmaps.
    for (const id of ["#eventsByTypeChart", "#tripsByDayChart"]) {
      const box = await page.locator(id).boundingBox();
      expect(box && box.width > 0 && box.height > 0, `${id} has a laid-out box`).toBeTruthy();
    }
    expect(await nonBlankPixels(page, "eventsByTypeChart")).toBeGreaterThan(100);
    expect(await nonBlankPixels(page, "tripsByDayChart")).toBeGreaterThan(100);
  });

  // ── Gate 1b: LIVE data binding — the charts + stats reflect WHATEVER
  //    /api/analytics returns, proving the screen binds to the response and
  //    does NOT render hard-coded seed constants. (The seed is fixed, so a
  //    constant-returning fake would pass every other gate; only a DISTINCT
  //    intercepted payload can disprove that false-green.) ──────────────────
  test("live data binding — charts + stats reflect the served /api/analytics payload", async ({
    page,
  }) => {
    // Distinct from the seed in every field (seed = 3 trips / 3 events / 19.0 mi).
    const distinct = {
      total_trips: 7,
      total_distance_m: 16093.44, // exactly 10.0 mi
      total_events: 2,
      events_by_type: [
        { type: "sentry_clip", count: 5 },
        { type: "speed_bump", count: 9 },
      ],
      trips_by_day: [{ day: "2030-12-25", count: 7, distance_m: 16093.44 }],
    };
    await page.route("**/api/analytics", async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(distinct),
      });
    });

    await gotoAnalytics(page);

    // Live driving stats reflect the intercepted payload, not the seed.
    await expect(page.locator("#dsTotalDist")).toHaveText("10.0 mi");
    await expect(page.locator("#dsTripCount")).toHaveText("7");
    await expect(page.locator("#dsEventCount")).toHaveText("2");

    // Live Chart instances carry the intercepted datasets (labels humanised,
    // counts + day verbatim) — proving the hooks track the real render, not a
    // decoupled/hard-coded stub.
    const h = await hooks(page);
    expect(h.events!.labels).toEqual(["Sentry Clip", "Speed Bump"]);
    expect(h.events!.data).toEqual([5, 9]);
    expect(h.events!.elementCount).toBe(2);
    expect(h.trips!.labels).toEqual(["2030-12-25"]);
    expect(h.trips!.data).toEqual([7]);
    expect(h.trips!.elementCount).toBe(1);
  });

  // ── Gate 5: wiring proof — the served HTML runs the freshly-built bundle ─
  test("wiring — served HTML runs the built bundle and charts initialised", async ({
    page,
  }) => {
    const state = loadState();
    await gotoAnalytics(page);

    // (a) build id baked on disk == build id the live page exposes.
    const winBuild = await page.evaluate(
      () => (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__,
    );
    expect(winBuild, "window.__TESLAUSB_BUILD__ must be defined").toBeTruthy();
    expect(winBuild).not.toBe("dev");
    expect(winBuild).toBe(state.buildId);

    // (b) the charts controller's own hook reports the SAME build → the
    //     analytics JS that created the charts is the bundle under test
    //     (defends the documented "edited JS the page never loaded" failure mode).
    const h = await hooks(page);
    expect(h.build).toBe(state.buildId);

    // (c) the live Chart instances are reachable on window (Chart.js initialised).
    const hasCharts = await page.evaluate(
      () => !!(window as unknown as { __TESLAUSB_ANALYTICS__?: unknown }).__TESLAUSB_ANALYTICS__,
    );
    expect(hasCharts, "window.__TESLAUSB_ANALYTICS__ (controller) must exist").toBe(true);

    // (d) the ACTUALLY-EXECUTED document loaded the hashed bundle (defends the
    //     documented "edited JS the page never loaded" failure mode harder than
    //     a side-channel fetch): the live DOM's <script> graph + the browser's
    //     resource-timing both reference state.jsAsset, and the dev entry is gone.
    const loadedScripts = await page.evaluate(() =>
      Array.from(document.scripts).map((s) => s.src),
    );
    expect(
      loadedScripts.some((s) => s.includes(state.jsAsset)),
      `executed document must load ${state.jsAsset}; saw ${JSON.stringify(loadedScripts)}`,
    ).toBe(true);
    expect(loadedScripts.some((s) => s.includes("/src/main.tsx"))).toBe(false);
    const resourceNames = await page.evaluate(() =>
      (performance.getEntriesByType("resource") as PerformanceResourceTiming[]).map(
        (r) => r.name,
      ),
    );
    expect(
      resourceNames.some((n) => n.includes(state.jsAsset)),
      "resource-timing must include the hashed bundle",
    ).toBe(true);

    // (e) served index references the hashed assets, not the TS dev entry.
    const html = await (await page.request.get("/analytics")).text();
    expect(html).toContain(state.jsAsset);
    expect(html).not.toContain("/src/main.tsx");
    expect(html).toMatch(/\/assets\/index-[\w-]+\.js/);
    if (state.cssAsset) expect(html).toContain(state.cssAsset);

    // (f) the JS asset is served as JavaScript (not HTML via SPA fallback).
    const jsResp = await page.request.get(state.jsAsset);
    expect(jsResp.status()).toBe(200);
    expect(jsResp.headers()["content-type"] ?? "").toMatch(/javascript/);
  });

  // ── Gate 3 (read-only): no mutations; only the allowed catalog read ─────
  test("read-only — mutations impossible, only GET /api/analytics", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;

    // Capture data-plane requests with their resourceType + any WebSocket — a
    // fabricated-data regression would fetch a static JSON/data file (a
    // same-origin `fetch`/`xhr` that ISN'T /api/analytics) or open a socket;
    // the probe fixture records method/url but not resourceType, so attach a
    // local listener BEFORE navigating.
    const dataReqs: { url: string; rtype: string; method: string }[] = [];
    page.on("request", (r) =>
      dataReqs.push({ url: r.url(), rtype: r.resourceType(), method: r.method() }),
    );
    const sockets: string[] = [];
    page.on("websocket", (ws) => sockets.push(ws.url()));

    await gotoAnalytics(page);
    // Deterministic settle (the geometry wait already guarantees the
    // /api/analytics fetch completed); avoid networkidle's flakiness.
    await page.waitForTimeout(200);

    // No mutating HTTP method, ever (webd is read-only).
    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s): ${JSON.stringify(mutating)}`).toEqual([]);

    // No WebSocket of any kind (no mutation/telemetry side-channel).
    expect(sockets, `websocket(s) opened: ${JSON.stringify(sockets)}`).toEqual([]);

    // Same-origin only; every /api/ call is a GET to the whitelisted path with
    // NO query string (the contract is the single bare endpoint).
    const apiSeen = new Set<string>();
    for (const req of probe.requests) {
      const u = new URL(req.url);
      expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
      if (!u.pathname.startsWith("/api/")) continue;
      expect(req.method.toUpperCase(), `${req.method} ${u.pathname}`).toBe("GET");
      expect(ANALYTICS_API.has(u.pathname), `unexpected API path ${u.pathname}`).toBe(true);
      expect(u.search, `unexpected query on ${u.pathname}`).toBe("");
      apiSeen.add(u.pathname);
    }

    // Every same-origin DATA fetch (fetch/xhr) must be exactly /api/analytics —
    // closes the "fetch a fabricated /something.json" false-green that the
    // pathname-prefix whitelist alone would miss.
    const dataPlane = dataReqs.filter(
      (r) => new URL(r.url).origin === origin && ["fetch", "xhr"].includes(r.rtype),
    );
    for (const r of dataPlane) {
      const u = new URL(r.url);
      expect(
        u.pathname === "/api/analytics" && u.search === "",
        `unexpected data fetch (${r.rtype}) ${u.pathname}${u.search}`,
      ).toBe(true);
      expect(r.method.toUpperCase(), `${r.method} ${u.pathname}`).toBe("GET");
    }
    expect(dataPlane.length, "the screen must make its one /api/analytics fetch").toBeGreaterThan(0);

    // The one required endpoint was actually hit (defends against partial wiring).
    expect(apiSeen.has("/api/analytics"), "/api/analytics was never requested").toBe(true);

    // No mutation surface in the DOM (read-only screen has no POST form / submit).
    await expect(page.locator("form[method='post' i]")).toHaveCount(0);
    await expect(page.locator("button[type=submit], input[type=submit]")).toHaveCount(0);
  });

  // ── Gate 3 (console + network): zero warnings/errors/pageerror, no failures ─
  test("clean — zero console warnings/errors/pageerror and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    await gotoAnalytics(page);
    // Flush any deferred Chart.js/layout callbacks across two animation frames
    // so a late console warning/error can't slip past the assertion. (Charts
    // run animation:false, so there's no pending tween — this is belt-and-braces
    // and avoids networkidle's flakiness.)
    await page.evaluate(
      () =>
        new Promise<void>((r) =>
          requestAnimationFrame(() => requestAnimationFrame(() => r())),
        ),
    );
    await page.waitForTimeout(200);

    assertCleanConsole(probe);

    expect(
      probe.failedRequests,
      `failed request(s): ${JSON.stringify(probe.failedRequests)}`,
    ).toEqual([]);

    // No external (off-origin) request at all.
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
  test("perf — capture TTFB/DCL/FCP + slowest requests", async ({
    page,
  }, testInfo) => {
    const navStart = Date.now();
    await gotoAnalytics(page);
    const chartsReadyMs = await page.evaluate(() => performance.now());

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

    const report = {
      environment:
        "dev webd (cargo debug build) on Windows host; Chromium via Playwright; " +
        "fresh context per test (cold cache). NOTE: spa.md's <~2s 'interactive' " +
        "target is the ON-DEVICE (Raspberry Pi) profile — these are dev-box " +
        "numbers, reported not asserted against that bar.",
      viewport: testInfo.project.name,
      ttfbMs: timings.ttfbMs,
      domContentLoadedMs: timings.domContentLoadedMs,
      domInteractiveMs: timings.domInteractiveMs,
      loadMs: timings.loadMs,
      fcpMs: timings.fcpMs,
      chartsReadyMs: Math.round(chartsReadyMs),
      wallClockNavMs: Date.now() - navStart,
      slowestRequests: timings.slowestRequests,
    };

    const out = resolve(ARTIFACTS, `perf-analytics-${testInfo.project.name}.json`);
    writeFileSync(out, JSON.stringify(report, null, 2));
    await testInfo.attach(`perf-analytics-${testInfo.project.name}.json`, {
      body: JSON.stringify(report, null, 2),
      contentType: "application/json",
    });
    console.log(`[uat][perf:analytics:${testInfo.project.name}]`, JSON.stringify(report, null, 2));

    expect(report.fcpMs, "FCP should be present").not.toBeNull();
    expect(report.fcpMs!).toBeLessThan(6000);
    expect(report.chartsReadyMs).toBeLessThan(8000);
  });

  // ── Gate 4: responsive — render + screenshot at this project's viewport ─
  test("responsive — renders at viewport and screenshot captured", async ({
    page,
  }, testInfo) => {
    await gotoAnalytics(page);

    // Dashboard + both charts present regardless of breakpoint.
    await expect(page.locator("#analyticsDashboard")).toBeVisible();
    await expect(page.locator("#eventsByTypeChart")).toBeVisible();
    await expect(page.locator("#tripsByDayChart")).toBeVisible();

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

    const shot = resolve(ARTIFACTS, `analytics-${testInfo.project.name}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    await testInfo.attach(`analytics-${testInfo.project.name}.png`, {
      path: shot,
      contentType: "image/png",
    });
    console.log(`[uat][screenshot:analytics:${testInfo.project.name}] ${shot}`);
  });
});
