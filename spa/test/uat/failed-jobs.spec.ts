import {
  test,
  expect,
  loadState,
  ARTIFACTS,
  type Probe,
} from "./helpers";
import type { Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { SHELL_POLL_ALLOWLIST } from "./screen-helpers";

// ── Failed jobs screen UAT (fe-failed-jobs) ───────────────────────────────
// Each test drives the REAL bundle served by webd against the seeded read-only
// catalog (global-setup). The screen at /failed-jobs is a read-only snapshot of
// the jobs webd retained as FAILED (contract D2 webd-api.md §2.1/§3): it issues
// exactly ONE GET — /api/jobs/failed — which returns a WRAPPED envelope
// { "jobs": JobStatus[] }, a bounded ring (≤100) of failures, OLDEST-first.
// The screen renders newest-failure-first (reverse of the ring), with states
// loading / error(+Retry) / empty("No failed jobs") / populated.
//
// The functional / ordering / cap / error tests intercept /api/jobs/failed with
// deterministic fixtures so assertions never depend on the build host. The
// clean / read-only / wiring / empty tests hit the REAL endpoint (a fresh seed
// has no failed jobs, so it returns {jobs:[]} → the empty state) to prove it
// returns 2xx with a clean console. Screenshots are captured as artifacts.

// The only read API this screen may call. webd is read-only; anything outside
// this set (or any non-GET) is a hard failure.
const ALLOWED_API = new Set(["/api/jobs/failed"]);

// Deterministic fixture — the ring is OLDEST-first on the wire (job_ids
// ascending). Field names mirror the webd serde DTO exactly. One job carries a
// handoff_id + detail; the last has neither handoff_id nor detail set.
const FAILED_FIXTURE = {
  jobs: [
    {
      job_id: 41,
      kind: "clip_delete",
      state: "failed",
      progress: null,
      detail: "io error",
      handoff_id: "h-41",
    },
    {
      job_id: 42,
      kind: "clip_delete",
      state: "failed",
      progress: null,
      detail: "LUN left ejected: handoff timeout",
      handoff_id: "h-42",
    },
    {
      job_id: 43,
      kind: "chime_install",
      state: "failed",
      progress: null,
    },
  ],
};

const EMPTY_FIXTURE = { jobs: [] };

/** Intercept the screen's single probe with a deterministic body. Must run
 *  BEFORE the navigation that triggers the mount-time fetch. */
async function routeJobs(page: Page, body: unknown, status = 200) {
  await page.route("**/api/jobs/failed", (r) =>
    r.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    }),
  );
}

