import { useEffect, useRef, useState } from "preact/hooks";
import type { ComponentChildren } from "preact";
import { Icon } from "./Icon";
import { api } from "../api/client";

/** Which primary nav entry is active for the current screen. */
export type NavKey = "map" | "analytics" | "media" | "cloud" | "settings";

interface NavEntry {
  key: NavKey;
  href: string;
  icon: string;
  label: string;
}

// Mirrors base.html's rail/tab order and icons. hrefs are the eventual SPA
// routes; only the media hub (Task 5.2) is implemented — other screens land
// in Task 5.3 and currently fall back to the shell via webd's SPA fallback.
const NAV: NavEntry[] = [
  { key: "map", href: "/", icon: "map-pin", label: "Map" },
  { key: "analytics", href: "/analytics", icon: "bar-chart-2", label: "Analytics" },
  { key: "media", href: "/media", icon: "music", label: "Media" },
  { key: "cloud", href: "/cloud", icon: "cloud", label: "Cloud" },
  { key: "settings", href: "/settings", icon: "settings", label: "Settings" },
];

function applyTheme(theme: "light" | "dark") {
  const root = document.documentElement;
  if (theme === "dark") root.setAttribute("data-theme", "dark");
  else root.removeAttribute("data-theme");
  try {
    localStorage.setItem("theme", theme);
  } catch {
    /* storage may be unavailable; non-fatal */
  }
}

function currentTheme(): "light" | "dark" {
  return document.documentElement.getAttribute("data-theme") === "dark"
    ? "dark"
    : "light";
}

/** System-health severity → dot CSS class (parity with base.html poller). */
const SEV_CLASS: Record<string, string> = {
  ok: "health-dot-ok",
  warn: "health-dot-warn",
  error: "health-dot-error",
  unknown: "health-dot-unknown",
};

/** System-health severity → user-facing status label. */
const SEV_LABEL: Record<string, string> = {
  ok: "All systems normal",
  warn: "System degraded",
  error: "System error",
  unknown: "System status",
};

/**
 * The app chrome: top bar, desktop sidebar rail, mobile bottom tabs, theme
 * toggle, and the system-health status dot — a faithful port of the legacy
 * `base.html`. The health dot polls `/api/system/health` for live v1 parity and
 * stays hidden until the first successful poll arrives.
 */
