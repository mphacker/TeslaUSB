import { test, expect, loadState, ARTIFACTS, type Probe } from "./helpers";
import {
  assertCleanConsole,
  assertCleanNetwork,
  capturePerf,
  captureScreenshot,
} from "./screen-helpers";

const PATH = "/cloud";
const SCREEN = "shell";

test.describe("shell UAT", () => {
  test("health dot parity — poll reveals status and maps severity class", async ({
    page,
    probe,
  }, testInfo) => {
    const state = loadState();
    const currentProbe: Probe = probe;
    expect(ARTIFACTS).toContain("artifacts");

    await page.route("**/api/system/health", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ overall: "warn", subsystems: {} }),
      }),
    );

    const healthPoll = page.waitForRequest((r) => {
      const u = new URL(r.url());
      return r.method() === "GET" && u.pathname === "/api/system/health";
    });

    await page.goto(PATH, { waitUntil: "load" });
    await healthPoll;
    await expect(page.locator(".container[data-screen='cloud-archive']")).toBeVisible();
    expect(
      currentProbe.requests.some(
        (r) => new URL(r.url).origin === new URL(state.baseURL).origin,
      ),
    ).toBe(true);

    const link = page.locator("#health-dot-link");
    const dot = page.locator("#health-dot");
    await expect(link).toBeVisible();
    await expect(link).toHaveJSProperty("hidden", false);
    await expect(dot).toHaveClass(/\bhealth-dot\b/);
    await expect(dot).toHaveClass(/\bhealth-dot-warn\b/);
    const severityCount = await dot.evaluate((el) =>
      ["health-dot-ok", "health-dot-warn", "health-dot-error", "health-dot-unknown"].filter(
        (name) => el.classList.contains(name),
      ).length,
    );
    expect(severityCount).toBe(1);
    await expect(link).toHaveAttribute("title", /\S+/);
    await expect(link).toHaveAttribute("aria-label", /\S+/);

    assertCleanConsole(currentProbe);
    assertCleanNetwork(currentProbe);
    await captureScreenshot(page, testInfo, SCREEN);
  });

  test("theme toggle persists and flips both ways", async ({ page }) => {
    await page.goto(PATH, { waitUntil: "load" });
    await expect(page.locator(".container[data-screen='cloud-archive']")).toBeVisible();

    const theme = () =>
      page.evaluate(() => document.documentElement.getAttribute("data-theme") ?? "light");

    const initial = await theme();
    await page.locator(".theme-toggle-btn").click();
    const toggled = await theme();
    expect(toggled).not.toBe(initial);
    await expect.poll(() => page.evaluate(() => localStorage.getItem("theme"))).toBe(toggled);

    await page.locator(".theme-toggle-btn").click();
    await expect.poll(theme).toBe(initial);
    await expect.poll(() => page.evaluate(() => localStorage.getItem("theme"))).toBe(initial);
  });

  test("perf — capture TTFB/FCP + slowest requests", async ({
    page,
  }, testInfo) => {
    await page.goto(PATH, { waitUntil: "load" });
    await expect(page.locator(".container[data-screen='cloud-archive']")).toBeVisible();
    await capturePerf(page, testInfo, SCREEN);
  });
});
