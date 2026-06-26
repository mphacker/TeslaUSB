import {
  test,
  expect,
  loadState,
  ARTIFACTS,
  GADGET_STATUS_OK,
  type Probe,
} from "./helpers";
import type { Page } from "@playwright/test";
import { writeFileSync, statSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { SHELL_POLL_ALLOWLIST } from "./screen-helpers";
// Each test drives the REAL bundle served by webd against a seeded read-only
// catalog with materialised archive MP4 fixtures (global-setup). Fresh context
// per test ⇒ cold cache, no bleed.
//
// PARITY: the event-player screen reproduces the legacy Flask event_player.html
// (fullscreen native <video> over webd's byte-range /stream, plus the Tesla
// telemetry HUD overlay). webd is read-only here EXCEPT the operator-gated clip
// delete (covered by clip-delete.spec.ts); the archive mutation stays DEFERRED
// to webd 5.1c and renders inert. These gates never issue the (destructive)
// DELETE — they only prove the read path + that opening/cancelling the confirm
// dialog fires no mutation.
//
// FALSE-GREEN GUARDS (the substantive part — "tests pass / 200 OK" is NOT
// enough). Hardened after an adversarial review of this very spec:
//  - STREAMING is proven against the <video> ELEMENT's OWN request (resourceType
//    'media'): it must come back 206 + Accept-Ranges: bytes + a valid
//    Content-Range — NOT merely the test's own probe fetch, and NOT a 200 full
//    download. A supplementary explicit Range: bytes=0-0 fetch corroborates it.
//  - DECODE is proven by playing the muted video and asserting readyState>=2
//    with a real duration>0 AND videoWidth>0 (the bytes actually decoded into a
//    frame), gated by a canPlayType preflight.
//  - HUD RENDER is proven across MULTIPLE telemetry buckets (so a controller
//    that hard-codes one state cannot pass): we seek to several times, recompute
//    the expected HUD from the ACTUAL currentTime (robust to seek drift), and
//    assert the LIVE controller handle (window.__TESLAUSB_HUD__.getState()) AND
//    the mutated HUD DOM match — and that the observed speeds actually VARY.
//    We also assert the HUD is GEOMETRICALLY visible over the video (computed
//    style + bounding-box intersection + elementFromPoint hit-test).
//  - WIRING is proven by window.__TESLAUSB_BUILD__ === the on-disk bundle id and
//    the served HTML referencing the hashed /assets/index-*.js.
//
// KNOWN COVERAGE GAP (flagged to the integrator, NOT silently shaved): the
// production SEI path (dashcam-mp4.ts parsing real Tesla SEI out of the streamed
// MP4) is NOT exercised here — the SMPTE fixtures carry no Tesla SEI and no real
// archive footage exists yet (the archiver is unbuilt). The HUD is driven from
// the seeded fixture (window.__TESLAUSB_HUD_FIXTURE__), the integrator-endorsed
// precedent. The SEI decoder is a faithful port of the legacy parser; a live SEI
// fixture is owed once real archive clips exist.

// Seeded telemetry track (the SMPTE fixtures carry no embedded Tesla SEI, so the
// UAT injects samples — integrator-endorsed precedent, NOT fabrication). Uses
// ONLY the real legacy SEI fields. Time-bucketed with DISTINCT, sentinel-unique
// states so a hard-coded HUD cannot accidentally match across buckets:
//   [0,0.5)   → 0mph,  gear P, brake on,            no AP
//   [0.5,1.5) → 30mph, gear D, LEFT blinker,        Self-Driving
//   [1.5,2.5) → 50mph, gear D, RIGHT blinker,       Self-Driving
//   [2.5,3]   → 20mph, gear D, brake on,            no AP
const HUD_FIXTURE = [
  { time: 0, speedMps: 0, gear: 0, steeringAngle: 0, blinkerLeft: false, blinkerRight: false, brakeApplied: true, acceleratorPedalPosition: 0, autopilotState: 0 },
  { time: 0.5, speedMps: 13.4, gear: 1, steeringAngle: -30, blinkerLeft: true, blinkerRight: false, brakeApplied: false, acceleratorPedalPosition: 0.35, autopilotState: 1 },
  { time: 1.5, speedMps: 22.4, gear: 1, steeringAngle: 45, blinkerLeft: false, blinkerRight: true, brakeApplied: false, acceleratorPedalPosition: 0.6, autopilotState: 1 },
  { time: 2.5, speedMps: 8.9, gear: 1, steeringAngle: 10, blinkerLeft: false, blinkerRight: false, brakeApplied: true, acceleratorPedalPosition: 0, autopilotState: 0 },
];

const KNOWN_CAMERAS = new Set(["front", "back", "left_repeater", "right_repeater"]);
const MPS_TO_MPH = 2.23694;
const CLIP_MP4 = readFileSync(resolve(process.cwd(), "test", "fixtures", "clip.mp4"));

interface ExpectedHud {
  speed: number;
  gear: string;
  blinkerLeft: boolean;
  blinkerRight: boolean;
  autopilotActive: boolean;
}

/** Independent oracle: the HUD state expected at playback time `t`, derived
 *  directly from HUD_FIXTURE (last sample whose time <= t). A reimplementation
 *  of sampleAt+sampleToHud on purpose — it must agree with the controller. */
function expectedHud(t: number): ExpectedHud {
  let s: (typeof HUD_FIXTURE)[number] | null = null;
  for (const x of HUD_FIXTURE) {
    if (x.time <= t) s = x;
    else break;
  }
  if (!s) return { speed: 0, gear: "P", blinkerLeft: false, blinkerRight: false, autopilotActive: false };
  return {
    speed: Math.round(Math.abs(s.speedMps) * MPS_TO_MPH),
    gear: ["P", "D", "R", "N"][s.gear] ?? "P",
    blinkerLeft: s.blinkerLeft === true,
    blinkerRight: s.blinkerRight === true,
    autopilotActive: s.autopilotState !== 0,
  };
}

// Read APIs the event-player is permitted to call, with the allowed query keys
// per path. webd is read-only; anything outside this set (or any non-GET/HEAD,
// any unexpected query key/value) is a hard failure. A 206 to /stream is a
// SUCCESS (range request), not an error.
const ALLOWED_API: { re: RegExp; params: (sp: URLSearchParams) => boolean }[] = [
  { re: /^\/api\/events$/, params: (sp) => [...sp.keys()].every((k) => ["after", "limit", "trip"].includes(k)) },
  { re: /^\/api\/events\/\d+$/, params: (sp) => [...sp.keys()].length === 0 },
  { re: /^\/api\/clips$/, params: (sp) => [...sp.keys()].every((k) => ["after", "limit", "folder_class"].includes(k)) },
  { re: /^\/api\/clips\/\d+$/, params: (sp) => [...sp.keys()].length === 0 },
  {
    re: /^\/api\/clips\/\d+\/stream$/,
    params: (sp) =>
      [...sp.keys()].every((k) => k === "camera") &&
      (!sp.has("camera") || KNOWN_CAMERAS.has(sp.get("camera")!)),
  },
];

function assertCleanConsole(probe: Probe) {
  const consoleErrors = probe.consoleErrors.filter((entry) => {
    if (!entry.text.includes("Failed to load resource: the server responded with a status of 404")) {
      return true;
    }
    return !/\/api\/events\/\d+(?::\d+)?$/.test(entry.location);
  });
  expect(probe.pageErrors, `pageerror(s): ${JSON.stringify(probe.pageErrors)}`).toEqual([]);
  expect(consoleErrors, `console error(s): ${JSON.stringify(consoleErrors)}`).toEqual([]);
  expect(probe.consoleWarnings, `console warning(s): ${JSON.stringify(probe.consoleWarnings)}`).toEqual([]);
}

interface StreamObservation {
  url: string;
  status: number;
  resourceType: string;
  acceptRanges: string | null;
  contentRange: string | null;
  camera: string;
}

/** Attach listeners (BEFORE navigation) that capture the byte-range /stream
 *  traffic the <video> element itself issues — separately from any probe fetch,
 *  so the streaming proof binds to the real media element, not the test's own
 *  corroborating fetch. */
function trackStreams(page: Page): { responses: StreamObservation[] } {
  const responses: StreamObservation[] = [];
  page.on("response", (r) => {
    try {
      const u = new URL(r.url());
      if (!/^\/api\/clips\/\d+\/stream$/.test(u.pathname)) return;
      responses.push({
        url: r.url(),
        status: r.status(),
        resourceType: r.request().resourceType(),
        acceptRanges: r.headers()["accept-ranges"] ?? null,
        contentRange: r.headers()["content-range"] ?? null,
        camera: u.searchParams.get("camera") ?? "front",
      });
    } catch {
      /* ignore non-URL */
    }
  });
  return { responses };
}

/** Navigate to /events with the HUD overlay ENABLED and seeded telemetry
 *  injected (must run before any page script ⇒ addInitScript before goto). */
async function gotoPlayerHudOn(page: Page) {
  await page.addInitScript((fixture) => {
    try {
      localStorage.setItem("seiOverlayEnabled", "true");
    } catch {
      /* ignore */
    }
    (window as unknown as { __TESLAUSB_HUD_FIXTURE__?: unknown }).__TESLAUSB_HUD_FIXTURE__ = fixture;
  }, HUD_FIXTURE);
  await page.goto("/events", { waitUntil: "load" });
  await expect(page.locator("[data-screen=event-player]")).toBeVisible();
  await expect(page.locator("#mainVideo")).toHaveAttribute("src", /\/api\/clips\/\d+\/stream/);
}

/** Navigate to /events with the HUD overlay in its DEFAULT (hidden) state —
 *  the event-player parity baseline (no telemetry, no localStorage). */
async function gotoPlayerHudOff(page: Page) {
  await page.goto("/events", { waitUntil: "load" });
  await expect(page.locator("[data-screen=event-player]")).toBeVisible();
  await expect(page.locator("#mainVideo")).toHaveAttribute("src", /\/api\/clips\/\d+\/stream/);
}

async function routeTrimEventListDropId(page: Page, dropId: number) {
  await page.route("**/api/events*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (/^\/api\/events$/.test(path)) {
      const resp = await route.fetch();
      const body = await resp.json() as {
        items?: Array<{ id?: number; [k: string]: unknown }>;
        [k: string]: unknown;
      };
      body.items = (body.items ?? []).filter((e) => e.id !== dropId);
      await route.fulfill({ response: resp, json: body });
      return;
    }

    if (/^\/api\/events\/\d+$/.test(path)) {
      await route.fallback();
      return;
    }
    await route.fallback();
  });
}