/** Settle: bundle executed, failed-jobs screen structure painted. */
async function gotoFailedJobs(page: Page) {
  await page.goto("/failed-jobs", { waitUntil: "load" });
  await expect(page.locator("[data-screen=failed-jobs]")).toBeVisible();
  await expect(page.locator(".fj-header .fj-title")).toBeVisible();
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

test.describe("failed jobs UAT", () => {
  // ── Gate 1: functional — shell, populated list, newest-first, fields ────
  test("functional — shell, populated list rendered newest-first with fields", async ({
    page,
    probe,
  }, testInfo) => {
    await routeJobs(page, FAILED_FIXTURE);
    await gotoFailedJobs(page);

    // App shell parity: brand + toast region.
    await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");
    await expect(page.locator("#toast-container")).toHaveCount(1);

    // Active nav is Settings (no dedicated jobs nav key). Assert against the nav
    // actually visible at this breakpoint (rail >=1024px else bottom tabs).
    const isMobile = testInfo.project.name.includes("375");
    const activeNav = page.locator(
      isMobile ? ".bottom-tabs .tab-item.active" : ".sidebar-rail .nav-item.active",
    );
    await expect(activeNav).toBeVisible();
    await expect(activeNav).toHaveAttribute("aria-current", "page");
    await expect(activeNav).toContainText("Settings");

    // Title + live status line (3 failed jobs).
    await expect(page.locator(".fj-title")).toHaveText("Failed jobs");
    await expect(page.locator('[data-testid="fj-status"]')).toHaveText("3 failed jobs.");

    // One card per failed job, rendered NEWEST-first = reverse of the oldest-
    // first ring ⇒ job_ids 43, 42, 41 (NOT a job_id sort — insertion order).
    const items = page.locator('[data-testid="failed-jobs-list"] .fj-item');
    await expect(items).toHaveCount(3);
    await expect(items.nth(0)).toHaveAttribute("data-job-id", "43");
    await expect(items.nth(1)).toHaveAttribute("data-job-id", "42");
    await expect(items.nth(2)).toHaveAttribute("data-job-id", "41");

    // Newest card (43, chime_install): kind + "Failed" badge; NO handoff/detail
    // rows (the fixture left both unset → omitted, not fabricated).
    const newest = items.nth(0);
    await expect(newest.locator(".fj-kind")).toHaveText("chime_install");
    await expect(newest.locator(".fj-badge")).toContainText("Failed");
    await expect(newest.locator(".fj-dl")).toContainText("Job ID");
    await expect(newest.locator(".fj-dl")).toContainText("43");
    await expect(newest.locator(".fj-dl")).not.toContainText("Handoff");
    await expect(newest.locator(".fj-detail")).toHaveCount(0);

    // A card with handoff + detail (42) surfaces both verbatim.
    const mid = items.nth(1);
    await expect(mid.locator(".fj-dl")).toContainText("Handoff");
    await expect(mid.locator(".fj-dl")).toContainText("h-42");
    await expect(mid.locator(".fj-detail")).toHaveText("LUN left ejected: handoff timeout");

    // Progress is null in the fixture ⇒ degraded to "—" (never fabricated).
    await expect(newest.locator(".fj-dl")).toContainText("\u2014");

    // No empty/error states while populated.
    await expect(page.locator('[data-testid="failed-jobs-empty"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="failed-jobs-error"]')).toHaveCount(0);

    assertCleanConsole(probe);
  });

  // ── Gate 2: empty — fresh seed (REAL endpoint) shows the empty state ─────
  test("empty — real endpoint with no failures shows 'No failed jobs'", async ({
    page,
    probe,
  }) => {
    // No interception: a fresh seeded catalog has no failed jobs, so the REAL
    // /api/jobs/failed returns {jobs:[]} ⇒ the empty state (not an error).
    await gotoFailedJobs(page);
    await page.waitForLoadState("networkidle");

    await expect(page.locator('[data-testid="failed-jobs-empty"]')).toBeVisible();
    await expect(page.locator('[data-testid="failed-jobs-empty"] .fj-empty-title')).toHaveText(
      "No failed jobs",
    );
    await expect(page.locator('[data-testid="fj-status"]')).toHaveText("No failed jobs.");
    // Empty is distinct from error — no error card.
    await expect(page.locator('[data-testid="failed-jobs-error"]')).toHaveCount(0);

    assertCleanConsole(probe);
  });

  // ── Gate 3: error + retry — 5xx shows error state; Retry re-GETs ─────────
  test("error — 5xx renders error state and Retry recovers", async ({ page }) => {
    // First load fails (500). The screen must show a handled error state, not a
    // pageerror, and offer Retry.
    await routeJobs(page, { error: { code: "boom", message: "server boom" } }, 500);
    await gotoFailedJobs(page);

    await expect(page.locator('[data-testid="failed-jobs-error"]')).toBeVisible();
    await expect(page.locator('[data-testid="fj-status"]')).toHaveText(
      "Couldn't load failed jobs.",
    );

    // Re-point the route to a healthy empty body, then Retry: the screen must
    // re-GET and transition out of the error state into the empty state.
    await page.unroute("**/api/jobs/failed");
    await routeJobs(page, EMPTY_FIXTURE);
    await page.locator('[data-testid="fj-retry"]').click();

    await expect(page.locator('[data-testid="failed-jobs-empty"]')).toBeVisible();
    await expect(page.locator('[data-testid="failed-jobs-error"]')).toHaveCount(0);
  });

  // ── Gate 4: cap note — a full ring surfaces the bounded-snapshot note ────
  test("cap — a ring at capacity surfaces the bounded-snapshot note", async ({ page }) => {
    const full = {
      jobs: Array.from({ length: 100 }, (_, i) => ({
        job_id: 1000 + i,
        kind: "clip_delete",
        state: "failed",
        progress: null,
        detail: `failure ${i}`,
      })),
    };
    await routeJobs(page, full);
    await gotoFailedJobs(page);

    await expect(page.locator('[data-testid="failed-jobs-list"] .fj-item')).toHaveCount(100);
    await expect(page.locator('[data-testid="failed-jobs-cap"]')).toBeVisible();
    // Newest-first: job_id 1099 is at the top.
    await expect(
      page.locator('[data-testid="failed-jobs-list"] .fj-item').first(),
    ).toHaveAttribute("data-job-id", "1099");
  });

  // ── Gate 5: wiring proof (the freshly-built bundle is what executed) ─────
  test("wiring — served HTML loads the hashed bundle that actually ran", async ({ page }) => {
    const state = loadState();
    await gotoFailedJobs(page);

    const winBuild = await page.evaluate(
      () => (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__,
    );
    expect(winBuild, "window.__TESLAUSB_BUILD__ must be defined").toBeTruthy();
    expect(winBuild).not.toBe("dev");
    expect(winBuild).toBe(state.buildId);

    const html = await (await page.request.get("/failed-jobs")).text();
    expect(html).toContain(state.jsAsset);
    expect(html).not.toContain("/src/main.tsx");

    const jsResp = await page.request.get(state.jsAsset);
    expect(jsResp.status()).toBe(200);
    expect(jsResp.headers()["content-type"] ?? "").toMatch(/javascript/);

    if (state.cssAsset) {
      const cssResp = await page.request.get(state.cssAsset);
      expect(cssResp.status()).toBe(200);
      expect(cssResp.headers()["content-type"] ?? "").toMatch(/css/);
    }
  });

  // ── Gate 6: read-only — only the whitelisted GET; no mutation ───────────
  test("read-only — only /api/jobs/failed GET, Refresh re-GETs, no mutation", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    await gotoFailedJobs(page);
    await page.waitForLoadState("networkidle");

    // Refresh must issue another GET to the same endpoint (still read-only).
    await page.locator('[data-testid="fj-refresh"]').click();
    await page.waitForLoadState("networkidle");

    const seen = new Set<string>();
    let jobsGets = 0;
    for (const req of probe.requests) {
      const u = new URL(req.url);
      expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
      if (!u.pathname.startsWith("/api/")) continue;
      if (SHELL_POLL_ALLOWLIST.has(u.pathname)) continue;
      expect(req.method.toUpperCase(), `${req.method} ${u.pathname}`).toBe("GET");
      expect(ALLOWED_API.has(u.pathname), `unexpected API path ${u.pathname}`).toBe(true);
      if (u.pathname === "/api/jobs/failed") jobsGets += 1;
      seen.add(u.pathname);
    }
    expect(seen.has("/api/jobs/failed"), "/api/jobs/failed was never requested").toBe(true);
    // Mount fetch + the Refresh click ⇒ at least two GETs.
    expect(jobsGets, "Refresh should re-GET the snapshot").toBeGreaterThanOrEqual(2);

    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s): ${JSON.stringify(mutating)}`).toEqual([]);

    // Read-only by design: no <form> on the screen.
    await expect(page.locator("[data-screen=failed-jobs] form")).toHaveCount(0);
  });

  // ── Gate 7: clean — zero console noise, no non-2xx (REAL endpoint) ───────
  test("clean — zero console warnings/errors/pageerror and no non-2xx", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    // No interception: drive the REAL webd endpoint (empty {jobs:[]}). The
    // screen must render with a clean console and 2xx everywhere.
    await gotoFailedJobs(page);
    await page.waitForLoadState("networkidle");
    // Bounded post-load window catches a delayed render/probe issue.
    await page.waitForTimeout(750);

    assertCleanConsole(probe);

    expect(
      probe.failedRequests,
      `failed request(s): ${JSON.stringify(probe.failedRequests)}`,
    ).toEqual([]);

    const bad = probe.responses.filter(
      (r) => new URL(r.url).origin === origin && r.status >= 400,
    );
    expect(bad, `non-2xx response(s): ${JSON.stringify(bad)}`).toEqual([]);
  });

  // ── Gate 8: performance — capture + report (dev-box profile) ─────────────
  test("perf — capture TTFB/DCL/FCP/interactive + slowest requests", async ({
    page,
  }, testInfo) => {
    const navStart = Date.now();
    await page.goto("/failed-jobs", { waitUntil: "load" });
    await expect(page.locator("[data-screen=failed-jobs]")).toBeVisible();
    const contentVisibleMs = await page.evaluate(() => performance.now());

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

    // Interaction responsiveness: theme toggle must take effect (genuinely
    // interactive, not just painted).
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
        "dev webd (cargo debug build) on host; Chromium via Playwright; fresh " +
        "context per test (cold cache). The <~2s 'interactive' target is the " +
        "ON-DEVICE (Raspberry Pi) profile — these are dev-box numbers, reported " +
        "not asserted against that bar.",
      viewport: testInfo.project.name,
      ttfbMs: timings.ttfbMs,
      domContentLoadedMs: timings.domContentLoadedMs,
      domInteractiveMs: timings.domInteractiveMs,
      loadMs: timings.loadMs,
      fcpMs: timings.fcpMs,
      contentVisibleMs: Math.round(contentVisibleMs),
      themeToggleResponseMs: themeToggleMs,
      wallClockNavMs: Date.now() - navStart,
      slowestRequests: timings.slowestRequests,
    };

    const out = resolve(ARTIFACTS, `perf-failed-jobs-${testInfo.project.name}.json`);
    writeFileSync(out, JSON.stringify(report, null, 2));
    await testInfo.attach(`perf-failed-jobs-${testInfo.project.name}.json`, {
      body: JSON.stringify(report, null, 2),
      contentType: "application/json",
    });
    console.log(
      `[uat][perf:failed-jobs:${testInfo.project.name}]`,
      JSON.stringify(report, null, 2),
    );

    expect(report.fcpMs, "FCP should be present").not.toBeNull();
    expect(report.fcpMs!).toBeLessThan(5000);
    expect(report.contentVisibleMs).toBeLessThan(5000);
  });

  // ── Gate 9: responsive — render + screenshot at this project's viewport ─
  test("responsive — renders at viewport and screenshot captured", async ({
    page,
  }, testInfo) => {
    await routeJobs(page, FAILED_FIXTURE);
    await gotoFailedJobs(page);

    // Content present regardless of breakpoint.
    await expect(page.locator(".fj-header")).toBeVisible();
    await expect(page.locator('[data-testid="failed-jobs-list"]')).toBeVisible();
    await expect(page.locator('[data-testid="fj-refresh"]')).toBeVisible();

    // Breakpoint-specific chrome: desktop shows the rail, mobile the tabs.
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

    const shot = resolve(ARTIFACTS, `failed-jobs-${testInfo.project.name}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    await testInfo.attach(`failed-jobs-${testInfo.project.name}.png`, {
      path: shot,
      contentType: "image/png",
    });
    console.log(`[uat][screenshot:failed-jobs:${testInfo.project.name}] ${shot}`);
  });
});
