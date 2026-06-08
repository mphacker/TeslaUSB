import { render } from "preact";
import "./styles/hub.css";
import { MediaHub } from "./screens/MediaHub";

// Replaced at build time by Vite's `define` — a unique id per build used by the
// UAT to prove the freshly-built bundle is the code actually running.
declare const __TESLAUSB_BUILD__: string;

/**
 * SPA entry. Task 5.2 ships a single screen (the media hub); client-side
 * routing for the remaining parity screens arrives in Task 5.3. webd's
 * SPA-fallback serves this bundle for any non-API route.
 */
(window as unknown as { __TESLAUSB_BUILD__: string }).__TESLAUSB_BUILD__ =
  typeof __TESLAUSB_BUILD__ === "string" ? __TESLAUSB_BUILD__ : "dev";

const root = document.getElementById("app");
if (root) {
  render(<MediaHub />, root);
}
