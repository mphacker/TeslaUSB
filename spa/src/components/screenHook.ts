import { useEffect } from "preact/hooks";

/** The current SPA build id (embedded at build time), or "dev". */
export function buildId(): string {
  return (
    (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__ ??
    "dev"
  );
}

/**
 * Publishes a wiring-proof hook (`window.__TESLAUSB_MEDIA_HOOKS__`) naming the
 * screen that actually produced the live DOM, plus the build id. The UAT reads
 * this to prove the right module is mounted (defends the documented "edited JS
 * the page never loaded" failure mode). Read-only; sets no app state.
 */
export function useScreenHook(screen: string): void {
  useEffect(() => {
    (
      window as unknown as {
        __TESLAUSB_MEDIA_HOOKS__?: { build: string; screen: string };
      }
    ).__TESLAUSB_MEDIA_HOOKS__ = { build: buildId(), screen };
  }, [screen]);
}
