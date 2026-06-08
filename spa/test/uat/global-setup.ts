import { spawn, execFileSync } from "node:child_process";
import {
  mkdirSync,
  writeFileSync,
  readFileSync,
  readdirSync,
  existsSync,
  openSync,
} from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// ── Paths (all derived from this file's location — no cwd assumptions) ──
const HERE = dirname(fileURLToPath(import.meta.url));
const SPA = resolve(HERE, "..", ".."); // spa/
const REPO = resolve(SPA, ".."); // repo root
const ART = resolve(HERE, "artifacts");
const STATE = resolve(ART, "uat-state.json");
const SEED_DB = resolve(ART, "catalog.db");
const DIST = resolve(SPA, "dist");
const WEBD = resolve(
  REPO,
  "rust",
  "target",
  "debug",
  process.platform === "win32" ? "webd.exe" : "webd",
);
const NPM = process.platform === "win32" ? "npm.cmd" : "npm";

const HOST = "127.0.0.1";
const PORT = Number(process.env.UAT_PORT ?? 8131);
const BASE = `http://${HOST}:${PORT}`;
// UAT_FAST=1 reuses already-built artifacts (seed DB, dist bundle, webd binary)
// for fast local iteration. Default does the full, hermetic build.
const FAST = process.env.UAT_FAST === "1";

function run(cmd: string, args: string[], cwd: string) {
  console.log(`[uat] $ ${cmd} ${args.join(" ")}  (cwd=${cwd})`);
  // shell:true so Windows can launch `.cmd` shims (npm.cmd); our args are
  // simple, space-free tokens so no extra quoting is required.
  execFileSync(cmd, args, { cwd, stdio: "inherit", shell: true });
}

async function waitForReady(child: { exitCode: number | null }, timeoutMs: number) {
  const deadline = Date.now() + timeoutMs;
  let lastErr = "";
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(
        `webd exited early (code ${child.exitCode}). See ${resolve(ART, "webd.log")}`,
      );
    }
    try {
      const r = await fetch(`${BASE}/api/days`);
      if (r.ok) {
        const body = (await r.json()) as unknown[];
        if (Array.isArray(body) && body.length > 0) return; // seeded data live
        lastErr = "/api/days returned empty — seed not loaded";
      } else {
        lastErr = `/api/days → HTTP ${r.status}`;
      }
    } catch (e) {
      lastErr = (e as Error).message;
    }
    await new Promise((res) => setTimeout(res, 250));
  }
  throw new Error(`webd not ready within ${timeoutMs}ms (last: ${lastErr})`);
}

/** Pull the build id Vite baked into the emitted bundle (wiring-proof oracle). */
function readBundleIdentity(): { buildId: string; jsAsset: string; cssAsset: string } {
  const assetsDir = resolve(DIST, "assets");
  const files = readdirSync(assetsDir);
  const js = files.find((f) => /^index-.*\.js$/.test(f));
  const css = files.find((f) => /^index-.*\.css$/.test(f));
  if (!js) throw new Error(`no hashed index JS in ${assetsDir} — did the build run?`);
  const src = readFileSync(resolve(assetsDir, js), "utf8");
  const m = src.match(/__TESLAUSB_BUILD__\s*[:=]\s*"([^"]+)"/);
  if (!m) throw new Error("could not find __TESLAUSB_BUILD__ in built bundle");
  return { buildId: m[1], jsAsset: `/assets/${js}`, cssAsset: css ? `/assets/${css}` : "" };
}

export default async function globalSetup() {
  mkdirSync(ART, { recursive: true });

  if (!FAST) {
    // 1. Seed a fresh read-only catalog (3 trips / 6 clips / 3 events).
    run(process.execPath, [resolve(SPA, "test", "seed", "build-db.mjs"), SEED_DB], SPA);
    // 2. Build the hashed static bundle (tsc typecheck + vite build).
    run(NPM, ["run", "build"], SPA);
    // 3. Ensure the webd binary is current.
    run("cargo", ["build", "-p", "webd"], resolve(REPO, "rust"));
  }

  for (const [label, p] of [
    ["seed DB", SEED_DB],
    ["bundle", resolve(DIST, "index.html")],
    ["webd binary", WEBD],
  ] as const) {
    if (!existsSync(p)) {
      throw new Error(`${label} missing at ${p}${FAST ? " (UAT_FAST=1 — build first)" : ""}`);
    }
  }

  const identity = readBundleIdentity();

  // Refuse to run if something is already on the port — otherwise readiness
  // could pass against a STALE webd and the suite would test the wrong server.
  try {
    await fetch(`${BASE}/api/days`);
    throw new Error(
      `port ${PORT} already serving — a stale webd is bound to ${BASE}. ` +
        `Stop it (or set UAT_PORT) before running the UAT.`,
    );
  } catch (e) {
    const msg = (e as Error).message;
    if (msg.includes("already serving")) throw e; // our own guard above
    // ECONNREFUSED / fetch failed ⇒ port is free, which is what we want.
  }

  // 4. Spawn webd serving the built bundle + seeded DB, bound to loopback.
  const logFd = openSync(resolve(ART, "webd.log"), "w");
  const child = spawn(WEBD, [], {
    cwd: REPO,
    env: {
      ...process.env,
      WEBD_DB: SEED_DB,
      WEBD_STATIC: DIST,
      WEBD_BIND: `${HOST}:${PORT}`,
      RUST_LOG: process.env.RUST_LOG ?? "warn",
    },
    stdio: ["ignore", logFd, logFd],
  });
  child.on("error", (e) => {
    throw new Error(`failed to spawn webd: ${e.message}`);
  });

  // Persist the pid IMMEDIATELY so global-teardown can reap webd even if the
  // readiness check below throws (otherwise a failed startup leaks the process).
  writeFileSync(
    STATE,
    JSON.stringify(
      {
        baseURL: BASE,
        pid: child.pid,
        seedDb: SEED_DB,
        dist: DIST,
        ...identity,
        startedAt: new Date().toISOString(),
      },
      null,
      2,
    ),
  );

  // 5. Preflight: block until the live server serves seeded catalog data.
  await waitForReady(child, 30_000);

  console.log(`[uat] webd ready at ${BASE} (pid ${child.pid}, build ${identity.buildId})`);
}
