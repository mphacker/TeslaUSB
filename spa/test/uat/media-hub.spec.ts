import {
  test,
  expect,
  loadState,
  ALLOWED_API,
  ARTIFACTS,
  type Probe,
} from "./helpers";
import type { Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";

// ── Task 5.2 UAT gate (spa.md §5/§6) ──────────────────────────────────────
// Each test drives the REAL bundle served by webd against a seeded read-only
// catalog (global-setup). Fresh context per test ⇒ cold cache, no bleed.
//
// PARITY NOTE: the captured baseline at docs/tasks/parity-baseline/media-hub/
// is the legacy *settings dashboard* (route /settings/), driven by
// /api/system/health|metrics|wifi — none of which webd's read-only catalog API
// exposes, and whose config panels are mutation forms (forbidden here). This
// media hub is therefore a READ-ONLY reinterpretation that reuses the baseline's
// exact visual primitives (device-status card, settings-section, system-health
// rows grid, metric-tile grid, media-pill nav). We assert structural + visual
// parity of those primitives and capture screenshots as artifacts; we do NOT
// pixel-diff against the settings-dashboard PNG (that mismatch is by design and
// documented in spa/README.md). Flagged to the integrator.

/** Settle: bundle executed, catalog data painted. */
async function gotoHub(page: Page) {
  await page.goto("/", { waitUntil: "load" });
  await expect(page.locator("[data-testid=catalog-metrics]")).toBeVisible();
  await expect(page.locator("[data-testid=recent-days]")).toContainText("2024-06-01");
}

function metric(page: Page, name: string) {
  return page.locator(`.metric-tile[data-metric=${name}]`);
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

test.describe("media hub UAT", () => {
  // ── Gate 1: functional parity ──────────────────────────────────────────
  test("functional parity — landing, nav, health, metrics, days, media tiles", async ({
    page,
    probe,
  }, testInfo) => {
    await gotoHub(page);

    // App shell (base.html parity): brand present.
    await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");

    // Active nav — assert against the nav that is ACTUALLY visible at this
    // breakpoint (rail >=1024px, bottom-tabs <1024px). Checking the hidden one
    // would let a wrong mobile active-tab slip through.
    const isMobile = testInfo.project.name.includes("375");
    const activeNav = page.locator(
      isMobile ? ".bottom-tabs .tab-item.active" : ".sidebar-rail .nav-item.active",
    );
    await expect(activeNav).toBeVisible();
    await expect(activeNav).toHaveAttribute("aria-current", "page");
    await expect(activeNav).toContainText("Media");

    // Landing device-status card.
    const card = page.locator(".device-status-card");
    await expect(card).toBeVisible();
    await expect(card).toContainText("TeslaUSB");
    await expect(card).toContainText("Read-only catalog browser");

    // System Health — overall + all five rows populated from seeded catalog.
    const health = page.locator("[data-testid=system-health-card]");
    await expect(health.locator("[data-testid=health-overall]")).toContainText(
      "Catalog online — read-only",
    );
    for (const text of [
      "6 clips indexed", // 6 seeded, page not full (no "+")
      "3 trips catalogued",
      "3 events",
      "1 day with trips",
      "Online — read-only (webd)",
    ]) {
      await expect(health).toContainText(text);
    }

    // Catalog metric tiles (5) — every tile's value AND detail vs seeded data.
    await expect(page.locator("[data-testid=catalog-metrics] .metric-tile")).toHaveCount(5);
    await expect(metric(page, "trips").locator(".metric-value")).toHaveText("3");
    await expect(metric(page, "trips").locator(".metric-detail")).toHaveText("1 driving day");
    await expect(metric(page, "distance").locator(".metric-value")).toHaveText("19 mi");
    await expect(metric(page, "distance").locator(".metric-detail")).toHaveText(
      "total catalogued",
    );
    await expect(metric(page, "events").locator(".metric-value")).toHaveText("3");
    await expect(metric(page, "events").locator(".metric-detail")).toHaveText("3 types");
    await expect(metric(page, "clips").locator(".metric-value")).toHaveText("6");
    await expect(metric(page, "clips").locator(".metric-detail")).toHaveText("camera clips");
    await expect(metric(page, "days").locator(".metric-value")).toHaveText("1");
    await expect(metric(page, "days").locator(".metric-detail")).toHaveText("with driving");

    // Recent driving days — EXACTLY one seeded day, with its trip/event/distance.
    const days = page.locator("[data-testid=recent-days]");
    await expect(days.locator(".health-name")).toHaveCount(1);
    await expect(days.locator(".health-name")).toHaveText("2024-06-01");
    // trip-linked events only (the trip-less sentry event is excluded by webd).
    await expect(days.locator(".health-value")).toHaveText("3 trips · 2 events · 19 mi");

    // Media tiles (browse pills) — 5 entries, right labels AND hrefs.
    const pills = page.locator("[data-testid=browse-tiles] .media-pill");
    await expect(pills).toHaveCount(5);
    const expectedPills = [
      { label: "Map", href: "/" },
      { label: "Analytics", href: "/analytics" },
      { label: "Events", href: "/events" },
      { label: "Clips", href: "/media" },
      { label: "Cloud", href: "/cloud" },
    ];
    for (let i = 0; i < expectedPills.length; i++) {
      await expect(pills.nth(i)).toContainText(expectedPills[i].label);
      await expect(pills.nth(i)).toHaveAttribute("href", expectedPills[i].href);
    }

    assertCleanConsole(probe);
  });

  // ── Gate 5: wiring proof (the freshly-built bundle is what executed) ─────
  test("wiring proof — served HTML loads the hashed bundle that actually ran", async ({
    page,
  }) => {
    const state = loadState();
    await gotoHub(page);

    // (a) The build id baked into the on-disk bundle equals the one the live
    //     page exposes ⇒ the executed JS IS the freshly-built bundle.
    const winBuild = await page.evaluate(
      () => (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__,
    );
    expect(winBuild, "window.__TESLAUSB_BUILD__ must be defined").toBeTruthy();
    expect(winBuild).not.toBe("dev"); // not the un-built dev entry
    expect(winBuild).toBe(state.buildId);

    // (b) The served HTML references the hashed assets, not the TS dev entry.
    const html = await (await page.request.get("/")).text();
    expect(html).toContain(state.jsAsset);
    expect(html).not.toContain("/src/main.tsx");
    expect(html).toMatch(/\/assets\/index-[\w-]+\.js/);
    if (state.cssAsset) expect(html).toContain(state.cssAsset);

    // (c) The JS asset is served as JavaScript (not HTML via SPA-fallback).
    const jsResp = await page.request.get(state.jsAsset);
    expect(jsResp.status()).toBe(200);
    expect(jsResp.headers()["content-type"] ?? "").toMatch(/javascript/);

    // (d) The hashed bundle CSS is served as CSS (not a fallback-HTML 200).
    if (state.cssAsset) {
      const bundleCss = await page.request.get(state.cssAsset);
      expect(bundleCss.status()).toBe(200);
      expect(bundleCss.headers()["content-type"] ?? "").toMatch(/css/);
    }

    // (e) Carried legacy CSS resolves as CSS too.
    const legacyCss = await page.request.get("/static/css/style.css");
    expect(legacyCss.status()).toBe(200);
    expect(legacyCss.headers()["content-type"] ?? "").toMatch(/css/);
  });

  // ── Gate 3 (read-only): no mutations; only allowed catalog reads ────────
  test("read-only — mutations impossible, required catalog GETs all made", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    await gotoHub(page);
    await page.waitForLoadState("networkidle");

    // No mutating HTTP method, ever.
    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s): ${JSON.stringify(mutating)}`).toEqual([]);

    // Every request is same-origin (no third-party calls / exfiltration), and
    // every /api/ call is a GET to a whitelisted path.
    const apiSeen = new Map<string, string>(); // pathname -> search
    for (const req of probe.requests) {
      const u = new URL(req.url);
      expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
      if (!u.pathname.startsWith("/api/")) continue;
      expect(req.method.toUpperCase(), `${req.method} ${u.pathname}`).toBe("GET");
      expect(ALLOWED_API.has(u.pathname), `unexpected API path ${u.pathname}`).toBe(true);
      apiSeen.set(u.pathname, u.search);
    }

    // The data the screen shows must actually come from the catalog API — assert
    // each required endpoint was hit (defends against hardcoded/partial wiring).
    for (const p of ["/api/analytics", "/api/days", "/api/clips", "/api/settings"]) {
      expect(apiSeen.has(p), `required endpoint ${p} was never requested`).toBe(true);
    }
    expect(apiSeen.get("/api/clips")).toContain("limit=500");

    // No mutation surface in the DOM at all (read-only screen has no <form>).
    await expect(page.locator("form")).toHaveCount(0);
    await expect(page.locator("button[type=submit]")).toHaveCount(0);
  });

  // ── Gate 3 (console + network): zero warnings/errors/pageerror, no failures ─
  test("clean — zero console warnings/errors/pageerror and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    await gotoHub(page);
    await page.waitForLoadState("networkidle");

    assertCleanConsole(probe);

    expect(
      probe.failedRequests,
      `failed request(s): ${JSON.stringify(probe.failedRequests)}`,
    ).toEqual([]);

    // No same-origin response should be an error status. (webd's SPA fallback
    // returns 200 for unknown routes, so a 4xx/5xx here is a real problem.)
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
    await page.goto("/", { waitUntil: "load" });
    await expect(page.locator("[data-testid=catalog-metrics]")).toBeVisible();
    // Catalog data painted, measured in-page against the navigation time origin
    // (independent of the runner's clock). This is a "metrics visible" proxy,
    // not a formal TTI — labelled as such in the report.
    const catalogMetricsVisibleMs = await page.evaluate(() => performance.now());

    const timings = await page.evaluate(() => {
      const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming;
      const fcp = performance
        .getEntriesByType("paint")
        .find((p) => p.name === "first-contentful-paint");
      const resources = (performance.getEntriesByType("resource") as PerformanceResourceTiming[])
        .map((r) => ({
          url: r.name,
          ms: Math.round(r.duration * 10) / 10,
          type: r.initiatorType,
        }))
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

    // Interaction responsiveness: a real user action (theme toggle) must take
    // effect — proves the app is genuinely interactive, not just painted.
    const beforeTheme = await page.evaluate(
      () => document.documentElement.getAttribute("data-theme") ?? "light",
    );
    const tToggleStart = Date.now();
    await page.locator(".theme-toggle-btn").click();
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.getAttribute("data-theme") ?? "light"),
      )
      .not.toBe(beforeTheme);
    const themeToggleMs = Date.now() - tToggleStart;

    const report = {
      environment:
        "dev webd (cargo debug build) on Windows host; Chromium via Playwright; " +
        "fresh context per test (cold cache). NOTE: spa.md's <~2s 'interactive' " +
        "target is the ON-DEVICE (Raspberry Pi) profile — these are dev-box " +
        "numbers and are reported, not asserted, against that bar.",
      viewport: testInfo.project.name,
      ttfbMs: timings.ttfbMs,
      domContentLoadedMs: timings.domContentLoadedMs,
      domInteractiveMs: timings.domInteractiveMs,
      loadMs: timings.loadMs,
      fcpMs: timings.fcpMs,
      catalogMetricsVisibleMs: Math.round(catalogMetricsVisibleMs),
      themeToggleResponseMs: themeToggleMs,
      wallClockNavMs: Date.now() - navStart,
      slowestRequests: timings.slowestRequests,
    };

    const out = resolve(ARTIFACTS, `perf-${testInfo.project.name}.json`);
    writeFileSync(out, JSON.stringify(report, null, 2));
    await testInfo.attach(`perf-${testInfo.project.name}.json`, {
      body: JSON.stringify(report, null, 2),
      contentType: "application/json",
    });
    console.log(`[uat][perf:${testInfo.project.name}]`, JSON.stringify(report, null, 2));

    // Loose dev-box sanity bounds — catch gross regressions without flaking on
    // the on-device target. (FCP/interactive on a debug webd + cold cache.)
    expect(report.fcpMs, "FCP should be present").not.toBeNull();
    expect(report.fcpMs!).toBeLessThan(5000);
    expect(report.catalogMetricsVisibleMs).toBeLessThan(5000);
  });

  // ── Gate 4: responsive — render + screenshot at this project's viewport ─
  test("responsive — renders at viewport and screenshot captured", async ({
    page,
  }, testInfo) => {
    await gotoHub(page);

    // Content present regardless of breakpoint.
    await expect(page.locator(".device-status-card")).toBeVisible();
    await expect(page.locator("[data-testid=browse-tiles]")).toBeVisible();

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

    const shot = resolve(ARTIFACTS, `hub-${testInfo.project.name}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    await testInfo.attach(`hub-${testInfo.project.name}.png`, {
      path: shot,
      contentType: "image/png",
    });
    console.log(`[uat][screenshot:${testInfo.project.name}] ${shot}`);
  });
});
