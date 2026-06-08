import { readFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const STATE = resolve(HERE, "artifacts", "uat-state.json");

export default async function globalTeardown() {
  if (!existsSync(STATE)) return;
  let pid: number | undefined;
  let baseURL: string | undefined;
  try {
    const s = JSON.parse(readFileSync(STATE, "utf8"));
    pid = s.pid;
    baseURL = s.baseURL;
  } catch {
    return;
  }
  if (!pid) return;

  try {
    process.kill(pid); // single webd process — SIGTERM/TerminateProcess
  } catch (e) {
    console.log(`[uat] webd (pid ${pid}) not running: ${(e as Error).message}`);
    return;
  }

  // Wait until the process is actually gone (don't leave a half-dead server
  // holding the port for the next run). process.kill(pid, 0) throws once reaped.
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    try {
      process.kill(pid, 0);
    } catch {
      console.log(`[uat] stopped webd (pid ${pid})`);
      return;
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  // Last resort: confirm the port is no longer serving.
  try {
    if (baseURL) await fetch(`${baseURL}/api/days`);
    console.warn(`[uat] webd (pid ${pid}) may still be running on ${baseURL}`);
  } catch {
    /* port closed — good enough */
  }
}