async function routeClipAnglesAsRoUsb(page: Page) {
  await page.route("**/api/clips/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!/^\/api\/clips\/\d+$/.test(path)) {
      await route.fallback();
      return;
    }

    const resp = await route.fetch();
    const body = await resp.json() as {
      angles?: Array<{ camera?: string; view_kind?: string; [key: string]: unknown }>;
      [key: string]: unknown;
    };
    const angles = Array.isArray(body.angles) ? body.angles : [];
    expect(angles.length, `${path} should include seeded angles`).toBeGreaterThan(0);
    for (const angle of angles) {
      expect(typeof angle.camera, `${path} angle camera should be a string`).toBe("string");
      expect(angle, `${path} angle should include view_kind`).toHaveProperty("view_kind");
    }
    body.angles = angles.map((angle) => ({ ...angle, view_kind: "ro_usb" }));
    await route.fulfill({ response: resp, json: body });
  });
}

interface DecodeEvidence {
  readyState: number;
  duration: number;
  videoWidth: number;
  videoHeight: number;
  currentTime: number;
}

/** Play the muted video until it has genuinely DECODED a frame (readyState>=2,
 *  duration>0, videoWidth>0 ⇒ bytes decoded, not merely downloaded). Returns
 *  the decode evidence. Pauses afterwards so callers can seek deterministically. */
async function decodeVideo(page: Page): Promise<DecodeEvidence> {
  const video = page.locator("#mainVideo");
  const canPlay = await video.evaluate((el: HTMLVideoElement) =>
    el.canPlayType('video/mp4; codecs="avc1.42E01E"'),
  );
  expect(canPlay, "H.264 baseline must be playable in this browser").not.toBe("");

  await video.evaluate(async (el: HTMLVideoElement) => {
    el.muted = true;
    try {
      await el.play();
    } catch {
      /* muted autoplay should be allowed; ignore */
    }
  });
  await page.waitForFunction(
    () => {
      const el = document.getElementById("mainVideo") as HTMLVideoElement | null;
      return !!el && el.readyState >= 2 && el.duration > 0 && el.videoWidth > 0;
    },
    undefined,
    { timeout: 15_000 },
  );
  const decode = await video.evaluate((el: HTMLVideoElement) => ({
    readyState: el.readyState,
    duration: el.duration,
    videoWidth: el.videoWidth,
    videoHeight: el.videoHeight,
    currentTime: el.currentTime,
  }));
  await video.evaluate((el: HTMLVideoElement) => el.pause());
  return decode;
}

/** Seek the (paused) video to ~t, then prove the HUD is telemetry-driven at the
 *  ACTUAL resting time: recompute the expected state from the real currentTime
 *  (robust to any seek drift), wait for the live controller to converge, and
 *  assert both the controller handle and the mutated DOM. Returns the observed
 *  speed so the caller can prove the HUD actually VARIED across buckets. */
