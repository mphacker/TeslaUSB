import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

// The SPA is served by `webd` from a static dir (hashed bundle in `dist/`).
// In dev, `/api/*` is proxied to a running `webd` (set WEBD_DEV_TARGET to
// override; defaults to the conventional dev bind 127.0.0.1:8080).
const apiTarget = process.env.WEBD_DEV_TARGET ?? "http://127.0.0.1:8080";

// A unique build id baked into the bundle. The UAT reads it back at runtime
// (window.__TESLAUSB_BUILD__) to prove the freshly-built hashed bundle — not a
// stale asset or the dev entry — is what actually executed in the browser.
const buildId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

export default defineConfig({
  plugins: [preact()],
  define: {
    __TESLAUSB_BUILD__: JSON.stringify(buildId),
  },
  build: {
    target: "es2022",
    outDir: "dist",
    assetsDir: "assets",
    sourcemap: false,
  },
  server: {
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
    },
  },
});