export function Shell({
  active,
  children,
}: {
  active: NavKey;
  children: ComponentChildren;
}) {
  const [theme, setTheme] = useState<"light" | "dark">(currentTheme());
  const dotRef = useRef<HTMLSpanElement>(null);
  const linkRef = useRef<HTMLAnchorElement>(null);
  const modeDotRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    let timer: number | undefined;
    let mounted = true;
    let inFlight: AbortController | undefined;
    async function poll() {
      const ctrl = new AbortController();
      inFlight?.abort();
      inFlight = ctrl;
      // Guard against an out-of-order resolve: once a newer tick (or unmount)
      // supersedes this poll, its result must not overwrite the dot with stale
      // data. (Aborts are swallowed below — V1 keeps the last known colour.)
      const superseded = () => !mounted || inFlight !== ctrl;
      try {
        const data = await api.systemHealth(ctrl.signal);
        if (superseded()) return;
        const raw = typeof data.overall === "string" ? data.overall : "unknown";
        const sev = Object.prototype.hasOwnProperty.call(SEV_CLASS, raw)
          ? raw
          : "unknown";
        const msg = SEV_LABEL[sev] ?? SEV_LABEL.unknown;
        const dot = dotRef.current;
        const link = linkRef.current;
        if (!dot || !link) return;
        dot.className = `status-dot health-dot ${SEV_CLASS[sev] ?? SEV_CLASS.unknown}`;
        link.title = msg;
        link.setAttribute("aria-label", msg);
        link.hidden = false;
      } catch {
        // Network blip or abort (supersede/unmount included) — keep the last
        // known dot colour and never hide an already-revealed dot. Exact V1
        // parity: base.html's poll() does nothing on !r.ok / catch
        // ("network blip — keep last known colour"); the dot's only hidden
        // state is its pre-first-success default.
      }
    }
    void poll().catch(() => {});
    timer = window.setInterval(() => void poll().catch(() => {}), 30000);
    return () => {
      mounted = false;
      inFlight?.abort();
      if (timer) window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    let timer: number | undefined;
    let mounted = true;
    let inFlight: AbortController | undefined;
    async function poll() {
      const ctrl = new AbortController();
      inFlight?.abort();
      inFlight = ctrl;
      // Guard against an out-of-order resolve: once a newer tick (or unmount)
      // supersedes this poll, its result must not overwrite the dot with stale
      // data. (Aborts are swallowed below — keep the last known colour.)
      const superseded = () => !mounted || inFlight !== ctrl;
      try {
        const g = await api.gadgetStatus(ctrl.signal);
        if (superseded()) return;
        const present = g.present && g.bound && g.udc_state === "configured";
        const dot = modeDotRef.current;
        if (!dot) return;
        const label = present
          ? g.handoff_active
            ? "USB drive busy — syncing"
            : "USB drive connected to vehicle"
          : "USB status unknown";
        dot.className = `status-dot ${present ? "status-present" : "status-unknown"}`;
        dot.title = label;
        dot.setAttribute("aria-label", label);
      } catch {
        // Network blip or abort (supersede/unmount included) — keep the last
        // known dot colour.
      }
    }
    void poll().catch(() => {});
    timer = window.setInterval(() => void poll().catch(() => {}), 30000);
    return () => {
      mounted = false;
      inFlight?.abort();
      if (timer) window.clearInterval(timer);
    };
  }, []);

  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    applyTheme(next);
    setTheme(next);
  };

  // ── Follow the OS color-scheme while the user hasn't made an explicit choice
  //    (parity with v1 main.js): if localStorage has no "theme", an OS switch
  //    flips the app too. A manual toggle writes localStorage and opts out. ──
  useEffect(() => {
    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (e: MediaQueryListEvent) => {
      let stored: string | null = null;
      try {
        stored = localStorage.getItem("theme");
      } catch {
        /* storage unavailable — treat as no explicit choice */
      }
      if (stored) return; // user picked a theme; don't override it
      const root = document.documentElement;
      if (e.matches) root.setAttribute("data-theme", "dark");
      else root.removeAttribute("data-theme");
      setTheme(e.matches ? "dark" : "light");
    };
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  return (
    <>
      <header class="top-bar">
        <div class="top-bar-left">
          <a href="/" class="top-bar-brand">
            <Icon name="hard-drive" />
            <span class="top-bar-title">TeslaUSB</span>
          </a>
        </div>
        <div class="top-bar-right">
          <a
            href="/settings#system-health-card"
            class="health-dot-link"
            id="health-dot-link"
            title="System status"
            aria-label="System status"
            ref={linkRef}
            hidden
          >
            <span
              class="status-dot health-dot"
              id="health-dot"
              data-status-dot="health"
              ref={dotRef}
            />
          </a>
          <span
            class="status-dot status-unknown"
            id="mode-dot"
            data-status-dot="mode"
            title="USB status unknown"
            aria-label="USB status unknown"
            ref={modeDotRef}
          />
          <button
            class="theme-toggle-btn"
            onClick={toggleTheme}
            aria-label="Toggle dark mode"
          >
            <Icon name={theme === "dark" ? "sun" : "moon"} id="theme-icon-svg" />
          </button>
        </div>
      </header>

      <nav class="sidebar-rail" id="sidebarRail" aria-label="Main navigation">
        {NAV.map((n) => (
          <a
            key={n.key}
            href={n.href}
            class={`nav-item${n.key === active ? " active" : ""}`}
            title={n.label}
            aria-current={n.key === active ? "page" : undefined}
          >
            <Icon name={n.icon} />
            <span class="nav-label">{n.label}</span>
          </a>
        ))}
      </nav>

      <nav class="bottom-tabs" aria-label="Main navigation">
        {NAV.map((n) => (
          <a
            key={n.key}
            href={n.href}
            class={`tab-item${n.key === active ? " active" : ""}`}
            aria-current={n.key === active ? "page" : undefined}
          >
            <Icon name={n.icon} />
            <span class="tab-label">{n.label}</span>
          </a>
        ))}
      </nav>

      <main class="main-content" role="main">
        {children}
      </main>

      {/* Toast region — parity with base.html. No toasts are raised by the
          read-only catalog UI, but the live region is kept for structural
          parity and future use. */}
      <div class="toast-container" id="toast-container" aria-live="polite" />
    </>
  );
}
