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
  const t0 = Date.now();
  await page.goto(BASE + "/", { waitUntil: "networkidle", timeout: 30000 });
  const ttI = Date.now() - t0;
  let overlay = "n/a";
  try {
    await page.locator("#btnVideos").click({ timeout: 8000 });
    await page.locator("#videoPanel.open").waitFor({ timeout: 8000 });
    await page.locator("#vpTabClips").click({ timeout: 8000 });
    await page.locator("[data-testid=vp-clips] [data-testid^=vp-clip-link-]").first().click({ timeout: 8000 });
    await page.locator("[data-testid=video-overlay]").waitFor({ state: "visible", timeout: 8000 });
    overlay = "OPENED";
  } catch (e) { overlay = "fail/" + e.message.slice(0, 60); }
  await page.screenshot({ path: `${out}/live-overlay-${name}.png`, fullPage: false });
  console.log(`${name}: interactive=${ttI}ms overlay=${overlay}`);
  await ctx.close();
}
await run("desktop-1280", 1280, 900);
await run("mobile-375", 375, 800);
await browser.close();
console.log("consoleErrors:", consoleErrors.length); consoleErrors.forEach((e) => console.log("  " + e));
console.log("badRequests:", badRequests.length); badRequests.forEach((e) => console.log("  " + e));
process.exit(consoleErrors.length || badRequests.length ? 2 : 0);
