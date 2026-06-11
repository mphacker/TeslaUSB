import { render } from "preact";
import "leaflet/dist/leaflet.css";
import "leaflet.markercluster/dist/MarkerCluster.css";
import "leaflet.markercluster/dist/MarkerCluster.Default.css";
import "./styles/hub.css";
import "./styles/mapping.css";
import "./styles/bulk-delete.css";
import { Router } from "./router";

// Replaced at build time by Vite's `define` — a unique id per build used by the
// UAT to prove the freshly-built bundle is the code actually running.
declare const __TESLAUSB_BUILD__: string;

/**
 * SPA entry. Task 5.3 introduces the client-side {@link Router}; webd's
 * SPA-fallback serves this bundle for any non-API route, and the router then
 * renders the matching screen inside the shared Shell.
 */
(window as unknown as { __TESLAUSB_BUILD__: string }).__TESLAUSB_BUILD__ =
  typeof __TESLAUSB_BUILD__ === "string" ? __TESLAUSB_BUILD__ : "dev";

const root = document.getElementById("app");
if (root) {
  render(<Router />, root);
}
