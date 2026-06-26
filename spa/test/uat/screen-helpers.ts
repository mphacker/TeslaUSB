import { expect, loadState, ARTIFACTS, type Probe } from "./helpers";
import type { Page, TestInfo } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";

// Shared UAT helpers for the read-only v1-parity media-section screens
// (Boombox / Music / Light Shows / Wraps / License Plates / Cloud / …). Each
// such screen is strictly read-only: it reproduces the v1 look but makes no API
// calls and exposes no mutation surface. These helpers encode that contract
// once so every screen's spec stays consistent.

export interface MediaHooks {
  build: string;
  screen: string;
}

export function hooks(page: Page): Promise<MediaHooks | undefined> {
  return page.evaluate(
    () =>
      (window as unknown as { __TESLAUSB_MEDIA_HOOKS__?: MediaHooks })
        .__TESLAUSB_MEDIA_HOOKS__,
  );
}

/** Navigate directly to `path` and wait until `screenId`'s module has mounted. */
export async function gotoScreen(page: Page, path: string, screenId: string) {
  await page.goto(path, { waitUntil: "load" });
  await expect(
    page.locator(`.container[data-screen="${screenId}"]`),
  ).toBeVisible();
  await page.waitForFunction(
    (id) => {
      const h = (
        window as unknown as { __TESLAUSB_MEDIA_HOOKS__?: { screen: string } }
      ).__TESLAUSB_MEDIA_HOOKS__;
      return !!h && h.screen === id;
    },
    screenId,
  );
}

/** The app-shell chrome assertions: brand + the MEDIA nav entry active. */
export async function assertMediaChrome(page: Page, testInfo: TestInfo) {
  await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");
  const isMobile = testInfo.project.name.includes("375");
  const activeNav = page.locator(
    isMobile
      ? ".bottom-tabs .tab-item.active"
      : ".sidebar-rail .nav-item.active",
  );
  await expect(activeNav).toBeVisible();
  await expect(activeNav).toHaveAttribute("aria-current", "page");
  await expect(activeNav).toContainText("Media");
}

/** All six media pills, in v1 order, with `activeKey` the active page. */
export async function assertMediaPills(page: Page, activeKey: string) {
  const order = ["chimes", "music", "boombox", "shows", "wraps", "plates"];
  const pills = page.locator(".media-pills .media-pill");
  await expect(pills).toHaveCount(order.length);
  for (let i = 0; i < order.length; i++) {
    await expect(pills.nth(i)).toHaveAttribute("data-pill", order[i]);
  }
  await expect(page.locator("a.media-pill")).toHaveCount(6);
  const active = page.locator(`.media-pill[data-pill="${activeKey}"]`);
  await expect(active).toHaveClass(/\bactive\b/);
  await expect(active).toHaveAttribute("aria-current", "page");
}

export function assertCleanConsole(probe: Probe) {
  expect(
    probe.pageErrors,
    `pageerror(s): ${JSON.stringify(probe.pageErrors)}`,
  ).toEqual([]);
  expect(
    probe.consoleErrors,
    `console error(s): ${JSON.stringify(probe.consoleErrors)}`,
  ).toEqual([]);
  expect(
    probe.consoleWarnings,
    `console warning(s): ${JSON.stringify(probe.consoleWarnings)}`,
  ).toEqual([]);
}

// Shell polls health on every page for v1 parity; allow only this global read.
// Exported so the per-screen read-only specs (which inline their own /api/
// guards rather than calling assertReadOnly) can skip the same global poll.
export const SHELL_POLL_ALLOWLIST = new Set(["/api/system/health"]);

/** No mutating HTTP, no websockets, no /api/ calls, no mutation surface. */
export async function assertReadOnly(
  page: Page,
  probe: Probe,
  sockets: string[],
) {
  const origin = new URL(loadState().baseURL).origin;
  const mutating = probe.requests.filter((r) =>
    ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
  );
  expect(mutating, `mutating request(s): ${JSON.stringify(mutating)}`).toEqual(
    [],
  );
  expect(sockets, `websocket(s): ${JSON.stringify(sockets)}`).toEqual([]);
  for (const req of probe.requests) {
    const u = new URL(req.url);
    expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
    expect(
      u.pathname.startsWith("/api/") && !SHELL_POLL_ALLOWLIST.has(u.pathname),
      `unexpected API call ${req.method} ${u.pathname}`,
    ).toBe(false);
  }
  await expect(page.locator("form[method='post' i]")).toHaveCount(0);
  await expect(
    page.locator("button[type=submit], input[type=submit]"),
  ).toHaveCount(0);
  await expect(page.locator("input[type=file]")).toHaveCount(0);
}

