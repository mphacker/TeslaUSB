import { useEffect, useState } from "preact/hooks";
import type { ComponentType } from "preact";
import { Shell, type NavKey } from "./components/Shell";
import { MediaHub } from "./screens/MediaHub";
import { Media } from "./screens/Media";
import { Boombox } from "./screens/Boombox";
import { Music } from "./screens/Music";
import { LightShows } from "./screens/LightShows";
import { Wraps } from "./screens/Wraps";
import { LicensePlates } from "./screens/LicensePlates";
import { TripMap } from "./screens/TripMap";
import { Analytics } from "./screens/Analytics";
import { EventPlayer } from "./screens/EventPlayer";
import { StorageHealth } from "./screens/StorageHealth";
import { FailedJobs } from "./screens/FailedJobs";
import { CloudArchive } from "./screens/CloudArchive";
import { CaptivePortal } from "./screens/CaptivePortal";
import { ComingSoon } from "./screens/ComingSoon";

/**
 * One screen in the SPA. The router is intentionally tiny and convention-driven:
 * a new parity screen joins the app by appending ONE {@link Route} row here and
 * adding its own `src/screens/<X>.tsx` file — no other shared file changes, so
 * parallel screen lanes never collide beyond this single registry.
 *
 *  - `path`   : the exact (slash-normalised) pathname that selects this screen.
 *  - `active` : which primary-nav entry the {@link Shell} highlights for it.
 *  - `screen` : the content component, rendered inside the shared {@link Shell}.
 *  - `title`  : a friendly label used by the {@link ComingSoon} fallback.
 */
export interface Route {
  path: string;
  active: NavKey;
  screen: ComponentType;
  title: string;
}

// ── The route registry ─────────────────────────────────────────────────────
// Home/media/settings reconciliation (OP-4, reversible default):
//  · `/`        → the trip MAP (the legacy HOME screen).
//  · `/settings`→ the Path-A device/settings DASHBOARD (Task 5.2's screen — it
//                 self-describes as a settings dashboard, captured at the legacy
//                 `/settings/`, and its UAT asserts the Settings nav active, so
//                 it lives here rather than under Media). Imported as `MediaHub`
//                 for filename continuity with the frozen 5.2 lane.
//  · `/media`   → the Media section (`screens/Media.tsx`): the v1 Lock Chimes
//                 page (media pill sub-nav + lock-chime sections), the screen
//                 the legacy `/media/` redirect actually landed on.
// Every primary screen is now a real parity port; the generic ComingSoon screen
// survives only as the client-side fallback for an unknown in-app path (see
// matchRoute) so links never dead-end or trigger a full reload.
export const ROUTES: Route[] = [
  { path: "/", active: "map", screen: TripMap, title: "Map" },
  { path: "/media", active: "media", screen: Media, title: "Media" },
  { path: "/music", active: "media", screen: Music, title: "Music" },
  { path: "/boombox", active: "media", screen: Boombox, title: "Boombox" },
  { path: "/light_shows", active: "media", screen: LightShows, title: "Light Shows" },
  { path: "/wraps", active: "media", screen: Wraps, title: "Wraps" },
  { path: "/license_plates", active: "media", screen: LicensePlates, title: "License Plates" },
  { path: "/analytics", active: "analytics", screen: Analytics, title: "Analytics" },
  { path: "/events", active: "map", screen: EventPlayer, title: "Events" },
  { path: "/cloud", active: "cloud", screen: CloudArchive, title: "Cloud" },
  { path: "/captive-portal", active: "settings", screen: CaptivePortal, title: "Wi-Fi setup" },
  { path: "/settings", active: "settings", screen: MediaHub, title: "Settings" },
  { path: "/storage", active: "settings", screen: StorageHealth, title: "Storage" },
  { path: "/failed-jobs", active: "settings", screen: FailedJobs, title: "Failed jobs" },
];

/** Strip a trailing slash (except for root) so `/media/` matches `/media`. */
function normalizePath(pathname: string): string {
  if (pathname.length > 1 && pathname.endsWith("/")) {
    return pathname.replace(/\/+$/, "") || "/";
  }
  return pathname;
}

/** Resolve a pathname to a route, falling back to a generic ComingSoon. */
function matchRoute(pathname: string): { active: NavKey; screen: ComponentType } {
  const p = normalizePath(pathname);
  const hit = ROUTES.find((r) => r.path === p);
  if (hit) return { active: hit.active, screen: hit.screen };
  // Unknown in-app path → a client-side placeholder (no hard 404 page).
  return { active: "map", screen: () => <ComingSoon title="Not found" /> };
}

/**
 * Should the router handle this anchor click as an in-app navigation, or let
 * the browser take it? Returns the target pathname when we should intercept,
 * else `null`. Deliberately conservative — anything that isn't an unmodified,
 * same-origin, in-app document navigation is left to the browser.
 */
function interceptable(e: MouseEvent): string | null {
  // Only plain left-clicks with no modifier keys (preserve open-in-new-tab etc).
  if (e.defaultPrevented) return null;
  if (e.button !== 0) return null;
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return null;

  const anchor = (e.target as Element | null)?.closest?.("a");
  if (!anchor) return null;
  // download / explicit target / rel=external opt out.
  if (anchor.hasAttribute("download")) return null;
  const target = anchor.getAttribute("target");
  if (target && target !== "_self") return null;
  const href = anchor.getAttribute("href");
  if (!href) return null;

  let url: URL;
  try {
    url = new URL(href, window.location.href);
  } catch {
    return null;
  }
  // External origin (covers mailto:/tel:/blob:/data: — their origin differs).
  if (url.origin !== window.location.origin) return null;
  // Server-owned paths must hit the network, not the SPA router.
  if (/^\/(api|static|assets)\//.test(url.pathname)) return null;
  // Same-page hash link → let the browser scroll, don't route.
  if (url.pathname === window.location.pathname && url.hash) return null;

  return url.pathname + url.search + url.hash;
}

/**
 * The SPA router: tracks `location.pathname`, intercepts in-app anchor clicks
 * for push-state navigation (no full reload), restores state on back/forward,
 * and renders the matched screen inside the shared {@link Shell}.
 *
 * The Shell is hoisted here (not per-screen) so the app chrome — top bar, nav
 * rail/tabs, theme toggle — is a single stable instance across navigations
 * (no remount/flash), and screens only ever provide their content + nav key.
 */
export function Router() {
  const [pathname, setPathname] = useState(() => window.location.pathname);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      const to = interceptable(e);
      if (to === null) return;
      e.preventDefault();
      if (to !== window.location.pathname + window.location.search + window.location.hash) {
        window.history.pushState({}, "", to);
      }
      setPathname(window.location.pathname);
      // Land at the top on screen change (browser does this on real nav).
      window.scrollTo(0, 0);
    };
    const onPop = () => setPathname(window.location.pathname);
    document.addEventListener("click", onClick);
    window.addEventListener("popstate", onPop);
    return () => {
      document.removeEventListener("click", onClick);
      window.removeEventListener("popstate", onPop);
    };
  }, []);

  const { active, screen: Screen } = matchRoute(pathname);
  return (
    <Shell active={active}>
      <Screen />
    </Shell>
  );
}
