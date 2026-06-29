import { chromium } from "playwright-core";

const BASE = process.env.LIVE_BASE || "http://10.0.0.224";
const out = "test/uat/artifacts";
const consoleErrors = [];
const badRequests = [];

// Telemetry fixture (TelemetrySample[]). At t=1.0 -> driving state.
const FIXTURE = [
  { time: 0, speedMps: 0, gear: 0, steeringAngle: 0, blinkerLeft: false, blinkerRight: false, brakeApplied: true, acceleratorPedalPosition: 0, autopilotState: 0 },
  { time: 1, speedMps: 31.3, gear: 2, steeringAngle: -18, blinkerLeft: true, blinkerRight: false, brakeApplied: false, acceleratorPedalPosition: 0.42, autopilotState: 2 },
  { time: 3, speedMps: 8.9, gear: 1, steeringAngle: 10, blinkerLeft: false, blinkerRight: false, brakeApplied: true, acceleratorPedalPosition: 0, autopilotState: 0 },
];

const browser = await chromium.launch();
async function run(name, w, h) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h } });
  const page = await ctx.newPage();
  page.on("console", (m) => {
    if (m.type() === "error" || m.type() === "warning") consoleErrors.push(`[${name}] ${m.type()}: ${m.text()}`);
  });
  page.on("pageerror", (e) => consoleErrors.push(`[${name}] pageerror: ${e.message}`));
  page.on("response", (r) => { if (r.status() >= 400) badRequests.push(`[${name}] ${r.status()} ${r.url()}`); });
  await page.addInitScript((fx) => { window.__TESLAUSB_HUD_FIXTURE__ = fx; }, FIXTURE);

  await page.goto(BASE + "/", { waitUntil: "networkidle", timeout: 30000 });
  let result = { overlay: "n/a", hud: null };
  try {
    await page.locator("#btnVideos").click({ timeout: 8000 });
    await page.locator("#videoPanel.open").waitFor({ timeout: 8000 });
    await page.locator("#vpTabClips").click({ timeout: 8000 });
    await page.locator("[data-testid=vp-clips] [data-testid^=vp-clip-link-]").first().click({ timeout: 8000 });
    await page.locator("[data-testid=video-overlay]").waitFor({ state: "visible", timeout: 8000 });
    result.overlay = "OPENED";

    const video = page.locator("[data-testid=vp-overlay-video]");
    await video.waitFor({ state: "attached", timeout: 8000 });
    // wait until the overlay video can seek (has a streamable src + metadata)
    await page.waitForFunction(() => {
      const el = document.getElementById("overlayVideo");
      return !!el && el.readyState >= 1 && el.duration > 0;
    }, { timeout: 12000 }).catch(() => {});
    await video.evaluate((el) => { el.pause(); el.currentTime = 1.0; });
    await page.waitForFunction(() => {
      const el = document.getElementById("overlayVideo");
      return !!el && !el.seeking && Math.abs(el.currentTime - 1.0) < 0.4;
    }, { timeout: 8000 }).catch(() => {});
    // nudge a frame so HudController.apply runs at the new time
    await page.waitForTimeout(400);

    result.hud = await page.evaluate(() => {
      const t = (id) => document.getElementById(id);
      const cssVar = (id, v) => { const el = t(id); return el ? getComputedStyle(el).getPropertyValue(v).trim() || el.style.getPropertyValue(v).trim() : null; };
      const cls = (id) => { const el = t(id); return el ? el.className : null; };
      return {
        gear: t("olGear")?.textContent ?? null,
        speed: t("olSpeedVal")?.textContent ?? null,
        unit: t("olSpeedUnit")?.textContent ?? null,
        wheel: cssVar("olWheel", "--wheel-rotation"),
        brake: cssVar("olBrake", "--pedal-fill"),
        throttle: cssVar("olThrottle", "--pedal-fill"),
        blinkerL: cls("olBlinkerL"),
        blinkerR: cls("olBlinkerR"),
        ap: { text: t("olAP2")?.textContent ?? null, cls: cls("olAP2") },
        ct: document.getElementById("overlayVideo")?.currentTime,
      };
    });
  } catch (e) { result.overlay = "fail/" + e.message.slice(0, 80); }
  await page.screenshot({ path: `${out}/live-hud-${name}.png`, fullPage: false });
  console.log(`${name}: overlay=${result.overlay}`);
  console.log(`  hud=${JSON.stringify(result.hud)}`);
  await ctx.close();
}
await run("desktop-1280", 1280, 900);
await run("mobile-375", 375, 800);
await browser.close();
console.log("consoleErrors:", consoleErrors.length); consoleErrors.forEach((e) => console.log("  " + e));
console.log("badRequests:", badRequests.length); badRequests.forEach((e) => console.log("  " + e));
process.exit(consoleErrors.length || badRequests.length ? 2 : 0);