async function seekAndAssertHud(page: Page, t: number): Promise<number> {
  const video = page.locator("#mainVideo");
  await video.evaluate((el: HTMLVideoElement, target: number) => {
    el.pause();
    el.currentTime = target;
  }, t);
  // Wait for the seek to actually land (the 'seeked' event has fired and the
  // clock is at/near the target) before reading telemetry-derived state.
  await page.waitForFunction(
    (target) => {
      const el = document.getElementById("mainVideo") as HTMLVideoElement | null;
      return !!el && el.paused && !el.seeking && Math.abs(el.currentTime - (target as number)) < 0.4;
    },
    t,
    { timeout: 10_000 },
  );
  const ct = await video.evaluate((el: HTMLVideoElement) => el.currentTime);
  const exp = expectedHud(ct);

  // The live controller must converge on the expected state for THIS time.
  await expect
    .poll(
      () =>
        page.evaluate(() => {
          const h = (window as unknown as { __TESLAUSB_HUD__?: { getState(): { speed: number } } })
            .__TESLAUSB_HUD__;
          return h ? h.getState().speed : -1;
        }),
      { timeout: 5000, message: `HUD speed should converge to ${exp.speed} at t=${ct}` },
    )
    .toBe(exp.speed);

  const state = await page.evaluate(() => {
    const h = (
      window as unknown as {
        __TESLAUSB_HUD__?: { source: string; frames: number; getState(): Record<string, unknown> };
      }
    ).__TESLAUSB_HUD__;
    return h ? { source: h.source, frames: h.frames, state: h.getState() } : null;
  });
  expect(state, "controller handle must exist").not.toBeNull();
  expect(state!.frames, "rAF HUD loop must have applied frames").toBeGreaterThan(0);
  expect(state!.state.gear, `t=${ct} gear`).toBe(exp.gear);
  expect(state!.state.blinkerLeft, `t=${ct} blinkerLeft`).toBe(exp.blinkerLeft);
  expect(state!.state.blinkerRight, `t=${ct} blinkerRight`).toBe(exp.blinkerRight);
  expect(state!.state.autopilotActive, `t=${ct} autopilotActive`).toBe(exp.autopilotActive);

  // The mutated HUD DOM reflects the same state (the overlay the user sees).
  await expect(page.locator("#hudSpeed")).toHaveText(String(exp.speed));
  await expect(page.locator("#hudGear")).toHaveText(exp.gear);
  await expect(page.locator("#blinkerLeft")).toHaveClass(exp.blinkerLeft ? /active/ : /^((?!active).)*$/);
  await expect(page.locator("#blinkerRight")).toHaveClass(exp.blinkerRight ? /active/ : /^((?!active).)*$/);
  if (exp.autopilotActive) {
    await expect(page.locator("#autopilotIndicator")).toHaveClass(/active/);
  } else {
    await expect(page.locator("#autopilotIndicator")).not.toHaveClass(/active/);
  }
  return exp.speed;
}