/** Clean network: no failed, no off-origin, no non-2xx same-origin. */
export function assertCleanNetwork(probe: Probe) {
  const origin = new URL(loadState().baseURL).origin;
  expect(
    probe.failedRequests,
    `failed request(s): ${JSON.stringify(probe.failedRequests)}`,
  ).toEqual([]);
  const offOrigin = probe.requests.filter(
    (r) => new URL(r.url).origin !== origin,
  );
  expect(offOrigin, `off-origin: ${JSON.stringify(offOrigin)}`).toEqual([]);
  const bad = probe.responses.filter(
    (r) => new URL(r.url).origin === origin && r.status >= 400,
  );
  expect(bad, `non-2xx: ${JSON.stringify(bad)}`).toEqual([]);
}

/** Prove the served HTML runs the freshly-built bundle + this screen module. */
export async function assertWiring(page: Page, path: string, screenId: string) {
  const state = loadState();
  const winBuild = await page.evaluate(
    () =>
      (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__,
  );
  expect(winBuild, "window.__TESLAUSB_BUILD__ must be defined").toBeTruthy();
  expect(winBuild).not.toBe("dev");
  expect(winBuild).toBe(state.buildId);

  const h = await hooks(page);
  expect(h, "window.__TESLAUSB_MEDIA_HOOKS__ must exist").toBeTruthy();
  expect(h!.build).toBe(state.buildId);
  expect(h!.screen).toBe(screenId);

  const loadedScripts = await page.evaluate(() =>
    Array.from(document.scripts).map((s) => s.src),
  );
  expect(
    loadedScripts.some((s) => s.includes(state.jsAsset)),
    `executed document must load ${state.jsAsset}`,
  ).toBe(true);
  expect(loadedScripts.some((s) => s.includes("/src/main.tsx"))).toBe(false);

  const html = await (await page.request.get(path)).text();
  expect(html).toContain(state.jsAsset);
  expect(html).not.toContain("/src/main.tsx");
  if (state.cssAsset) expect(html).toContain(state.cssAsset);
  expect(html).toContain("/static/css/style.css");
}

/** Capture perf timings to an artifact + assert dev-box sanity bounds. */
export async function capturePerf(
  page: Page,
  testInfo: TestInfo,
  name: string,
) {
  const readyMs = await page.evaluate(() => performance.now());
  const timings = await page.evaluate(() => {
    const nav = performance.getEntriesByType(
      "navigation",
    )[0] as PerformanceNavigationTiming;
    const fcp = performance
      .getEntriesByType("paint")
      .find((p) => p.name === "first-contentful-paint");
    const resources = (
      performance.getEntriesByType("resource") as PerformanceResourceTiming[]
    )
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
      fcpMs: fcp ? Math.round(fcp.startTime) : null,
      slowestRequests: resources,
    };
  });
  const report = {
    environment:
      "dev webd (cargo debug) on Windows host; Chromium via Playwright; cold " +
      "cache. spa.md's <~2s 'interactive' bar is the ON-DEVICE (Pi) profile — " +
      "these are dev-box numbers, reported not asserted against that bar.",
    viewport: testInfo.project.name,
    screen: name,
    ...timings,
    screenReadyMs: Math.round(readyMs),
  };
  const out = resolve(ARTIFACTS, `perf-${name}-${testInfo.project.name}.json`);
  writeFileSync(out, JSON.stringify(report, null, 2));
  await testInfo.attach(`perf-${name}-${testInfo.project.name}.json`, {
    body: JSON.stringify(report, null, 2),
    contentType: "application/json",
  });
  expect(report.fcpMs, "FCP should be present").not.toBeNull();
  expect(report.fcpMs!).toBeLessThan(6000);
  expect(report.screenReadyMs).toBeLessThan(8000);
}

/** Full-page screenshot artifact + breakpoint chrome assertions. */
export async function captureScreenshot(
  page: Page,
  testInfo: TestInfo,
  name: string,
) {
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
  const shot = resolve(ARTIFACTS, `${name}-${testInfo.project.name}.png`);
  await page.screenshot({ path: shot, fullPage: true });
  await testInfo.attach(`${name}-${testInfo.project.name}.png`, {
    path: shot,
    contentType: "image/png",
  });
  console.log(`[uat][screenshot:${name}:${testInfo.project.name}] ${shot}`);
}
