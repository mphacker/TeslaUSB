import { test as base, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const ARTIFACTS = resolve(HERE, "artifacts");

export interface UatState {
  baseURL: string;
  pid: number;
  seedDb: string;
  dist: string;
  buildId: string;
  jsAsset: string;
  cssAsset: string;
}

/** Per-run handshake written by global-setup (built bundle id, hashed assets). */
export function loadState(): UatState {
  return JSON.parse(readFileSync(resolve(ARTIFACTS, "uat-state.json"), "utf8"));
}

/** Captures everything the UAT gates assert against: console, page errors,
 *  and every network request/response the page made. Listeners attach before
 *  the test body navigates, so nothing is missed. */
export interface Probe {
  consoleErrors: { text: string; location: string }[];
  consoleWarnings: { text: string; location: string }[];
  pageErrors: string[];
  requests: { method: string; url: string }[];
  responses: { url: string; status: number; contentType: string }[];
  failedRequests: { url: string; failure: string }[];
}

export const GADGET_STATUS_OK = {
  present: true,
  bound: true,
  bound_udc: "fe980000.usb",
  udc_state: "configured",
  lun_file: "/data/teslausb/cam.img",
  media_lun_file: "/data/teslausb/media.img",
  handoff_active: false,
  pending_mutations: 0,
  applying_mutations: 0,
  media_ro_mounted: true,
  media_ro_path: "/run/teslausb/media-ro",
  media_ro_error: null,
  last_handoff_id: "h-42",
  last_result: "done",
};

export const test = base.extend<{ probe: Probe }>({
  probe: async ({ page }, use) => {
    const probe: Probe = {
      consoleErrors: [],
      consoleWarnings: [],
      pageErrors: [],
      requests: [],
      responses: [],
      failedRequests: [],
    };
    page.on("console", (m) => {
      const loc = m.location();
      const where = loc ? `${loc.url}:${loc.lineNumber}` : "";
      if (m.type() === "error") probe.consoleErrors.push({ text: m.text(), location: where });
      else if (m.type() === "warning")
        probe.consoleWarnings.push({ text: m.text(), location: where });
    });
    page.on("pageerror", (e) => probe.pageErrors.push(e.message ?? String(e)));
    page.on("request", (r) => probe.requests.push({ method: r.method(), url: r.url() }));
    page.on("response", (r) =>
      probe.responses.push({
        url: r.url(),
        status: r.status(),
        contentType: r.headers()["content-type"] ?? "",
      }),
    );
    page.on("requestfailed", (r) =>
      probe.failedRequests.push({
        url: r.url(),
        failure: r.failure()?.errorText ?? "unknown",
      }),
    );
    await page.route("**/api/gadget/status", (r) =>
      r.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(GADGET_STATUS_OK),
      }),
    );
    await use(probe);
  },
});

export { expect };

/** Read APIs the settings-dashboard screen is permitted to call. webd is
 *  read-only; anything outside this set (or any non-GET) is a hard failure.
 *  The dashboard reads /api/settings (config-form bindings), /api/clips (Video
 *  Indexer enrichment), and the three read-only device-status probes (5.1d).
 *  The full catalog client is exercised separately by api-client.spec.ts. */
export const ALLOWED_API = new Set([
  "/api/settings",
  "/api/clips",
  "/api/system/health",
  "/api/system/metrics",
  "/api/storage/health",
  "/api/gadget/status",
]);
