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

// Captive Portal / Wi-Fi setup UAT — v1 parity, strictly read-only. Drives the
// REAL bundle webd serves at /captive-portal. Parity target: legacy
// captive_portal.html (status card + available/saved networks + manual form).
// B-1 has no wifid status endpoint, so the page reproduces the v1 look but makes
// zero API calls, renders all controls inert, and shows honest empty/pending
// states instead of fabricated networks.

const PATH = "/captive-portal";
const SCREEN = "captive-portal";

/** App-shell chrome: brand present + the SETTINGS nav entry active. */
async function assertChrome(page: Page, testInfo: TestInfo) {
  await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");
  const isMobile = testInfo.project.name.includes("375");
  const activeNav = page.locator(
    isMobile ? ".bottom-tabs .tab-item.active" : ".sidebar-rail .nav-item.active",
  );
  await expect(activeNav).toBeVisible();
  await expect(activeNav).toHaveAttribute("aria-current", "page");
  await expect(activeNav).toContainText("Settings");
}

test.describe("captive-portal UAT", () => {
  test("parity — settings nav active, hero/status/networks/manual", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertChrome(page, testInfo);

    await expect(page.locator(".captive-title")).toHaveText(
      "Connect TeslaUSB to Wi-Fi",
    );
    // Honest read-only banner (no live scan/connect).
    await expect(page.locator("[data-testid=captive-banner]")).toContainText(
      "operator-gated",
    );
    // Status degrades to an honest "not connected / offline" state.
    await expect(page.locator(".captive-status-chip.is-offline")).toContainText(
      "Not connected",
    );
    // Network lists render their v1 empty-states.
    await expect(
      page.locator("[data-testid=captive-networks-empty]"),
    ).toBeVisible();
    await expect(
      page.locator("[data-testid=captive-saved-empty]"),
    ).toBeVisible();
    // The manual form's inputs and buttons are inert.
    await expect(page.locator("#manual-ssid")).toBeDisabled();
    await expect(page.locator("#manual-passphrase")).toBeDisabled();
    const buttons = page.locator(".captive-page button");
    await expect(buttons.first()).toBeDisabled();
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
    await expect(page.locator(".captive-page")).toBeVisible();
    await captureScreenshot(page, testInfo, SCREEN);
  });
});
