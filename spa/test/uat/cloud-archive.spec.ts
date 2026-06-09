import { test, expect } from "./helpers";
import type { Page, TestInfo } from "@playwright/test";
import {
  gotoScreen,
  assertCleanConsole,
  assertCleanNetwork,
  assertReadOnly,
  assertWiring,
  capturePerf,
  captureScreenshot,
} from "./screen-helpers";

// Cloud Archive UAT — v1 parity, strictly read-only. Drives the REAL bundle
// webd serves at /cloud. Parity target: legacy cloud_archive.html (sync status +
// stat cards + provider setup + sync settings + queue + history). B-1 has no
// cloud config endpoint, so the page reproduces the v1 look but makes zero API
// calls, renders every control inert, and shows honest "—"/empty pending states
// instead of fabricated counters and history.

const PATH = "/cloud";
const SCREEN = "cloud-archive";

/** App-shell chrome: brand present + the CLOUD nav entry active. */
async function assertChrome(page: Page, testInfo: TestInfo) {
  await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");
  const isMobile = testInfo.project.name.includes("375");
  const activeNav = page.locator(
    isMobile ? ".bottom-tabs .tab-item.active" : ".sidebar-rail .nav-item.active",
  );
  await expect(activeNav).toBeVisible();
  await expect(activeNav).toHaveAttribute("aria-current", "page");
  await expect(activeNav).toContainText("Cloud");
}

test.describe("cloud-archive UAT", () => {
  test("parity — cloud nav active, status/provider/settings/queue/history", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertChrome(page, testInfo);

    // Idle sync status banner.
    await expect(page.locator("#syncStatusCard")).toContainText("Cloud Sync");
    await expect(
      page.locator("[data-testid=cloud-sync-subtitle]"),
    ).toContainText("Configure a provider below");
    // The v1 section scaffolding is present.
    await expect(page.locator(".settings-section summary")).toHaveCount(4);
    await expect(
      page.locator(".settings-section summary").filter({ hasText: "Cloud Provider" }),
    ).toHaveCount(1);
    await expect(
      page.locator(".settings-section summary").filter({ hasText: "Sync Settings" }),
    ).toHaveCount(1);
    await expect(page.locator(".info-box")).toContainText("How sync works");
    // Provider + settings controls are inert.
    await expect(page.locator("#providerSelect")).toBeDisabled();
    // Queue + history render honest pending/empty states.
    await expect(page.locator("[data-testid=cloud-queue-empty]")).toBeAttached();
    await expect(
      page.locator("[data-testid=cloud-history-empty]"),
    ).toContainText("No sync sessions yet");
  });

  test("wiring — served HTML runs the built bundle and the module ran", async ({
    page,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertWiring(page, PATH, SCREEN);
  });

  test("read-only — no mutations, no API calls, no mutation surface", async ({
    page,
    probe,
  }) => {
    const sockets: string[] = [];
    page.on("websocket", (ws) => sockets.push(ws.url()));
    await gotoScreen(page, PATH, SCREEN);
    await page.waitForTimeout(200);
    await assertReadOnly(page, probe, sockets);
  });

  test("clean — zero console warnings/errors and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await page.waitForTimeout(200);
    assertCleanConsole(probe);
    assertCleanNetwork(probe);
  });

  test("perf — capture TTFB/FCP + slowest requests", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await capturePerf(page, testInfo, SCREEN);
  });

  test("responsive — renders at viewport and screenshot captured", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("#syncStatusCard")).toBeVisible();
    await captureScreenshot(page, testInfo, SCREEN);
  });
});
