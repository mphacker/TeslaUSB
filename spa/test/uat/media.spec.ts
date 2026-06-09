import { test, expect, loadState, ARTIFACTS, type Probe } from "./helpers";
import type { Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";

// ── Task 5.3 UAT gate (spa.md §5/§6) ──────────────────────────────────────
// Drives the REAL bundle served by webd against the seeded read-only catalog
// (global-setup) at `/media`. The Media screen is the media-section landing hub
// (spa.md §3 "Home / media hub: landing, nav, media tiles") — a grid of feature
// tiles into the per-feature media screens, plus Recent Clips + Recent Events
// drawn LIVE from the read-only catalog.
//
// PARITY NOTE — baseline: the committed `parity-baseline/media-hub/` capture is
// the SETTINGS dashboard (url `/settings/`), mislabeled; it is NOT this screen.
// The hub therefore realises spa.md §3's intent in house style (cards/borders/
// Inter/tabular-nums), not those pixels.
//
// PARITY NOTE — data boundary: webd is read-only. The hub reads ONLY
// `GET /api/clips` and `GET /api/events` (both cursor pages, ascending by id).
// It drains every page and sorts newest-first client-side for the "recent"
// lists. Seed = 6 clips / 3 events (all 3 events carry a clip_id, so all are
// "playable"). Expected newest-first orders below are by started_at / t, which
// are fixed epoch seconds → deterministic regardless of the host timezone (so
// the suite asserts ORDER + tz-independent fields, never rendered clock text).

/** webd read paths the media hub is permitted to call (read-only API). */
const MEDIA_API = new Set(["/api/clips", "/api/events"]);

/** Clips newest-first by started_at (seed ids → hours 21,18,14,12,8,7). */
const EXPECT_CLIP_ORDER = [6, 5, 4, 3, 2, 1];
/** Per-clip tz-independent facts: duration "M:SS", folder label, sentry flag. */
const EXPECT_CLIPS: Record<number, { dur: string; folder: string; sentry: boolean }> = {
  6: { dur: "0:25", folder: "Sentry Clips", sentry: true },
  5: { dur: "1:00", folder: "Recent Clips", sentry: false },
  4: { dur: "0:30", folder: "Sentry Clips", sentry: true },
  3: { dur: "0:50", folder: "Saved Clips", sentry: false },
  2: { dur: "0:45", folder: "Saved Clips", sentry: false },
  1: { dur: "1:00", folder: "Recent Clips", sentry: false },
};

/** Events newest-first by t (seed ids → hours 14.15, 12.4, 7.3). */
const EXPECT_EVENT_ORDER = [3, 2, 1];
const EXPECT_EVENTS: Record<number, { title: string; sev: string }> = {
  3: { title: "Sentry event", sev: "media-sev-error" }, // severity 1
  2: { title: "Hard acceleration", sev: "media-sev-warn" }, // severity 2
  1: { title: "Harsh braking", sev: "media-sev-warn" }, // severity 2
};

/** The eight feature tiles, in render order; only `events` is a real screen. */
const EXPECT_TILES = [
  "events",
  "boombox",
  "music",
  "shows",
  "chimes",
  "plates",
  "wraps",
  "cloud",
];

interface MediaHooks {
  build: string;
  clipCount: number;
  eventCount: number;
}

function hooks(page: Page): Promise<MediaHooks | undefined> {
  return page.evaluate(
    () =>
      (window as unknown as { __TESLAUSB_MEDIA_HOOKS__?: MediaHooks })
        .__TESLAUSB_MEDIA_HOOKS__,
  );
}

/** Navigate to /media and wait until the live recent lists have rendered (the
 *  wiring hook reports the seeded counts), removing any fetch/render race. */
async function gotoMedia(page: Page) {
  await page.goto("/media", { waitUntil: "load" });
  await expect(page.locator(".container[data-screen=media]")).toBeVisible();
  await page.waitForFunction(() => {
    const h = (
      window as unknown as { __TESLAUSB_MEDIA_HOOKS__?: { clipCount: number; eventCount: number } }
    ).__TESLAUSB_MEDIA_HOOKS__;
    return !!h && h.clipCount === 6 && h.eventCount === 3;
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

test.describe("media hub UAT", () => {
  // ── Gate 1: functional parity — tiles + live recent clips/events ────────
  test("functional parity — header, feature tiles, live recent clips + events", async ({
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

    // (a) Landing header.
    await expect(page.locator(".container[data-screen=media] h2")).toContainText("Media");

    // (b) Feature tiles — all eight present, in order, as real links; the only
    //     implemented target (events) is marked ready and points at /events.
    const tiles = page.locator(".media-tiles .media-tile");
    await expect(tiles).toHaveCount(EXPECT_TILES.length);
    for (let i = 0; i < EXPECT_TILES.length; i++) {
      const tile = tiles.nth(i);
      await expect(tile).toHaveAttribute("data-feature", EXPECT_TILES[i]);
    }
    const eventsTile = page.locator(".media-tile[data-feature=events]");
    await expect(eventsTile).toHaveAttribute("data-ready", "true");
    await expect(eventsTile).toHaveAttribute("href", "/events");
    // Tiles are real anchors (link role) — not role-overridden.
    await expect(
      page.getByRole("link", { name: /Clips & Events/ }),
    ).toBeVisible();

    // (c) Recent Clips — LIVE, newest-first, with tz-independent facts.
    await expect(page.locator("[data-testid=clips-loading]")).toHaveCount(0);
    const clipRows = page.locator("[data-testid=recent-clips] .media-list-item");
    await expect(clipRows).toHaveCount(EXPECT_CLIP_ORDER.length);
    for (let i = 0; i < EXPECT_CLIP_ORDER.length; i++) {
      const id = EXPECT_CLIP_ORDER[i];
      const row = clipRows.nth(i);
      await expect(row).toHaveAttribute("data-clip-id", String(id));
      await expect(row.locator(".media-list-meta")).toHaveText(EXPECT_CLIPS[id].dur);
      await expect(row.locator(".media-list-sub")).toContainText(EXPECT_CLIPS[id].folder);
      await expect(row.locator(".media-tag-sentry")).toHaveCount(
        EXPECT_CLIPS[id].sentry ? 1 : 0,
      );
    }

    // (d) Recent Events — LIVE, newest-first, severity dot reflects severity.
    await expect(page.locator("[data-testid=events-loading]")).toHaveCount(0);
    const eventRows = page.locator("[data-testid=recent-events] .media-list-item");
    await expect(eventRows).toHaveCount(EXPECT_EVENT_ORDER.length);
    for (let i = 0; i < EXPECT_EVENT_ORDER.length; i++) {
      const id = EXPECT_EVENT_ORDER[i];
      const row = eventRows.nth(i);
      await expect(row).toHaveAttribute("data-event-id", String(id));
      await expect(row.locator(".media-list-title")).toHaveText(EXPECT_EVENTS[id].title);
      await expect(row.locator(".media-list-icon")).toHaveClass(
        new RegExp(EXPECT_EVENTS[id].sev),
      );
    }
  });

  // ── Gate 1b: LIVE data binding — the lists reflect WHATEVER the API returns,
  //    proving the screen binds to the response (drains + sorts) and does NOT
  //    render hard-coded seed constants. Only a DISTINCT intercepted payload can
  //    disprove that false-green. ─────────────────────────────────────────────
  test("live data binding — recent lists reflect the served catalog payload", async ({
    page,
  }) => {
    // A clips page whose NEWEST clip (by started_at) is NOT the highest id, to
    // prove the screen sorts by timestamp (not id) and binds to the payload.
    const clips = {
      items: [
        {
          id: 11,
          canonical_key: "k-oldest",
          started_at: 1_000,
          ended_at: 1_060,
          partition: "p1",
          folder_class: "RecentClips",
          is_sentry: false,
          duration_s: 12,
          availability: "present",
          angles: [],
        },
        {
          id: 12,
          canonical_key: "k-newest",
          started_at: 9_999,
          ended_at: 10_059,
          partition: "p1",
          folder_class: "SentryClips",
          is_sentry: true,
          duration_s: 125, // → "2:05"
          availability: "present",
          angles: [],
        },
      ],
      next_cursor: null,
      limit: 200,
    };
    const events = {
      items: [
        {
          id: 21,
          type: "speed_bump",
          severity: 3,
          t: 5_000,
          lat: null,
          lon: null,
          clip_id: 12,
          trip_id: null,
          front_frame_index: null,
          front_frame_offset_ms: null,
          description: "Speed bump",
        },
        {
          id: 22,
          type: "door_open",
          severity: null,
          t: 1_000,
          lat: null,
          lon: null,
          clip_id: null, // not playable → must be filtered OUT
          trip_id: null,
          front_frame_index: null,
          front_frame_offset_ms: null,
          description: "Door open",
        },
      ],
      next_cursor: null,
      limit: 200,
    };
    await page.route("**/api/clips**", async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(clips) });
    });
    await page.route("**/api/events**", async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(events) });
    });

    await page.goto("/media", { waitUntil: "load" });
    await expect(page.locator(".container[data-screen=media]")).toBeVisible();
    await page.waitForFunction(() => {
      const h = (
        window as unknown as { __TESLAUSB_MEDIA_HOOKS__?: { clipCount: number; eventCount: number } }
      ).__TESLAUSB_MEDIA_HOOKS__;
      return !!h && h.clipCount === 2 && h.eventCount === 1;
    });

    // Clips: newest-by-timestamp first (id 12 though it is the higher id only
    // because its started_at is later — id 11 has the lower timestamp).
    const clipRows = page.locator("[data-testid=recent-clips] .media-list-item");
    await expect(clipRows).toHaveCount(2);
    await expect(clipRows.nth(0)).toHaveAttribute("data-clip-id", "12");
    await expect(clipRows.nth(0).locator(".media-list-meta")).toHaveText("2:05");
    await expect(clipRows.nth(0).locator(".media-tag-sentry")).toHaveCount(1);
    await expect(clipRows.nth(1)).toHaveAttribute("data-clip-id", "11");

    // Events: only the clip-linked one survives the playable filter.
    const eventRows = page.locator("[data-testid=recent-events] .media-list-item");
    await expect(eventRows).toHaveCount(1);
    await expect(eventRows.nth(0)).toHaveAttribute("data-event-id", "21");
    await expect(eventRows.nth(0).locator(".media-list-title")).toHaveText("Speed bump");
  });

  // ── Gate 5: wiring proof — the served HTML runs the freshly-built bundle ─
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

    // (b) the Media module's OWN hook reports the SAME build → the media JS that
    //     produced the DOM is the bundle under test (defends the documented
    //     "edited JS the page never loaded" failure mode).
    const h = await hooks(page);
    expect(h, "window.__TESLAUSB_MEDIA_HOOKS__ must exist").toBeTruthy();
    expect(h!.build).toBe(state.buildId);
    expect(h!.clipCount).toBe(6);
    expect(h!.eventCount).toBe(3);

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

    // (e) the JS asset is served as JavaScript (not HTML via SPA fallback).
    const jsResp = await page.request.get(state.jsAsset);
    expect(jsResp.status()).toBe(200);
    expect(jsResp.headers()["content-type"] ?? "").toMatch(/javascript/);
  });

  // ── Gate 3 (read-only): no mutations; only the allowed catalog reads ────
  test("read-only — mutations impossible, only GET /api/clips + /api/events", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;

    const sockets: string[] = [];
    page.on("websocket", (ws) => sockets.push(ws.url()));

    await gotoMedia(page);
    await page.waitForTimeout(200);

    // No mutating HTTP method, ever (webd is read-only).
    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s): ${JSON.stringify(mutating)}`).toEqual([]);

    // No WebSocket of any kind.
    expect(sockets, `websocket(s) opened: ${JSON.stringify(sockets)}`).toEqual([]);

    // Same-origin only; every /api/ call is a GET to one of the two whitelisted
    // catalog paths (query strings — after/limit — are expected here).
    const apiSeen = new Set<string>();
    for (const req of probe.requests) {
      const u = new URL(req.url);
      expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
      if (!u.pathname.startsWith("/api/")) continue;
      expect(req.method.toUpperCase(), `${req.method} ${u.pathname}`).toBe("GET");
      expect(MEDIA_API.has(u.pathname), `unexpected API path ${u.pathname}`).toBe(true);
      apiSeen.add(u.pathname);
    }
    expect(apiSeen.has("/api/clips"), "/api/clips was never requested").toBe(true);
    expect(apiSeen.has("/api/events"), "/api/events was never requested").toBe(true);

    // No mutation surface in the DOM (read-only screen has no POST form / submit).
    await expect(page.locator("form[method='post' i]")).toHaveCount(0);
    await expect(page.locator("button[type=submit], input[type=submit]")).toHaveCount(0);
  });

  // ── Gate 3 (console + network): zero warnings/errors, no failed/non-2xx ──
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

  // ── Gate 2: performance — capture + report (dev-box profile) ────────────
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
      recentReadyMs: Math.round(readyMs),
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
    expect(report.recentReadyMs).toBeLessThan(8000);
  });

  // ── Gate 4: responsive — render + screenshot at this project's viewport ─
  test("responsive — renders at viewport and screenshot captured", async ({
    page,
  }, testInfo) => {
    await gotoMedia(page);

    // Hub content present regardless of breakpoint.
    await expect(page.locator(".media-tiles")).toBeVisible();
    await expect(page.locator("[data-testid=recent-clips]")).toBeVisible();
    await expect(page.locator("[data-testid=recent-events]")).toBeVisible();

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
