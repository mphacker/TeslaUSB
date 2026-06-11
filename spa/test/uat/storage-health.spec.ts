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

// ── Storage health screen UAT (fe-storage-health) ─────────────────────────
// Each test drives the REAL bundle served by webd against a seeded read-only
// catalog (global-setup). The screen at /storage is a read-only reproduction
// of the legacy "Storage" page (docs/tasks/parity-baseline/storage/): it shows
// LIVE storage/health/metrics and degrades unsupported facts (allocation, SD
// wear telemetry, retention governor) to "—" rather than fabricating them.
//
// The functional-parity test intercepts the four read-only probes with
// deterministic fixtures so the assertions never depend on the build host's
// real /proc or disk (webd on a non-Linux host honestly degrades those). The
// clean / read-only / wiring tests hit the REAL endpoints to prove they return
// 2xx with a clean console. We capture screenshots as artifacts (no pixel-diff).

// The read APIs the storage-health screen is permitted to call. webd is
// read-only; anything outside this set (or any non-GET) is a hard failure.
const ALLOWED_API = new Set([
  "/api/storage",
  "/api/storage/health",
  "/api/system/metrics",
  "/api/system/health",
]);

// Deterministic fixtures. Field names mirror the webd serde DTOs exactly.
// Byte values are whole GiB/MiB so humanBytes() renders clean, asserttable text.
const GIB = 1024 ** 3;
const MIB = 1024 ** 2;

const STORAGE_FIXTURE = {
  filesystems: [
    {
      mount: "/mnt/teslausb",
      device: "/dev/mmcblk0p3",
      fstype: "ext4",
      free_bytes: 200 * GIB,
      total_bytes: 256 * GIB,
      free_inodes: 1_900_000,
      total_inodes: 2_000_000,
    },
    {
      mount: "/boot",
      device: "/dev/mmcblk0p1",
      fstype: "vfat",
      free_bytes: 200 * MIB,
      total_bytes: 256 * MIB,
      free_inodes: 0,
      total_inodes: 0,
    },
  ],
  governor: null,
};

const STORAGE_HEALTH_FIXTURE = {
  severity: "ok",
  summary: "200 GB free of 256 GB",
  device: "/dev/mmcblk0p3",
  fstype: "ext4",
  mount: "/mnt/teslausb",
  used_bytes: 56 * GIB,
  total_bytes: 256 * GIB,
  fs_errors: null,
  io_errors_24h: null,
  trim: null,
};

const METRICS_FIXTURE = {
  uptime_s: 123456,
  load: { one: 0.15, five: 0.22, fifteen: 0.18 },
  mem: { total_bytes: 512 * MIB, available_bytes: 256 * MIB, used_pct: 50 },
  swap: null,
  cpu_temp_c: 47.2,
  updated_at: 1700000000,
};

const SYS_HEALTH_FIXTURE = {
  overall: "ok",
  subsystems: {
    disk: { severity: "ok", message: "200.0 GB free of 256.0 GB (78%)" },
    storage_writable: { severity: "ok", message: "archive root writable" },
    teslafat_0: { severity: "warn", message: "TeslaCam exFAT inactive" },
    gadget: { severity: "ok", message: "USB gadget configured (attached)" },
  },
};

/** Intercept the four read-only probes with deterministic fixtures so the
 *  functional assertions are host-independent. The specific `/storage/health`
 *  route is registered last so Playwright (matches most-recent first) resolves
 *  it before the broader `/storage` glob. Must run BEFORE the navigation that
 *  triggers the screen's mount-time fetches. */
