import {
  test,
  expect,
  loadState,
  ARTIFACTS,
  GADGET_STATUS_OK,
  type Probe,
} from "./helpers";
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

    const modeDot = page.locator("#mode-dot");
    await expect(modeDot).toBeVisible();
    await expect(modeDot).toHaveClass(/\bstatus-present\b/);
    await expect(modeDot).toHaveAttribute("title", "USB drive connected to vehicle");
    await expect(modeDot).toHaveAttribute("aria-label", "USB drive connected to vehicle");
    const shellOrder = await page.locator(".top-bar-right").evaluate((el) => {
      const nodes = Array.from(el.children);
      const idx = (selector: string) =>
        nodes.findIndex((n) => n.matches(selector));
      return {
        health: idx("#health-dot-link"),
        mode: idx("#mode-dot"),
        theme: idx(".theme-toggle-btn"),
      };
    });
    expect(shellOrder.health).toBeGreaterThanOrEqual(0);
    expect(shellOrder.mode).toBeGreaterThan(shellOrder.health);
    expect(shellOrder.theme).toBeGreaterThan(shellOrder.mode);

    assertCleanConsole(currentProbe);
    assertCleanNetwork(currentProbe);
    await captureScreenshot(page, testInfo, SCREEN);
  });

  test("mode dot stays visible and gray when gadget status is unavailable", async ({
    page,
    probe,
  }) => {
    const currentProbe: Probe = probe;
    await page.unroute("**/api/gadget/status");
    await page.route("**/api/gadget/status", (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: "gadget unavailable" }),
      }),
    );

    await page.goto(PATH, { waitUntil: "load" });
    await expect(page.locator(".container[data-screen='cloud-archive']")).toBeVisible();

    const modeDot = page.locator("#mode-dot");
    await expect(modeDot).toBeVisible();
    await expect(modeDot).toHaveClass(/\bstatus-unknown\b/);
    await expect(modeDot).toHaveAttribute("title", "USB status unknown");
    await expect(modeDot).toHaveAttribute("aria-label", "USB status unknown");

    const expected503 = currentProbe.consoleErrors.filter(
      (e) => e.text.includes("503") && e.location.includes("/api/gadget/status"),
    );
    expect(expected503.length, "expected exactly one 503 resource-load log").toBe(1);
    const otherConsoleErrors = currentProbe.consoleErrors.filter(
      (e) => !(e.text.includes("503") && e.location.includes("/api/gadget/status")),
    );
    expect(
      otherConsoleErrors,
      `unexpected console error(s): ${JSON.stringify(otherConsoleErrors)}`,
    ).toEqual([]);
    expect(
      currentProbe.pageErrors,
      `pageerror(s): ${JSON.stringify(currentProbe.pageErrors)}`,
    ).toEqual([]);
    expect(
      currentProbe.consoleWarnings,
      `console warning(s): ${JSON.stringify(currentProbe.consoleWarnings)}`,
    ).toEqual([]);
    expect(
      currentProbe.failedRequests,
      `failed request(s): ${JSON.stringify(currentProbe.failedRequests)}`,
    ).toEqual([]);
    const bad = currentProbe.responses.filter((r) => {
      const u = new URL(r.url);
      return u.pathname === "/api/gadget/status" && r.status === 503;
    });
    expect(bad.length).toBeGreaterThanOrEqual(1);
    const otherBad = currentProbe.responses.filter((r) => {
      const u = new URL(r.url);
      return r.status >= 400 && u.pathname !== "/api/gadget/status";
    });
    expect(otherBad, `unexpected non-2xx: ${JSON.stringify(otherBad)}`).toEqual([]);
  });

  test("operation banner appears during handoff-active sync", async ({
    page,
    probe,
  }, testInfo) => {
    const currentProbe: Probe = probe;
    await page.unroute("**/api/gadget/status");
    await page.route("**/api/gadget/status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ...GADGET_STATUS_OK, handoff_active: true }),
      }),
    );

    await page.goto(PATH, { waitUntil: "load" });
    await expect(page.locator(".container[data-screen='cloud-archive']")).toBeVisible();

    const modeDot = page.locator("#mode-dot");
    await expect(modeDot).toBeVisible();
    await expect(modeDot).toHaveClass(/\bstatus-present\b/);
    await expect(modeDot).toHaveAttribute("title", "USB drive busy — syncing");
    await expect(modeDot).toHaveAttribute("aria-label", "USB drive busy — syncing");

    const banner = page.getByTestId("operation-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toHaveAttribute("role", "alert");
    await expect(banner).toContainText("File operation in progress...");
    await expect(page.getByTestId("operation-details")).toHaveText("Completing soon...");
    await expect(banner.locator("strong")).toHaveText("File operation in progress...");

    assertCleanConsole(currentProbe);
    assertCleanNetwork(currentProbe);
    await captureScreenshot(page, testInfo, `${SCREEN}-operation-active`);
  });

  test("operation banner stays hidden when handoff is inactive", async ({
    page,
    probe,
  }, testInfo) => {
    const currentProbe: Probe = probe;
    await page.unroute("**/api/gadget/status");
    await page.route("**/api/gadget/status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ...GADGET_STATUS_OK, handoff_active: false }),
      }),
    );

    await page.goto(PATH, { waitUntil: "load" });
    await expect(page.locator(".container[data-screen='cloud-archive']")).toBeVisible();

    const modeDot = page.locator("#mode-dot");
    await expect(modeDot).toBeVisible();
    await expect(modeDot).toHaveClass(/\bstatus-present\b/);
    await expect(modeDot).toHaveAttribute("title", "USB drive connected to vehicle");
    await expect(modeDot).toHaveAttribute("aria-label", "USB drive connected to vehicle");
    await expect(page.getByTestId("operation-banner")).toHaveCount(0);

    assertCleanConsole(currentProbe);
    assertCleanNetwork(currentProbe);
    await captureScreenshot(page, testInfo, `${SCREEN}-operation-inactive`);
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
