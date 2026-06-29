import { chromium } from "playwright-core";

const BASE = process.env.LIVE_BASE || "http://10.0.0.224";
const out = "test/uat/artifacts";
const consoleErrors = [];
const badRequests = [];

const browser = await chromium.launch();
async function run(name, w, h) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h } });
  const page = await ctx.newPage();
  page.on("console", (m) => {
    if (m.type() === "error" || m.type() === "warning") consoleErrors.push(`[${name}] ${m.type()}: ${m.text()}`);
  });
  page.on("pageerror", (e) => consoleErrors.push(`[${name}] pageerror: ${e.message}`));
  page.on("response", (r) => { if (r.status() >= 400) badRequests.push(`[${name}] ${r.status()} ${r.url()}`); });
  await page.goto(BASE + "/", { waitUntil: "networkidle", timeout: 30000 });
  // Click a map event marker, then its popup "Watch video" link → overlay
  let result = "no-marker";
  try {
    const markers = page.locator(".leaflet-marker-icon");
    const n = await markers.count();
    for (let i = 0; i < n; i++) {
      await markers.nth(i).click({ timeout: 4000 }).catch(() => {});
      const link = page.locator("a.map-watch-link");
      if (await link.count()) {
        await link.first().click({ timeout: 4000 });
        await page.locator("[data-testid=video-overlay]").waitFor({ state: "visible", timeout: 6000 });
        result = `OVERLAY-FROM-MARKER(idx=${i})`;
        break;
      }
      // close popup before next marker
      await page.keyboard.press("Escape").catch(() => {});
    }
  } catch (e) { result = "fail/" + e.message.slice(0, 60); }
  await page.screenshot({ path: `${out}/live-marker-${name}.png` });
  console.log(`${name}: marker→overlay=${result}`);
  await ctx.close();
}
await run("desktop-1280", 1280, 900);
await run("mobile-375", 375, 800);
await browser.close();
console.log("consoleErrors:", consoleErrors.length); consoleErrors.forEach((e) => console.log("  " + e));
console.log("badRequests:", badRequests.length); badRequests.forEach((e) => console.log("  " + e));
process.exit(consoleErrors.length || badRequests.length ? 2 : 0);
