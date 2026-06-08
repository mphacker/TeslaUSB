import { defineConfig } from "@playwright/test";

// Durable UAT for the media hub (Task 5.2 §5/§6 gate).
//
// The suite drives the REAL served app: global-setup seeds an indexd catalog,
// builds the hashed SPA bundle, builds `webd`, then spawns `webd` serving the
// bundle + seeded DB. Tests run against that live server at a fixed loopback
// port. global-teardown stops the server. Nothing here touches the device.
//
// Port is fixed (deterministic) so `use.baseURL` can be set statically; the
// per-run handshake (built bundle id, hashed asset name) is passed to tests via
// artifacts/uat-state.json written by global-setup.
const PORT = Number(process.env.UAT_PORT ?? 8131);
const ARTIFACTS = "test/uat/artifacts";

export default defineConfig({
  testDir: "./test/uat",
  outputDir: `${ARTIFACTS}/test-results`,
  // One webd instance, shared read-only across projects — keep it serial.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  globalSetup: "./test/uat/global-setup.ts",
  globalTeardown: "./test/uat/global-teardown.ts",
  reporter: [
    ["list"],
    ["html", { outputFolder: `${ARTIFACTS}/report`, open: "never" }],
  ],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    browserName: "chromium",
    trace: "retain-on-failure",
    // Screenshots are captured explicitly by the responsive test into the
    // artifacts dir; the auto-screenshot would only fire on failure.
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [
    {
      name: "desktop-1280",
      use: { viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 },
    },
    {
      name: "mobile-375",
      use: { viewport: { width: 375, height: 812 }, deviceScaleFactor: 2 },
    },
  ],
});