test.describe("event-player UAT", () => {
  // ── Blanket read-only + network-status invariant across EVERY exercised
  //    state (camera switches, screenshots, perf playback). Cheap checks that
  //    cannot legitimately fail in a read-only screen; console/failed-request
  //    gates live in their dedicated tests (media aborts on deliberate src
  //    changes are legitimate and handled there). ──
  test.afterEach(async ({ probe }) => {
    const origin = new URL(loadState().baseURL).origin;
    for (const req of probe.requests) {
      expect(new URL(req.url).origin, `off-origin request to ${req.url}`).toBe(origin);
    }
    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s): ${JSON.stringify(mutating)}`).toEqual([]);
    const bad = probe.responses.filter(
      (r) =>
        new URL(r.url).origin === origin &&
        r.status >= 400 &&
        !(r.status === 404 && /^\/api\/events\/\d+$/.test(new URL(r.url).pathname)),
    );
    expect(bad, `non-2xx same-origin response(s): ${JSON.stringify(bad)}`).toEqual([]);
  });

  // ── Gate 1: streaming + decode + HUD render (the substantive false-green guard) ─
  test("streaming + decode + telemetry-driven HUD over the live video", async ({
    page,
    probe,
  }) => {
    const streams = trackStreams(page);
    await gotoPlayerHudOn(page);

    // (a) DECODE — play the muted video; it must reach readyState>=2 with a real
    //     duration AND a decoded frame (videoWidth>0): the bytes truly decoded.
    const decode = await decodeVideo(page);
    expect(decode.readyState, "video must have decoded current data").toBeGreaterThanOrEqual(2);
    expect(decode.duration, "video must have a real duration").toBeGreaterThan(0);
    expect(decode.videoWidth, "decoded frame must have pixel dimensions").toBeGreaterThan(0);

    // (b) STREAMING — the <video> ELEMENT's OWN request (resourceType 'media')
    //     must be a 206 range response with Accept-Ranges + a valid Content-Range.
    //     This binds to the real media element, NOT the corroborating fetch below.
    const mediaResp = streams.responses.filter((r) => r.resourceType === "media");
    expect(mediaResp.length, "the <video> element must have issued a media request").toBeGreaterThan(0);
    const partial = mediaResp.filter((r) => r.status === 206);
    expect(
      partial.length,
      `the <video> media request must be served 206 (got ${JSON.stringify(mediaResp)})`,
    ).toBeGreaterThan(0);
    expect(partial[0].acceptRanges).toBe("bytes");
    expect(partial[0].contentRange, `content-range=${partial[0].contentRange}`).toMatch(
      /^bytes \d+-\d+\/\d+$/,
    );

    // (c) Corroborate deterministically with an explicit Range: bytes=0-0 fetch.
    const src = await page.locator("#mainVideo").getAttribute("src");
    const range = await page.evaluate(async (url: string) => {
      const r = await fetch(url, { headers: { Range: "bytes=0-0" } });
      return {
        status: r.status,
        acceptRanges: r.headers.get("accept-ranges"),
        contentRange: r.headers.get("content-range"),
        contentType: r.headers.get("content-type"),
      };
    }, src!);
    expect(range.status, "explicit Range request must be 206").toBe(206);
    expect(range.acceptRanges).toBe("bytes");
    expect(range.contentRange, `content-range=${range.contentRange}`).toMatch(/^bytes 0-0\/\d+$/);
    expect(range.contentType ?? "").toMatch(/video\/mp4/);

    // (d) HUD RENDER — must be GEOMETRICALLY visible over the video (not merely
    //     present in the DOM): on-screen, opaque, intersecting the video, and the
    //     top element at the HUD centre is the HUD (drawn ABOVE the video).
    await seekAndAssertHud(page, 2.0); // settle into a non-default bucket first
    await expect(page.locator("#teslaHud")).not.toHaveClass(/hidden/);
    await expect(page.locator("#seiToggle")).toBeChecked();
    const geo = await page.evaluate(() => {
      const hud = document.getElementById("teslaHud")!;
      const vid = document.getElementById("mainVideo")!;
      const hb = hud.getBoundingClientRect();
      const vb = vid.getBoundingClientRect();
      const cs = getComputedStyle(hud);
      // The overlay is intentionally pointer-events:none (click-through to the
      // video controls — matching the legacy player), so elementFromPoint would
      // normally pass straight through it. Temporarily enable hit-testing on the
      // HUD subtree to probe PAINT ORDER (is it drawn above the video at its
      // centre?), then restore so we never leave the overlay interactive.
      const prevPE = hud.style.pointerEvents;
      hud.style.pointerEvents = "auto";
      const cx = hb.left + hb.width / 2;
      const cy = hb.top + hb.height / 2;
      const hit = document.elementFromPoint(cx, cy);
      const hitIsHud = !!hit && (hit === hud || hud.contains(hit));
      hud.style.pointerEvents = prevPE;
      return {
        display: cs.display,
        visibility: cs.visibility,
        opacity: Number(cs.opacity),
        pointerEvents: cs.pointerEvents,
        area: hb.width * hb.height,
        intersects: hb.left < vb.right && hb.right > vb.left && hb.top < vb.bottom && hb.bottom > vb.top,
        hitIsHud,
      };
    });
    expect(geo.display, "HUD must not be display:none").not.toBe("none");
    expect(geo.visibility, "HUD must not be visibility:hidden").not.toBe("hidden");
    expect(geo.opacity, "HUD must be opaque").toBeGreaterThan(0);
    expect(geo.area, "HUD must have a real box").toBeGreaterThan(0);
    expect(geo.intersects, "HUD must overlap the video").toBe(true);
    // Intentional click-through overlay (parity with legacy): non-interactive…
    expect(geo.pointerEvents, "HUD overlay must be click-through").toBe("none");
    // …yet painted ABOVE the video at its centre (raised stacking layer).
    expect(geo.hitIsHud, "HUD must paint above the video at its centre").toBe(true);

    // (e) The HUD is genuinely TELEMETRY-driven: across multiple buckets it must
    //     show DISTINCT states (a hard-coded HUD cannot match all of them). We
    //     recompute the expected state from the real resting time inside
    //     seekAndAssertHud, so this is robust to seek drift.
    const speeds = new Set<number>();
    speeds.add(await seekAndAssertHud(page, 1.0)); // → 30 mph bucket
    speeds.add(await seekAndAssertHud(page, 2.0)); // → 50 mph bucket
    speeds.add(await seekAndAssertHud(page, 2.8)); // → 20 mph bucket
    expect(
      [...speeds].sort((a, b) => a - b),
      `HUD must vary with telemetry across buckets (saw ${[...speeds]})`,
    ).toEqual([20, 30, 50]);

    const handle = await page.evaluate(() => {
      const h = (window as unknown as { __TESLAUSB_HUD__?: { source: string; sampleCount: number } })
        .__TESLAUSB_HUD__;
      return h ? { source: h.source, sampleCount: h.sampleCount } : null;
    });
    expect(handle!.source, "telemetry source is the seeded fixture").toBe("fixture");
    expect(handle!.sampleCount).toBe(HUD_FIXTURE.length);

    assertCleanConsole(probe);
  });

  // ── Gate 2: wiring proof (the freshly-built bundle is what executed) ─────
  test("wiring proof — served HTML loads the hashed bundle that actually ran", async ({
    page,
  }) => {
    const state = loadState();
    await gotoPlayerHudOff(page);

    const winBuild = await page.evaluate(
      () => (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__,
    );
    expect(winBuild, "window.__TESLAUSB_BUILD__ must be defined").toBeTruthy();
    expect(winBuild).not.toBe("dev");
    expect(winBuild).toBe(state.buildId);

    const html = await (await page.request.get("/events")).text();
    expect(html).toContain(state.jsAsset);
    expect(html).not.toContain("/src/main.tsx");
    expect(html).toMatch(/\/assets\/index-[\w-]+\.js/);
    if (state.cssAsset) expect(html).toContain(state.cssAsset);

    const jsResp = await page.request.get(state.jsAsset);
    expect(jsResp.status()).toBe(200);
    expect(jsResp.headers()["content-type"] ?? "").toMatch(/javascript/);

    if (state.cssAsset) {
      const bundleCss = await page.request.get(state.cssAsset);
      expect(bundleCss.status()).toBe(200);
      expect(bundleCss.headers()["content-type"] ?? "").toMatch(/css/);
    }
  });

  // ── Gate 2b: the player honors the event's front_frame_offset_ms — it seeks
  //    to the event moment on load instead of always starting at 0. The first
  //    playable seeded event (harsh_braking, clip 2) has front_frame_offset=1500
  //    and all angles start at offset_ms=0, so the front cam must land at ~1.5s.
  test("event-offset seek — loads at front_frame_offset_ms, not 0", async ({
    page,
  }) => {
    await gotoPlayerHudOff(page);
    // Wait for metadata + the one-shot seek to settle near the event moment.
    await page.waitForFunction(
      () => {
        const el = document.getElementById("mainVideo") as HTMLVideoElement | null;
        return !!el && el.readyState >= 1 && el.duration > 0 && el.currentTime > 1.0;
      },
      undefined,
      { timeout: 15_000 },
    );
    const ct = await page
      .locator("#mainVideo")
      .evaluate((el: HTMLVideoElement) => el.currentTime);
    // 1.5s ± tolerance for keyframe snapping; the key assertion is "not 0".
    expect(ct, `currentTime=${ct} should be near the 1.5s event offset`).toBeGreaterThan(1.0);
    expect(ct).toBeLessThan(2.4);
  });

  // ── Legacy v1 deep link: /videos/event/<clip> must resolve to the Event
  //    player via webd's SPA fallback + the client router prefix alias. ──
  test("legacy route — /videos/event/<clip> lands on the event player", async ({
    page,
  }) => {
    await page.goto("/videos/event/2024-06-01_07-18-00", { waitUntil: "load" });
    await expect(page.locator("[data-screen=event-player]")).toBeVisible();
    await expect(page.locator("#mainVideo")).toHaveAttribute(
      "src",
      /\/api\/clips\/\d+\/stream/,
    );
  });

  test("direct clip deep-link — /events?clip=1 resolves event-less archived clip 1", async ({
    page,
  }) => {
    const streams = trackStreams(page);
    await page.goto("/events?clip=1", { waitUntil: "load" });
    await expect(page.locator("[data-screen=event-player]")).toBeVisible();
    await expect(page.locator("#mainVideo")).toHaveAttribute("src", /\/api\/clips\/1\/stream/);

    const mediaResp = streams.responses.filter((r) =>
      r.resourceType === "media" && /\/api\/clips\/1\/stream/.test(r.url)
    );
    expect(mediaResp.length, "direct clip 1 must issue a media request").toBeGreaterThan(0);
    expect(mediaResp.some((r) => r.status === 206), "direct clip 1 media request must be 206").toBe(true);
    const partial = mediaResp.find((r) => r.status === 206)!;
    expect(partial.acceptRanges).toBe("bytes");
    expect(partial.contentRange, `content-range=${partial.contentRange}`).toMatch(
      /^bytes \d+-\d+\/\d+$/,
    );

    const decode = await decodeVideo(page);
    expect(decode.readyState).toBeGreaterThanOrEqual(2);
    expect(decode.duration).toBeGreaterThan(0);
    expect(decode.videoWidth).toBeGreaterThan(0);

    await expect(page.locator("[data-testid=video-unarchived]")).toHaveCount(0);
    await expect(page.locator("[data-testid=event-nav]")).toHaveCount(0);
    await expect(page.locator(".event-location")).toHaveText("RecentClips");
    await expect(page.locator(".event-datetime")).not.toHaveText("\u2014");
  });

  test("deep-link guard — same-path ?clip change re-resolves without reload", async ({
    page,
  }) => {
    await page.goto("/events?clip=1", { waitUntil: "load" });
    await expect(page.locator("#mainVideo")).toHaveAttribute("src", /\/api\/clips\/1\/stream/);

    await page.evaluate(() => window.history.pushState({}, "", "/events?clip=5"));
    await expect(page.locator("#mainVideo")).toHaveAttribute("src", /\/api\/clips\/5\/stream/);

    await page.goBack();
    await expect(page.locator("#mainVideo")).toHaveAttribute("src", /\/api\/clips\/1\/stream/);
  });

  // ── Gate 3 (read-only): only whitelisted GET; switched camera streams; the
  //    archive control stays inert; opening + cancelling the Delete confirm
  //    fires NO mutation ──────────────────────────────────────────────────
  test("read-only — only whitelisted GET, switched camera decodes, delete-confirm gated + inert archive", async ({
    page,
    probe,
  }) => {
    const streams = trackStreams(page);
    await gotoPlayerHudOff(page);
    await expect(page.locator(".camera-option[data-camera=front]")).toHaveClass(/active/);
    await decodeVideo(page); // front camera decodes

    function assertOnlyWhitelistedApi(): Set<string> {
      const seen = new Set<string>();
      for (const req of probe.requests) {
        const u = new URL(req.url);
        if (!u.pathname.startsWith("/api/")) continue;
        if (SHELL_POLL_ALLOWLIST.has(u.pathname)) continue;
        expect(["GET", "HEAD"].includes(req.method.toUpperCase()), `${req.method} ${u.pathname}`).toBe(true);
        const rule = ALLOWED_API.find((r) => r.re.test(u.pathname));
        expect(rule, `unexpected API path ${u.pathname}`).toBeTruthy();
        expect(rule!.params(u.searchParams), `bad query on ${u.pathname}${u.search}`).toBe(true);
        seen.add(u.pathname);
      }
      return seen;
    }
    const apiSeen = assertOnlyWhitelistedApi();
    expect([...apiSeen].some((p) => p === "/api/events"), "/api/events was never requested").toBe(true);
    expect(
      [...apiSeen].some((p) => /\/api\/clips\/\d+\/stream/.test(p)),
      "the byte-range /stream was never requested",
    ).toBe(true);

    // Switch camera (a READ): a new GET /stream?camera=back must be issued, come
    // back 206, AND decode (proving the switched stream actually works).
    await page.locator(".camera-option[data-camera=back]").click();
    await expect(page.locator(".camera-option[data-camera=back]")).toHaveClass(/active/);
    await expect(page.locator("#mainVideo")).toHaveAttribute("src", /camera=back/);
    await expect
      .poll(() => streams.responses.filter((r) => r.camera === "back" && r.status === 206).length, {
        timeout: 8000,
        message: "switched (back) camera must produce a 206 range response",
      })
      .toBeGreaterThan(0);
    const backDecode = await decodeVideo(page);
    expect(backDecode.readyState, "switched camera must decode").toBeGreaterThanOrEqual(2);
    expect(backDecode.videoWidth).toBeGreaterThan(0);

    // Archive stays deferred/inert (no navigation, no request).
    await expect(page.locator("#archiveButton")).toHaveClass(/disabled/);
    await expect(page.locator("#archiveButton")).toHaveAttribute("aria-disabled", "true");
    const urlBefore = page.url();
    await page.locator("#archiveButton").click();
    await page.waitForTimeout(150);
    expect(page.url(), "inert archive must not navigate").toBe(urlBefore);

    // The Delete control is now ACTIVE (operator-gated) — but opening AND
    // cancelling the confirm dialog must NOT issue any mutation (the DELETE only
    // fires on explicit confirm; covered destructively-mocked in clip-delete.spec).
    await expect(page.locator("#deleteButton")).not.toHaveClass(/disabled/);
    await expect(page.locator("#deleteButton")).toHaveAttribute("aria-disabled", "false");
    await page.locator("#deleteButton").click();
    await expect(page.locator("[data-testid=delete-dialog]")).toBeVisible();
    // Cancel closes it with no navigation and (asserted below) no request.
    await page.locator(".delete-modal-btn.cancel").click();
    await expect(page.locator("[data-testid=delete-dialog]")).toHaveCount(0);
    expect(page.url(), "cancelling delete must not navigate").toBe(urlBefore);

    // Re-assert the FULL whitelist after the exercise + zero mutating methods.
    assertOnlyWhitelistedApi();
    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s): ${JSON.stringify(mutating)}`).toEqual([]);
  });

  test("downloads — single-angle + whole-clip ZIP endpoints and href wiring", async ({
    page,
    probe,
  }, testInfo) => {
    const asLower = (v: string | undefined) => (v ?? "").toLowerCase();
    // Parse the bare media type (drop any `; charset=…` parameter) so the check
    // is an EXACT match — `application/zip-bad` or `application/mp4` must NOT pass.
    const mediaType = (v: string | undefined) => asLower(v).split(";")[0].trim();
    const expectAttachmentHeaders = (
      headers: Record<string, string>,
      expectedContentType: string,
      expectedFilename: string,
      label: string,
    ) => {
      const disposition = asLower(headers["content-disposition"]);
      expect(disposition, `${label} content-disposition`).toContain("attachment");
      // Bind the response to the EXACT clip/camera — a wrong filename (e.g. a
      // different clip's zip) must fail rather than pass on a loose pattern.
      expect(disposition, `${label} content-disposition filename`).toContain(
        `filename="${expectedFilename.toLowerCase()}"`,
      );
      expect(mediaType(headers["content-type"]), `${label} content-type`).toBe(expectedContentType);
    };

    await page.setViewportSize({ width: 1280, height: 800 });
    await gotoPlayerHudOff(page);

    const src = await page.locator("#mainVideo").getAttribute("src");
    const clipMatch = src?.match(/\/api\/clips\/(\d+)\/stream/);
    expect(clipMatch, `mainVideo src did not include clip id: ${src}`).toBeTruthy();
    const clipId = clipMatch![1];

    const activeCamera = await page.locator(".camera-option.active[data-camera]").first().getAttribute("data-camera");
    expect(activeCamera, "active camera data-camera").toBeTruthy();
    expect(activeCamera, "seeded default active camera").toBe("front");

    await expect(page.locator("#downloadButton")).toHaveAttribute("href", `/api/clips/${clipId}/export`);
    await expect(page.locator("#downloadButton")).toHaveAttribute("download", /^(|true)$/);

    const angleHref = `/api/clips/${clipId}/angles/${encodeURIComponent(activeCamera!)}/download`;
    const angleButton = page.locator("#downloadAngleButton");
    await expect(angleButton).toHaveAttribute("href", angleHref);
    await expect(angleButton).toHaveAttribute("download", /^(|true)$/);
    await expect(angleButton).toHaveAttribute("aria-disabled", "false");
    await expect(angleButton).not.toHaveClass(/disabled/);

    const headExp = await page.request.head(`/api/clips/${clipId}/export`);
    expect(headExp.ok(), "whole-clip export HEAD endpoint").toBe(true);
    expectAttachmentHeaders(
      headExp.headers(),
      "application/zip",
      `clip-${clipId}.zip`,
      "whole-clip export HEAD",
    );

    const getExp = await page.request.get(`/api/clips/${clipId}/export`);
    expect(getExp.ok(), "whole-clip export GET endpoint").toBe(true);
    expectAttachmentHeaders(
      getExp.headers(),
      "application/zip",
      `clip-${clipId}.zip`,
      "whole-clip export GET",
    );
    expect((await getExp.body()).length, "whole-clip export GET body bytes").toBeGreaterThan(0);

    const headAng = await page.request.head(angleHref);
    expect(headAng.ok(), "single-angle download HEAD endpoint").toBe(true);
    expectAttachmentHeaders(
      headAng.headers(),
      "video/mp4",
      `clip-${clipId}-${activeCamera}.mp4`,
      "single-angle download HEAD",
    );

    const getAng = await page.request.get(angleHref);
    expect(getAng.ok(), "single-angle download GET endpoint").toBe(true);
    expectAttachmentHeaders(
      getAng.headers(),
      "video/mp4",
      `clip-${clipId}-${activeCamera}.mp4`,
      "single-angle download GET",
    );
    expect((await getAng.body()).length, "single-angle download GET body bytes").toBeGreaterThan(0);

    const [zipDl] = await Promise.all([
      page.waitForEvent("download"),
      page.locator("#downloadButton").click(),
    ]);
    expect(zipDl.suggestedFilename()).toBe(`clip-${clipId}.zip`);
    const zipDlPath = await zipDl.path();
    expect(zipDlPath).toBeTruthy();
    expect(statSync(zipDlPath!).size).toBeGreaterThan(0);

    const [angleDl] = await Promise.all([
      page.waitForEvent("download"),
      angleButton.click(),
    ]);
    expect(angleDl.suggestedFilename()).toMatch(new RegExp(`^clip-${clipId}-${activeCamera}\\.mp4$`));
    const angleDlPath = await angleDl.path();
    expect(angleDlPath).toBeTruthy();
    expect(statSync(angleDlPath!).size).toBeGreaterThan(0);

    const availableCameras = await page
      .locator(".camera-option:not(.unavailable):not(.download-option)[data-camera]")
      .evaluateAll((els) =>
        els
          .map((el) => (el as HTMLElement).dataset.camera ?? "")
          .filter((cam): cam is string => cam.length > 0),
      );
    expect(availableCameras.length, "seeded clip should expose >=2 archive cameras").toBeGreaterThanOrEqual(2);
    const differentCamera = availableCameras.find((cam) => cam !== activeCamera);
    expect(differentCamera, "must find a non-active available camera").toBeTruthy();
    const switchedCamera = differentCamera!;
    await page.locator(`.camera-option[data-camera="${switchedCamera}"]`).click();
    await expect(page.locator(`.camera-option[data-camera="${switchedCamera}"]`)).toHaveClass(/active/);
    await expect(angleButton).toHaveAttribute(
      "href",
      `/api/clips/${clipId}/angles/${encodeURIComponent(switchedCamera)}/download`,
    );

    const [switchedAngleDl] = await Promise.all([
      page.waitForEvent("download"),
      angleButton.click(),
    ]);
    expect(switchedAngleDl.suggestedFilename()).toBe(`clip-${clipId}-${switchedCamera}.mp4`);
    const switchedAngleDlPath = await switchedAngleDl.path();
    expect(switchedAngleDlPath).toBeTruthy();
    expect(statSync(switchedAngleDlPath!).size).toBeGreaterThan(0);

    const desktopShot = resolve(ARTIFACTS, "event-player-downloads-desktop.png");
    await page.screenshot({ path: desktopShot, fullPage: true });
    await testInfo.attach("event-player-downloads-desktop.png", {
      path: desktopShot,
      contentType: "image/png",
    });

    await page.setViewportSize({ width: 375, height: 812 });
    await expect(page.locator(".event-player-container")).toBeVisible();
    const mobileShot = resolve(ARTIFACTS, "event-player-downloads-mobile.png");
    await page.screenshot({ path: mobileShot, fullPage: true });
    await testInfo.attach("event-player-downloads-mobile.png", {
      path: mobileShot,
      contentType: "image/png",
    });

    assertCleanConsole(probe);
  });

  test("downloads — click feedback shows Preparing then Downloading without breaking native download", async ({
    page,
    probe,
  }) => {
    await page.unroute("**/api/gadget/status");
    await page.route("**/api/gadget/status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(GADGET_STATUS_OK),
      }),
    );

    await page.setViewportSize({ width: 1280, height: 800 });
    await gotoPlayerHudOff(page);

    const src = await page.locator("#mainVideo").getAttribute("src");
    const clipMatch = src?.match(/\/api\/clips\/(\d+)\/stream/);
    expect(clipMatch, `mainVideo src did not include clip id: ${src}`).toBeTruthy();
    const clipId = clipMatch![1];
    const activeCamera = await page
      .locator(".camera-option.active[data-camera]")
      .first()
      .getAttribute("data-camera");
    expect(activeCamera, "active camera data-camera").toBeTruthy();

    const exportButton = page.locator("#downloadButton");
    const exportLabel = exportButton.locator(".camera-label");
    const exportHref = `/api/clips/${clipId}/export`;
    await expect(exportButton).toHaveAttribute("href", exportHref);
    // Start listening BEFORE the click so we prove the native anchor download
    // still fires even though the click also kicks off the cosmetic feedback.
    const zipDlPromise = page.waitForEvent("download");
    await exportButton.click();
    // Cosmetic feedback engages immediately, and the href is NOT stripped while
    // busy (busy only dims/blocks re-clicks — the in-flight download is real).
    await expect(exportLabel).toHaveText("Preparing...");
    await expect(exportButton).toHaveClass(/busy/);
    await expect(exportButton).toHaveAttribute("href", exportHref);
    const zipDl = await zipDlPromise;
    expect(zipDl.suggestedFilename()).toBe(`clip-${clipId}.zip`);
    await expect(exportLabel).toHaveText("Downloading...", { timeout: 3000 });

    const angleButton = page.locator("#downloadAngleButton");
    const angleLabel = angleButton.locator(".camera-label");
    const angleHref = `/api/clips/${clipId}/angles/${activeCamera}/download`;
    await expect(angleButton).toHaveAttribute("href", angleHref);
    const angleDlPromise = page.waitForEvent("download");
    await angleButton.click();
    await expect(angleLabel).toHaveText("Preparing...");
    await expect(angleButton).toHaveClass(/busy/);
    await expect(angleButton).toHaveAttribute("href", angleHref);
    const angleDl = await angleDlPromise;
    expect(angleDl.suggestedFilename()).toBe(`clip-${clipId}-${activeCamera}.mp4`);
    await expect(angleLabel).toHaveText("Downloading...", { timeout: 3000 });

    assertCleanConsole(probe);
  });

  test("ro_usb clip playback parity — streams video without unarchived overlay", async ({
    page,
    probe,
  }) => {
    const streams = trackStreams(page);
    await routeClipAnglesAsRoUsb(page);
    await page.route("**/api/clips/*/stream*", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (!/^\/api\/clips\/\d+\/stream$/.test(path)) {
        await route.fallback();
        return;
      }
      const headers = {
        "content-type": "video/mp4",
        "accept-ranges": "bytes",
        "content-length": String(CLIP_MP4.byteLength),
        "content-range": `bytes 0-${Math.max(0, CLIP_MP4.byteLength - 1)}/${CLIP_MP4.byteLength}`,
      };
      if (route.request().method().toUpperCase() === "HEAD") {
        await route.fulfill({ status: 206, headers });
        return;
      }
      await route.fulfill({ status: 206, headers, body: CLIP_MP4 });
    });

    await page.goto("/events?clip=2", { waitUntil: "load" });
    await expect(page.locator("[data-screen=event-player]")).toBeVisible();
    await expect(page.locator("#mainVideo")).toHaveAttribute("src", /\/api\/clips\/2\/stream/);
    await expect(page.locator('[data-testid="video-unarchived"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="video-stream-unavailable"]')).toHaveCount(0);
    await expect
      .poll(
        () =>
          streams.responses.filter((r) => r.resourceType === "media" && r.status === 206).length,
        { timeout: 10_000 },
      )
      .toBeGreaterThan(0);

    const angleButton = page.locator("#downloadAngleButton");
    const zipButton = page.locator("#downloadButton");

    await expect(angleButton).toHaveClass(/disabled/);
    await expect(angleButton).toHaveAttribute("aria-disabled", "true");
    expect(await angleButton.getAttribute("href")).toBeNull();

    await expect(zipButton).toHaveClass(/disabled/);
    await expect(zipButton).toHaveAttribute("aria-disabled", "true");
    expect(await zipButton.getAttribute("href")).toBeNull();

    let downloaded = false;
    page.on("download", () => {
      downloaded = true;
    });
    const urlBefore = page.url();
    await angleButton.click({ force: true });
    await zipButton.click({ force: true });
    await page.waitForTimeout(300);
    expect(downloaded, "disabled download controls must be inert").toBe(false);
    expect(page.url(), "disabled controls must not navigate").toBe(urlBefore);
    await expect(page.locator("[data-screen=event-player]")).toBeVisible();

    assertCleanConsole(probe);
  });

  test("ro_usb stream 410 — shows graceful notice without console errors", async ({
    page,
    probe,
  }) => {
    await page.addInitScript(() => {
      const originalFetch = window.fetch.bind(window);
      window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
        const requestUrl =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        const requestMethod =
          init?.method ??
          (typeof input === "string" || input instanceof URL
            ? undefined
            : input.method);
        try {
          const url = new URL(requestUrl, window.location.origin);
          if (
            /^\/api\/clips\/\d+\/stream$/.test(url.pathname) &&
            (requestMethod ?? "GET").toUpperCase() === "HEAD"
          ) {
            return Promise.resolve(new Response(null, { status: 410, statusText: "Gone" }));
          }
        } catch {
          /* fall through to real fetch */
        }
        return originalFetch(input, init);
      };
    });
    await routeClipAnglesAsRoUsb(page);

    await page.goto("/events?clip=2", { waitUntil: "load" });
    await expect(page.locator("[data-screen=event-player]")).toBeVisible();
    await expect(page.locator("#mainVideo")).not.toHaveAttribute("src", /\/api\/clips\/2\/stream/);
    await expect(page.locator('[data-testid="video-unarchived"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="video-stream-unavailable"]')).toBeVisible();
    await expect(page.locator('[data-testid="video-stream-unavailable"]')).toContainText(
      "no longer available",
    );
    assertCleanConsole(probe);
  });

  test("ro_usb stream HEAD 500 — shows graceful notice and leaves video src empty", async ({
    page,
    probe,
  }) => {
    await page.addInitScript(() => {
      const originalFetch = window.fetch.bind(window);
      window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
        const requestUrl =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        const requestMethod =
          init?.method ??
          (typeof input === "string" || input instanceof URL
            ? undefined
            : input.method);
        try {
          const url = new URL(requestUrl, window.location.origin);
          if (
            /^\/api\/clips\/\d+\/stream$/.test(url.pathname) &&
            (requestMethod ?? "GET").toUpperCase() === "HEAD"
          ) {
            return Promise.resolve(
              new Response(null, { status: 500, statusText: "Internal Server Error" }),
            );
          }
        } catch {
          /* fall through to real fetch */
        }
        return originalFetch(input, init);
      };
    });
    await routeClipAnglesAsRoUsb(page);

    await page.goto("/events?clip=2", { waitUntil: "load" });
    await expect(page.locator("[data-screen=event-player]")).toBeVisible();
    await expect(page.locator('[data-testid="video-stream-unavailable"]')).toBeVisible();
    await expect(page.locator('[data-testid="video-stream-unavailable"]')).toContainText(
      "no longer available",
    );
    const src = await page.locator("#mainVideo").evaluate((el: HTMLVideoElement) => el.getAttribute("src") ?? "");
    expect(src).toBe("");
    assertCleanConsole(probe);
  });

  // ── Gate 4 (console + network): zero warnings/errors, no failed/non-2xx ──
  test("clean — zero console warnings/errors/pageerror and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    await gotoPlayerHudOn(page);
    // Real media path: decode + drive the HUD across buckets, so a media/decoder
    // error or a delayed failed request would surface here.
    await decodeVideo(page);
    await seekAndAssertHud(page, 2.0);
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(750);

    assertCleanConsole(probe);

    // This gate does NOT switch camera / re-navigate, so there is no legitimate
    // media abort — any failed request is a real fault.
    expect(
      probe.failedRequests,
      `failed request(s): ${JSON.stringify(probe.failedRequests)}`,
    ).toEqual([]);

    const bad = probe.responses.filter(
      (r) => new URL(r.url).origin === origin && r.status >= 400,
    );
    expect(bad, `non-2xx response(s): ${JSON.stringify(bad)}`).toEqual([]);
  });

  // ── Gate 5: performance — capture + report (incl. video first-frame) ────
  test("perf — capture TTFB/DCL/FCP + video first-frame + slowest requests", async ({
    page,
  }, testInfo) => {
    const navStart = Date.now();
    await page.goto("/events", { waitUntil: "load" });
    await expect(page.locator("[data-screen=event-player]")).toBeVisible();
    await expect(page.locator("#mainVideo")).toHaveAttribute("src", /\/api\/clips\/\d+\/stream/);
    const contentVisibleMs = await page.evaluate(() => performance.now());

    const firstFrameStart = Date.now();
    await page.locator("#mainVideo").evaluate(async (el: HTMLVideoElement) => {
      el.muted = true;
      try {
        await el.play();
      } catch {
        /* ignore */
      }
    });
    await page.waitForFunction(
      () => {
        const el = document.getElementById("mainVideo") as HTMLVideoElement | null;
        return !!el && el.readyState >= 2 && el.videoWidth > 0;
      },
      undefined,
      { timeout: 15_000 },
    );
    const videoFirstFrameMs = Date.now() - firstFrameStart;

    const timings = await page.evaluate(() => {
      const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming;
      const fcp = performance.getEntriesByType("paint").find((p) => p.name === "first-contentful-paint");
      const resources = (performance.getEntriesByType("resource") as PerformanceResourceTiming[])
        .map((r) => ({ url: r.name, ms: Math.round(r.duration * 10) / 10, type: r.initiatorType }))
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

    const report = {
      environment:
        "dev webd (cargo debug build) on Windows host; Chromium via Playwright; " +
        "fresh context per test (cold cache). NOTE: spa.md's <~2s 'interactive' " +
        "target is the ON-DEVICE (Raspberry Pi) profile — these are dev-box " +
        "numbers and are reported, not asserted, against that bar.",
      viewport: testInfo.project.name,
      ttfbMs: timings.ttfbMs,
      domContentLoadedMs: timings.domContentLoadedMs,
      domInteractiveMs: timings.domInteractiveMs,
      loadMs: timings.loadMs,
      fcpMs: timings.fcpMs,
      contentVisibleMs: Math.round(contentVisibleMs),
      videoFirstFrameMs,
      wallClockNavMs: Date.now() - navStart,
      slowestRequests: timings.slowestRequests,
    };

    const out = resolve(ARTIFACTS, `perf-event-player-${testInfo.project.name}.json`);
    writeFileSync(out, JSON.stringify(report, null, 2));
    await testInfo.attach(`perf-event-player-${testInfo.project.name}.json`, {
      body: JSON.stringify(report, null, 2),
      contentType: "application/json",
    });
    console.log(`[uat][perf:event-player:${testInfo.project.name}]`, JSON.stringify(report, null, 2));

    expect(report.fcpMs, "FCP should be present").not.toBeNull();
    expect(report.fcpMs!).toBeLessThan(5000);
    expect(report.contentVisibleMs).toBeLessThan(5000);
  });

  // ── Gate 6: responsive — render + screenshots at this viewport ──────────
  test("responsive — renders at viewport; parity + HUD screenshots captured", async ({
    page,
  }, testInfo) => {
    const viewport = testInfo.project.name;

    // (a) Parity baseline capture: default (HUD hidden) state.
    await gotoPlayerHudOff(page);
    await expect(page.locator(".event-player-container")).toBeVisible();
    await expect(page.locator(".camera-selector")).toBeVisible();
    await expect(page.locator(".main-video")).toBeVisible();
    await expect(page.locator("#teslaHud")).toHaveClass(/hidden/);
    await decodeVideo(page);
    await page.locator("#mainVideo").evaluate((el: HTMLVideoElement) => {
      el.pause();
      el.currentTime = 0.2;
    });
    const parityShot = resolve(ARTIFACTS, `event-player-${viewport}.png`);
    await page.screenshot({ path: parityShot, fullPage: true });
    await testInfo.attach(`event-player-${viewport}.png`, { path: parityShot, contentType: "image/png" });
    console.log(`[uat][screenshot:event-player:${viewport}] ${parityShot}`);

    // (b) HUD-overlay baseline capture: overlay enabled + seeded telemetry.
    await page.addInitScript((fixture) => {
      try {
        localStorage.setItem("seiOverlayEnabled", "true");
      } catch {
        /* ignore */
      }
      (window as unknown as { __TESLAUSB_HUD_FIXTURE__?: unknown }).__TESLAUSB_HUD_FIXTURE__ = fixture;
    }, HUD_FIXTURE);
    await page.goto("/events", { waitUntil: "load" });
    await expect(page.locator("[data-screen=event-player]")).toBeVisible();
    await expect(page.locator("#mainVideo")).toHaveAttribute("src", /\/api\/clips\/\d+\/stream/);
    await decodeVideo(page);
    await seekAndAssertHud(page, 2.0);
    await expect(page.locator("#teslaHud")).not.toHaveClass(/hidden/);
    const hudShot = resolve(ARTIFACTS, `event-player-hud-${viewport}.png`);
    await page.screenshot({ path: hudShot, fullPage: true });
    await testInfo.attach(`event-player-hud-${viewport}.png`, { path: hudShot, contentType: "image/png" });
    console.log(`[uat][screenshot:event-player-hud:${viewport}] ${hudShot}`);
  });

  test("deep-link — out-of-window ?event resolves via by-id lookup and plays", async ({
    page,
    probe,
  }) => {
    const streams = trackStreams(page);
    const requestedPaths: string[] = [];
    page.on("request", (r) => {
      try { requestedPaths.push(new URL(r.url()).pathname); } catch { /* ignore */ }
    });
    await routeTrimEventListDropId(page, 2);
    await page.goto("/events?event=2", { waitUntil: "load" });
    await expect(page.locator(".event-location")).toHaveText("Hard acceleration");
    await expect(page.locator("#mainVideo")).toHaveAttribute("src", /\/api\/clips\/3\/stream/);

    const mediaResp = streams.responses.filter(
      (r) => r.resourceType === "media" && /\/api\/clips\/3\/stream/.test(r.url),
    );
    expect(mediaResp.length, "by-id event target must issue a media request").toBeGreaterThan(0);
    expect(
      mediaResp.some((r) => r.status === 206),
      "by-id event target media request must be 206",
    ).toBe(true);
    const partial = mediaResp.find((r) => r.status === 206)!;
    expect(partial.acceptRanges).toBe("bytes");
    expect(partial.contentRange, `content-range=${partial.contentRange}`).toMatch(
      /^bytes \d+-\d+\/\d+$/,
    );

    await expect(page.locator("[data-testid=event-nav]")).toHaveCount(0);
    assertCleanConsole(probe);
    expect(
      requestedPaths.includes("/api/events/2"),
      "out-of-window deep-link must use the by-id lookup",
    ).toBe(true);
    expect(
      requestedPaths.some((p) => /^\/api\/clips\/2(\/stream)?$/.test(p)),
      "playlist-top clip 2 must never be fetched on the out-of-window path",
    ).toBe(false);
  });

  test("deep-link — missing ?event falls back to top with a notice, no console error", async ({
    page,
    probe,
  }) => {
    await routeTrimEventListDropId(page, 2);
    await page.goto("/events?event=999999", { waitUntil: "load" });
    await expect(page.locator(".event-location")).toHaveText("Harsh braking");
    await expect(page.locator("#mainVideo")).toHaveAttribute("src", /\/api\/clips\/2\/stream/);
    await expect(page.locator(".event-player-notice")).toContainText("no longer available");
    assertCleanConsole(probe);
  });

  test("deep-link — ?event=bad&clip=N preserves clip fallback", async ({
    page,
  }) => {
    await routeTrimEventListDropId(page, 2);
    await page.goto("/events?event=999999&clip=4", { waitUntil: "load" });
    await expect(page.locator("#mainVideo")).toHaveAttribute("src", /\/api\/clips\/4\/stream/);
  });

  // ── Gate 7: deep-link — `?event=` / `?clip=` start the playlist on a
  //    specific moment (the map→video hand-off target). Falls back to the
  //    top of the playlist when the param is absent or unmatched. ──
  test("deep-link — ?event= and ?clip= select the starting playlist entry", async ({
    page,
  }) => {
    const location = page.locator(".event-location");
    const video = page.locator("#mainVideo");

    // Default (no param) → top of the playlist: event 1 (clip 2).
    await page.goto("/events", { waitUntil: "load" });
    await expect(page.locator("[data-screen=event-player]")).toBeVisible();
    await expect(location).toHaveText("Harsh braking");
    await expect(video).toHaveAttribute("src", /\/api\/clips\/2\/stream/);

    // ?event=2 → the hard-acceleration event (clip 3), NOT index 0.
    await page.goto("/events?event=2", { waitUntil: "load" });
    await expect(location).toHaveText("Hard acceleration");
    await expect(video).toHaveAttribute("src", /\/api\/clips\/3\/stream/);

    // ?clip=4 → first event on that clip: the trip-less sentry event (clip 4).
    await page.goto("/events?clip=4", { waitUntil: "load" });
    await expect(location).toHaveText("Sentry event");
    await expect(video).toHaveAttribute("src", /\/api\/clips\/4\/stream/);

    // Unmatched id → graceful fallback to the top of the playlist.
    await page.goto("/events?event=99999", { waitUntil: "load" });
    await expect(location).toHaveText("Harsh braking");
    await expect(video).toHaveAttribute("src", /\/api\/clips\/2\/stream/);
  });

  // ── Gate 8: playlist nav — prev/next step through the loaded events,
  //    clamped at the ends, with a live position indicator. Each step
  //    re-resolves the clip and reloads the stream. ──
  test("playlist nav — prev/next walk the event playlist", async ({ page }) => {
    const location = page.locator(".event-location");
    const video = page.locator("#mainVideo");
    const pos = page.locator("[data-testid=event-nav-pos]");
    const prev = page.locator("[data-testid=event-nav-prev]");
    const next = page.locator("[data-testid=event-nav-next]");

    await page.goto("/events", { waitUntil: "load" });
    await expect(page.locator("[data-screen=event-player]")).toBeVisible();

    // Start: top of a 3-event playlist; prev disabled, next enabled.
    await expect(pos).toHaveText("1 / 3");
    await expect(location).toHaveText("Harsh braking");
    await expect(prev).toBeDisabled();
    await expect(next).toBeEnabled();

    // Next → event 2 (clip 3); the stream actually reloads.
    await next.click();
    await expect(pos).toHaveText("2 / 3");
    await expect(location).toHaveText("Hard acceleration");
    await expect(video).toHaveAttribute("src", /\/api\/clips\/3\/stream/);

    // Next → event 3 (clip 4); now at the end, next disabled.
    await next.click();
    await expect(pos).toHaveText("3 / 3");
    await expect(location).toHaveText("Sentry event");
    await expect(video).toHaveAttribute("src", /\/api\/clips\/4\/stream/);
    await expect(next).toBeDisabled();
    await expect(prev).toBeEnabled();

    // Prev → back to event 2.
    await prev.click();
    await expect(pos).toHaveText("2 / 3");
    await expect(location).toHaveText("Hard acceleration");
    await expect(video).toHaveAttribute("src", /\/api\/clips\/3\/stream/);
  });
});
