import { test, expect, loadState, ARTIFACTS, type Probe } from "./helpers";
import type { Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";

// ── Media (Lock Chimes) UAT — v1 parity ───────────────────────────────────
// Drives the REAL bundle served by webd at `/media`. Parity target: the legacy
// Flask app's `/media/` 302-redirected to `/lock_chimes/`, so the visible
// "media page" was the LOCK CHIMES manager with a media pill sub-nav
// (Chimes/Music/Boombox/Shows/Wraps/Plates). This screen reproduces that v1
// look using the carried-over legacy stylesheet (`/static/css/style.css`).
//
// Backend reality: the only chime endpoints (POST/DELETE /api/chimes) route
// through the gadgetd eject-handoff (operator-gated), and there is NO chime
// read endpoint yet, so the screen is strictly READ-ONLY — it makes no API
// calls and renders honest "pending" states for the data-dependent sections
// instead of fabricating a library/active chime/scheduler.

/** The media pill sub-nav, in v1 render order. Only "chimes" is active/built. */
const EXPECT_PILLS = ["chimes", "music", "boombox", "shows", "wraps", "plates"];

interface MediaHooks {
  build: string;
  screen: string;
}

function hooks(page: Page): Promise<MediaHooks | undefined> {
  return page.evaluate(
    () =>
      (window as unknown as { __TESLAUSB_MEDIA_HOOKS__?: MediaHooks })
        .__TESLAUSB_MEDIA_HOOKS__,
  );
}

/** Navigate to /media and wait until the Lock Chimes screen has rendered. */
async function gotoMedia(page: Page) {
  await page.goto("/media", { waitUntil: "load" });
  await expect(page.locator(".container[data-screen=media]")).toBeVisible();
  await page.waitForFunction(() => {
    const h = (
      window as unknown as { __TESLAUSB_MEDIA_HOOKS__?: { screen: string } }
    ).__TESLAUSB_MEDIA_HOOKS__;
    return !!h && h.screen === "lock-chimes";
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

test.describe("media (lock chimes) UAT", () => {
  // ── Gate 1: v1 parity — chrome, pill sub-nav, lock-chime sections ───────
  test("parity — Media nav active, media pills, Lock Chimes sections", async ({
    page,
  }, testInfo) => {
    await gotoMedia(page);

    // App shell (base.html parity): brand present, MEDIA nav active.
    await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");
    const isMobile = testInfo.project.name.includes("375");
    const activeNav = page.locator(
      isMobile ? ".bottom-tabs .tab-item.active" : ".sidebar-rail .nav-item.active",
    );
    await expect(activeNav).toBeVisible();
    await expect(activeNav).toHaveAttribute("aria-current", "page");
    await expect(activeNav).toContainText("Media");

    // (a) Media pill sub-nav — all six, in v1 order; "chimes" is the active
    //     link, the rest are inert "Soon" pills (not links to dead routes).
    const pills = page.locator(".media-pills .media-pill");
    await expect(pills).toHaveCount(EXPECT_PILLS.length);
    for (let i = 0; i < EXPECT_PILLS.length; i++) {
      await expect(pills.nth(i)).toHaveAttribute("data-pill", EXPECT_PILLS[i]);
    }
    const chimes = page.locator(".media-pill[data-pill=chimes]");
    await expect(chimes).toHaveClass(/\bactive\b/);
    await expect(chimes).toHaveAttribute("href", "/media");
    // The five unbuilt features are disabled spans, not anchors.
    await expect(page.locator(".media-pill.media-pill-disabled")).toHaveCount(5);
    await expect(page.locator("a.media-pill")).toHaveCount(1);

    // (b) Lock Chimes heading + the v1 section set (each present and honest).
    await expect(
      page.locator(".container[data-screen=media] h2"),
    ).toHaveText("Lock Chimes");
    await expect(page.locator("#activeChimeSection")).toBeVisible();
    await expect(page.locator("#chimeUploadControls summary")).toHaveText(
      "Upload New Chime",
    );
    await expect(page.locator("#chimeSchedulerSection summary")).toHaveText(
      "Chime Scheduler",
    );
    await expect(page.locator("#randomChimeGroupsSection summary")).toHaveText(
      "Random Chime Groups",
    );
    await expect(page.locator(".media-library-heading")).toHaveText(
      "Chime Library",
    );
    // Pending sections are honest (no fabricated data), not operational.
    await expect(page.locator("[data-testid=active-chime-pending]")).toBeVisible();
    await expect(page.locator("[data-testid=library-pending]")).toBeVisible();
  });

  // ── Gate 2: wiring proof — the served HTML runs the freshly-built bundle ─
  test("wiring — served HTML runs the built bundle and the media module ran", async ({
    page,
  }) => {
    const state = loadState();
    await gotoMedia(page);

    // (a) build id baked on disk == build id the live page exposes.
    const winBuild = await page.evaluate(
      () => (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__,
    );
    expect(winBuild, "window.__TESLAUSB_BUILD__ must be defined").toBeTruthy();
    expect(winBuild).not.toBe("dev");
    expect(winBuild).toBe(state.buildId);

    // (b) the Media module's OWN hook reports the SAME build + this screen.
    const h = await hooks(page);
    expect(h, "window.__TESLAUSB_MEDIA_HOOKS__ must exist").toBeTruthy();
    expect(h!.build).toBe(state.buildId);
    expect(h!.screen).toBe("lock-chimes");

    // (c) the ACTUALLY-EXECUTED document loaded the hashed bundle, not the dev TS.
    const loadedScripts = await page.evaluate(() =>
      Array.from(document.scripts).map((s) => s.src),
    );
    expect(
      loadedScripts.some((s) => s.includes(state.jsAsset)),
      `executed document must load ${state.jsAsset}; saw ${JSON.stringify(loadedScripts)}`,
    ).toBe(true);
    expect(loadedScripts.some((s) => s.includes("/src/main.tsx"))).toBe(false);

    // (d) served index references the hashed assets, not the TS dev entry.
    const html = await (await page.request.get("/media")).text();
    expect(html).toContain(state.jsAsset);
    expect(html).not.toContain("/src/main.tsx");
    if (state.cssAsset) expect(html).toContain(state.cssAsset);

    // (e) the legacy stylesheet that carries the v1 look is referenced.
    expect(html).toContain("/static/css/style.css");
  });

  // ── Gate 3 (read-only): screen makes no API calls and no mutations ──────
  test("read-only — no mutations, no API calls, no mutation surface", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;

    const sockets: string[] = [];
    page.on("websocket", (ws) => sockets.push(ws.url()));

    await gotoMedia(page);
    await page.waitForTimeout(200);

    // No mutating HTTP method, ever.
    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s): ${JSON.stringify(mutating)}`).toEqual([]);

    // No WebSocket of any kind.
    expect(sockets, `websocket(s) opened: ${JSON.stringify(sockets)}`).toEqual([]);

    // The Lock Chimes screen has no read endpoint to call → it issues ZERO
    // /api/ requests (same-origin only otherwise).
    for (const req of probe.requests) {
      const u = new URL(req.url);
      expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
      expect(
        u.pathname.startsWith("/api/"),
        `unexpected API call ${req.method} ${u.pathname}`,
      ).toBe(false);
    }

    // No mutation surface in the DOM (no POST form / submit on this page).
    await expect(page.locator("form[method='post' i]")).toHaveCount(0);
    await expect(page.locator("button[type=submit], input[type=submit]")).toHaveCount(0);
    // The eject-handoff mutation is operator-gated and deliberately not wired:
    // no file input is exposed.
    await expect(page.locator("input[type=file]")).toHaveCount(0);
  });

  // ── Gate 4 (console + network): zero warnings/errors, no failed/non-2xx ──
  test("clean — zero console warnings/errors/pageerror and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    await gotoMedia(page);
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

    const offOrigin = probe.requests.filter((r) => new URL(r.url).origin !== origin);
    expect(offOrigin, `off-origin request(s): ${JSON.stringify(offOrigin)}`).toEqual([]);

    const bad = probe.responses.filter(
      (r) => new URL(r.url).origin === origin && r.status >= 400,
    );
    expect(bad, `non-2xx response(s): ${JSON.stringify(bad)}`).toEqual([]);
  });

  // ── Gate 5: performance — capture + report (dev-box profile) ────────────
  test("perf — capture TTFB/DCL/FCP + slowest requests", async ({ page }, testInfo) => {
    const navStart = Date.now();
    await gotoMedia(page);
    const readyMs = await page.evaluate(() => performance.now());

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
      screenReadyMs: Math.round(readyMs),
      wallClockNavMs: Date.now() - navStart,
      slowestRequests: timings.slowestRequests,
    };

    const out = resolve(ARTIFACTS, `perf-media-${testInfo.project.name}.json`);
    writeFileSync(out, JSON.stringify(report, null, 2));
    await testInfo.attach(`perf-media-${testInfo.project.name}.json`, {
      body: JSON.stringify(report, null, 2),
      contentType: "application/json",
    });
    console.log(`[uat][perf:media:${testInfo.project.name}]`, JSON.stringify(report, null, 2));

    expect(report.fcpMs, "FCP should be present").not.toBeNull();
    expect(report.fcpMs!).toBeLessThan(6000);
    expect(report.screenReadyMs).toBeLessThan(8000);
  });

  // ── Gate 6: responsive — render + screenshot at this project's viewport ─
  test("responsive — renders at viewport and screenshot captured", async ({
    page,
  }, testInfo) => {
    await gotoMedia(page);

    // Content present regardless of breakpoint.
    await expect(page.locator(".media-pills")).toBeVisible();
    await expect(page.locator("#activeChimeSection")).toBeVisible();

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

    const shot = resolve(ARTIFACTS, `media-${testInfo.project.name}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    await testInfo.attach(`media-${testInfo.project.name}.png`, {
      path: shot,
      contentType: "image/png",
    });
    console.log(`[uat][screenshot:media:${testInfo.project.name}] ${shot}`);
  });
});
