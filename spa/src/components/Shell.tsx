import { useEffect, useRef, useState } from "preact/hooks";
import type { ComponentChildren } from "preact";
import { Icon } from "./Icon";

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

/**
 * The app chrome: top bar, desktop sidebar rail, mobile bottom tabs, theme
 * toggle, and the system-health status dot — a faithful port of the legacy
 * `base.html`. The health dot polls `/api/system/health`; webd's read-only
 * catalog API does not expose that endpoint, so the dot simply stays hidden
 * (its default), which is the graceful-degradation behaviour base.html already
 * specifies ("hidden until the first poll arrives").
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

  useEffect(() => {
    // webd's read-only catalog API does not expose /api/system/health, so the
    // poll is OFF by default — requesting an absent endpoint would add a 404 to
    // every run and muddy the "no unexpected non-2xx" UAT gate. The markup is
    // kept for structural parity with base.html; the dot stays hidden (exactly
    // base.html's "hidden until first poll" behaviour). A deployment that DOES
    // provide the endpoint can opt in via VITE_ENABLE_HEALTH_POLL=true.
    if (import.meta.env.VITE_ENABLE_HEALTH_POLL !== "true") return;
    let timer: number | undefined;
    let cancelled = false;
    async function poll() {
      try {
        const r = await fetch("/api/system/health", {
          credentials: "same-origin",
        });
        if (!r.ok || cancelled) return;
        const data = (await r.json()) as {
          overall?: { severity?: string; message?: string };
        };
        const sev = data.overall?.severity ?? "unknown";
        const msg = data.overall?.message ?? "System status";
        const dot = dotRef.current;
        const link = linkRef.current;
        if (!dot || !link) return;
        dot.className = `status-dot health-dot ${SEV_CLASS[sev] ?? SEV_CLASS.unknown}`;
        link.title = msg;
        link.setAttribute("aria-label", msg);
        link.hidden = false;
      } catch {
        /* network blip or endpoint absent — keep the dot hidden */
      }
    }
    poll();
    timer = window.setInterval(poll, 30000);
    return () => {
      cancelled = true;
      if (timer) window.clearInterval(timer);
    };
  }, []);

  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    applyTheme(next);
    setTheme(next);
  };

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
          <button
            class="theme-toggle-btn"
            onClick={toggleTheme}
            aria-label="Toggle dark mode"
          >
            <Icon name="moon" id="theme-icon-svg" />
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