async function routeProbes(page: Page) {
  const json = (body: unknown) => ({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
  await page.route("**/api/storage", (r) => r.fulfill(json(STORAGE_FIXTURE)));
  await page.route("**/api/system/metrics", (r) => r.fulfill(json(METRICS_FIXTURE)));
  await page.route("**/api/system/health", (r) => r.fulfill(json(SYS_HEALTH_FIXTURE)));
  await page.route("**/api/storage/health", (r) => r.fulfill(json(STORAGE_HEALTH_FIXTURE)));
}

/** Settle: bundle executed, storage screen structure painted. */
async function gotoStorage(page: Page) {
  await page.goto("/storage", { waitUntil: "load" });
  await expect(page.locator("[data-screen=storage-health]")).toBeVisible();
  await expect(page.locator(".storage-header .storage-title")).toBeVisible();
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

test.describe("storage health UAT", () => {
  // ── Gate 1: functional + structural parity ─────────────────────────────
  test("functional — shell, live capacity/health/metrics, degraded facts", async ({
    page,
    probe,
  }, testInfo) => {
    await routeProbes(page);
    await gotoStorage(page);

    // App shell parity: brand + toast region.
    await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");
    await expect(page.locator("#toast-container")).toHaveCount(1);

    // Active nav is Settings (no dedicated storage nav key). Assert against the
    // nav actually visible at this breakpoint (rail >=1024px else bottom tabs).
    const isMobile = testInfo.project.name.includes("375");
    const activeNav = page.locator(
      isMobile ? ".bottom-tabs .tab-item.active" : ".sidebar-rail .nav-item.active",
    );
    await expect(activeNav).toBeVisible();
    await expect(activeNav).toHaveAttribute("aria-current", "page");
    await expect(activeNav).toContainText("Settings");

    // Header — title + live SD pills from the primary filesystem (/mnt/teslausb:
    // 256 GB total, 200 GB free ⇒ 56 GB used). Allocation pills degrade to "—".
    await expect(page.locator(".storage-title")).toHaveText("Storage");
    const pills = page.locator(".storage-header .storage-pill-row .storage-pill");
    await expect(pills).toHaveText([
      "SD total: 256 GB",
      "SD free: 200 GB",
      "Used: 56 GB",
      "Allocated: \u2014",
      "OS + reserve: \u2014",
      "Unallocated: \u2014",
    ]);

    // Primary capacity bar painted with a real used segment. 56 GB used of
    // 256 GB ⇒ 22% used ⇒ the calm "used" band (not warn/crit).
    const primaryUsed = page.locator(".storage-header [data-primary-used]");
    await expect(primaryUsed).toHaveCount(1);
    await expect(primaryUsed).toHaveClass("cap-seg cap-seg-used");

    // Storage Health card — severity badge + summary + the live facts; the SD
    // wear-telemetry rows (errors / I/O / TRIM) degrade to "—" (not fabricated).
    await expect(page.locator("#storage-health-card .storage-badge")).toHaveAttribute(
      "data-severity",
      "ok",
    );
    await expect(page.locator("#storage-health-summary")).toHaveText(
      "200 GB free of 256 GB",
    );
    await expect(page.locator("#storage-health-grid dd")).toHaveText([
      "/dev/mmcblk0p3",
      "ext4",
      "/mnt/teslausb",
      "56 GB",
      "256 GB",
      "\u2014",
      "\u2014",
      "\u2014",
    ]);

    // Filesystems — one row per mounted filesystem from the fixture (2). The
    // primary volume is 56 GB used of 256 GB ⇒ 22% used.
    await expect(page.locator("#filesystems-list .fs-item")).toHaveCount(2);
    await expect(
      page.locator('#filesystems-list .fs-item[data-fs-mount="/mnt/teslausb"]'),
    ).toContainText("22%");

    // Subsystem status — storage-relevant rows; teslafat_1 has no probe data in
    // the fixture so it degrades to "—" while the others carry live messages.
    const subText = await page.locator("#storage-subsystems-grid").innerText();
    expect(subText).toContain("archive root writable");
    expect(subText).toContain("TeslaCam exFAT inactive");
    expect(subText).toContain("USB gadget configured (attached)");
    expect(subText).toContain("\u2014"); // teslafat_1 (Media) unprobed → degraded

    // Live resources — mem/swap/load/uptime; swap is null ⇒ "—"/"none".
    await expect(page.locator("#storage-metric-mem .storage-metric-value")).toHaveText("50%");
    await expect(page.locator("#storage-metric-mem .storage-metric-detail")).toHaveText(
      "256 MB / 512 MB",
    );
    await expect(page.locator("#storage-metric-swap .storage-metric-value")).toHaveText("\u2014");
    await expect(page.locator("#storage-metric-load .storage-metric-value")).toHaveText(
      "0.15 / 0.22 / 0.18",
    );
    await expect(page.locator("#storage-metric-uptime .storage-metric-value")).toHaveText(
      "up 1d 10h 17m",
    );
    await expect(page.locator("#storage-metric-temp .storage-metric-value")).toHaveText(
      "47.2 \u00b0C",
    );
    await expect(page.locator("#storage-metric-temp .storage-metric-detail")).toHaveText(
      "Nominal",
    );

    // Retention headroom — governor is null ⇒ degraded note (no fabricated figure).
    await expect(page.locator('[data-testid="retention-degraded"]')).toBeVisible();

    assertCleanConsole(probe);
  });

  // ── Gate 2: wiring proof (the freshly-built bundle is what executed) ─────
  test("wiring — served HTML loads the hashed bundle that actually ran", async ({
    page,
  }) => {
    const state = loadState();
    await gotoStorage(page);

    const winBuild = await page.evaluate(
      () => (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__,
    );
    expect(winBuild, "window.__TESLAUSB_BUILD__ must be defined").toBeTruthy();
    expect(winBuild).not.toBe("dev");
    expect(winBuild).toBe(state.buildId);

    const html = await (await page.request.get("/storage")).text();
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

  // ── Gate 3: read-only — only whitelisted GETs; no mutation ──────────────
  test("read-only — only whitelisted GET probes, no mutating requests", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    await gotoStorage(page);
    await page.waitForLoadState("networkidle");

    const seen = new Set<string>();
    for (const req of probe.requests) {
      const u = new URL(req.url);
      expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
      if (!u.pathname.startsWith("/api/")) continue;
      expect(req.method.toUpperCase(), `${req.method} ${u.pathname}`).toBe("GET");
      expect(ALLOWED_API.has(u.pathname), `unexpected API path ${u.pathname}`).toBe(true);
      seen.add(u.pathname);
    }
    // Prove the four read-only probes are actually wired in.
    expect(seen.has("/api/storage"), "/api/storage was never requested").toBe(true);
    expect(seen.has("/api/storage/health"), "/api/storage/health never requested").toBe(true);
    expect(seen.has("/api/system/metrics"), "/api/system/metrics never requested").toBe(true);
    expect(seen.has("/api/system/health"), "/api/system/health never requested").toBe(true);

    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s): ${JSON.stringify(mutating)}`).toEqual([]);

    // The screen has no <form> and no submit/save controls (read-only by design).
    await expect(page.locator("[data-screen=storage-health] form")).toHaveCount(0);
    await expect(page.locator("[data-screen=storage-health] button")).toHaveCount(0);
  });

  // ── Gate 4: clean — zero console noise, no failed/non-2xx (REAL endpoints) ─
  test("clean — zero console warnings/errors/pageerror and no non-2xx", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    // No interception here: drive the REAL webd probes. On a non-Linux build
    // host they honestly degrade (unknown/empty) and the screen must still
    // render with a clean console and 2xx everywhere.
    await gotoStorage(page);
    await page.waitForLoadState("networkidle");
    // Bounded post-load window catches a delayed poller / a probe that 5xx's.
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

  // ── Gate 5: performance — capture + report (dev-box profile) ────────────
  test("perf — capture TTFB/DCL/FCP/interactive + slowest requests", async ({
    page,
  }, testInfo) => {
    const navStart = Date.now();
    await page.goto("/storage", { waitUntil: "load" });
    await expect(page.locator("[data-screen=storage-health]")).toBeVisible();
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

    const out = resolve(ARTIFACTS, `perf-storage-${testInfo.project.name}.json`);
    writeFileSync(out, JSON.stringify(report, null, 2));
    await testInfo.attach(`perf-storage-${testInfo.project.name}.json`, {
      body: JSON.stringify(report, null, 2),
      contentType: "application/json",
    });
    console.log(`[uat][perf:storage:${testInfo.project.name}]`, JSON.stringify(report, null, 2));

    expect(report.fcpMs, "FCP should be present").not.toBeNull();
    expect(report.fcpMs!).toBeLessThan(5000);
    expect(report.contentVisibleMs).toBeLessThan(5000);
  });

  // ── Gate 6: responsive — render + screenshot at this project's viewport ─
  test("responsive — renders at viewport and screenshot captured", async ({
    page,
  }, testInfo) => {
    await routeProbes(page);
    await gotoStorage(page);

    // Content present regardless of breakpoint.
    await expect(page.locator("#storage-health-card")).toBeVisible();
    await expect(page.locator("#filesystems-card")).toBeVisible();
    await expect(page.locator("#storage-resources-card")).toBeVisible();

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

    const shot = resolve(ARTIFACTS, `storage-${testInfo.project.name}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    await testInfo.attach(`storage-${testInfo.project.name}.png`, {
      path: shot,
      contentType: "image/png",
    });
    console.log(`[uat][screenshot:storage:${testInfo.project.name}] ${shot}`);
  });
});
